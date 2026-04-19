"""Fetch Spotify track data for all distinct URIs in the database and insert into music.track_data.

Queries distinct spotify_track_uri from music.clean_data and music.raw_recently_played,
skips URIs already present in track_data, calls the Spotify API for each new track,
and appends results to music.track_data.

"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import time
import logging
import urllib.parse
from typing import List, Dict, Any, Set

import pymysql
import requests
import pandas as pd
from sqlalchemy import create_engine, text

from config.spotify_creds import SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET
from config.db_creds import DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

SPOTIFY_TOKEN_URL = 'https://accounts.spotify.com/api/token'
SPOTIFY_API_BASE  = 'https://api.spotify.com/v1'
TRACK_ID_RE       = re.compile(r'(?:spotify:track:|https?://open\.spotify\.com/track/)([A-Za-z0-9_-]+)')

DB_PORT = int(DB_PORT)

REQUEST_DELAY      = 5.0    # seconds between each API call
FLUSH_EVERY        = 100    # write to DB every N tracks so crashes don't lose progress
TOKEN_LIFETIME     = 55 * 60  # refresh token after 55 min (tokens expire at 60)
MAX_TRACKS_PER_RUN = 500    # stay under Spotify dev mode's ~600 daily request cap


def get_engine():
    pw = urllib.parse.quote_plus(DB_PASSWORD)
    return create_engine(f'mysql+pymysql://{DB_USER}:{pw}@{DB_HOST}:{DB_PORT}/{DB_NAME}')


def get_distinct_track_uris() -> List[str]:
    conn = pymysql.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
        db=DB_NAME, port=DB_PORT, cursorclass=pymysql.cursors.DictCursor
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT DISTINCT spotify_track_uri AS uri FROM clean_data
                    WHERE spotify_track_uri IS NOT NULL
                UNION
                SELECT DISTINCT spotify_track_uri AS uri FROM raw_recently_played
                    WHERE spotify_track_uri IS NOT NULL
            """)
            return [r['uri'] for r in cursor.fetchall()]
    finally:
        conn.close()


def get_existing_uris(engine) -> Set[str]:
    """Return all source_uri values already stored in track_data."""
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT source_uri FROM music.track_data"))
            return {row[0] for row in result.fetchall()}
    except Exception:
        return set()  # table doesn't exist yet


def get_existing_max_artists(engine) -> int:
    """Return how many artist_X_name columns already exist in track_data (0 if none)."""
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = 'music'
                  AND TABLE_NAME   = 'track_data'
                  AND COLUMN_NAME LIKE 'artist_%_name'
            """))
            return result.scalar() or 0
    except Exception:
        return 0


def ensure_artist_columns(engine, needed: int, current_max: int) -> None:
    """ALTER TABLE to add extra artist columns when a batch has more artists than expected."""
    if needed <= current_max:
        return
    try:
        with engine.connect() as conn:
            for i in range(current_max + 1, needed + 1):
                conn.execute(text(
                    f"ALTER TABLE music.track_data ADD COLUMN `artist_{i}_name` VARCHAR(500) NULL"
                ))
                conn.execute(text(
                    f"ALTER TABLE music.track_data ADD COLUMN `artist_{i}_uri` VARCHAR(500) NULL"
                ))
                logger.info('Added artist_%d columns to track_data', i)
            conn.commit()
    except Exception as exc:
        logger.warning('Could not add artist columns: %s', exc)


def extract_track_id(uri: str) -> str:
    m = TRACK_ID_RE.search(uri)
    if m:
        return m.group(1)
    raise ValueError(f'Unrecognized Spotify track URI: {uri}')


def get_spotify_token() -> str:
    r = requests.post(SPOTIFY_TOKEN_URL, data={'grant_type': 'client_credentials'},
                      auth=(SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET))
    r.raise_for_status()
    return r.json()['access_token']


def call_spotify(endpoint: str, token: str, params=None, max_retries=5) -> Dict[str, Any]:
    headers = {'Authorization': f'Bearer {token}'}
    url = f'{SPOTIFY_API_BASE}{endpoint}'
    backoff = 1.0
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=30)
        except (requests.exceptions.ConnectTimeout,
                requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError) as exc:
            logger.warning('Network error on attempt %d/%d: %s — retrying in %.1fs',
                           attempt + 1, max_retries, exc, backoff)
            time.sleep(backoff)
            backoff *= 2
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            retry_after = int(r.headers.get('Retry-After', '1'))
            logger.warning('Rate limited; sleeping %s seconds', retry_after)
            time.sleep(retry_after)
            continue
        if 500 <= r.status_code < 600:
            logger.warning('Server error %s; backoff %.1f', r.status_code, backoff)
            time.sleep(backoff)
            backoff *= 2
            continue
        logger.warning('Unexpected status %s for %s', r.status_code, url)
        return {}
    raise RuntimeError(f'Failed to call {url} after {max_retries} attempts')


def flatten_track(track: Dict, album: Dict, artists: List[Dict], max_artists: int) -> Dict:
    return {
        'track_name':             track.get('name'),
        'duration_ms':            track.get('duration_ms'),
        'is_playable':            track.get('is_playable'),
        'popularity':             track.get('popularity'),
        'album_name':             album.get('name'),
        'release_date':           album.get('release_date'),
        'release_date_precision': album.get('release_date_precision'),
        'album_uri':              album.get('uri'),
        **{f'artist_{i}_name': artists[i-1].get('name') if i <= len(artists) else None
           for i in range(1, max_artists + 1)},
        **{f'artist_{i}_uri':  artists[i-1].get('uri')  if i <= len(artists) else None
           for i in range(1, max_artists + 1)},
    }


def flush_to_db(pending: List, max_artists: int, engine) -> None:
    rows = []
    for track, album, artists, uri in pending:
        row = flatten_track(track, album, artists, max_artists)
        row['source_uri'] = uri
        rows.append(row)
    df = pd.DataFrame(rows)
    df.to_sql('track_data', engine, schema='music',
              index=False, if_exists='append', method='multi', chunksize=500)
    logger.info('Flushed %d rows to music.track_data', len(rows))


def main():
    engine = get_engine()

    all_uris      = get_distinct_track_uris()
    existing_uris = get_existing_uris(engine)
    uris          = [u for u in all_uris if u not in existing_uris]

    logger.info('Total distinct URIs: %d | Already in DB: %d | To fetch: %d',
                len(all_uris), len(existing_uris), len(uris))

    if not uris:
        logger.info('Nothing new to fetch — exiting.')
        return

    if len(uris) > MAX_TRACKS_PER_RUN:
        logger.info('Capping this run at %d tracks (%d remaining after this run)',
                    MAX_TRACKS_PER_RUN, len(uris) - MAX_TRACKS_PER_RUN)
        uris = uris[:MAX_TRACKS_PER_RUN]

    token         = get_spotify_token()
    token_fetched = time.time()
    max_artists   = get_existing_max_artists(engine)  # baseline from existing table structure
    pending       = []

    for i, uri in enumerate(uris, start=1):
        # Refresh token if near expiry
        if time.time() - token_fetched > TOKEN_LIFETIME:
            logger.info('Token near expiry — refreshing...')
            token = get_spotify_token()
            token_fetched = time.time()

        try:
            track_id = extract_track_id(uri)
        except ValueError:
            logger.warning('Skipping unrecognized URI: %s', uri)
            continue

        logger.info('(%d/%d) %s', i, len(uris), track_id)

        track   = call_spotify(f'/tracks/{track_id}', token)
        album   = track.get('album') or {}
        artists = track.get('artists') or []

        pending.append((track, album, artists, uri))

        time.sleep(REQUEST_DELAY)

        # Flush to DB every FLUSH_EVERY tracks and on the final track
        if len(pending) >= FLUSH_EVERY or i == len(uris):
            batch_max = max(len(a) for _, _, a, _ in pending)
            if batch_max > max_artists:
                ensure_artist_columns(engine, batch_max, max_artists)
                max_artists = batch_max
            flush_to_db(pending, max_artists, engine)
            pending = []

    logger.info('Done.')


if __name__ == '__main__':
    main()

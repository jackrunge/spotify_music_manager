"""Sync tag-to-track mappings from Spotify #hashtag playlists into music.tag_mapping.

Walks all of the current user's playlists, picks out the ones whose names
start with '#' (e.g. '#spanish', '#banger'), and rebuilds music.tag_mapping
with the union of (tag_id, spotify_track_uri) pairs found in those playlists.

Strategy: wipe music.tag_mapping and rebuild it from scratch each run, so
adding/removing a track from a playlist (or deleting a playlist entirely)
naturally flows through to the DB.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import urllib.parse
from typing import Dict, List, Set, Tuple

import pandas as pd
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from sqlalchemy import create_engine, text

from config.spotify_creds import SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI
from config.db_creds import DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME

SCOPE         = "playlist-read-private"
PLAYLIST_PAGE = 50      # Spotify max for current_user_playlists
TRACK_PAGE    = 100     # Spotify max for playlist_items


def get_engine():
    pw = urllib.parse.quote_plus(DB_PASSWORD)
    url = f"mysql+pymysql://{DB_USER}:{pw}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    return create_engine(url)


def get_spotify_client() -> spotipy.Spotify:
    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SCOPE,
        cache_path="config/.cache"
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def load_tags(engine) -> Dict[str, int]:
    """Return {lowercased_tag_name: tag_id} so we can match against playlist names."""
    with engine.connect() as conn:
        result = conn.execute(text("SELECT tag_id, tag FROM music.tags"))
        return {row.tag.lower(): row.tag_id for row in result.fetchall()}


def fetch_all_playlists(sp: spotipy.Spotify) -> List[Dict]:
    """Page through current_user_playlists until exhausted."""
    playlists, offset = [], 0
    while True:
        page = sp.current_user_playlists(limit=PLAYLIST_PAGE, offset=offset)
        items = page.get("items") or []
        playlists.extend(items)
        if len(items) < PLAYLIST_PAGE:
            break
        offset += PLAYLIST_PAGE
    return playlists


def fetch_playlist_track_uris(sp: spotipy.Spotify, playlist_id: str) -> List[str]:
    """Return all valid Spotify track URIs in a playlist, skipping local tracks and podcasts."""
    uris, offset = [], 0
    while True:
        page = sp.playlist_items(
            playlist_id,
            limit=TRACK_PAGE,
            offset=offset,
            fields="items(item(uri,type)),next"
        )
        items = page.get("items") or []
        for item in items:
            # Spotify's newer playlist_items response wraps the song under "item"
            # (generalized to support both tracks and episodes); older responses use "track"
            song = item.get("item") or item.get("track") or {}
            if song.get("type") != "track":
                continue  # skip episodes (podcasts)
            uri = song.get("uri")
            if uri and uri.startswith("spotify:track:"):
                uris.append(uri)
        if not page.get("next"):
            break
        offset += TRACK_PAGE
    return uris


def build_mappings(sp: spotipy.Spotify, tags: Dict[str, int]) -> List[Tuple[int, str]]:
    """Walk hashtag playlists and return list of (tag_id, spotify_track_uri) tuples."""
    playlists = fetch_all_playlists(sp)
    print(f"Found {len(playlists)} total playlists")

    hashtag_playlists = [p for p in playlists if p.get("name", "").startswith("#")]
    print(f"Found {len(hashtag_playlists)} #hashtag playlists\n")

    mappings: List[Tuple[int, str]] = []
    matched_tags: Set[str] = set()

    for pl in hashtag_playlists:
        name = pl["name"]
        # Strip exactly one leading '#'; '##foo' won't match and will fall through below
        tag_name = name[1:].lower() if name.startswith("#") else name.lower()
        tag_id = tags.get(tag_name)
        if tag_id is None:
            print(f"  [skip] {name!r}: no matching tag in DB")
            continue

        uris = fetch_playlist_track_uris(sp, pl["id"])
        print(f"  [ok]   {name}: {len(uris)} tracks")
        mappings.extend((tag_id, uri) for uri in uris)
        matched_tags.add(tag_name)

    missing = sorted(set(tags.keys()) - matched_tags)
    if missing:
        print(f"\n[note] Tags in DB with no matching Spotify playlist: {missing}")

    return mappings


def write_mappings(engine, mappings: List[Tuple[int, str]]) -> None:
    """Wipe music.tag_mapping and bulk-insert the fresh mappings."""
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM music.tag_mapping"))
        conn.commit()
    print("\nCleared music.tag_mapping")

    if not mappings:
        print("No mappings to insert.")
        return

    df = pd.DataFrame(mappings, columns=["tag_id", "spotify_track_uri"]).drop_duplicates()
    df.to_sql("tag_mapping", engine, schema="music",
              index=False, if_exists="append", method="multi", chunksize=500)
    print(f"Inserted {len(df)} rows into music.tag_mapping")


def main():
    engine = get_engine()
    sp = get_spotify_client()

    tags = load_tags(engine)
    print(f"Loaded {len(tags)} tags from music.tags\n")

    mappings = build_mappings(sp, tags)
    write_mappings(engine, mappings)


if __name__ == "__main__":
    main()

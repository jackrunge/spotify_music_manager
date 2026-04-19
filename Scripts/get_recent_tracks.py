"""Fetch the current user's last played tracks and insert into MySQL."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict

import pandas as pd
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from sqlalchemy import create_engine, text

from config.spotify_creds import SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI
from config.db_creds import DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME

SCOPE = "user-read-recently-played"
LIMIT = 50


def get_engine():
    pw = urllib.parse.quote_plus(DB_PASSWORD)
    url = f"mysql+pymysql://{DB_USER}:{pw}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    return create_engine(url)


def insert_to_db(resp: Dict, call_made: datetime, engine) -> None:
    # --- recently_played_calls ---
    cursors = resp.get("cursors") or {}
    calls_df = pd.DataFrame([{
        "before_ts": int(cursors.get("before", 0)),
        "after_ts":  int(cursors.get("after",  0)),
        "call_made": call_made,
    }])
    calls_df.to_sql("recently_played_calls", engine, schema="music",
                    index=False, if_exists="append", method="multi")
    print("Inserted 1 row into music.recently_played_calls")

    # --- raw_recently_played ---
    items = resp.get("items") or []
    if not items:
        print("No tracks to insert into music.raw_recently_played")
        return

    rows = []
    for item in items:
        track = item.get("track") or {}
        rows.append({
            "track_name":        track.get("name", ""),
            "played_at":         (
                pd.to_datetime(item["played_at"], utc=True)
                  .tz_convert("America/New_York")
                  .tz_localize(None)
            ),
            "spotify_track_uri": track.get("uri", ""),
        })

    tracks_df = pd.DataFrame(rows)
    tracks_df.to_sql("raw_recently_played", engine, schema="music",
                     index=False, if_exists="append", method="multi")
    print(f"Inserted {len(tracks_df)} rows into music.raw_recently_played")

    # --- deduplication ---
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TEMPORARY TABLE _dedup
            SELECT DISTINCT track_name, played_at, spotify_track_uri
            FROM music.raw_recently_played
        """))
        conn.execute(text("DELETE FROM music.raw_recently_played"))
        conn.execute(text("""
            INSERT INTO music.raw_recently_played
            SELECT * FROM _dedup
        """))
        conn.execute(text("DROP TEMPORARY TABLE _dedup"))
        conn.commit()
    print("Deduplicated music.raw_recently_played")


def get_recent_tracks() -> Dict:
    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SCOPE,
        cache_path="config/.cache"
    )
    sp = spotipy.Spotify(auth_manager=auth_manager)
    kwargs = {"limit": LIMIT}
    return sp.current_user_recently_played(**kwargs)


def main():
    engine  = get_engine()
    eastern = ZoneInfo("America/New_York")
    call_made = datetime.now(timezone.utc).astimezone(eastern).replace(tzinfo=None)

    resp = get_recent_tracks()
    if not resp:
        print("No response from Spotify. Make sure scopes and credentials are correct.")
        return

    insert_to_db(resp, call_made, engine)


if __name__ == "__main__":
    main()

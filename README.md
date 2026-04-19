# Spotify Music Manager

A personal data engineering project for tracking, analyzing, and automating Spotify listening behavior using the Spotify Web API and a locally hosted MySQL database.

---

## Motivation

Spotify's native interface doesn't expose granular listening history or give you real control over playlist curation. I wanted a system that:

- Keeps a permanent, accurate record of every track I've attempted to listen to — including skips, partial plays, and play percentage
- Lets me analyze listening patterns over time (by song, time of day, day of week, skip rate, etc.)
- Automates playlist curation using a flexible tag-based system I control entirely

---

## Architecture Overview

The project uses a **dual-ingestion model** to balance data richness with recency:

- **Extended Streaming History** — requested directly from Spotify, covering several years and 80,000+ play events with rich metadata (device, shuffle state, skip reason, remote vs. offline, etc.). Updated periodically (planned: monthly).
- **Recently Played API** — polls the Spotify API for the 50 most recent tracks on an hourly schedule via a local Raspberry Pi. Fewer columns but live and continuous.

Both sources feed into a unified `plays` table after transformation and deduplication, with source-appropriate columns populated where available.

---

## Features

### Listening History Ingestion
- Loaded 80,000+ rows of extended streaming history exported from Spotify
- Ongoing ingestion of recently played tracks via Spotify API on hourly schedule
- Deduplication and reconciliation logic between historical and live data sources

### Play Quality Classification
- Unifies `raw_extended_history` and `raw_recently_played` into a single `plays` table
- Filters out low-confidence events (near-zero ms played with no intentional skip signal) to avoid false skips
- Classifies each play event into `full_listen`, `natural_end`, `partial`, or `skip` using `reason_end` as the primary signal and play ratio as fallback
- Intelligent back-button handling: a quick back = skip, but a back after a meaningful listen = full listen (replay)
- For recently-played data (no `ms_played` available), infers play duration from the gap to the next track using window functions
- Handles edge cases: last song before an extended break classified as `natural_end` rather than skip
- Deduplicates plays appearing in both sources within a ±60s window on the same URI

### Track Aggregate Statistics
- Per-track rollups in `track_stats`: play counts, skip/completion rates, total listen time, first/last played timestamps
- Unique days played (differentiates binge-listening from sustained favorites)
- Peak listening month per track (when a song had its moment)
- Skip and completion rates computed over decisive plays only — `natural_end` excluded to avoid penalizing session boundaries

### Listening Analytics
- Aggregated play counts and skip rates per track
- Time-based pattern analysis: listening behavior by hour of day and day of week *(planned)*
- Monthly automated reports: most-skipped tracks (removal candidates) and most-played tracks *(planned)*
- Extended history enables deeper analysis: device, shuffle state, skip reason, offline/remote plays

### Tag-Based Playlist Management *(in progress)*
- Dedicated Spotify playlists serve as tag sources (e.g., "chill", "upbeat", "energetic", "rap", "banger", "indie")
- API integration pulls all songs from each playlist and writes tag associations to a relational table
- Automation queries any combination of tags to dynamically build and sync Spotify playlists

---

## Database Schema

**Raw ingestion tables** (populated by ingestion scripts):

```
raw_extended_history                 # from Spotify's extended streaming history JSONs
├── ts                               # ISO 8601 UTC timestamp
├── ms_played
├── spotify_track_uri
├── master_metadata_track_name
├── master_metadata_album_artist_name
├── master_metadata_album_album_name
├── reason_start                     # trackdone | fwdbtn | clickrow | playbtn | ...
├── reason_end                       # trackdone | fwdbtn | backbtn | endplay | ...
├── skipped                          # Spotify's own flag (unreliable pre-Oct 2022)
├── shuffle, offline, platform, incognito_mode, ...

raw_recently_played                  # polled hourly from Spotify Web API
├── played_at
├── spotify_track_uri
└── track_name

recently_played_calls                # audit log of each API call made
├── before_ts
├── after_ts
└── call_made
```

**Transformed tables** (built from the raw tables by pipeline scripts):

```
track_data                           # one row per unique track, enriched via Spotify API
├── source_uri           (PK — Spotify track URI)
├── track_name
├── artist_1_name        (additional artist columns added dynamically)
├── artist_1_uri
├── album_name
├── album_uri
├── duration_ms
├── popularity
├── release_date
└── ...additional Spotify metadata

plays                                # unified play events from both sources
├── play_id              (PK, auto_increment)
├── spotify_track_uri    (FK → track_data)
├── played_at            (datetime, Eastern time)
├── ms_played            (nullable — unknown for natural_end in recently_played)
├── play_percentage      (ms_played / duration_ms, capped at 1.0)
├── play_quality         (full_listen | natural_end | partial | skip)
├── skipped              (bool — derived from play_quality)
└── source               (extended_history | recently_played)

track_stats                          # per-track aggregate rollups
├── spotify_track_uri    (PK)
├── track_name, artist_name
├── play_count, unique_days_played
├── full_listen_count, natural_end_count, partial_count, skip_count, unclassified_count
├── avg_play_percentage, total_ms_played, total_minutes_played
├── skip_rate, completion_rate
├── first_played_at, last_played_at, days_since_last
└── peak_listen_month, peak_month_play_count
```

**Planned tables** (tag-based playlist management):

```
playlists
├── playlist_id          (PK)
├── playlist_name
├── spotify_playlist_uri
└── tag_id               (FK → tags, can be NULL)

playlist_mapping
├── playlist_id          (FK → playlists)
└── spotify_track_uri    (FK → track_data)

tags
├── tag_id               (PK)
├── tag_name
└── description

tag_mapping
├── spotify_track_uri    (FK → track_data)
└── tag_id               (FK → tags)
```

---

## Tech Stack

- **Python** — API integration, ingestion scripts, transformation logic, automation
- **MySQL** — locally hosted relational database
- **Spotify Web API** — recently played endpoint, playlist management, track metadata
- **Raspberry Pi** *(planned)* — local scheduler for hourly API polling

---

## Project Structure

```
spotify-music-manager/
├── Source Data/                   # Extended streaming history JSONs (gitignored)
├── Scripts/
│   ├── ingest_history.py          # Loads all JSONs in a folder into raw_extended_history
│   ├── get_recent_tracks.py       # Polls Spotify API for recently played tracks
│   ├── get_track_info.py          # Fetches track metadata from Spotify API → track_data
│   ├── classify_plays.py          # Unifies raw tables into plays with quality classification
│   ├── build_track_stats.py       # Aggregates plays into per-track stats
│   ├── tag_manager.py             # (planned) Syncs playlist-based tags to DB
│   ├── playlist_builder.py        # (planned) Builds Spotify playlists from tag queries
│   └── monthly_report.py          # (planned) Generates monthly listening analytics
├── config/                        # gitignored
│   ├── spotify_creds.py           # Spotify API credentials
│   ├── db_creds.py                # Database credentials
│   └── .cache                     # Spotify OAuth token cache
└── README.md
```

### Pipeline Order

Scripts are designed to run in this order:

1. **`ingest_history.py`** — one-time / periodic: load Spotify's extended history JSONs into `raw_extended_history`
2. **`get_recent_tracks.py`** — scheduled hourly: fetch recent plays into `raw_recently_played`
3. **`get_track_info.py`** — run after new URIs appear in either source: enrich `track_data` with metadata
4. **`classify_plays.py`** — rebuild `plays` from the raw tables with quality classification
5. **`build_track_stats.py`** — aggregate `plays` into `track_stats`

---

## Status

**Complete:**
- Extended streaming history loaded into MySQL (80,000+ rows)
- Spotify API connection established, credentials modularized in `config/`
- Recent tracks ingestion script with auto-deduplication after each ingestion run
- Track metadata retrieval script with resume support, rate-limit handling, and token refresh
- Unified `plays` table with play quality classification (full_listen / natural_end / partial / skip)
- Per-track aggregate stats in `track_stats` (play counts, skip/completion rates, unique days, peak month, etc.)

**In Progress:**
- Raspberry Pi scheduling for hourly API polling
- Tag-based playlist management system
- Monthly analytics automation
- Threshold tuning for skip classification based on observed data

---

## Setup

1. Clone the repo
2. Install dependencies: `pip install -r requirements.txt`
3. Add Spotify API credentials to `config/spotify_creds.py`
4. Add database credentials to `config/db_creds.py`
5. Ensure a local MySQL instance is running with a `music` schema
6. Run scripts from the project root (e.g. `python Scripts/get_recent_tracks.py`)

> Note: Spotify's API app tier (development mode) limits access to 5 users and caps requests at roughly 600/day for metadata endpoints. This project is configured for personal use only.

---

## Future Ideas

- Web dashboard for visualizing listening trends (Streamlit or Flask)
- Anomaly detection on listening patterns
- Auto-generated monthly "top tracks" playlist pushed directly to Spotify

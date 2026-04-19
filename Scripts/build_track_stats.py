"""Aggregate per-track statistics from music.plays into music.track_stats.

Drops and recreates music.track_stats from scratch each run.

Produces one row per unique spotify_track_uri with:
- Play counts (total + per play_quality category)
- Average play percentage
- Total time played
- Skip rate and completion rate (computed over "decisive" plays)
- First/last played timestamps and days since last play
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import urllib.parse
from sqlalchemy import create_engine, text

from config.db_creds import DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME


def get_engine():
    pw = urllib.parse.quote_plus(DB_PASSWORD)
    return create_engine(f"mysql+pymysql://{DB_USER}:{pw}@{DB_HOST}:{DB_PORT}/{DB_NAME}")


DROP_TABLE = "DROP TABLE IF EXISTS music.track_stats"

CREATE_TABLE = """
CREATE TABLE music.track_stats (
    spotify_track_uri       VARCHAR(100)  PRIMARY KEY,
    track_name              VARCHAR(500)  NULL,
    artist_name             VARCHAR(500)  NULL,
    play_count              INT           NOT NULL,
    unique_days_played      INT           NOT NULL,
    full_listen_count       INT           NOT NULL,
    natural_end_count       INT           NOT NULL,
    partial_count           INT           NOT NULL,
    skip_count              INT           NOT NULL,
    unclassified_count      INT           NOT NULL,
    avg_play_percentage     DECIMAL(5,4)  NULL,
    total_ms_played         BIGINT        NULL,
    total_minutes_played    DECIMAL(12,2) NULL,
    skip_rate               DECIMAL(5,4)  NULL,
    completion_rate         DECIMAL(5,4)  NULL,
    first_played_at         DATETIME      NULL,
    last_played_at          DATETIME      NULL,
    days_since_last         INT           NULL,
    peak_listen_month       VARCHAR(7)    NULL,
    peak_month_play_count   INT           NULL,
    INDEX idx_play_count    (play_count),
    INDEX idx_skip_rate     (skip_rate),
    INDEX idx_last_played   (last_played_at)
)
"""

INSERT_STATS = """
INSERT INTO music.track_stats (
    spotify_track_uri, track_name, artist_name,
    play_count, unique_days_played,
    full_listen_count, natural_end_count, partial_count, skip_count, unclassified_count,
    avg_play_percentage, total_ms_played, total_minutes_played,
    skip_rate, completion_rate,
    first_played_at, last_played_at, days_since_last,
    peak_listen_month, peak_month_play_count
)
WITH peak_months AS (
    -- Per-track, the month with the most plays. Tiebreaker: most recent month wins.
    SELECT spotify_track_uri, month, monthly_plays
    FROM (
        SELECT
            spotify_track_uri,
            DATE_FORMAT(played_at, '%Y-%m') AS month,
            COUNT(*)                        AS monthly_plays,
            ROW_NUMBER() OVER (
                PARTITION BY spotify_track_uri
                ORDER BY COUNT(*) DESC, DATE_FORMAT(played_at, '%Y-%m') DESC
            ) AS rn
        FROM music.plays
        WHERE spotify_track_uri IS NOT NULL
          AND spotify_track_uri != ''
        GROUP BY spotify_track_uri, DATE_FORMAT(played_at, '%Y-%m')
    ) m
    WHERE rn = 1
)
SELECT
    p.spotify_track_uri,
    MAX(td.track_name)    AS track_name,
    MAX(td.artist_1_name) AS artist_name,

    COUNT(*)                                                                 AS play_count,
    COUNT(DISTINCT DATE(p.played_at))                                        AS unique_days_played,

    SUM(CASE WHEN p.play_quality = 'full_listen' THEN 1 ELSE 0 END)          AS full_listen_count,
    SUM(CASE WHEN p.play_quality = 'natural_end' THEN 1 ELSE 0 END)          AS natural_end_count,
    SUM(CASE WHEN p.play_quality = 'partial'     THEN 1 ELSE 0 END)          AS partial_count,
    SUM(CASE WHEN p.play_quality = 'skip'        THEN 1 ELSE 0 END)          AS skip_count,
    SUM(CASE WHEN p.play_quality IS NULL         THEN 1 ELSE 0 END)          AS unclassified_count,

    AVG(p.play_percentage)                                                   AS avg_play_percentage,
    SUM(p.ms_played)                                                         AS total_ms_played,
    ROUND(SUM(p.ms_played) / 60000.0, 2)                                     AS total_minutes_played,

    -- Skip rate: skips / decisive plays (full_listen + skip + partial)
    -- natural_end and unclassified excluded because they aren't real skip decisions
    CASE
        WHEN SUM(CASE WHEN p.play_quality IN ('full_listen','skip','partial') THEN 1 ELSE 0 END) > 0
        THEN SUM(CASE WHEN p.play_quality = 'skip' THEN 1 ELSE 0 END)
           / SUM(CASE WHEN p.play_quality IN ('full_listen','skip','partial') THEN 1 ELSE 0 END)
        ELSE NULL
    END AS skip_rate,

    -- Completion rate: full listens / decisive plays
    CASE
        WHEN SUM(CASE WHEN p.play_quality IN ('full_listen','skip','partial') THEN 1 ELSE 0 END) > 0
        THEN SUM(CASE WHEN p.play_quality = 'full_listen' THEN 1 ELSE 0 END)
           / SUM(CASE WHEN p.play_quality IN ('full_listen','skip','partial') THEN 1 ELSE 0 END)
        ELSE NULL
    END AS completion_rate,

    MIN(p.played_at)                                                         AS first_played_at,
    MAX(p.played_at)                                                         AS last_played_at,
    DATEDIFF(CURRENT_DATE, MAX(p.played_at))                                 AS days_since_last,

    MAX(pm.month)                                                            AS peak_listen_month,
    MAX(pm.monthly_plays)                                                    AS peak_month_play_count
FROM music.plays p
LEFT JOIN music.track_data td
       ON p.spotify_track_uri = td.source_uri
LEFT JOIN peak_months pm
       ON p.spotify_track_uri = pm.spotify_track_uri
WHERE p.spotify_track_uri IS NOT NULL
  AND p.spotify_track_uri != ''
GROUP BY p.spotify_track_uri
"""

SUMMARY_QUERY = """
SELECT
    COUNT(*)                           AS unique_tracks,
    SUM(play_count)                    AS total_plays,
    ROUND(AVG(play_count), 2)          AS avg_plays_per_track,
    MAX(play_count)                    AS max_plays_for_one_track,
    ROUND(AVG(skip_rate), 4)           AS avg_skip_rate,
    ROUND(AVG(completion_rate), 4)     AS avg_completion_rate
FROM music.track_stats
"""

TOP_PLAYED_QUERY = """
SELECT track_name, artist_name, play_count, full_listen_count, skip_count,
       ROUND(completion_rate, 3) AS completion_rate
FROM music.track_stats
ORDER BY play_count DESC
LIMIT 10
"""

MOST_SKIPPED_QUERY = """
SELECT track_name, artist_name, play_count, skip_count,
       ROUND(skip_rate, 3) AS skip_rate
FROM music.track_stats
WHERE play_count >= 5
ORDER BY skip_rate DESC, skip_count DESC
LIMIT 10
"""


def main():
    engine = get_engine()
    with engine.connect() as conn:
        print("Dropping existing music.track_stats table...")
        conn.execute(text(DROP_TABLE))

        print("Creating music.track_stats table...")
        conn.execute(text(CREATE_TABLE))

        print("Aggregating from music.plays...")
        r = conn.execute(text(INSERT_STATS))
        print(f"  Inserted {r.rowcount} rows")

        conn.commit()

        print("\n=== Overall summary ===")
        row = conn.execute(text(SUMMARY_QUERY)).fetchone()
        print(f"  Unique tracks:               {row[0]}")
        print(f"  Total plays:                 {row[1]}")
        print(f"  Avg plays per track:         {row[2]}")
        print(f"  Max plays for one track:     {row[3]}")
        print(f"  Avg skip rate across tracks: {row[4]}")
        print(f"  Avg completion rate:         {row[5]}")

        print("\n=== Top 10 most played ===")
        print(f"{'track':<40} {'artist':<25} {'plays':>6} {'full':>6} {'skip':>6} {'comp%':>6}")
        print("-" * 95)
        for row in conn.execute(text(TOP_PLAYED_QUERY)):
            tn = (row[0] or '')[:38]
            an = (row[1] or '')[:23]
            print(f"{tn:<40} {an:<25} {row[2]:>6} {row[3]:>6} {row[4]:>6} {str(row[5]):>6}")

        print("\n=== Top 10 most skipped (min 5 plays) ===")
        print(f"{'track':<40} {'artist':<25} {'plays':>6} {'skips':>6} {'skip%':>6}")
        print("-" * 90)
        for row in conn.execute(text(MOST_SKIPPED_QUERY)):
            tn = (row[0] or '')[:38]
            an = (row[1] or '')[:23]
            print(f"{tn:<40} {an:<25} {row[2]:>6} {row[3]:>6} {str(row[4]):>6}")


if __name__ == '__main__':
    main()

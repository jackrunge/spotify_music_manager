"""Transform raw_extended_history and raw_recently_played into the unified music.plays table.

Drops and recreates music.plays from scratch each run.

Logic overview:
- raw_extended_history (rich signals: reason_end, reason_start, ms_played):
    * Filters out near-zero-ms rows that aren't intentional skips (likely errors / queue ghosts)
    * Classifies using reason_end as primary signal, play ratio as fallback
- raw_recently_played (minimal signals: played_at only):
    * Uses gap-to-next-track (LEAD window function) to infer play ratio
    * Any gap > SESSION_GAP_SECONDS treated as session end (not a skip)
    * Deduplicates against extended history plays within ±60s on same URI

Timezone: all played_at values stored in America/New_York to match raw_recently_played.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import urllib.parse
from sqlalchemy import create_engine, text

from config.db_creds import DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME

# --- Classification thresholds (tune these after seeing results) ---
FULL_LISTEN_RATIO   = 0.8    # play_ratio >= this → full_listen
SKIP_RATIO          = 0.15   # play_ratio <  this → skip
BACKBTN_THRESHOLD   = 0.2    # backbtn with play_ratio <  this → skip, otherwise → full_listen (replay)
SESSION_GAP_SECONDS = 300    # gap > 5 min between tracks in recently_played → natural_end


def get_engine():
    pw = urllib.parse.quote_plus(DB_PASSWORD)
    return create_engine(f"mysql+pymysql://{DB_USER}:{pw}@{DB_HOST}:{DB_PORT}/{DB_NAME}")


DROP_TABLE = "DROP TABLE IF EXISTS music.plays"

CREATE_TABLE = """
CREATE TABLE music.plays (
    play_id           BIGINT        AUTO_INCREMENT PRIMARY KEY,
    spotify_track_uri VARCHAR(100)  NOT NULL,
    played_at         DATETIME      NOT NULL,
    ms_played         BIGINT        NULL,
    play_percentage   DECIMAL(5,4)  NULL,
    play_quality      VARCHAR(20)   NULL,
    skipped           TINYINT(1)    NULL,
    source            VARCHAR(20)   NOT NULL,
    INDEX idx_uri        (spotify_track_uri),
    INDEX idx_played_at  (played_at),
    INDEX idx_source     (source)
)
"""

# --- Extended history → plays -----------------------------------------------
# ts is stored as mediumtext in ISO-8601 UTC ('2023-04-09T01:15:42Z').
# Convert to DATETIME via REPLACE trick (simpler than STR_TO_DATE format escaping),
# then CONVERT_TZ to Eastern. COALESCE fallback so the script still works if
# MySQL's timezone tables aren't loaded (will just leave the value in UTC).
INSERT_FROM_EXTENDED_HISTORY = f"""
INSERT INTO music.plays
    (spotify_track_uri, played_at, ms_played, play_percentage, play_quality, skipped, source)
SELECT
    spotify_track_uri,
    played_at,
    ms_played,
    play_percentage,
    play_quality,
    CASE
        WHEN play_quality = 'skip'                                        THEN 1
        WHEN play_quality IN ('full_listen', 'natural_end', 'partial')    THEN 0
        ELSE NULL
    END AS skipped,
    'extended_history' AS source
FROM (
    SELECT
        reh.spotify_track_uri,
        COALESCE(
            CONVERT_TZ(
                CAST(REPLACE(REPLACE(reh.ts, 'T', ' '), 'Z', '') AS DATETIME),
                '+00:00',
                'America/New_York'
            ),
            CAST(REPLACE(REPLACE(reh.ts, 'T', ' '), 'Z', '') AS DATETIME)
        ) AS played_at,
        reh.ms_played,
        CASE
            WHEN td.duration_ms > 0
                THEN LEAST(reh.ms_played / td.duration_ms, 1.0)
            ELSE NULL
        END AS play_percentage,
        CASE
            -- Clear completions
            WHEN reh.reason_end = 'trackdone'
                THEN 'full_listen'

            -- Intentional skip via forward button
            WHEN reh.reason_end = 'fwdbtn'
                THEN 'skip'

            -- Back button: skip if they bailed early, replay if they listened meaningfully first
            WHEN reh.reason_end = 'backbtn'
                 AND td.duration_ms > 0
                 AND reh.ms_played / td.duration_ms < {BACKBTN_THRESHOLD}
                THEN 'skip'
            WHEN reh.reason_end = 'backbtn'
                 AND td.duration_ms > 0
                 AND reh.ms_played / td.duration_ms >= {BACKBTN_THRESHOLD}
                THEN 'full_listen'

            -- Session endings (not a skip)
            WHEN reh.reason_end IN ('endplay', 'logout',
                                   'unexpected-exit', 'unexpected-exit-while-paused',
                                   'remote')
                THEN 'natural_end'

            -- Track error — treat as not-a-skip but low quality
            WHEN reh.reason_end = 'trackerror'
                THEN 'natural_end'

            -- Unknown / missing reason_end — fall back to play ratio
            WHEN td.duration_ms > 0
                 AND reh.ms_played / td.duration_ms >= {FULL_LISTEN_RATIO}
                THEN 'full_listen'
            WHEN td.duration_ms > 0
                 AND reh.ms_played / td.duration_ms < {SKIP_RATIO}
                THEN 'skip'

            ELSE 'partial'
        END AS play_quality
    FROM music.raw_extended_history reh
    LEFT JOIN music.track_data td
           ON reh.spotify_track_uri = td.source_uri
    WHERE reh.spotify_track_uri IS NOT NULL
      AND reh.spotify_track_uri != ''
      -- Filter out likely-error rows (zero ms with no skip signal)
      AND NOT (
              reh.ms_played = 0
          AND (reh.reason_end   IS NULL OR reh.reason_end   = 'unknown')
          AND (reh.reason_start IS NULL OR reh.reason_start = 'unknown')
      )
) t
"""

# --- Recently played → plays -------------------------------------------------
# No ms_played available. We infer play_ratio from the gap to the *next* track's
# played_at (using LEAD). A very long gap means the listening session ended rather
# than the track being skipped.
INSERT_FROM_RECENTLY_PLAYED = f"""
INSERT INTO music.plays
    (spotify_track_uri, played_at, ms_played, play_percentage, play_quality, skipped, source)
SELECT
    spotify_track_uri,
    played_at,
    CASE
        -- Full listen: cap at track duration (they couldn't play longer than the song)
        WHEN play_quality = 'full_listen' AND duration_ms IS NOT NULL
            THEN LEAST(gap_seconds * 1000, duration_ms)
        -- Skip / partial: the gap IS how long they played before moving on
        WHEN play_quality IN ('skip', 'partial')
            THEN gap_seconds * 1000
        -- natural_end or unknown: can't know how long they actually listened before stopping
        ELSE NULL
    END AS ms_played,
    play_percentage,
    play_quality,
    CASE
        WHEN play_quality = 'skip'                                        THEN 1
        WHEN play_quality IN ('full_listen', 'natural_end', 'partial')    THEN 0
        ELSE NULL
    END AS skipped,
    'recently_played' AS source
FROM (
    SELECT
        rrp.spotify_track_uri,
        rrp.played_at,
        td.duration_ms,
        TIMESTAMPDIFF(SECOND, rrp.played_at,
                      LEAD(rrp.played_at) OVER (ORDER BY rrp.played_at)) AS gap_seconds,
        CASE
            WHEN LEAD(rrp.played_at) OVER (ORDER BY rrp.played_at) IS NULL
                THEN NULL  -- last track in the window, can't classify
            WHEN td.duration_ms IS NULL OR td.duration_ms = 0
                THEN NULL
            ELSE LEAST(
                TIMESTAMPDIFF(SECOND, rrp.played_at,
                              LEAD(rrp.played_at) OVER (ORDER BY rrp.played_at))
                    * 1000.0 / td.duration_ms,
                1.0
            )
        END AS play_percentage,
        CASE
            -- Last track in our window — no next track to compare, can't classify
            WHEN LEAD(rrp.played_at) OVER (ORDER BY rrp.played_at) IS NULL
                THEN NULL

            -- Large gap = session boundary, not a skip
            WHEN TIMESTAMPDIFF(SECOND, rrp.played_at,
                               LEAD(rrp.played_at) OVER (ORDER BY rrp.played_at))
                 > {SESSION_GAP_SECONDS}
                THEN 'natural_end'

            -- Need duration to compute play ratio
            WHEN td.duration_ms IS NULL OR td.duration_ms = 0
                THEN NULL

            WHEN TIMESTAMPDIFF(SECOND, rrp.played_at,
                               LEAD(rrp.played_at) OVER (ORDER BY rrp.played_at))
                    * 1000.0 / td.duration_ms >= {FULL_LISTEN_RATIO}
                THEN 'full_listen'

            WHEN TIMESTAMPDIFF(SECOND, rrp.played_at,
                               LEAD(rrp.played_at) OVER (ORDER BY rrp.played_at))
                    * 1000.0 / td.duration_ms < {SKIP_RATIO}
                THEN 'skip'

            ELSE 'partial'
        END AS play_quality
    FROM music.raw_recently_played rrp
    LEFT JOIN music.track_data td
           ON rrp.spotify_track_uri = td.source_uri
    WHERE rrp.spotify_track_uri IS NOT NULL
      AND rrp.spotify_track_uri != ''
) t
WHERE NOT EXISTS (
    -- Skip rows already captured by extended history (same URI within ±60s)
    SELECT 1
    FROM music.plays p
    WHERE p.source            = 'extended_history'
      AND p.spotify_track_uri = t.spotify_track_uri
      AND ABS(TIMESTAMPDIFF(SECOND, p.played_at, t.played_at)) <= 60
)
"""

SUMMARY_QUERY = """
SELECT source, play_quality, COUNT(*) AS n
FROM music.plays
GROUP BY source, play_quality
ORDER BY source, play_quality
"""


def main():
    engine = get_engine()
    with engine.connect() as conn:
        print("Dropping existing music.plays table...")
        conn.execute(text(DROP_TABLE))

        print("Creating music.plays table...")
        conn.execute(text(CREATE_TABLE))

        print("Inserting from raw_extended_history...")
        r1 = conn.execute(text(INSERT_FROM_EXTENDED_HISTORY))
        print(f"  Inserted {r1.rowcount} rows from raw_extended_history")

        print("Inserting from raw_recently_played...")
        r2 = conn.execute(text(INSERT_FROM_RECENTLY_PLAYED))
        print(f"  Inserted {r2.rowcount} rows from raw_recently_played")

        conn.commit()

        print("\n=== Summary ===")
        print(f"{'source':<20} {'play_quality':<15} {'count':>8}")
        print("-" * 45)
        for row in conn.execute(text(SUMMARY_QUERY)):
            print(f"{row[0]:<20} {str(row[1]):<15} {row[2]:>8}")


if __name__ == '__main__':
    main()

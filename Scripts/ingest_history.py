"""Ingest all Spotify extended streaming history JSONs in a folder into music.raw_extended_history.

Usage:
    python ingest_history.py path/to/folder

Reads every .json file in the folder, concatenates them, performs basic cleaning,
and appends to music.raw_extended_history (creating the table if it doesn't exist).
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import urllib.parse
from pathlib import Path
import pandas as pd
from sqlalchemy import create_engine

from config.db_creds import DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME

TABLE_NAME = 'raw_extended_history'
SCHEMA     = 'music'


def read_json_flexible(path):
    """Try normal JSON then NDJSON (lines=True)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    try:
        return pd.read_json(p)
    except Exception:
        try:
            return pd.read_json(p, lines=True)
        except Exception as e:
            raise ValueError(f"Could not parse JSON file {path}: {e}")


def basic_clean(df):
    # Normalize column names
    df = df.rename(columns=lambda c: c.strip().lower().replace(' ', '_'))

    # Drop completely-empty columns
    df = df.dropna(axis=1, how='all')

    # Strip whitespace in string columns
    for c in df.select_dtypes(include=['object']).columns:
        try:
            df[c] = df[c].astype('string').str.strip()
        except Exception:
            pass

    # Drop exact duplicate rows
    df = df.drop_duplicates()
    return df


def get_engine():
    pw = urllib.parse.quote_plus(DB_PASSWORD)
    return create_engine(
        f"mysql+pymysql://{DB_USER}:{pw}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )


def main():
    parser = argparse.ArgumentParser(
        description='Ingest all Spotify extended streaming history JSONs from a folder '
                    f'into {SCHEMA}.{TABLE_NAME}'
    )
    parser.add_argument('folder', help='Folder containing the extended streaming history JSON files')
    parser.add_argument('--if-exists', choices=['fail', 'replace', 'append'], default='append',
                        help='Behavior if the table already exists (default: append)')
    parser.add_argument('--chunksize', type=int, default=1000)
    parser.add_argument('--no-clean', action='store_true', help='Skip basic cleaning')
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        raise ValueError(f"'{args.folder}' is not a directory")

    json_files = sorted(folder.glob('*.json'))
    if not json_files:
        print(f"No .json files found in {folder}")
        return

    print(f"Found {len(json_files)} JSON file(s) in {folder}")

    # Read and concatenate all files
    dfs = []
    for f in json_files:
        print(f"  Reading {f.name}...")
        df = read_json_flexible(f)
        print(f"    {len(df)} rows")
        dfs.append(df)

    combined = pd.concat(dfs, ignore_index=True)
    print(f"\nCombined: {len(combined)} rows, {len(combined.columns)} columns")

    if not args.no_clean:
        print("Cleaning...")
        combined = basic_clean(combined)
        print(f"After cleaning: {len(combined)} rows, {len(combined.columns)} columns")

    engine = get_engine()
    print(f"\nUploading to {SCHEMA}.{TABLE_NAME} (if_exists='{args.if_exists}')...")
    combined.to_sql(
        TABLE_NAME, engine, schema=SCHEMA,
        index=False, if_exists=args.if_exists,
        chunksize=args.chunksize, method='multi'
    )
    print(f"Done. Uploaded {len(combined)} rows to {SCHEMA}.{TABLE_NAME}")


if __name__ == '__main__':
    main()

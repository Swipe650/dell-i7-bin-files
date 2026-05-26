#!/usr/bin/env python3
"""
Fetch scrobbles from Last.fm for a given user and time range,
export to a JSON file compatible with the TIDAL scrobbler's import feature.

Load creds from the keyring before running :
    python3 -c "import keyring; print(keyring.get_password('LastFM', 'api_key'))"

Usage examples:
    python fetch_lastfm_scrobbles.py --user myuser --apikey abc123 --start 1716379200 --end 1716393600
    python fetch_lastfm_scrobbles.py --user myuser --apikey abc123 --start "2026-05-25 12:00" --end "2026-05-25 17:00"
"""

import argparse
import json
import sys
import time
from datetime import datetime

import requests

API_BASE = "http://ws.audioscrobbler.com/2.0/"
REQUEST_DELAY = 0.22  # seconds between API requests (max 5 per second)

def parse_time_arg(value):
    """Try to parse a Unix timestamp or a human‑readable date string."""
    try:
        # Unix timestamp (seconds)
        return int(value)
    except ValueError:
        pass
    # Try common date formats
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            return int(dt.timestamp())
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Cannot parse time value: {value}")

def fetch_page(session, user, api_key, page, from_ts, to_ts):
    """Fetch a single page of recent tracks and return the parsed JSON response."""
    params = {
        "method": "user.getrecenttracks",
        "user": user,
        "api_key": api_key,
        "limit": 200,
        "page": page,
        "from": from_ts,
        "to": to_ts,
        "format": "json",
    }
    resp = session.get(API_BASE, params=params)
    resp.raise_for_status()
    return resp.json()

def extract_scrobble(track):
    """Convert a Last.fm track dict into the import‑compatible scrobble dict."""
    # Skip tracks that don't have a date (e.g., "now playing")
    date_info = track.get("date")
    if not date_info or not date_info.get("uts"):
        return None

    timestamp = int(date_info["uts"])

    # Extract album art: use the 'extralarge' image if available
    images = track.get("image", [])
    art_url = ""
    for img in images:
        if img.get("size") == "extralarge":
            art_url = img.get("#text", "")
            break
    if not art_url and images:
        # Fall back to the last image (largest)
        art_url = images[-1].get("#text", "")

    return {
        "timestamp": timestamp,
        "track": track["name"],
        "artist": track["artist"]["#text"],
        "album": track.get("album", {}).get("#text", "") or "",
        "art_url": art_url,
        "duration_sec": 0,          # Last.fm doesn't provide this
        "quality": "low",           # unknown
        "bit_depth": 0,
        "sample_rate": 0,
        "codec": "",
        "playlist": None,
        "lastfm_scrobbled": 0,      # we'll treat these as local scrobbles
        "genre": None,
    }

def main():
    parser = argparse.ArgumentParser(description="Export Last.fm scrobbles to JSON")
    parser.add_argument("--user", required=True, help="Last.fm username")
    parser.add_argument("--apikey", required=True, help="Last.fm API key")
    parser.add_argument("--start", required=True, type=parse_time_arg, help="Start time (Unix timestamp or 'YYYY-MM-DD HH:MM')")
    parser.add_argument("--end", required=True, type=parse_time_arg, help="End time")
    parser.add_argument("--output", default="lastfm_export.json", help="Output JSON file (default: lastfm_export.json)")
    args = parser.parse_args()

    print(f"Fetching scrobbles for user '{args.user}' from {args.start} to {args.end}...")
    scrobbles = []
    page = 1
    total_pages = 1

    with requests.Session() as session:
        session.headers.update({"User-Agent": "TIDALScrobbler/1.0"})

        while page <= total_pages:
            print(f"  Page {page}/{total_pages}", end="\r")
            data = fetch_page(session, args.user, args.apikey, page, args.start, args.end)
            tracks = data.get("recenttracks", {}).get("track", [])
            if not isinstance(tracks, list):
                tracks = [tracks]  # single track responses come as a dict

            for track in tracks:
                entry = extract_scrobble(track)
            if entry:
                if not (args.start <= entry["timestamp"] <= args.end):
                    continue
                scrobbles.append(entry)                
                if entry:
                    scrobbles.append(entry)

            # Update pagination info from response
            total_pages = int(data.get("recenttracks", {}).get("@attr", {}).get("totalPages", 1))
            page += 1
            time.sleep(REQUEST_DELAY)

    print(f"\nFetched {len(scrobbles)} scrobbles.")

    # Write the JSON file
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(scrobbles, f, indent=2)
    print(f"Exported to {args.output}")

    # Deduplication hint
    print("\nImport this file into your scrobbler via the Monthly page -> Import JSON button.")
    print("Existing scrobbles with the same timestamp, track, and artist will be skipped automatically.")

if __name__ == "__main__":
    main()

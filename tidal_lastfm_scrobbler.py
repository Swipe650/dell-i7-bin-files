#!/usr/bin/env python3
import warnings
import os
import sys

# Suppress Eventlet deprecation warning during import
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import eventlet

# Suppress "RLock(s) were not greened" message during monkey patch
old_stderr = sys.stderr
sys.stderr = open(os.devnull, 'w')
eventlet.monkey_patch()
sys.stderr = old_stderr

# The rest of your imports follow below
import time
import subprocess
import pylast
import keyring
# … (all other code stays unchanged)

POLL_INTERVAL = 1
SCROBBLE_THRESHOLD = 0.5          # 50% of track
MIN_SECONDS_TO_SCROBBLE = 240     # or 4 minutes

LASTFM_SERVICE = "LastFM"

current_track = {
    "title": "",
    "artist": "",
    "album": "",
    "duration_sec": 0,
    "start_time": 0,
    "max_position": 0,
    "scrobbled": False,
}
last_position = 0

def get_lastfm_network():
    try:
        username = keyring.get_password(LASTFM_SERVICE, "username")
        api_key = keyring.get_password(LASTFM_SERVICE, "api_key")
        api_secret = keyring.get_password(LASTFM_SERVICE, "api_secret")
        password = keyring.get_password(LASTFM_SERVICE, "password")
        if not all([username, api_key, api_secret, password]):
            print("❌ Last.fm credentials missing.")
            return None
        password_hash = pylast.md5(password)
        network = pylast.LastFMNetwork(
            api_key=api_key,
            api_secret=api_secret,
            username=username,
            password_hash=password_hash
        )
        network.get_authenticated_user()
        print(f"✅ Authenticated as: {username}")
        return network
    except Exception as e:
        print(f"❌ Auth failed: {e}")
        return None

def update_now_playing(network, artist, title, album):
    try:
        network.update_now_playing(artist=artist, title=title, album=album)
        print(f"📡 Now playing: {artist} - {title}")
    except Exception as e:
        print(f"⚠️  Now playing error: {e}")

def scrobble_track(network, artist, title, album, timestamp):
    try:
        network.scrobble(artist=artist, title=title, album=album, timestamp=timestamp)
        print(f"📀 Scrobbled: {artist} - {title}")
    except Exception as e:
        print(f"❌ Scrobble error: {e}")

PLAYERCTL_CMD = ["playerctl", "-p", "wiimplay"]

def run_playerctl(*args):
    try:
        result = subprocess.run(PLAYERCTL_CMD + list(args), capture_output=True, text=True, timeout=2)
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None

def get_playerctl_metadata():
    title = run_playerctl("metadata", "xesam:title")
    if not title:
        return None
    artist = run_playerctl("metadata", "xesam:artist") or ""
    album = run_playerctl("metadata", "xesam:album") or ""
    length_us = run_playerctl("metadata", "mpris:length")
    position_sec = run_playerctl("position")
    duration_sec = int(length_us) / 1_000_000 if length_us and length_us.isdigit() else 0
    pos = float(position_sec) if position_sec else 0
    return {
        "title": title,
        "artist": artist,
        "album": album,
        "duration_sec": duration_sec,
        "position": pos,
    }

def should_scrobble(max_position, duration_sec):
    if max_position <= 0:
        return False
    if duration_sec <= 0:
        return max_position >= MIN_SECONDS_TO_SCROBBLE
    progress = max_position / duration_sec
    return progress >= SCROBBLE_THRESHOLD or max_position >= MIN_SECONDS_TO_SCROBBLE

def main():
    network = get_lastfm_network()
    if not network:
        return

    global current_track, last_position
    last_title = None

    while True:
        meta = get_playerctl_metadata()
        if meta and meta["title"]:
            title = meta["title"]
            artist = meta["artist"]
            album = meta["album"]
            duration = meta["duration_sec"]
            position = meta["position"]

            # Track change detection
            if title != last_title:
                # Scrobble previous track if needed
                if last_title is not None and not current_track["scrobbled"]:
                    if should_scrobble(current_track["max_position"], current_track["duration_sec"]):
                        scrobble_track(network, current_track["artist"], current_track["title"],
                                       current_track["album"], current_track["start_time"])
                        current_track["scrobbled"] = True

                # New track
                current_track = {
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "duration_sec": duration,
                    "start_time": int(time.time()),
                    "max_position": 0,
                    "scrobbled": False,
                }
                last_title = title
                update_now_playing(network, artist, title, album)

            # Update max position
            if position > current_track["max_position"]:
                current_track["max_position"] = position

            # Check if track ended (position stuck)
            if position == last_position and last_position > 0 and not current_track["scrobbled"]:
                if should_scrobble(current_track["max_position"], current_track["duration_sec"]):
                    scrobble_track(network, current_track["artist"], current_track["title"],
                                   current_track["album"], current_track["start_time"])
                    current_track["scrobbled"] = True

            last_position = position

        else:
            # No track playing – scrobble any pending unsrobbled track
            if last_title is not None and not current_track["scrobbled"]:
                if should_scrobble(current_track["max_position"], current_track["duration_sec"]):
                    scrobble_track(network, current_track["artist"], current_track["title"],
                                   current_track["album"], current_track["start_time"])
                    current_track["scrobbled"] = True
                last_title = None

        eventlet.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    print("🎵 TIDAL → Last.fm Scrobbler (minimal, no API)")
    print("Press Ctrl+C to stop.")
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")

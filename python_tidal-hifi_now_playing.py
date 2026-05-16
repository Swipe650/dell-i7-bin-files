#!/usr/bin/env python3
import eventlet
eventlet.monkey_patch()

import time
import threading
import subprocess
import requests
from flask import Flask, render_template_string, request, jsonify
from flask_socketio import SocketIO

# ------------------------- CONFIGURATION -------------------------
BASE_URL = "http://127.0.0.1:47836"
CURRENT_URL = f"{BASE_URL}/current"
POLL_INTERVAL = 1

# Album quality cache (same as before)
album_cache = {}

# ------------------------- FLASK & SOCKET.IO -------------------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")   # uses eventlet

# Global store for current track data
current_track_data = {
    "track": "",
    "artist": "",
    "album": "",
    "art": "",
    "current": "0:00",
    "duration": "0:00",
    "duration_sec": 0,
    "progress": 0,
    "volume": 50,
    "shuffle": "off",
    "repeat": "OFF",
    "playing_from": "Playing from: Unknown",
    "quality_raw": "",
    "quality": "low",
    "bitDepth": 0,
    "sampleRate": 0,
    "codec": "",
    "badgeText": ""
}

# ------------------------- PLAYERCTL HELPERS -------------------------
PLAYERCTL_CMD = ["playerctl", "-i", "plasma-browser-integration"]

def run_playerctl(*args):
    try:
        result = subprocess.run(
            PLAYERCTL_CMD + list(args),
            capture_output=True,
            text=True,
            timeout=2
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        print(f"playerctl error: {e}")
    return None

def get_playerctl_metadata():
    title = run_playerctl("metadata", "xesam:title")
    if title is None:
        return None
    artist = run_playerctl("metadata", "xesam:artist") or ""
    album = run_playerctl("metadata", "xesam:album") or ""
    length_us = run_playerctl("metadata", "mpris:length")
    position_sec = run_playerctl("position")

    duration_sec = int(length_us) / 1_000_000 if length_us and length_us.isdigit() else 0
    pos = float(position_sec) if position_sec else 0

    return {
        "track": title,
        "artist": artist,
        "album": album,
        "duration_sec": duration_sec,
        "position": pos
    }

# ------------------------- HTTP API FETCH (with caching) -------------------------
def fetch_http_details():
    """Fetch album art, quality, and player state from HTTP API.
       Updates global current_track_data and album_cache."""
    global current_track_data
    try:
        resp = requests.get(CURRENT_URL, timeout=2, headers={'Cache-Control': 'no-cache'})
        if resp.status_code != 200:
            return
        data = resp.json()
        audio_quality = data.get("audioQuality", {})
        quality_raw = audio_quality.get("quality", "")
        badge_text = audio_quality.get("badgeText", "")
        bit_depth = audio_quality.get("bitDepth", 0)
        sample_rate = audio_quality.get("sampleRate", 0)
        codec = audio_quality.get("codec", "")
        art = data.get("image", "")
        album_name = data.get("album", "")
        artist_name = data.get("artist", "")

        # Update art and player state (these are always taken from HTTP)
        current_track_data["art"] = art
        player = data.get("player", {})
        current_track_data["shuffle"] = "on" if player.get("shuffle") else "off"
        current_track_data["repeat"] = player.get("repeat", "OFF")
        current_track_data["volume"] = round(data.get("volume", 0) * 100)
        current_track_data["playing_from"] = f"Playing from: {data.get('playingFrom', 'Unknown')}"

        # --- CACHING LOGIC (exactly as in the old working script) ---
        # Check if this response has detailed quality
        has_detailed = (bit_depth > 0 and sample_rate > 0) or (badge_text and ('kHz' in badge_text or 'kbps' in badge_text))
        if has_detailed and album_name and artist_name:
            cache_key = f"{artist_name}|{album_name}".lower()
            # Cache it (overwrite if we have better details, e.g., higher sample rate)
            existing = album_cache.get(cache_key)
            if not existing or (sample_rate > existing.get("sampleRate", 0)):
                album_cache[cache_key] = audio_quality
                print(f"Cached quality for {album_name}: {badge_text}")

        # If current quality is simplified (max/high) and we have a cache entry, use cached details
        if quality_raw.lower() in ["max", "high"] and album_name and artist_name:
            cache_key = f"{artist_name}|{album_name}".lower()
            cached = album_cache.get(cache_key)
            if cached:
                print(f"Using cached quality for {album_name}: {cached.get('badgeText')}")
                # Override with cached values
                quality_raw = cached.get("quality", quality_raw)
                badge_text = cached.get("badgeText", badge_text)
                bit_depth = cached.get("bitDepth", bit_depth)
                sample_rate = cached.get("sampleRate", sample_rate)
                codec = cached.get("codec", codec)

        # Update global track data with (possibly cached) quality info
        current_track_data["badgeText"] = badge_text
        current_track_data["quality_raw"] = quality_raw
        current_track_data["bitDepth"] = bit_depth
        current_track_data["sampleRate"] = sample_rate
        current_track_data["codec"] = codec

        # Map to internal quality for color coding
        ql = quality_raw.lower()
        if ql in ["hi_res_lossless", "max"]:
            current_track_data["quality"] = "hi_res_lossless"
        elif ql in ["lossless", "high"]:
            current_track_data["quality"] = "lossless"
        else:
            current_track_data["quality"] = "low"

    except Exception as e:
        print(f"HTTP fetch error: {e}")

# ------------------------- BACKGROUND POLLER (playerctl + HTTP) -------------------------
def background_poller():
    """Poll playerctl and HTTP API every second, emit updates."""
    global current_track_data
    last_title = None
    while True:
        # 1. Get reliable metadata from playerctl
        meta = get_playerctl_metadata()
        if meta:
            title = meta["track"]
            track_changed = (title != last_title)
            if track_changed:
                print(f"Track changed: {title}")
                # Update basic metadata (title, artist, album, duration)
                current_track_data["track"] = title
                current_track_data["artist"] = meta["artist"]
                current_track_data["album"] = meta["album"]
                current_track_data["duration_sec"] = meta["duration_sec"]
                mins = int(meta["duration_sec"] // 60)
                secs = int(meta["duration_sec"] % 60)
                current_track_data["duration"] = f"{mins}:{secs:02d}"
                last_title = title

            # Always update position and progress from playerctl
            pos = meta["position"]
            dur = current_track_data["duration_sec"]
            progress = (pos / dur * 100) if dur > 0 else 0
            mins = int(pos // 60)
            secs = int(pos % 60)
            current_track_data["current"] = f"{mins}:{secs:02d}"
            current_track_data["progress"] = round(progress, 1)

        # 2. Fetch HTTP details (art, quality, player state) – this also updates cache
        fetch_http_details()

        # 3. Emit combined update to all clients
        socketio.emit('update', current_track_data)

        eventlet.sleep(POLL_INTERVAL)

# ------------------------- FLASK ROUTES (controls via HTTP) -------------------------
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/control/<action>', methods=['POST'])
def control(action):
    endpoints = {'playpause': 'playpause', 'next': 'next', 'previous': 'previous'}
    if action in endpoints:
        try:
            requests.post(f"{BASE_URL}/player/{endpoints[action]}", timeout=2)
        except:
            pass
    return ('', 204)

@app.route('/toggle_shuffle', methods=['POST'])
def toggle_shuffle():
    try:
        requests.post(f"{BASE_URL}/player/shuffle/toggle", headers={'accept': 'text/plain'}, data='', timeout=2)
    except:
        pass
    return ('', 204)

@app.route('/toggle_repeat', methods=['POST'])
def toggle_repeat():
    try:
        requests.post(f"{BASE_URL}/player/repeat/toggle", headers={'accept': 'text/plain'}, data='', timeout=2)
    except:
        pass
    return ('', 204)

@app.route('/seek/<int:seconds>', methods=['PUT'])
def seek(seconds):
    try:
        requests.put(f"{BASE_URL}/player/seek/absolute?seconds={seconds}", timeout=2)
    except:
        pass
    return ('', 204)

@app.route('/cache/clear', methods=['GET', 'POST'])
def clear_cache():
    album_cache.clear()
    print("Album cache cleared")
    return ('', 204)

@app.route('/cache/view')
def view_cache():
    if not album_cache:
        return "<h3>Cache is empty</h3><p><a href='/'>Back</a></p>"
    html = "<h3>Cached Albums</h3><ul>"
    for key, val in album_cache.items():
        html += f"<li><strong>{key}</strong><br>&nbsp;&nbsp;{val.get('badgeText', 'N/A')}</li>"
    html += "</ul><p><a href='/'>Back</a></p>"
    return html

# ------------------------- FRONTEND HTML (unchanged, paste your working template) -------------------------
# (I'll include the exact same HTML from your previous stable version)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title id="page-title">Now Playing</title>
    <link id="favicon" rel="icon" type="image/png" href="">
    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
    <style>
        body { margin:0; font-family:-apple-system; color:white; overflow:hidden; }
        .bg { position:fixed; width:100%; height:100%; background-size:cover; filter:blur(10px) brightness(0.35); z-index:-1; transition: background-image 0.5s ease; }
        .overlay { display:flex; height:100vh; align-items:center; justify-content:center; }
        .card { display:flex; gap:40px; background:rgba(0,0,0,0.4); padding:30px; border-radius:20px; backdrop-filter:blur(20px); max-width:90vw; }
        .art { width:260px; border-radius:16px; }
        .info { display:flex; flex-direction:column; justify-content:center; min-width: 400px; flex: 1; }
        .track { font-size:2em; word-break:break-word; overflow-wrap:break-word; }
        .artist { color:#ccc; }
        .album { color:#999; margin-bottom:20px; }
        .playing-from { font-size:0.9em; margin-top:5px; margin-bottom:10px; display:flex; align-items:center; gap:8px; }
        .progress-container { width:100%; height:6px; background:rgba(255,255,255,0.2); border-radius:10px; overflow:hidden; cursor:default; min-width: 300px; }
        .progress { height:100%; background:#1db954; width:0%; transition:width 0.2s linear; }
        .time { display:flex; justify-content:space-between; font-size:0.8em; color:#aaa; }
        .controls { margin-top:20px; display:flex; gap:20px; flex-wrap: wrap; }
        .btn { background:rgba(255,255,255,0.1); border:none; color:white; padding:10px 15px; border-radius:10px; cursor:pointer; font-size:1em; transition: background-color 0.2s ease; }
        .btn:hover { background:rgba(255,255,255,0.25); }
        .meta { margin-top:10px; font-size:0.85em; color:#bbb; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
        .quality-badge { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 12px; font-size: 0.85em; font-weight: 600; letter-spacing: 0.5px; color: #ddd; background: rgba(255,255,255,0.1); }
        .bitrate { font-size:0.95em; font-family: monospace; letter-spacing: 0.5px; font-weight: bold; transition: all 0.3s ease; padding: 4px 10px; border-radius: 12px; backdrop-filter: blur(8px); display: inline-block; }
        .bitrate-max { color: #FFB347; background-color: rgba(255, 179, 71, 0.20); box-shadow: 0 0 5px rgba(255, 179, 71, 0.2); }
        .bitrate-high { color: #40E0D0; background-color: rgba(64, 224, 208, 0.20); box-shadow: 0 0 5px rgba(64, 224, 208, 0.2); }
        .bitrate-low { color: #888888; background-color: rgba(136, 136, 136, 0.20); box-shadow: 0 0 5px rgba(136, 136, 136, 0.2); }
        .clickable { cursor: pointer; padding: 2px 6px; border-radius: 8px; transition: background-color 0.2s ease; display: inline-block; }
        .clickable:hover { background-color: rgba(255,255,255,0.2); }
        @media (max-width: 768px) {
            .card { flex-direction: column; align-items: center; gap:20px; padding:20px; }
            .art { width:200px; }
            .track { font-size:1.5em; text-align:center; }
            .artist, .album { text-align:center; }
            .playing-from { justify-content:center; }
            .info { min-width: 280px; }
            .meta { flex-direction: column; gap: 5px; text-align: center; }
            .bitrate { margin-top: 5px; }
            .controls { justify-content: center; }
        }
    </style>
</head>
<body>
    <div id="bg" class="bg"></div>
    <div class="overlay">
        <div class="card">
            <img id="art" class="art" src="" />
            <div class="info">
                <div id="track" class="track"></div>
                <div id="artist" class="artist"></div>
                <div id="album" class="album"></div>
                <div id="playingFrom" class="playing-from"></div>
                <div class="progress-container" id="progress-container">
                    <div id="progress" class="progress"></div>
                </div>
                <div class="time"><span id="current"></span><span id="duration"></span></div>
                <div class="controls">
                    <button class="btn" onclick="control('previous')">⏮ Previous</button>
                    <button class="btn" onclick="control('playpause')">⏯ Play/Pause</button>
                    <button class="btn" onclick="control('next')">⏭ Next</button>
                </div>
                <div class="meta">
                    <div style="display: flex; gap: 8px; flex-wrap: wrap; align-items: center;">
                        <span id="metaText"></span>
                        <span id="qualityBadge" class="quality-badge"></span>
                    </div>
                    <span id="bitrate" class="bitrate"></span>
                </div>
            </div>
        </div>
    </div>
<script>
const socket = io({ reconnection: true, reconnectionAttempts: Infinity, reconnectionDelay: 1000 });
let trackDurationSec = 0;
const artCache = new Map();

function updateArt(url) {
    if (!url) return;
    if (artCache.has(url)) {
        document.getElementById('art').src = artCache.get(url);
        document.getElementById('bg').style.backgroundImage = `url('${artCache.get(url)}')`;
        return;
    }
    const img = new Image();
    img.onload = () => { artCache.set(url, img.src); document.getElementById('art').src = img.src; document.getElementById('bg').style.backgroundImage = `url('${img.src}')`; };
    img.src = url;
}

const QUALITY_DISPLAY_MAP = { 'HI_RES_LOSSLESS': 'HI_RES_LOSSLESS', 'LOSSLESS': 'LOSSLESS', 'max': 'HI_RES_LOSSLESS', 'high': 'LOSSLESS', 'low': 'Low' };
const BITRATE_DEFAULTS = { 'hi_res_lossless': '24-bit 44.1kHz', 'lossless': '16-bit 44.1kHz', 'low': '96 kbps' };

function updateBitrateColor(qualityType, bitrateText) {
    const el = document.getElementById('bitrate');
    el.classList.remove('bitrate-max', 'bitrate-high', 'bitrate-low');
    if (qualityType === 'max' || qualityType === 'hi_res_lossless') el.classList.add('bitrate-max');
    else if (qualityType === 'high' || qualityType === 'lossless') el.classList.add('bitrate-high');
    else el.classList.add('bitrate-low');
}

function getQualityDisplay(q) { return QUALITY_DISPLAY_MAP[q] || q; }
function getBitrateText(quality, bitDepth, sampleRate, badgeText) {
    if (badgeText && /(bit|kHz|kbps)/.test(badgeText)) return badgeText;
    if (bitDepth && sampleRate) return `${bitDepth}-bit ${sampleRate/1000}kHz`;
    return BITRATE_DEFAULTS[quality] || 'Unknown';
}

function toggleShuffle() { fetch('/toggle_shuffle', { method: 'POST' }); }
function toggleRepeat() { fetch('/toggle_repeat', { method: 'POST' }); }

function updateUI(data) {
    document.getElementById('track').innerText = data.track;
    document.getElementById('artist').innerText = data.artist;
    document.getElementById('album').innerText = data.album;
    document.getElementById('playingFrom').innerHTML = data.playing_from;
    document.getElementById('current').innerText = data.current;
    document.getElementById('duration').innerText = data.duration;
    document.getElementById('progress').style.width = data.progress + '%';
    document.getElementById('metaText').innerHTML = `💿 Volume: ${data.volume}% | 🔀 <span class="clickable" onclick="toggleShuffle()">Shuffle: ${data.shuffle}</span> | 🔁 <span class="clickable" onclick="toggleRepeat()">Repeat: ${data.repeat}</span>`;
    
    const badge = document.getElementById('qualityBadge');
    if (data.quality_raw) { badge.innerText = getQualityDisplay(data.quality_raw); badge.style.display = 'inline-flex'; }
    else { badge.style.display = 'none'; }
    
    const bitrateText = getBitrateText(data.quality, data.bitDepth, data.sampleRate, data.badgeText);
    document.getElementById('bitrate').innerText = bitrateText;
    updateBitrateColor(data.quality, bitrateText);
    updateArt(data.art);
    document.getElementById('page-title').innerText = `${data.artist} - ${data.track}`;
    document.getElementById('favicon').href = data.art;
    trackDurationSec = data.duration_sec || trackDurationSec;
}

socket.on('update', (data) => updateUI(data));
socket.on('connect', () => console.log('Connected'));
socket.on('disconnect', () => setTimeout(() => socket.connect(), 1000));

function control(action) { fetch(`/control/${action}`, { method: 'POST' }); }
document.getElementById('progress-container').addEventListener('click', (e) => {
    const rect = e.target.closest('.progress-container').getBoundingClientRect();
    const percent = (e.clientX - rect.left) / rect.width;
    fetch(`/seek/${Math.floor(percent * trackDurationSec)}`, { method: 'PUT' });
});
</script>
</body>
</html>
"""

# ------------------------- MAIN ENTRY POINT -------------------------
if __name__ == '__main__':
    poller_thread = threading.Thread(target=background_poller, daemon=True)
    poller_thread.start()
    print("Starting TIDAL HIFI PLAYER (playerctl + HTTP cache)")
    print("Open http://127.0.0.1:5000")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)

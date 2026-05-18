#!/usr/bin/env python3
import eventlet
eventlet.monkey_patch()

import time
import threading
import subprocess
import requests
import sqlite3
import json
import os
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, jsonify, send_file
from flask_socketio import SocketIO
import io

# ------------------------- CONFIGURATION -------------------------
BASE_URL = "http://127.0.0.1:47836"
CURRENT_URL = f"{BASE_URL}/current"
POLL_INTERVAL = 1
DATABASE = "scrobbles.db"
SCROBBLE_THRESHOLD = 0.5
MIN_SECONDS_TO_SCROBBLE = 240

album_cache = {}
last_cached_album = None

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

current_track_data = {
    "track": "", "artist": "", "album": "", "art": "", "current": "0:00", "duration": "0:00",
    "duration_sec": 0, "progress": 0, "volume": 50, "shuffle": "off", "repeat": "OFF",
    "playing_from": "Playing from: Unknown", "quality_raw": "", "quality": "low",
    "bitDepth": 0, "sampleRate": 0, "codec": "", "badgeText": ""
}

session = {
    "current_track_id": None, "track_start_time": 0, "max_position": 0,
    "last_track_data": None, "lock": threading.Lock()
}

# ------------------------- DATABASE -------------------------
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS scrobbles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER NOT NULL,
        track TEXT NOT NULL,
        artist TEXT NOT NULL,
        album TEXT,
        art_url TEXT,
        duration_sec INTEGER,
        quality TEXT,
        bit_depth INTEGER,
        sample_rate INTEGER,
        codec TEXT
    )''')
    conn.commit()
    conn.close()

def add_scrobble(track, artist, album, art_url, duration_sec, quality, bit_depth, sample_rate, codec):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''INSERT INTO scrobbles 
        (timestamp, track, artist, album, art_url, duration_sec, quality, bit_depth, sample_rate, codec)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (int(time.time()), track, artist, album, art_url, duration_sec, quality, bit_depth, sample_rate, codec))
    conn.commit()
    conn.close()
    print(f"Scrobbled: {artist} - {track}")

def get_all_scrobbles(limit=100, offset=0):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''SELECT * FROM scrobbles ORDER BY timestamp DESC LIMIT ? OFFSET ?''', (limit, offset))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def count_scrobbles():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM scrobbles')
    count = c.fetchone()[0]
    conn.close()
    return count

def get_top_artists(limit=25):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''SELECT artist, COUNT(*) as playcount 
                 FROM scrobbles 
                 GROUP BY artist 
                 ORDER BY playcount DESC 
                 LIMIT ?''', (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"artist": row[0], "playcount": row[1]} for row in rows]

def get_top_albums(limit=25):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''SELECT artist, album, COUNT(*) as playcount 
                 FROM scrobbles 
                 WHERE album IS NOT NULL AND album != ''
                 GROUP BY artist, album 
                 ORDER BY playcount DESC 
                 LIMIT ?''', (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"artist": row[0], "album": row[1], "playcount": row[2]} for row in rows]

def get_top_tracks(limit=25):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''SELECT artist, track, COUNT(*) as playcount 
                 FROM scrobbles 
                 GROUP BY artist, track 
                 ORDER BY playcount DESC 
                 LIMIT ?''', (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"artist": row[0], "track": row[1], "playcount": row[2]} for row in rows]

def get_listening_time():
    """Return total listening hours for today, this week, this month, this year."""
    now = datetime.now()
    today_start = int(datetime(now.year, now.month, now.day).timestamp())
    week_start = int((now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    month_start = int(datetime(now.year, now.month, 1).timestamp())
    year_start = int(datetime(now.year, 1, 1).timestamp())
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    def sum_seconds(since_ts):
        c.execute('SELECT SUM(duration_sec) FROM scrobbles WHERE timestamp >= ? AND duration_sec > 0', (since_ts,))
        result = c.fetchone()[0]
        return result if result else 0
    
    today_sec = sum_seconds(today_start)
    week_sec = sum_seconds(week_start)
    month_sec = sum_seconds(month_start)
    year_sec = sum_seconds(year_start)
    conn.close()
    
    return {
        "today": round(today_sec / 3600, 1),
        "week": round(week_sec / 3600, 1),
        "month": round(month_sec / 3600, 1),
        "year": round(year_sec / 3600, 1)
    }

def export_scrobbles_to_json():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM scrobbles ORDER BY timestamp')
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def import_scrobbles_from_json(data):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    inserted = 0
    for item in data:
        c.execute('SELECT id FROM scrobbles WHERE timestamp=? AND track=? AND artist=?',
                  (item['timestamp'], item['track'], item['artist']))
        if not c.fetchone():
            c.execute('''INSERT INTO scrobbles 
                (timestamp, track, artist, album, art_url, duration_sec, quality, bit_depth, sample_rate, codec)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (item['timestamp'], item['track'], item['artist'], item.get('album'),
                 item.get('art_url'), item.get('duration_sec'), item.get('quality'),
                 item.get('bit_depth'), item.get('sample_rate'), item.get('codec')))
            inserted += 1
    conn.commit()
    conn.close()
    return inserted

# ------------------------- PLAYERCTL & HTTP -------------------------
PLAYERCTL_CMD = ["playerctl", "-i", "plasma-browser-integration"]

def run_playerctl(*args):
    try:
        result = subprocess.run(PLAYERCTL_CMD + list(args), capture_output=True, text=True, timeout=2)
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
    return {"track": title, "artist": artist, "album": album, "duration_sec": duration_sec, "position": pos}

def fetch_http_details():
    global current_track_data, last_cached_album
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

        current_track_data["art"] = art
        player = data.get("player", {})
        current_track_data["shuffle"] = "on" if player.get("shuffle") else "off"
        current_track_data["repeat"] = player.get("repeat", "OFF")
        current_track_data["volume"] = round(data.get("volume", 0) * 100)
        current_track_data["playing_from"] = f"Playing from: {data.get('playingFrom', 'Unknown')}"

        has_detailed = (bit_depth > 0 and sample_rate > 0) or (badge_text and ('kHz' in badge_text or 'kbps' in badge_text))
        if has_detailed and album_name and artist_name:
            cache_key = f"{artist_name}|{album_name}".lower()
            existing = album_cache.get(cache_key)
            if not existing or (sample_rate > existing.get("sampleRate", 0)):
                album_cache[cache_key] = audio_quality
                print(f"Cached quality for {album_name}: {badge_text}")

        if quality_raw.lower() in ["max", "high"] and album_name and artist_name:
            cache_key = f"{artist_name}|{album_name}".lower()
            cached = album_cache.get(cache_key)
            if cached:
                if last_cached_album != cache_key:
                    print(f"Using cached quality for {album_name}: {cached.get('badgeText')}")
                    last_cached_album = cache_key
                quality_raw = cached.get("quality", quality_raw)
                badge_text = cached.get("badgeText", badge_text)
                bit_depth = cached.get("bitDepth", bit_depth)
                sample_rate = cached.get("sampleRate", sample_rate)
                codec = cached.get("codec", codec)

        current_track_data["badgeText"] = badge_text
        current_track_data["quality_raw"] = quality_raw
        current_track_data["bitDepth"] = bit_depth
        current_track_data["sampleRate"] = sample_rate
        current_track_data["codec"] = codec

        ql = quality_raw.lower()
        if ql in ["hi_res_lossless", "max"]:
            current_track_data["quality"] = "hi_res_lossless"
        elif ql in ["lossless", "high"]:
            current_track_data["quality"] = "lossless"
        else:
            current_track_data["quality"] = "low"
    except Exception as e:
        print(f"HTTP fetch error: {e}")

def maybe_scrobble(previous_track_data, max_position, duration):
    if not previous_track_data or not previous_track_data.get("track"):
        return False
    if duration <= 0:
        return max_position >= MIN_SECONDS_TO_SCROBBLE
    progress = max_position / duration
    return progress >= SCROBBLE_THRESHOLD or max_position >= MIN_SECONDS_TO_SCROBBLE

def background_poller():
    global current_track_data
    last_title = None
    while True:
        meta = get_playerctl_metadata()
        if meta:
            title = meta["track"]
            track_changed = (title != last_title and last_title is not None)
            with session["lock"]:
                if track_changed and session["last_track_data"]:
                    if maybe_scrobble(session["last_track_data"], session["max_position"], session["last_track_data"]["duration_sec"]):
                        ld = session["last_track_data"]
                        add_scrobble(ld["track"], ld["artist"], ld["album"], ld["art_url"], ld["duration_sec"],
                                     ld["quality"], ld["bit_depth"], ld["sample_rate"], ld["codec"])
                    session["track_start_time"] = time.time()
                    session["max_position"] = 0
                    session["last_track_data"] = None
                if track_changed or not session["last_track_data"]:
                    session["last_track_data"] = {
                        "track": title, "artist": meta["artist"], "album": meta["album"],
                        "art_url": current_track_data.get("art", ""), "duration_sec": meta["duration_sec"],
                        "quality": current_track_data.get("quality", "low"),
                        "bit_depth": current_track_data.get("bitDepth", 0),
                        "sample_rate": current_track_data.get("sampleRate", 0),
                        "codec": current_track_data.get("codec", "")
                    }
                    session["track_start_time"] = time.time()
                    session["max_position"] = 0
                    last_title = title
                pos = meta["position"]
                if pos > session["max_position"]:
                    session["max_position"] = pos
            current_track_data["track"] = title
            current_track_data["artist"] = meta["artist"]
            current_track_data["album"] = meta["album"]
            current_track_data["duration_sec"] = meta["duration_sec"]
            mins = int(meta["duration_sec"] // 60)
            secs = int(meta["duration_sec"] % 60)
            current_track_data["duration"] = f"{mins}:{secs:02d}"
            pos = meta["position"]
            dur = current_track_data["duration_sec"]
            progress = (pos / dur * 100) if dur > 0 else 0
            mins = int(pos // 60)
            secs = int(pos % 60)
            current_track_data["current"] = f"{mins}:{secs:02d}"
            current_track_data["progress"] = round(progress, 1)
        fetch_http_details()
        with session["lock"]:
            if session["last_track_data"]:
                session["last_track_data"]["art_url"] = current_track_data.get("art", session["last_track_data"]["art_url"])
                session["last_track_data"]["quality"] = current_track_data.get("quality", session["last_track_data"]["quality"])
                session["last_track_data"]["bit_depth"] = current_track_data.get("bitDepth", session["last_track_data"]["bit_depth"])
                session["last_track_data"]["sample_rate"] = current_track_data.get("sampleRate", session["last_track_data"]["sample_rate"])
                session["last_track_data"]["codec"] = current_track_data.get("codec", session["last_track_data"]["codec"])
        socketio.emit('update', current_track_data)
        eventlet.sleep(POLL_INTERVAL)

# ------------------------- FLASK ROUTES -------------------------
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/scrobbles')
def scrobbles_page():
    return render_template_string(SCROBBLES_TEMPLATE)

@app.route('/api/scrobbles')
def api_scrobbles():
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    scrobbles = get_all_scrobbles(limit, offset)
    total = count_scrobbles()
    return jsonify({"scrobbles": scrobbles, "total": total})

@app.route('/api/now')
def api_now():
    return jsonify({
        "track": current_track_data.get("track", ""),
        "artist": current_track_data.get("artist", ""),
        "album": current_track_data.get("album", ""),
        "art": current_track_data.get("art", ""),
        "duration": current_track_data.get("duration", "0:00"),
        "current": current_track_data.get("current", "0:00"),
        "progress": current_track_data.get("progress", 0)
    })

@app.route('/api/stats')
def api_stats():
    top_artists = get_top_artists(25)
    top_albums = get_top_albums(25)
    top_tracks = get_top_tracks(25)
    total_scrobbles = count_scrobbles()
    return jsonify({
        "top_artists": top_artists,
        "top_albums": top_albums,
        "top_tracks": top_tracks,
        "total_scrobbles": total_scrobbles
    })

@app.route('/api/listening_time')
def api_listening_time():
    return jsonify(get_listening_time())

@app.route('/api/listening_clock')
def api_listening_clock():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    # Get scrobble count per hour of day (0-23)
    c.execute("""
        SELECT 
            strftime('%H', datetime(timestamp, 'unixepoch')) as hour,
            COUNT(*) as count
        FROM scrobbles
        GROUP BY hour
        ORDER BY hour
    """)
    rows = c.fetchall()
    conn.close()
    
    hour_counts = [0] * 24
    for row in rows:
        hour = int(row[0])
        hour_counts[hour] = row[1]
    
    max_count = max(hour_counts) if hour_counts else 0
    busiest_hour = hour_counts.index(max_count) if max_count > 0 else 0
    
    return jsonify({
        "hour_counts": hour_counts,
        "busiest_hour": busiest_hour,
        "busiest_hour_count": max_count
    })

@app.route('/api/scrobble/now', methods=['POST'])
def scrobble_now():
    with session["lock"]:
        if session["last_track_data"] and session["last_track_data"]["track"]:
            ld = session["last_track_data"]
            add_scrobble(ld["track"], ld["artist"], ld["album"], ld["art_url"], ld["duration_sec"],
                         ld["quality"], ld["bit_depth"], ld["sample_rate"], ld["codec"])
            return jsonify({"status": "scrobbled"})
    return jsonify({"status": "no track playing"}), 400

@app.route('/export')
def export_data():
    data = export_scrobbles_to_json()
    json_str = json.dumps(data, indent=2)
    return send_file(io.BytesIO(json_str.encode('utf-8')), mimetype='application/json',
                     as_attachment=True, download_name='scrobbles_export.json')

@app.route('/import', methods=['POST'])
def import_data():
    file = request.files.get('file')
    if not file:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        data = json.load(file)
        if not isinstance(data, list):
            return jsonify({"error": "JSON must be a list"}), 400
        inserted = import_scrobbles_from_json(data)
        return jsonify({"status": f"Imported {inserted} new scrobbles"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

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
    global album_cache, last_cached_album
    album_cache.clear()
    last_cached_album = None
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

# ------------------------- MAIN PLAYER PAGE (unchanged) -------------------------
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
        .track { font-size:2em; word-break:break-word; }
        .artist { color:#ccc; }
        .album { color:#999; margin-bottom:20px; }
        .playing-from { font-size:0.9em; margin-top:5px; margin-bottom:10px; display:flex; align-items:center; gap:8px; }
        .progress-container { width:100%; height:6px; background:rgba(255,255,255,0.2); border-radius:10px; overflow:hidden; cursor:default; }
        .progress { height:100%; background:#1db954; width:0%; transition:width 0.2s linear; }
        .time { display:flex; justify-content:space-between; font-size:0.8em; color:#aaa; }
        .controls { margin-top:20px; display:flex; gap:20px; flex-wrap: wrap; }
        .btn { background:rgba(255,255,255,0.1); border:none; color:white; padding:10px 15px; border-radius:10px; cursor:pointer; font-size:1em; transition: background-color 0.2s ease; }
        .btn:hover { background:rgba(255,255,255,0.25); }
        .meta { margin-top:10px; font-size:0.85em; color:#bbb; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
        .quality-badge { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 12px; font-size: 0.85em; font-weight: 600; background: rgba(255,255,255,0.1); }
        .bitrate { font-size:0.95em; font-family: monospace; padding: 4px 10px; border-radius: 12px; backdrop-filter: blur(8px); display: inline-block; }
        .bitrate-max { color: #FFB347; background-color: rgba(255, 179, 71, 0.20); }
        .bitrate-high { color: #40E0D0; background-color: rgba(64, 224, 208, 0.20); }
        .bitrate-low { color: #888888; background-color: rgba(136, 136, 136, 0.20); }
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

# ------------------------- SCROBBLES OVERVIEW PAGE (with listening clock pie chart) -------------------------
SCROBBLES_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Scrobble Overview · TIDAL</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Cdefs%3E%3CradialGradient id='grad' cx='50%25' cy='50%25' r='50%25'%3E%3Cstop offset='0%25' stop-color='%23e0e0e0'/%3E%3Cstop offset='70%25' stop-color='%23a0a0a0'/%3E%3Cstop offset='100%25' stop-color='%23404040'/%3E%3C/radialGradient%3E%3C/defs%3E%3Ccircle cx='50' cy='50' r='48' fill='url(%23grad)' stroke='%23333' stroke-width='2'/%3E%3Ccircle cx='50' cy='50' r='12' fill='%23333'/%3E%3C/svg%3E">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        :root {
            --bg-body: #f9f9f9;
            --bg-card: #ffffff;
            --bg-tools: #ffffff;
            --text-primary: #222;
            --text-secondary: #666;
            --text-muted: #999;
            --border-light: #e5e5e5;
            --border-card: #eee;
            --accent: #d51007;
            --accent-hover: #b00;
            --button-bg: #f5f5f5;
            --button-border: #ddd;
            --button-hover: #e9e9e9;
            --hover-row: #fef9f9;
            --shadow: rgba(0,0,0,0.05);
            --art-bg: #f0f0f0;
        }
        body.dark {
            --bg-body: #121212;
            --bg-card: #1e1e1e;
            --bg-tools: #1e1e1e;
            --text-primary: #eee;
            --text-secondary: #bbb;
            --text-muted: #888;
            --border-light: #2c2c2c;
            --border-card: #2c2c2c;
            --accent: #ff6b6b;
            --accent-hover: #ff8a8a;
            --button-bg: #2c2c2c;
            --button-border: #444;
            --button-hover: #3a3a3a;
            --hover-row: #2a2a2a;
            --shadow: rgba(0,0,0,0.3);
            --art-bg: #2c2c2c;
        }
        * { box-sizing: border-box; }
        body {
            font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
            background: var(--bg-body);
            margin: 0;
            padding: 0;
            color: var(--text-primary);
            transition: background 0.2s, color 0.2s;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 2rem 1rem;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            flex-wrap: wrap;
            margin-bottom: 2rem;
            border-bottom: 1px solid var(--border-light);
            padding-bottom: 1rem;
        }
        h1 {
            font-size: 1.8rem;
            font-weight: 500;
            margin: 0;
            color: var(--accent);
            letter-spacing: -0.5px;
        }
        .sub {
            color: var(--text-muted);
            font-size: 0.85rem;
        }
        .player-link {
            background: var(--button-bg);
            border: 1px solid var(--button-border);
            padding: 0.4rem 1rem;
            border-radius: 20px;
            text-decoration: none;
            color: var(--accent);
            font-size: 0.85rem;
            font-weight: 500;
            transition: all 0.2s;
        }
        .player-link:hover {
            background: var(--accent);
            color: white;
            border-color: var(--accent);
        }
        .now-playing {
            background: var(--bg-card);
            border-radius: 16px;
            padding: 1.5rem;
            margin-bottom: 2rem;
            display: flex;
            gap: 1.5rem;
            align-items: center;
            box-shadow: 0 2px 8px var(--shadow);
            border: 1px solid var(--border-card);
        }
        .now-art {
            width: 80px;
            height: 80px;
            border-radius: 12px;
            object-fit: cover;
            background: var(--art-bg);
        }
        .now-info {
            flex: 1;
        }
        .now-label {
            font-size: 0.7rem;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--accent);
            font-weight: 600;
        }
        .now-track {
            font-size: 1.4rem;
            font-weight: 600;
            margin: 0.2rem 0;
        }
        .now-artist, .now-album {
            color: var(--text-secondary);
            font-size: 0.9rem;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 1.5rem;
            margin-bottom: 1.5rem;
        }
        .stat-card {
            background: var(--bg-card);
            border-radius: 16px;
            padding: 1rem;
            box-shadow: 0 1px 4px var(--shadow);
            border: 1px solid var(--border-card);
        }
        .stat-card h3 {
            margin: 0 0 1rem 0;
            font-size: 1.2rem;
            font-weight: 500;
            color: var(--accent);
            border-left: 3px solid var(--accent);
            padding-left: 0.75rem;
        }
        .stat-list {
            list-style: none;
            padding: 0;
            margin: 0;
            max-height: 300px;
            overflow-y: auto;
            padding-right: 5px;
        }
        .stat-list li {
            display: flex;
            justify-content: space-between;
            padding: 0.5rem 0;
            border-bottom: 1px solid var(--border-card);
            font-size: 0.9rem;
        }
        .stat-list li span:first-child {
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            padding-right: 1rem;
        }
        .stat-count {
            font-weight: 600;
            color: var(--accent);
            flex-shrink: 0;
        }
        .stat-list::-webkit-scrollbar {
            width: 6px;
        }
        .stat-list::-webkit-scrollbar-track {
            background: var(--border-card);
            border-radius: 3px;
        }
        .stat-list::-webkit-scrollbar-thumb {
            background: #aaa;
            border-radius: 3px;
        }
        body.dark .stat-list::-webkit-scrollbar-thumb {
            background: #aaa;
        }
        .time-card {
            background: var(--bg-card);
            border-radius: 16px;
            padding: 0.6rem 1rem;
            margin-bottom: 1.5rem;
            text-align: center;
            box-shadow: 0 1px 4px var(--shadow);
            border: 1px solid var(--border-card);
        }
        .time-stats {
            display: flex;
            justify-content: space-around;
            flex-wrap: wrap;
            gap: 0.5rem;
            margin-top: 0;
        }
        .time-item {
            text-align: center;
            min-width: 70px;
        }
        .time-label {
            font-size: 0.65rem;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--text-muted);
        }
        .time-value {
            font-size: 1.2rem;
            font-weight: 600;
            color: var(--accent);
        }
        .total-scrobbles {
            text-align: center;
            font-size: 0.8rem;
            margin-top: 0.3rem;
            color: var(--text-muted);
        }
        .tools {
            background: var(--bg-tools);
            border-radius: 12px;
            padding: 1rem;
            margin-bottom: 2rem;
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            align-items: center;
            box-shadow: 0 1px 3px var(--shadow);
            border: 1px solid var(--border-card);
        }
        .tools button, .tools label {
            background: var(--button-bg);
            border: 1px solid var(--button-border);
            padding: 0.5rem 1rem;
            border-radius: 30px;
            font-size: 0.8rem;
            cursor: pointer;
            font-family: inherit;
            transition: all 0.2s;
            color: var(--text-primary);
        }
        .tools button:hover, .tools label:hover {
            background: var(--button-hover);
            border-color: var(--text-muted);
        }
        .theme-toggle {
            background: var(--button-bg);
            border: 1px solid var(--button-border);
            border-radius: 30px;
            padding: 0.5rem 1rem;
            font-size: 0.8rem;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }
        .scrobble-list {
            background: var(--bg-card);
            border-radius: 16px;
            box-shadow: 0 1px 4px var(--shadow);
            overflow: hidden;
            border: 1px solid var(--border-card);
            margin-top: 1rem;
        }
        .scrobble-item {
            display: flex;
            align-items: center;
            gap: 1rem;
            padding: 1rem;
            border-bottom: 1px solid var(--border-card);
            transition: background 0.15s;
        }
        .scrobble-item:hover {
            background: var(--hover-row);
        }
        .album-art {
            flex-shrink: 0;
            width: 56px;
            height: 56px;
            border-radius: 8px;
            object-fit: cover;
            background: var(--art-bg);
            box-shadow: 0 1px 2px var(--shadow);
        }
        .track-info {
            flex: 1;
            min-width: 0;
        }
        .track-name {
            font-weight: 600;
            font-size: 1rem;
            color: var(--text-primary);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .artist-name {
            font-size: 0.85rem;
            color: var(--text-secondary);
            margin-top: 2px;
        }
        .album-name {
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-top: 2px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .scrobble-date {
            flex-shrink: 0;
            font-size: 0.75rem;
            color: var(--text-muted);
            text-align: right;
            min-width: 120px;
        }
        .pagination {
            display: flex;
            justify-content: center;
            gap: 1rem;
            margin-top: 2rem;
            align-items: center;
        }
        .pagination button {
            background: var(--button-bg);
            border: 1px solid var(--button-border);
            padding: 0.5rem 1rem;
            border-radius: 30px;
            cursor: pointer;
            font-size: 0.8rem;
            transition: all 0.2s;
            color: var(--text-primary);
        }
        .pagination button:hover:not(:disabled) {
            background: var(--button-hover);
            border-color: var(--text-muted);
        }
        .pagination button:disabled {
            opacity: 0.4;
            cursor: default;
        }
        .pagination span {
            font-size: 0.85rem;
            color: var(--text-secondary);
        }
        .empty-message {
            padding: 3rem;
            text-align: center;
            color: var(--text-muted);
            font-size: 0.9rem;
        }
        footer {
            margin-top: 3rem;
            text-align: center;
            font-size: 0.7rem;
            color: var(--text-muted);
        }
        @media (max-width: 700px) {
            .scrobble-item { flex-wrap: wrap; }
            .scrobble-date { margin-left: 64px; text-align: left; width: 100%; }
            .now-playing { flex-direction: column; text-align: center; }
            .time-stats { flex-direction: column; align-items: center; }
        }
        canvas {
            max-height: 250px;
            width: auto;
            margin: 0 auto;
            display: block;
        }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1>📀 Scrobble Overview</h1>
            <div class="sub">your listening history, top charts & now playing</div>
        </div>
        <div style="display: flex; gap: 10px;">
            <button class="theme-toggle" id="themeToggleBtn" onclick="toggleTheme()">🌓 Dark/Light</button>
            <a href="/" class="player-link">◀ Now Playing (full)</a>
        </div>
    </div>

    <!-- Now Playing section -->
    <div class="now-playing" id="nowPlaying">
        <img id="nowArt" class="now-art" src="" alt="album art">
        <div class="now-info">
            <div class="now-label">🎧 SCROBBLING NOW</div>
            <div class="now-track" id="nowTrack">-</div>
            <div class="now-artist" id="nowArtist">-</div>
            <div class="now-album" id="nowAlbum">-</div>
        </div>
    </div>

    <!-- Stats grid: Top Artists, Albums, Tracks -->
    <div class="stats-grid" id="statsGrid">
        <div class="stat-card"><h3>Top Artists</h3><ul class="stat-list" id="topArtistsList"><li>Loading...</li></ul></div>
        <div class="stat-card"><h3>Top Albums</h3><ul class="stat-list" id="topAlbumsList"><li>Loading...</li></ul></div>
        <div class="stat-card"><h3>Top Tracks</h3><ul class="stat-list" id="topTracksList"><li>Loading...</li></ul></div>
    </div>

    <!-- Listening time card -->
    <div class="time-card">
        <div class="time-stats" id="listeningTime">
            <div class="time-item"><div class="time-label">Today</div><div class="time-value" id="timeToday">-</div></div>
            <div class="time-item"><div class="time-label">This Week</div><div class="time-value" id="timeWeek">-</div></div>
            <div class="time-item"><div class="time-label">This Month</div><div class="time-value" id="timeMonth">-</div></div>
            <div class="time-item"><div class="time-label">This Year</div><div class="time-value" id="timeYear">-</div></div>
        </div>
        <div class="total-scrobbles" id="totalScrobbles"></div>
    </div>

    <!-- Listening Clock Pie Chart -->
    <div class="stat-card">
        <h3>🕒 Listening Clock (Busiest Hours)</h3>
        <canvas id="clockPieChart" width="300" height="300"></canvas>
        <div id="busiestHourInfo" style="text-align: center; margin-top: 10px; font-weight: bold;"></div>
    </div>

    <!-- Tools: only export, import, refresh -->
    <div class="tools">
        <button onclick="exportData()">⬇️ Export JSON</button>
        <label style="display:inline-flex; align-items:center; gap:6px;">📂 Import JSON
            <input type="file" id="importFile" style="display:none" accept=".json" onchange="importData(this.files[0])">
        </label>
        <button onclick="location.reload()">🔄 Refresh</button>
    </div>

    <h3 style="margin: 1rem 0 0.5rem 0;">Recent Scrobbles</h3>
    <div id="scrobbleList" class="scrobble-list">
        <div class="empty-message">Loading your scrobbles...</div>
    </div>

    <div class="pagination" id="pagination"></div>
    <footer>scrobbles stored in scrobbles.db · auto‑scrobbled after 50% or 4 minutes</footer>
</div>

<script>
    // Theme handling
    function setTheme(theme) {
        if (theme === 'dark') {
            document.body.classList.add('dark');
        } else {
            document.body.classList.remove('dark');
        }
        localStorage.setItem('scrobbleTheme', theme);
    }
    function toggleTheme() {
        const isDark = document.body.classList.contains('dark');
        setTheme(isDark ? 'light' : 'dark');
    }
    const savedTheme = localStorage.getItem('scrobbleTheme');
    if (savedTheme === 'dark') document.body.classList.add('dark');

    // Now playing fetch
    function fetchNowPlaying() {
        fetch('/api/now')
            .then(r => r.json())
            .then(data => {
                document.getElementById('nowTrack').innerText = data.track || 'Nothing playing';
                document.getElementById('nowArtist').innerText = data.artist || '-';
                document.getElementById('nowAlbum').innerText = data.album || '-';
                const artEl = document.getElementById('nowArt');
                if (data.art && data.art !== '') {
                    artEl.src = data.art;
                } else {
                    artEl.src = 'https://via.placeholder.com/80?text=🎵';
                }
            })
            .catch(e => console.error('Now playing error:', e));
    }

    // Listening time fetch
    function fetchListeningTime() {
        fetch('/api/listening_time')
            .then(r => r.json())
            .then(data => {
                document.getElementById('timeToday').innerText = data.today + 'h';
                document.getElementById('timeWeek').innerText = data.week + 'h';
                document.getElementById('timeMonth').innerText = data.month + 'h';
                document.getElementById('timeYear').innerText = data.year + 'h';
            })
            .catch(e => console.error('Listening time error:', e));
    }

    // Stats fetch (top 25)
    function fetchStats() {
        fetch('/api/stats')
            .then(r => r.json())
            .then(data => {
                const total = data.total_scrobbles || 0;
                document.getElementById('totalScrobbles').innerText = `Total scrobbles: ${total}`;

                const artistsList = document.getElementById('topArtistsList');
                if (data.top_artists.length === 0) {
                    artistsList.innerHTML = '<li>No scrobbles yet</li>';
                } else {
                    artistsList.innerHTML = data.top_artists.map(a => `<li><span>${escapeHtml(a.artist)}</span><span class="stat-count">${a.playcount}</span></li>`).join('');
                }

                const albumsList = document.getElementById('topAlbumsList');
                if (data.top_albums.length === 0) {
                    albumsList.innerHTML = '<li>No albums yet</li>';
                } else {
                    albumsList.innerHTML = data.top_albums.map(a => `<li><span>${escapeHtml(a.artist)} – ${escapeHtml(a.album)}</span><span class="stat-count">${a.playcount}</span></li>`).join('');
                }

                const tracksList = document.getElementById('topTracksList');
                if (data.top_tracks.length === 0) {
                    tracksList.innerHTML = '<li>No tracks yet</li>';
                } else {
                    tracksList.innerHTML = data.top_tracks.map(t => `<li><span>${escapeHtml(t.artist)} – ${escapeHtml(t.track)}</span><span class="stat-count">${t.playcount}</span></li>`).join('');
                }
            })
            .catch(e => console.error('Stats error:', e));
    }

    // Listening Clock Pie Chart
    function fetchListeningClock() {
        fetch('/api/listening_clock')
            .then(r => r.json())
            .then(data => {
                const hour_counts = data.hour_counts;
                const busiest_hour = data.busiest_hour;
                const busiest_count = data.busiest_hour_count;
                
                // Prepare data for Chart.js (only hours with scrobbles)
                const labels = [];
                const counts = [];
                for (let i = 0; i < hour_counts.length; i++) {
                    if (hour_counts[i] > 0) {
                        labels.push(`${i}:00`);
                        counts.push(hour_counts[i]);
                    }
                }
                
                // Highlight the busiest hour slice
                const backgroundColors = labels.map((label, idx) => {
                    const hour = parseInt(label.split(':')[0]);
                    return hour === busiest_hour ? '#ff6384' : '#36a2eb';
                });
                
                const ctx = document.getElementById('clockPieChart').getContext('2d');
                new Chart(ctx, {
                    type: 'pie',
                    data: {
                        labels: labels,
                        datasets: [{
                            data: counts,
                            backgroundColor: backgroundColors,
                            borderWidth: 1
                        }]
                    },
                    options: {
                        responsive: true,
                        plugins: {
                            legend: { position: 'right' },
                            tooltip: { callbacks: { label: (tooltipItem) => `${tooltipItem.label}: ${tooltipItem.raw} scrobbles` } }
                        }
                    }
                });
                
                document.getElementById('busiestHourInfo').innerHTML = `🚀 Busiest hour: <strong>${busiest_hour}:00</strong> with <strong>${busiest_count}</strong> scrobbles`;
            })
            .catch(e => console.error('Listening clock error:', e));
    }

    // Recent scrobbles
    let currentOffset = 0;
    const limit = 25;

    function loadScrobbles(offset) {
        const container = document.getElementById('scrobbleList');
        container.innerHTML = '<div class="empty-message">Loading...</div>';
        fetch(`/api/scrobbles?limit=${limit}&offset=${offset}`)
            .then(r => r.json())
            .then(data => {
                renderScrobbles(data.scrobbles);
                renderPagination(data.total, offset);
            })
            .catch(e => {
                console.error(e);
                container.innerHTML = '<div class="empty-message">Error loading scrobbles.</div>';
            });
    }

    function formatRelativeTime(timestamp) {
        const seconds = Math.floor((Date.now() / 1000) - timestamp);
        if (seconds < 60) return 'just now';
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
        const hours = Math.floor(minutes / 60);
        if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
        const days = Math.floor(hours / 24);
        if (days < 7) return `${days} day${days === 1 ? '' : 's'} ago`;
        return new Date(timestamp * 1000).toLocaleDateString();
    }

    function escapeHtml(str) {
        if (!str) return '';
        return str.replace(/[&<>]/g, function(m) {
            if (m === '&') return '&amp;';
            if (m === '<') return '&lt;';
            if (m === '>') return '&gt;';
            return m;
        });
    }

    function renderScrobbles(scrobbles) {
        const container = document.getElementById('scrobbleList');
        if (!scrobbles.length) {
            container.innerHTML = '<div class="empty-message">✨ No scrobbles yet. Start listening!</div>';
            return;
        }
        let html = '';
        for (let s of scrobbles) {
            const artUrl = s.art_url || 'https://via.placeholder.com/56?text=🎵';
            const dateStr = formatRelativeTime(s.timestamp);
            html += `
                <div class="scrobble-item">
                    <img class="album-art" src="${escapeHtml(artUrl)}" onerror="this.src='https://via.placeholder.com/56?text=🎵'">
                    <div class="track-info">
                        <div class="track-name">${escapeHtml(s.track)}</div>
                        <div class="artist-name">${escapeHtml(s.artist)}</div>
                        <div class="album-name">${escapeHtml(s.album || '')}</div>
                    </div>
                    <div class="scrobble-date">${dateStr}</div>
                </div>
            `;
        }
        container.innerHTML = html;
    }

    function renderPagination(total, offset) {
        const pagDiv = document.getElementById('pagination');
        if (total <= limit) {
            pagDiv.innerHTML = '';
            return;
        }
        const currentPage = Math.floor(offset / limit) + 1;
        const totalPages = Math.ceil(total / limit);
        let html = `<button onclick="changePage(-1)" ${offset === 0 ? 'disabled' : ''}>◀ Previous</button>`;
        html += `<span>Page ${currentPage} of ${totalPages}</span>`;
        html += `<button onclick="changePage(1)" ${offset + limit >= total ? 'disabled' : ''}>Next ▶</button>`;
        pagDiv.innerHTML = html;
    }

    function changePage(delta) {
        let newOffset = currentOffset + delta * limit;
        if (newOffset < 0) newOffset = 0;
        currentOffset = newOffset;
        loadScrobbles(currentOffset);
    }

    function exportData() { window.location.href = '/export'; }

    function importData(file) {
        if (!file) return;
        const formData = new FormData();
        formData.append('file', file);
        fetch('/import', { method: 'POST', body: formData })
            .then(r => r.json())
            .then(data => {
                alert(data.status || data.error);
                loadScrobbles(currentOffset);
                fetchStats();
                fetchListeningTime();
                fetchListeningClock();  // refresh clock after import
            })
            .catch(e => alert('Import failed: ' + e));
    }

    // Initial loads
    fetchNowPlaying();
    fetchStats();
    fetchListeningTime();
    fetchListeningClock();
    loadScrobbles(0);
    setInterval(fetchNowPlaying, 5000);
</script>
</body>
</html>
"""

# ------------------------- MAIN -------------------------
if __name__ == '__main__':
    init_db()
    poller_thread = threading.Thread(target=background_poller, daemon=True)
    poller_thread.start()
    print("✅ TIDAL HIFI PLAYER with Listening Clock (pie chart + busiest hour)")
    print("📀 Scrobbles saved to scrobbles.db")
    print("🌐 Player: http://127.0.0.1:5000")
    print("📊 Overview (including listening clock): http://127.0.0.1:5000/scrobbles")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)

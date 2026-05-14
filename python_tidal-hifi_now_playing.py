import eventlet
eventlet.monkey_patch()

import time
import requests
from flask import Flask, render_template_string, request, jsonify
from flask_socketio import SocketIO

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "http://127.0.0.1:47836"
CURRENT_URL = f"{BASE_URL}/current"
POLL_INTERVAL = 1

# Album quality cache
album_cache = {}

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
        .progress-container { width:100%; height:6px; background:rgba(255,255,255,0.2); border-radius:10px; overflow:hidden; cursor:pointer; min-width: 300px; }
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

function toggleShuffle() { fetch('/toggle_shuffle', { method: 'POST' }).then(() => setTimeout(() => location.reload(), 100)); }
function toggleRepeat() { fetch('/toggle_repeat', { method: 'POST' }).then(() => setTimeout(() => location.reload(), 100)); }

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

def get_current_track():
    try:
        resp = requests.get(CURRENT_URL, timeout=2)
        resp.raise_for_status()
        data = resp.json()
        
        current_sec = data.get("currentInSeconds", 0)
        duration_sec = data.get("durationInSeconds", 1)
        progress = (current_sec / duration_sec * 100) if duration_sec else 0
        
        audio_quality = data.get("audioQuality", {})
        quality_raw = audio_quality.get("quality", "")
        badge_text = audio_quality.get("badgeText", "")
        
        # Cache logic
        album_name = data.get("album", "")
        artist_name = data.get("artist", "")
        cache_key = f"{artist_name}|{album_name}".lower()
        
        cached_quality = album_cache.get(cache_key)
        if cached_quality and quality_raw.lower() in ['max', 'high']:
            audio_quality = cached_quality
            quality_raw = audio_quality.get("quality", "")
            badge_text = audio_quality.get("badgeText", "")
        
        # Cache detailed quality when found
        has_detailed = badge_text and ('kHz' in badge_text or 'kbps' in badge_text)
        if has_detailed and album_name:
            album_cache[cache_key] = audio_quality
        
        # Map quality for color coding
        quality_lower = quality_raw.lower()
        if quality_lower in ['hi_res_lossless', 'max']:
            quality = 'hi_res_lossless'
        elif quality_lower in ['lossless', 'high']:
            quality = 'lossless'
        else:
            quality = 'low'
        
        player = data.get("player", {})
        
        return {
            "track": data.get("title"),
            "artist": artist_name,
            "album": album_name,
            "art": data.get("image"),
            "current": data.get("current"),
            "duration": data.get("duration"),
            "duration_sec": duration_sec,
            "progress": round(progress, 1),
            "volume": round(data.get("volume", 0) * 100),
            "shuffle": "on" if player.get("shuffle") else "off",
            "repeat": player.get("repeat", "OFF"),
            "playing_from": f"Playing from: {data.get('playingFrom', 'Unknown')}",
            "quality_raw": quality_raw,
            "quality": quality,
            "bitDepth": audio_quality.get("bitDepth", 0),
            "sampleRate": audio_quality.get("sampleRate", 0),
            "codec": audio_quality.get("codec", ""),
            "badgeText": badge_text
        }
    except Exception as e:
        print(f"ERROR: {e}")
        return {}

def background_task():
    last_data = {}
    while True:
        track_data = get_current_track()
        if track_data:
            if (track_data.get('track') != last_data.get('track') or
                track_data.get('badgeText') != last_data.get('badgeText') or
                abs(track_data.get('progress', 0) - last_data.get('progress', 0)) >= 0.5):
                socketio.emit('update', track_data)
                last_data = track_data.copy()
        socketio.sleep(1)

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

#@app.route('/cache/clear', methods=['POST'])
@app.route('/cache/clear', methods=['GET', 'POST'])
def clear_cache():
    album_cache.clear()
    print("Cache cleared by user")
    return ('', 204)

@app.route('/cache/view')
def view_cache():
    if not album_cache:
        return "<h3>Cache is empty</h3><p>Play some tracks to populate the cache.</p><p><a href='/'>Back to player</a></p>"
    html = "<h3>Cached Albums</h3><ul>"
    for key, val in album_cache.items():
        html += f"<li><strong>{key}</strong><br>&nbsp;&nbsp;{val.get('badgeText', 'N/A')} - {val.get('quality', 'N/A')}</li>"
    html += f"</ul><p><strong>Total cached: {len(album_cache)} albums</strong></p><p><a href='/'>Back to player</a></p>"
    return html

if __name__ == '__main__':
    print("=" * 50)
    print("TIDAL HIFI PLAYER")
    print("=" * 50)
    print(f"Cache will store detailed quality info per album")
    print(f"View cache at: http://127.0.0.1:5000/cache/view")
    print("=" * 50)
    socketio.start_background_task(background_task)
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)

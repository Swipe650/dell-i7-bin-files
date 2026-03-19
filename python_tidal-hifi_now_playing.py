import eventlet
eventlet.monkey_patch()  # Must be the very first thing

import time
import requests
from flask import Flask, render_template_string, request, jsonify
from flask_socketio import SocketIO

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "http://127.0.0.1:47836"
CURRENT_URL = f"{BASE_URL}/current"
POLL_INTERVAL = 1

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title id="page-title">Now Playing</title>
    <link id="favicon" rel="icon" type="image/png" href="">
    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
    <style>
        body { margin:0; font-family:-apple-system; color:white; overflow:hidden; }

        .bg {
            position:fixed;
            width:100%;
            height:100%;
            background-size:cover;
            filter:blur(10px) brightness(0.35);
            z-index:-1;
            transition: background-image 0.5s ease;
        }

        .overlay {
            display:flex;
            height:100vh;
            align-items:center;
            justify-content:center;
        }

        .card {
            display:flex;
            gap:48px;  /* Reduced from 60px (20% decrease) */
            background:rgba(0,0,0,0.4);
            padding:40px;  /* Reduced from 50px (20% decrease) */
            border-radius:22px;  /* Reduced from 28px (~20% decrease) */
            backdrop-filter:blur(20px);
        }

        .art {
            width:320px;  /* Reduced from 400px (20% decrease) */
            border-radius:16px;  /* Reduced from 20px (20% decrease) */
            box-shadow: 0 8px 32px rgba(0,0,0,0.6);  /* Adjusted shadow proportionally */
        }

        .info {
            display:flex;
            flex-direction:column;
            justify-content:center;
        }

        .track {
            font-size:2.56em;  /* Reduced from 3.2em (20% decrease) */
            font-weight:600;
            margin-bottom:8px;  /* Reduced from 10px (20% decrease) */
        }

        .artist {
            color:#ccc;
            font-size:1.44em;  /* Reduced from 1.8em (20% decrease) */
            margin-bottom:4px;  /* Reduced from 5px (20% decrease) */
        }

        .album {
            color:#999;
            margin-bottom:20px;  /* Reduced from 25px (20% decrease) */
            font-size:1.04em;  /* Reduced from 1.3em (20% decrease) */
        }

        .progress-container {
            width:480px;  /* Reduced from 600px (20% decrease) */
            height:8px;  /* Reduced from 10px (20% decrease) */
            background:rgba(255,255,255,0.2);
            border-radius:8px;  /* Reduced from 10px (20% decrease) */
            overflow:hidden;
            cursor:pointer;
            margin-bottom:6px;  /* Reduced from 8px (25% decrease) */
        }

        .progress {
            height:100%;
            background:#1db954;
            width:0%;
            transition:width 0.2s linear;
        }

        .time {
            display:flex;
            justify-content:space-between;
            font-size:0.8em;  /* Reduced from 1em (20% decrease) */
            color:#aaa;
        }

        .controls {
            margin-top:20px;  /* Reduced from 25px (20% decrease) */
            display:flex;
            gap:20px;  /* Reduced from 25px (20% decrease) */
        }

        .btn {
            background:rgba(255,255,255,0.1);
            border:none;
            color:white;
            padding:10px 14px;  /* Reduced from 12px 18px (~20% decrease) */
            border-radius:10px;  /* Reduced from 12px (~17% decrease) */
            cursor:pointer;
            font-size:0.96em;  /* Reduced from 1.2em (20% decrease) */
        }

        .btn:hover {
            background:rgba(255,255,255,0.25);
        }

        .meta {
            margin-top:12px;  /* Reduced from 15px (20% decrease) */
            font-size:0.8em;  /* Reduced from 1em (20% decrease) */
            color:#bbb;
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
                <div class="progress-container" id="progress-container">
                    <div id="progress" class="progress"></div>
                </div>
                <div class="time">
                    <span id="current"></span>
                    <span id="duration"></span>
                </div>
                <div class="controls">
                    <button class="btn" onclick="control('previous')">⏮</button>
                    <button class="btn" onclick="control('playpause')">⏯</button>
                    <button class="btn" onclick="control('next')">⏭</button>
                </div>
                <div id="meta" class="meta"></div>
            </div>
        </div>
    </div>
<script>
const socket = io();
let trackDurationSec = 0;

socket.on('update', (data) => {
    document.getElementById('track').innerText = data.track;
    document.getElementById('artist').innerText = data.artist;
    document.getElementById('album').innerText = data.album;
    document.getElementById('current').innerText = data.current;
    document.getElementById('duration').innerText = data.duration;
    document.getElementById('progress').style.width = data.progress + '%';
    document.getElementById('meta').innerText = `Volume: ${data.volume}% | Shuffle: ${data.shuffle} | Repeat: ${data.repeat}`;
    document.getElementById('art').src = data.art;
    document.getElementById('bg').style.backgroundImage = `url('${data.art}')`;
    document.getElementById('page-title').innerText = `${data.artist} - ${data.track}`;
    document.getElementById('favicon').href = data.art;

    trackDurationSec = data.duration_sec || trackDurationSec;
});

function control(action) {
    fetch(`/control/${action}`, { method: 'POST' });
}

const progressContainer = document.getElementById('progress-container');
progressContainer.addEventListener('click', (e) => {
    const rect = progressContainer.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const width = rect.width;
    const percent = clickX / width;
    const seekSeconds = Math.floor(percent * trackDurationSec);

    fetch(`/seek/${seekSeconds}`, { method: 'PUT' });
});
</script>
</body>
</html>
"""

def get_current_track():
    try:
        data = requests.get(CURRENT_URL).json()
        current_sec = data.get("currentInSeconds", 0)
        duration_sec = data.get("durationInSeconds", 1)
        progress = (current_sec / duration_sec) * 100 if duration_sec else 0
        return {
            "track": data.get("title"),
            "artist": data.get("artist"),
            "album": data.get("album"),
            "art": data.get("image"),
            "current": data.get("current"),
            "duration": data.get("duration"),
            "duration_sec": duration_sec,
            "progress": progress,
            "volume": round(data.get("volume", 0) * 100),
            "shuffle": data.get("player", {}).get("shuffle"),
            "repeat": data.get("player", {}).get("repeat")
        }
    except Exception as e:
        print("ERROR:", e)
        return {}

def background_task():
    while True:
        socketio.emit('update', get_current_track())
        socketio.sleep(POLL_INTERVAL)

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/control/<action>', methods=['POST'])
def control(action):
    try:
        url = None
        if action == 'playpause': url = f"{BASE_URL}/player/playpause"
        elif action == 'next': url = f"{BASE_URL}/player/next"
        elif action == 'previous': url = f"{BASE_URL}/player/previous"
        if url:
            try:
                requests.post(url, timeout=2)
            except Exception:
                try:
                    requests.get(url, timeout=2)
                except Exception as e:
                    print("CONTROL ERROR:", e)
    except Exception as e:
        print("CONTROL ROUTE ERROR:", e)
    return ('', 204)

@app.route('/seek/<int:seconds>', methods=['PUT'])
def seek(seconds):
    try:
        url = f"{BASE_URL}/player/seek/absolute?seconds={seconds}"
        try:
            requests.put(url, timeout=2)
        except Exception:
            try:
                requests.get(url, timeout=2)
            except Exception as e:
                print("SEEK ERROR:", e)
    except Exception as e:
        print("SEEK ROUTE ERROR:", e)
    return ('', 204)

@socketio.on('connect')
def connect():
    print("Client connected")

if __name__ == '__main__':
    socketio.start_background_task(background_task)
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)

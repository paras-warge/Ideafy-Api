import os
import re
import hashlib
import tempfile
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
from cachetools import TTLCache

app = Flask(__name__)
CORS(app)

cache = TTLCache(maxsize=200, ttl=300)

PLATFORM_PATTERNS = {
    "youtube": [r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)"],
    "facebook": [r"(?:facebook\.com/|fb\.watch/)", r"(?:m\.facebook\.com/|web\.facebook\.com/)"],
    "instagram": [r"(?:instagram\.com/(?:p/|reel/|tv/))"],
    "twitter": [r"(?:twitter\.com/|x\.com/)(?:\w+)/status/"],
}

SUPPORTED_PLATFORMS = ["youtube", "facebook", "instagram", "twitter"]
COOKIES_PATH = os.path.join(os.path.dirname(__file__), "cookies.txt")
PROXY_URL = os.environ.get("PROXY_URL", None)


# 🔥 NEW: Clean URL function
def clean_url(url):
    if "youtu.be" in url:
        video_id = url.split("/")[-1].split("?")[0]
        return f"https://www.youtube.com/watch?v={video_id}"
    return url.split("&")[0]


def get_cookies_path():
    if os.path.exists(COOKIES_PATH):
        return COOKIES_PATH

    cookies_content = os.environ.get("YOUTUBE_COOKIES", None)
    if cookies_content:
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        tmp.write(cookies_content)
        tmp.close()
        return tmp.name

    return None


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    for platform, patterns in PLATFORM_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, url_lower):
                return platform
    return "unknown"


def format_duration(seconds):
    if not seconds:
        return None
    seconds = int(seconds)
    return f"{seconds//60}:{seconds%60:02d}"


def estimate_file_size(tbr, duration):
    if tbr and duration:
        return int((tbr * 1000 / 8) * duration)
    return None


def extract_video_info(url: str):
    cache_key = hashlib.md5(url.encode()).hexdigest()
    if cache_key in cache:
        return cache[cache_key]

    cookies_path = get_cookies_path()

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,

        # 🔥 FIXED format (stable + 1080)
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "extractor_retries": 5,
        "retries": 5,

        # 🔥 IMPORTANT for YouTube
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },

        "http_headers": {
            "User-Agent": "com.google.android.youtube/17.31.35 (Linux; U; Android 11)",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if cookies_path:
        ydl_opts["cookiefile"] = cookies_path

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return process_info(info, url)


def process_info(info, original_url):
    formats_raw = info.get("formats") or []
    formats = []
    seen = set()

    for fmt in formats_raw:
        if not fmt.get("url"):
            continue

        height = fmt.get("height")
        if not height:
            continue

        fps = fmt.get("fps") or 30
        dynamic_range = fmt.get("dynamic_range") or "SDR"

        quality = f"{height}p"
        if fps >= 50:
            quality += " 60fps"
        if dynamic_range == "HDR":
            quality += " HDR"

        if quality in seen:
            continue
        seen.add(quality)

        formats.append({
            "quality": quality,
            "ext": fmt.get("ext"),
            "url": fmt.get("url"),
            "has_audio": fmt.get("acodec") not in [None, "none"],
        })

    return {
        "success": True,
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": format_duration(info.get("duration")),
        "formats": formats,
    }


@app.route("/api/video/info", methods=["POST"])
def get_video_info():
    data = request.get_json()

    url = clean_url((data.get("url") or "").strip())

    if not url:
        return jsonify({"success": False, "error": "URL required"}), 400

    try:
        return jsonify(extract_video_info(url))

    except yt_dlp.utils.DownloadError as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 422

    except Exception:
        return jsonify({
            "success": False,
            "error": "Server error"
        }), 500


@app.route("/api/healthz", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "cookies_loaded": get_cookies_path() is not None
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
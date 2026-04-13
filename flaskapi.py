import os
import re
import hashlib
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
from cachetools import TTLCache

app = Flask(__name__)
CORS(app)

cache = TTLCache(maxsize=200, ttl=300)

PLATFORM_PATTERNS = {
    "youtube": [
        r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)",
    ],
    "facebook": [
        r"(?:facebook\.com/|fb\.watch/)",
        r"(?:m\.facebook\.com/|web\.facebook\.com/)",
    ],
    "instagram": [
        r"(?:instagram\.com/(?:p/|reel/|tv/))",
    ],
    "twitter": [
        r"(?:twitter\.com/|x\.com/)(?:\w+)/status/",
    ],
}

SUPPORTED_PLATFORMS = ["youtube", "facebook", "instagram", "twitter"]

COOKIES_PATH = os.path.join(os.path.dirname(__file__), "cookies.txt")
PROXY_URL = os.environ.get("PROXY_URL", None)


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    for platform, patterns in PLATFORM_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, url_lower):
                return platform
    return "unknown"


def format_duration(seconds) -> str:
    if not seconds:
        return None
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def estimate_file_size(tbr, duration):
    if tbr and duration:
        return int((tbr * 1000 / 8) * duration)
    return None


def get_quality_label(height) -> str:
    if not height:
        return "Unknown"
    if height >= 2160:
        return "4K"
    elif height >= 1440:
        return "1440p"
    elif height >= 1080:
        return "1080p"
    elif height >= 720:
        return "720p"
    elif height >= 480:
        return "480p"
    elif height >= 360:
        return "360p"
    elif height >= 240:
        return "240p"
    else:
        return f"{height}p"


def extract_video_info(url: str):
    cache_key = hashlib.md5(url.encode()).hexdigest()
    if cache_key in cache:
        return cache[cache_key]

    platform = detect_platform(url)
    use_cookies = platform == "youtube" and os.path.exists(COOKIES_PATH)

    # 🔥 FIXED YDL OPTIONS
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,

        # ✅ Better format (audio included)
        "format": "best[ext=mp4]/best",

        "socket_timeout": 30,
        "extractor_retries": 5,

        # 🔥 Fix YouTube blocking
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,

        # 🔥 CRITICAL FIX
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
                "skip": ["dash", "hls"]
            }
        },

        # 🔥 Strong headers (mobile simulation)
        "http_headers": {
            "User-Agent": "com.google.android.youtube/17.31.35 (Linux; U; Android 11)",
            "Accept-Language": "en-US,en;q=0.9",
        },

        "retries": 5,
        "fragment_retries": 5,
    }

    if use_cookies:
        ydl_opts["cookiefile"] = COOKIES_PATH

    if PROXY_URL:
        ydl_opts["proxy"] = PROXY_URL

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        result = process_info(info, url)
        cache[cache_key] = result
        return result


def process_info(info: dict, original_url: str) -> dict:
    platform = detect_platform(original_url)

    title = info.get("title") or "Unknown Title"
    thumbnail = info.get("thumbnail") or ""
    duration = info.get("duration")
    uploader = info.get("uploader") or info.get("channel") or ""
    view_count = info.get("view_count")
    description = (info.get("description") or "")[:300]

    formats_raw = info.get("formats") or []
    formats = []
    seen_keys = set()
    audio_added = False

    # 🔥 FIX: only formats WITH AUDIO
    video_formats = [
        f for f in formats_raw
        if f.get("vcodec") not in [None, "none"]
        and f.get("acodec") not in [None, "none"]
        and f.get("url")
        and f.get("height")
    ]

    audio_formats = [
        f for f in formats_raw
        if f.get("vcodec") in [None, "none"]
        and f.get("acodec") not in [None, "none"]
        and f.get("url")
    ]

    video_formats.sort(key=lambda f: (f.get("height", 0), f.get("fps", 0) or 0), reverse=True)
    audio_formats.sort(key=lambda f: f.get("tbr", 0) or 0, reverse=True)

    for fmt in video_formats:
        height = fmt.get("height")
        if not height:
            continue

        fps = fmt.get("fps") or 0
        ext = fmt.get("ext") or "mp4"
        quality_label = get_quality_label(height)
        is_60fps = fps >= 50

        key = f"{quality_label}_{'60fps' if is_60fps else '30fps'}"
        if key in seen_keys:
            continue
        seen_keys.add(key)

        download_url = fmt.get("url") or fmt.get("manifest_url")
        if not download_url:
            continue

        tbr = fmt.get("tbr") or fmt.get("vbr")
        file_size = estimate_file_size(tbr, info.get("duration"))
        if fmt.get("filesize"):
            file_size = fmt.get("filesize")
        elif fmt.get("filesize_approx"):
            file_size = fmt.get("filesize_approx")

        recommended = quality_label == "720p" and not is_60fps

        formats.append({
            "format_id": fmt.get("format_id"),
            "quality": quality_label,
            "ext": ext if ext != "none" else "mp4",
            "resolution": f"{fmt.get('width', '?')}x{height}",
            "fps": fps,
            "vcodec": fmt.get("vcodec"),
            "acodec": fmt.get("acodec"),
            "file_size": file_size,
            "url": download_url,
            "type": "video",
            "recommended": recommended,
            "has_audio": True,
        })

    if audio_formats and not audio_added:
        best_audio = audio_formats[0]
        formats.append({
            "format_id": best_audio.get("format_id"),
            "quality": "Audio Only",
            "ext": "mp3",
            "url": best_audio.get("url"),
            "type": "audio",
            "recommended": False,
            "has_audio": True,
        })

    if not formats:
        best_url = info.get("url")
        if best_url:
            formats.append({
                "format_id": "best",
                "quality": "Best",
                "url": best_url,
                "type": "video",
                "recommended": True,
                "has_audio": True,
            })

    return {
        "success": True,
        "platform": platform,
        "title": title,
        "thumbnail": thumbnail,
        "duration": format_duration(duration),
        "duration_seconds": duration,
        "uploader": uploader,
        "view_count": view_count,
        "description": description,
        "formats": formats,
        "format_count": len(formats),
    }


@app.route("/api/video/info", methods=["POST"])
def get_video_info():
    data = request.get_json()
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"success": False, "error": "URL is required"}), 400

    try:
        return jsonify(extract_video_info(url))

    except yt_dlp.utils.DownloadError:
        return jsonify({
            "success": False,
            "error": "Video is restricted/private or blocked. Try another public video."
        }), 403

    except Exception:
        return jsonify({
            "success": False,
            "error": "Server error"
        }), 500


@app.route("/api/healthz", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "video-downloader-api",
        "supported_platforms": SUPPORTED_PLATFORMS,
        "proxy_enabled": PROXY_URL is not None,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
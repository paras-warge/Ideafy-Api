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
    elif height >= 144:
        return "144p"
    else:
        return f"{height}p"

def extract_video_info(url: str):
    cache_key = hashlib.md5(url.encode()).hexdigest()
    if cache_key in cache:
        return cache[cache_key]

    platform = detect_platform(url)
    cookies_path = get_cookies_path()
    use_cookies = platform == "youtube" and cookies_path is not None

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,

        # Get ALL formats so we can pick qualities
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",

        "socket_timeout": 30,
        "extractor_retries": 5,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,

        # Use both android and web clients to get more formats
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },

        "http_headers": {
            "User-Agent": "com.google.android.youtube/17.31.35 (Linux; U; Android 11)",
            "Accept-Language": "en-US,en;q=0.9",
        },

        "retries": 5,
        "fragment_retries": 5,
    }

    if use_cookies:
        ydl_opts["cookiefile"] = cookies_path

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

    # Video formats with audio (merged formats like 360p, 480p, 720p)
    merged_formats = [
        f for f in formats_raw
        if f.get("vcodec") not in [None, "none"]
        and f.get("acodec") not in [None, "none"]
        and f.get("url")
        and f.get("height")
    ]

    # Video only formats (high quality like 1080p, 1440p, 4K)
    video_only_formats = [
        f for f in formats_raw
        if f.get("vcodec") not in [None, "none"]
        and f.get("acodec") in [None, "none"]
        and f.get("url")
        and f.get("height")
    ]

    # Audio only formats
    audio_formats = [
        f for f in formats_raw
        if f.get("vcodec") in [None, "none"]
        and f.get("acodec") not in [None, "none"]
        and f.get("url")
    ]

    # Combine merged + video_only, sort by height + fps
    all_video_formats = merged_formats + video_only_formats
    all_video_formats.sort(
        key=lambda f: (f.get("height", 0), f.get("fps", 0) or 0),
        reverse=True
    )
    audio_formats.sort(key=lambda f: f.get("tbr", 0) or 0, reverse=True)

    for fmt in all_video_formats:
        height = fmt.get("height")
        if not height:
            continue

        fps = fmt.get("fps") or 0
        ext = fmt.get("ext") or "mp4"
        quality_label = get_quality_label(height)
        is_60fps = fps >= 50

        # Check HDR
        dynamic_range = fmt.get("dynamic_range") or ""
        is_hdr = "hdr" in dynamic_range.lower() if dynamic_range else False

        # Deduplicate by quality + fps + hdr combo
        key = f"{quality_label}_{'60fps' if is_60fps else '30fps'}_{'hdr' if is_hdr else 'sdr'}"
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

        recommended = quality_label == "720p" and not is_60fps and not is_hdr

        # Build quality label with fps and hdr tags
        display_quality = quality_label
        if is_hdr:
            display_quality = f"{quality_label} HDR"

        formats.append({
            "format_id": fmt.get("format_id"),
            "quality": display_quality,
            "ext": ext if ext != "none" else "mp4",
            "resolution": f"{fmt.get('width', '?')}x{height}",
            "fps": fps,
            "is_60fps": is_60fps,
            "is_hdr": is_hdr,
            "vcodec": fmt.get("vcodec"),
            "acodec": fmt.get("acodec"),
            "file_size": file_size,
            "url": download_url,
            "type": "video",
            "recommended": recommended,
            "has_audio": fmt.get("acodec") not in [None, "none"],
        })

    # Add best audio only option
    if audio_formats and not audio_added:
        best_audio = audio_formats[0]
        download_url = best_audio.get("url")
        if download_url:
            file_size = best_audio.get("filesize") or best_audio.get("filesize_approx")
            if not file_size:
                tbr = best_audio.get("tbr") or best_audio.get("abr")
                file_size = estimate_file_size(tbr, info.get("duration"))

            formats.append({
                "format_id": best_audio.get("format_id"),
                "quality": "Audio Only",
                "ext": "mp3",
                "resolution": None,
                "fps": None,
                "is_60fps": False,
                "is_hdr": False,
                "vcodec": None,
                "acodec": best_audio.get("acodec"),
                "file_size": file_size,
                "url": download_url,
                "type": "audio",
                "recommended": False,
                "has_audio": True,
            })
            audio_added = True

    # Fallback
    if not formats:
        best_url = info.get("url")
        if best_url:
            height = info.get("height")
            formats.append({
                "format_id": "best",
                "quality": get_quality_label(height) if height else "Best",
                "ext": info.get("ext", "mp4"),
                "resolution": f"{info.get('width', '?')}x{height}" if height else None,
                "fps": info.get("fps"),
                "is_60fps": False,
                "is_hdr": False,
                "vcodec": info.get("vcodec"),
                "acodec": info.get("acodec"),
                "file_size": info.get("filesize") or info.get("filesize_approx"),
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
    if not data:
        return jsonify({"success": False, "error": "Request body must be JSON"}), 400

    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"success": False, "error": "URL is required"}), 400

    if not url.startswith(("http://", "https://")):
        return jsonify({"success": False, "error": "Invalid URL format"}), 400

    platform = detect_platform(url)
    if platform == "unknown":
        return jsonify({
            "success": False,
            "error": "Unsupported platform. Only YouTube, Instagram, Facebook, and Twitter/X are supported."
        }), 422

    try:
        result = extract_video_info(url)
        return jsonify(result)
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        if "Private video" in error_msg or "This video is private" in error_msg:
            return jsonify({"success": False, "error": "This video is private and cannot be accessed."}), 403
        elif "not available" in error_msg.lower():
            return jsonify({"success": False, "error": "Video is not available. It may have been removed or restricted."}), 404
        elif "Sign in" in error_msg or "login" in error_msg.lower():
            return jsonify({"success": False, "error": "This content requires login and cannot be accessed."}), 403
        else:
            return jsonify({"success": False, "error": "Could not extract video information."}), 422
    except Exception:
        return jsonify({"success": False, "error": "An unexpected error occurred. Please try again."}), 500

@app.route("/api/video/detect-platform", methods=["POST"])
def detect_platform_endpoint():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "Request body must be JSON"}), 400

    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"success": False, "error": "URL is required"}), 400

    platform = detect_platform(url)
    supported = platform in SUPPORTED_PLATFORMS

    if not supported:
        return jsonify({
            "success": True,
            "platform": platform,
            "supported": False,
            "message": "Only YouTube, Instagram, Facebook, and Twitter/X are supported."
        })

    return jsonify({
        "success": True,
        "platform": platform,
        "supported": True,
    })

@app.route("/api/healthz", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "video-downloader-api",
        "supported_platforms": SUPPORTED_PLATFORMS,
        "proxy_enabled": PROXY_URL is not None,
        "cookies_loaded": get_cookies_path() is not None,
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
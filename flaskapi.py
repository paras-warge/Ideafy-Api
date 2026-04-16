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


def get_quality_label(height):
    if not height:
        return None
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


def get_ydl_opts_youtube(cookies_path):
    """YouTube: get all formats including adaptive streams"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "extractor_retries": 5,
        "retries": 5,
        # Get everything — we filter manually
        "format": "bestvideo+bestaudio/best",
        "extractor_args": {
            "youtube": {
                # android client bypasses bot check reliably
                "player_client": ["android", "web"],
            }
        },
        "http_headers": {
            "User-Agent": "com.google.android.youtube/17.31.35 (Linux; U; Android 11)",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if cookies_path:
        opts["cookiefile"] = cookies_path
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    return opts


def get_ydl_opts_social(platform):
    """Instagram / Facebook / Twitter: get best merged format only"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "extractor_retries": 5,
        "retries": 5,
        # Prefer progressive (merged) formats directly downloadable on mobile
        "format": "best[ext=mp4]/best",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    return opts


def extract_video_info(url: str):
    cache_key = hashlib.md5(url.encode()).hexdigest()
    if cache_key in cache:
        return cache[cache_key]

    platform = detect_platform(url)
    cookies_path = get_cookies_path()

    if platform == "youtube":
        ydl_opts = get_ydl_opts_youtube(cookies_path)
    else:
        ydl_opts = get_ydl_opts_social(platform)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        result = process_info(info, url, platform)
        cache[cache_key] = result
        return result


def process_youtube_formats(formats_raw, duration):
    """
    YouTube: return full quality range.
    - Merged formats (have both video+audio): directly downloadable
    - Video-only formats (1080p+): flagged has_audio=False
    - Audio-only: one best option
    """
    formats = []
    seen = set()
    audio_added = False

    # Separate merged, video-only, audio-only
    merged = [
        f for f in formats_raw
        if f.get("vcodec") not in [None, "none"]
        and f.get("acodec") not in [None, "none"]
        and f.get("url")
        and f.get("height")
    ]

    video_only = [
        f for f in formats_raw
        if f.get("vcodec") not in [None, "none"]
        and f.get("acodec") in [None, "none"]
        and f.get("url")
        and f.get("height")
    ]

    audio_only = [
        f for f in formats_raw
        if f.get("vcodec") in [None, "none"]
        and f.get("acodec") not in [None, "none"]
        and f.get("url")
    ]

    # Combine and sort
    all_video = merged + video_only
    all_video.sort(
        key=lambda f: (f.get("height", 0), f.get("fps", 0) or 0),
        reverse=True
    )
    audio_only.sort(key=lambda f: f.get("tbr", 0) or 0, reverse=True)

    for fmt in all_video:
        height = fmt.get("height")
        if not height:
            continue

        fps = fmt.get("fps") or 0
        is_60fps = fps >= 50
        dynamic_range = (fmt.get("dynamic_range") or "").lower()
        is_hdr = "hdr" in dynamic_range

        quality_label = get_quality_label(height)
        if not quality_label:
            continue

        # Build display label
        display = quality_label
        if is_60fps:
            display += " 60fps"
        if is_hdr:
            display += " HDR"

        # Deduplicate
        key = f"{height}_{'60' if is_60fps else '30'}_{'hdr' if is_hdr else 'sdr'}"
        if key in seen:
            continue
        seen.add(key)

        download_url = fmt.get("url") or fmt.get("manifest_url")
        if not download_url:
            continue

        has_audio = fmt.get("acodec") not in [None, "none"]

        tbr = fmt.get("tbr") or fmt.get("vbr")
        file_size = estimate_file_size(tbr, duration)
        if fmt.get("filesize"):
            file_size = fmt.get("filesize")
        elif fmt.get("filesize_approx"):
            file_size = fmt.get("filesize_approx")

        formats.append({
            "format_id": fmt.get("format_id"),
            "quality": display,
            "ext": fmt.get("ext") or "mp4",
            "resolution": f"{fmt.get('width', '?')}x{height}",
            "fps": fps,
            "is_60fps": is_60fps,
            "is_hdr": is_hdr,
            "file_size": file_size,
            "url": download_url,
            "type": "video",
            "recommended": quality_label == "720p" and not is_60fps and not is_hdr,
            "has_audio": has_audio,
        })

    # Best audio only
    if audio_only and not audio_added:
        best = audio_only[0]
        dl_url = best.get("url")
        if dl_url:
            file_size = best.get("filesize") or best.get("filesize_approx")
            if not file_size:
                file_size = estimate_file_size(best.get("tbr") or best.get("abr"), duration)
            formats.append({
                "format_id": best.get("format_id"),
                "quality": "Audio Only",
                "ext": "mp3",
                "resolution": None,
                "fps": None,
                "is_60fps": False,
                "is_hdr": False,
                "file_size": file_size,
                "url": dl_url,
                "type": "audio",
                "recommended": False,
                "has_audio": True,
            })
            audio_added = True

    return formats


def process_social_formats(formats_raw, duration):
    """
    Instagram / Facebook / Twitter:
    - Return ONLY best merged (video+audio) format
    - Plus audio only option
    Mobile needs direct downloadable single-file formats only.
    """
    formats = []
    audio_added = False

    # Only merged formats (have both video and audio)
    merged = [
        f for f in formats_raw
        if f.get("vcodec") not in [None, "none"]
        and f.get("acodec") not in [None, "none"]
        and f.get("url")
    ]

    # Audio only formats
    audio_only = [
        f for f in formats_raw
        if f.get("vcodec") in [None, "none"]
        and f.get("acodec") not in [None, "none"]
        and f.get("url")
    ]

    # Sort merged by quality
    merged.sort(
        key=lambda f: (f.get("height") or 0, f.get("tbr") or 0),
        reverse=True
    )
    audio_only.sort(key=lambda f: f.get("tbr", 0) or 0, reverse=True)

    seen_heights = set()

    for fmt in merged:
        height = fmt.get("height")
        dl_url = fmt.get("url") or fmt.get("manifest_url")
        if not dl_url:
            continue

        quality_label = get_quality_label(height) if height else "Best"
        key = str(height or "best")
        if key in seen_heights:
            continue
        seen_heights.add(key)

        file_size = fmt.get("filesize") or fmt.get("filesize_approx")
        if not file_size:
            file_size = estimate_file_size(fmt.get("tbr"), duration)

        formats.append({
            "format_id": fmt.get("format_id"),
            "quality": quality_label,
            "ext": fmt.get("ext") or "mp4",
            "resolution": f"{fmt.get('width', '?')}x{height}" if height else None,
            "fps": fmt.get("fps") or 0,
            "is_60fps": (fmt.get("fps") or 0) >= 50,
            "is_hdr": False,
            "file_size": file_size,
            "url": dl_url,
            "type": "video",
            "recommended": True if not seen_heights - {key} else False,
            "has_audio": True,
        })

    # Fallback: if no merged found use best available
    if not formats:
        best_url = None
        for f in formats_raw:
            if f.get("url"):
                best_url = f.get("url")
                height = f.get("height")
                file_size = f.get("filesize") or f.get("filesize_approx")
                formats.append({
                    "format_id": f.get("format_id"),
                    "quality": get_quality_label(height) if height else "Best",
                    "ext": f.get("ext") or "mp4",
                    "resolution": f"{f.get('width', '?')}x{height}" if height else None,
                    "fps": f.get("fps") or 0,
                    "is_60fps": False,
                    "is_hdr": False,
                    "file_size": file_size,
                    "url": best_url,
                    "type": "video",
                    "recommended": True,
                    "has_audio": True,
                })
                break

    # Audio only
    if audio_only and not audio_added:
        best = audio_only[0]
        dl_url = best.get("url")
        if dl_url:
            file_size = best.get("filesize") or best.get("filesize_approx")
            if not file_size:
                file_size = estimate_file_size(best.get("tbr") or best.get("abr"), duration)
            formats.append({
                "format_id": best.get("format_id"),
                "quality": "Audio Only",
                "ext": "mp3",
                "resolution": None,
                "fps": None,
                "is_60fps": False,
                "is_hdr": False,
                "file_size": file_size,
                "url": dl_url,
                "type": "audio",
                "recommended": False,
                "has_audio": True,
            })
            audio_added = True

    return formats


def process_info(info, original_url, platform):
    title = info.get("title") or "Unknown Title"
    thumbnail = info.get("thumbnail") or ""
    duration = info.get("duration")
    uploader = info.get("uploader") or info.get("channel") or ""
    view_count = info.get("view_count")
    description = (info.get("description") or "")[:300]
    formats_raw = info.get("formats") or []

    # Use platform-specific format processing
    if platform == "youtube":
        formats = process_youtube_formats(formats_raw, duration)
    else:
        formats = process_social_formats(formats_raw, duration)

    # Final fallback if still empty
    if not formats and info.get("url"):
        height = info.get("height")
        formats.append({
            "format_id": "best",
            "quality": get_quality_label(height) if height else "Best",
            "ext": info.get("ext") or "mp4",
            "resolution": f"{info.get('width', '?')}x{height}" if height else None,
            "fps": info.get("fps") or 0,
            "is_60fps": False,
            "is_hdr": False,
            "file_size": info.get("filesize") or info.get("filesize_approx"),
            "url": info.get("url"),
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

    url = clean_url((data.get("url") or "").strip())
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
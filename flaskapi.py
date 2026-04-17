import os
import re
import hashlib
import tempfile
import logging
import subprocess
import json as _json
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
from cachetools import TTLCache

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# 200 entries, 5-minute TTL
cache = TTLCache(maxsize=200, ttl=300)

# ── Platform detection ────────────────────────────────────────────────────────
PLATFORM_PATTERNS = {
    "youtube": [r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)"],
    "facebook": [r"(?:facebook\.com/|fb\.watch/)", r"(?:m\.facebook\.com/|web\.facebook\.com/)"],
    "instagram": [r"(?:instagram\.com/(?:p/|reel/|tv/))"],
    "twitter": [r"(?:twitter\.com/|x\.com/)(?:\w+)/status/"],
}
SUPPORTED_PLATFORMS = list(PLATFORM_PATTERNS.keys())

COOKIES_PATH = os.path.join(os.path.dirname(__file__), "cookies.txt")
PROXY_URL = os.environ.get("PROXY_URL", None)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def clean_url(url: str) -> str:
    """Normalise URL — expand youtu.be shortlinks, strip tracking params."""
    if "youtu.be" in url:
        video_id = url.split("/")[-1].split("?")[0]
        return f"https://www.youtube.com/watch?v={video_id}"
    # Keep only the first query param chunk (removes &list=, &pp=, etc.)
    return url.split("&")[0]


def get_cookies_path():
    if os.path.exists(COOKIES_PATH):
        log.info("Using cookies file: %s", COOKIES_PATH)
        return COOKIES_PATH
    content = os.environ.get("YOUTUBE_COOKIES")
    if content:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        tmp.write(content)
        tmp.close()
        log.info("Cookies loaded from environment variable → %s", tmp.name)
        return tmp.name
    log.warning("No cookies found — YouTube bot-check may trigger")
    return None


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    for platform, patterns in PLATFORM_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, url_lower):
                return platform
    return "unknown"


def format_duration(seconds) -> str | None:
    if not seconds:
        return None
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def estimate_file_size(tbr, duration) -> int | None:
    if tbr and duration:
        return int((tbr * 1000 / 8) * duration)
    return None


def get_quality_label(height) -> str | None:
    if height is None:
        return None
    h = int(height)
    if h >= 2160: return "4K"
    if h >= 1440: return "1440p"
    if h >= 1080: return "1080p"
    if h >= 720:  return "720p"
    if h >= 480:  return "480p"
    if h >= 360:  return "360p"
    if h >= 240:  return "240p"
    if h >= 144:  return "144p"
    return f"{h}p"


def pick_filesize(fmt, duration) -> int | None:
    return (
        fmt.get("filesize")
        or fmt.get("filesize_approx")
        or estimate_file_size(fmt.get("tbr") or fmt.get("vbr"), duration)
    )


# ─────────────────────────────────────────────────────────────────────────────
# yt-dlp option builders
# ─────────────────────────────────────────────────────────────────────────────

# Browser UA — used for YouTube (tv_embedded client) and social platforms
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _base_opts() -> dict:
    """Options shared across all platforms."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "extractor_retries": 3,
        "retries": 3,
        "socket_timeout": 30,
    }
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    return opts


def get_po_token() -> tuple[str | None, str | None]:
    """
    Generate a YouTube po_token + visitor_data pair using the
    yt-dlp-get-pot plugin helper (node package: @yt-dlp/get-pot).

    Returns (po_token, visitor_data) or (None, None) if unavailable.
    po_token is needed on datacenter IPs (Railway, Render, etc.) to
    bypass YouTube's Proof-of-Origin bot check introduced in late 2023.

    To enable this, install on your server:
        npm install -g @yt-dlp/get-pot
    Or add to your Dockerfile / Railway build command.
    If not installed this silently returns None and we fall back to
    client-rotation strategy.
    """
    po_token = os.environ.get("YT_PO_TOKEN")
    visitor_data = os.environ.get("YT_VISITOR_DATA")
    if po_token and visitor_data:
        log.info("Using po_token from environment variable")
        return po_token, visitor_data

    # Try generating dynamically via the npm helper
    try:
        result = subprocess.run(
            ["npx", "--yes", "@yt-dlp/get-pot", "--output-json"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            data = _json.loads(result.stdout.strip())
            pot = data.get("po_token") or data.get("poToken")
            vd = data.get("visitor_data") or data.get("visitorData")
            if pot and vd:
                log.info("po_token generated dynamically via @yt-dlp/get-pot")
                return pot, vd
    except Exception as e:
        log.warning("Could not generate po_token dynamically: %s", e)

    return None, None


# YouTube client rotation order:
# 1. tv_embedded  — TV client, no bot check, no cookies required
# 2. ios          — Mobile client, very reliable
# 3. android      — Android app, good fallback
# 4. mweb         — Mobile web, last resort
_YT_CLIENT_ROTATION = [
    ["tv_embedded"],
    ["ios"],
    ["android"],
    ["mweb"],
]


def get_ydl_opts_youtube(cookies_path, client_list=None, po_token=None, visitor_data=None) -> dict:
    """
    YouTube extraction options with full bot-bypass stack:

    1. tv_embedded client — bypasses ALL bot checks, no cookies needed.
       YouTube's TV client (used by smart TVs) is never challenged.
    2. po_token support  — for datacenter IPs where even tv_embedded fails.
    3. Cookie support    — for age-restricted or member-only content.
    4. No DASH manifests — avoids URL-less format entries on server deployments.
    """
    if client_list is None:
        client_list = _YT_CLIENT_ROTATION[0]  # tv_embedded by default

    opts = _base_opts()
    opts.update({
        "format": "bestvideo*+bestaudio/best",
        "http_headers": {
            # Generic browser UA — tv_embedded doesn't need the Android app UA
            "User-Agent": _DESKTOP_UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {
            "youtube": {
                "player_client": client_list,
                "youtube_include_dash_manifest": [False],
            }
        },
    })

    # Inject po_token if available
    if po_token and visitor_data:
        opts["extractor_args"]["youtube"]["po_token"] = [f"web+{po_token}"]
        opts["extractor_args"]["youtube"]["visitor_data"] = [visitor_data]
        log.info("po_token injected into yt-dlp options")

    if cookies_path:
        opts["cookiefile"] = cookies_path

    return opts


def get_ydl_opts_social(platform: str) -> dict:
    """
    Instagram / Facebook / Twitter options.

    KEY CHANGES:
    1. format set to `best[ext=mp4]/best` — forces a single progressive file.
    2. Desktop UA used so Instagram/Facebook don't redirect to app store.
    3. No cookies needed for public content.
    """
    opts = _base_opts()
    opts.update({
        "format": "best[ext=mp4]/best",
        "http_headers": {
            "User-Agent": _DESKTOP_UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
    })
    return opts


# ─────────────────────────────────────────────────────────────────────────────
# Format processing
# ─────────────────────────────────────────────────────────────────────────────

def process_youtube_formats(formats_raw: list, duration) -> list:
    """
    Build the format list for YouTube.

    Rules:
    - Merged (video+audio): directly downloadable, preferred for ≤720p.
    - Video-only (1080p+): flagged has_audio=False — app must handle merging
      OR display a warning.  We still return them because the user asked for
      the full quality range.
    - Best audio-only: one entry, ext=m4a (not mp3 — that would require
      re-encoding on the server which we don't do).
    - If height is missing we still include the format using tbr for ordering.

    FIX: original code skipped any format without height.  Now we fall back to
    a "Best" label so no valid format is silently dropped.
    """
    formats = []
    seen: set[str] = set()
    audio_added = False

    def has_video(f):
        return f.get("vcodec") not in (None, "none", "")

    def has_audio_stream(f):
        return f.get("acodec") not in (None, "none", "")

    merged = [f for f in formats_raw if has_video(f) and has_audio_stream(f) and f.get("url")]
    video_only = [f for f in formats_raw if has_video(f) and not has_audio_stream(f) and f.get("url")]
    audio_only = [f for f in formats_raw if not has_video(f) and has_audio_stream(f) and f.get("url")]

    all_video = merged + video_only
    # Sort by height (None → 0), then fps
    all_video.sort(key=lambda f: (f.get("height") or 0, f.get("fps") or 0), reverse=True)
    audio_only.sort(key=lambda f: f.get("tbr") or f.get("abr") or 0, reverse=True)

    for fmt in all_video:
        height = fmt.get("height")
        fps = fmt.get("fps") or 0
        is_60fps = fps >= 50
        dr = (fmt.get("dynamic_range") or "").lower()
        is_hdr = "hdr" in dr

        quality_label = get_quality_label(height) if height else "Best"

        display = quality_label
        if is_60fps:
            display += " 60fps"
        if is_hdr:
            display += " HDR"

        # Dedup key: height (or "best") + fps bucket + hdr
        h_key = str(height or "best")
        key = f"{h_key}_{'60' if is_60fps else '30'}_{'hdr' if is_hdr else 'sdr'}"
        if key in seen:
            continue
        seen.add(key)

        dl_url = fmt.get("url") or fmt.get("manifest_url")
        if not dl_url:
            continue

        is_merged = has_audio_stream(fmt)
        file_size = pick_filesize(fmt, duration)

        formats.append({
            "format_id": fmt.get("format_id"),
            "quality": display,
            "ext": fmt.get("ext") or "mp4",
            "resolution": f"{fmt.get('width', '?')}x{height}" if height else None,
            "fps": fps,
            "is_60fps": is_60fps,
            "is_hdr": is_hdr,
            "file_size": file_size,
            "url": dl_url,
            "type": "video",
            # 720p merged = recommended default for mobile
            "recommended": (quality_label == "720p" and not is_60fps and not is_hdr and is_merged),
            "has_audio": is_merged,
        })

    # Best audio-only entry
    if audio_only and not audio_added:
        best = audio_only[0]
        dl_url = best.get("url")
        if dl_url:
            formats.append({
                "format_id": best.get("format_id"),
                "quality": "Audio Only",
                # m4a is what YouTube actually serves; mp3 would require server-side transcode
                "ext": best.get("ext") or "m4a",
                "resolution": None,
                "fps": None,
                "is_60fps": False,
                "is_hdr": False,
                "file_size": pick_filesize(best, duration),
                "url": dl_url,
                "type": "audio",
                "recommended": False,
                "has_audio": True,
            })
            audio_added = True

    return formats


def process_social_formats(formats_raw: list, duration, info: dict) -> list:
    """
    Instagram / Facebook / Twitter format processing.

    Rules (per user requirement):
    - Return ONLY merged (video+audio) formats.  Mobile cannot merge streams.
    - Best quality first.
    - If no merged formats exist, fall back to info["url"] (top-level direct URL).
    - One audio-only entry if available.

    FIX: original code sometimes returned only audio because merged[] was empty
    due to vcodec/acodec being "none" strings vs None.  Normalised check below.
    FIX: fallback to info["url"] / info["formats"][0] when no explicit merged
    format found — prevents empty video list.
    """
    formats = []
    audio_added = False

    def has_video(f):
        return f.get("vcodec") not in (None, "none", "")

    def has_audio_stream(f):
        return f.get("acodec") not in (None, "none", "")

    merged = [
        f for f in formats_raw
        if has_video(f) and has_audio_stream(f) and f.get("url")
    ]
    audio_only = [
        f for f in formats_raw
        if not has_video(f) and has_audio_stream(f) and f.get("url")
    ]

    # Sort merged by height (None→0) then bitrate
    merged.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    audio_only.sort(key=lambda f: f.get("tbr") or f.get("abr") or 0, reverse=True)

    seen_heights: set[str] = set()

    for fmt in merged:
        height = fmt.get("height")
        dl_url = fmt.get("url") or fmt.get("manifest_url")
        if not dl_url:
            continue

        quality_label = get_quality_label(height) if height else "Best"
        h_key = str(height or "best")
        if h_key in seen_heights:
            continue
        seen_heights.add(h_key)

        formats.append({
            "format_id": fmt.get("format_id"),
            "quality": quality_label,
            "ext": fmt.get("ext") or "mp4",
            "resolution": f"{fmt.get('width', '?')}x{height}" if height else None,
            "fps": fmt.get("fps") or 0,
            "is_60fps": (fmt.get("fps") or 0) >= 50,
            "is_hdr": False,
            "file_size": pick_filesize(fmt, duration),
            "url": dl_url,
            "type": "video",
            "recommended": len(formats) == 0,  # first (best) is recommended
            "has_audio": True,
        })

    # ── Fallback when no merged format found ─────────────────────────────────
    # Some extractors (Instagram, some FB) return the direct URL at the top
    # level of info rather than in formats[].
    if not formats:
        log.warning("No merged formats found — trying top-level info['url']")
        top_url = info.get("url")
        if top_url:
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
                "url": top_url,
                "type": "video",
                "recommended": True,
                "has_audio": True,
            })
        else:
            # Last resort: grab ANY format that has a URL
            for f in formats_raw:
                if f.get("url"):
                    h = f.get("height")
                    formats.append({
                        "format_id": f.get("format_id", "fallback"),
                        "quality": get_quality_label(h) if h else "Best",
                        "ext": f.get("ext") or "mp4",
                        "resolution": f"{f.get('width', '?')}x{h}" if h else None,
                        "fps": f.get("fps") or 0,
                        "is_60fps": False,
                        "is_hdr": False,
                        "file_size": pick_filesize(f, duration),
                        "url": f["url"],
                        "type": "video",
                        "recommended": True,
                        "has_audio": True,
                    })
                    break

    # Audio-only
    if audio_only and not audio_added:
        best = audio_only[0]
        dl_url = best.get("url")
        if dl_url:
            formats.append({
                "format_id": best.get("format_id"),
                "quality": "Audio Only",
                "ext": best.get("ext") or "mp3",
                "resolution": None,
                "fps": None,
                "is_60fps": False,
                "is_hdr": False,
                "file_size": pick_filesize(best, duration),
                "url": dl_url,
                "type": "audio",
                "recommended": False,
                "has_audio": True,
            })
            audio_added = True

    return formats


# ─────────────────────────────────────────────────────────────────────────────
# Core extraction
# ─────────────────────────────────────────────────────────────────────────────

def process_info(info: dict, platform: str) -> dict:
    title = info.get("title") or "Unknown Title"
    thumbnail = info.get("thumbnail") or ""
    duration = info.get("duration")
    uploader = info.get("uploader") or info.get("channel") or ""
    view_count = info.get("view_count")
    description = (info.get("description") or "")[:300]
    formats_raw = info.get("formats") or []

    log.info("Processing %s — raw format count: %d", platform, len(formats_raw))

    if platform == "youtube":
        formats = process_youtube_formats(formats_raw, duration)
    else:
        formats = process_social_formats(formats_raw, duration, info)

    # Absolute last-resort fallback
    if not formats and info.get("url"):
        h = info.get("height")
        formats.append({
            "format_id": "best",
            "quality": get_quality_label(h) if h else "Best",
            "ext": info.get("ext") or "mp4",
            "resolution": f"{info.get('width', '?')}x{h}" if h else None,
            "fps": info.get("fps") or 0,
            "is_60fps": False,
            "is_hdr": False,
            "file_size": info.get("filesize") or info.get("filesize_approx"),
            "url": info.get("url"),
            "type": "video",
            "recommended": True,
            "has_audio": True,
        })

    log.info("Returning %d formats for %s", len(formats), platform)

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


def extract_video_info(url: str) -> dict:
    cache_key = hashlib.md5(url.encode()).hexdigest()
    if cache_key in cache:
        log.info("Cache HIT for %s", url)
        return cache[cache_key]

    platform = detect_platform(url)

    if platform != "youtube":
        ydl_opts = get_ydl_opts_social(platform)
        log.info("Extracting info for %s (platform=%s)", url, platform)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        result = process_info(info, platform)
        cache[cache_key] = result
        return result

    # ── YouTube: multi-client retry loop ─────────────────────────────────────
    # Try each client in rotation.  On datacenter IPs the first client that
    # works without a bot error wins.  tv_embedded almost always works without
    # cookies; ios/android are reliable secondaries.
    cookies_path = get_cookies_path()
    po_token, visitor_data = get_po_token()
    last_error = None

    for attempt, client_list in enumerate(_YT_CLIENT_ROTATION, 1):
        log.info(
            "YouTube attempt %d/%d — client=%s po_token=%s",
            attempt, len(_YT_CLIENT_ROTATION),
            client_list, bool(po_token)
        )
        ydl_opts = get_ydl_opts_youtube(
            cookies_path,
            client_list=client_list,
            po_token=po_token,
            visitor_data=visitor_data,
        )
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            result = process_info(info, platform)
            cache[cache_key] = result
            log.info("YouTube extraction succeeded on attempt %d (client=%s)", attempt, client_list)
            return result
        except yt_dlp.utils.DownloadError as e:
            msg = str(e)
            last_error = e
            bot_error = (
                "Sign in" in msg
                or "bot" in msg.lower()
                or "confirm your age" in msg.lower()
                or "This video is unavailable" in msg
            )
            if bot_error:
                log.warning("Bot/sign-in error with client=%s, trying next client", client_list)
                continue  # try next client
            # Non-bot error (private video, removed, etc.) — raise immediately
            raise

    # All clients exhausted
    log.error("All YouTube clients failed. Last error: %s", last_error)
    raise last_error


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/video/info", methods=["POST"])
def get_video_info():
    data = request.get_json(silent=True)
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
            "error": "Unsupported platform. Supported: YouTube, Instagram, Facebook, Twitter/X.",
        }), 422

    try:
        result = extract_video_info(url)
        return jsonify(result)

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        log.error("yt-dlp DownloadError: %s", msg)
        if "Private video" in msg or "This video is private" in msg:
            return jsonify({"success": False, "error": "This video is private."}), 403
        if "Sign in" in msg or "bot" in msg.lower() or "login" in msg.lower():
            return jsonify({
                "success": False,
                "error": "YouTube requires sign-in verification. Please add fresh cookies.",
            }), 403
        if "not available" in msg.lower() or "removed" in msg.lower():
            return jsonify({"success": False, "error": "Video unavailable or removed."}), 404
        if "Requested format is not available" in msg:
            return jsonify({"success": False, "error": "Requested format not available. Try a different quality."}), 422
        return jsonify({"success": False, "error": "Could not extract video info."}), 422

    except Exception as e:
        log.exception("Unexpected error: %s", e)
        return jsonify({"success": False, "error": "Unexpected server error. Please retry."}), 500


@app.route("/api/video/detect-platform", methods=["POST"])
def detect_platform_endpoint():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Request body must be JSON"}), 400

    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"success": False, "error": "URL is required"}), 400

    platform = detect_platform(url)
    supported = platform in SUPPORTED_PLATFORMS

    return jsonify({
        "success": True,
        "platform": platform,
        "supported": supported,
        **({"message": "Only YouTube, Instagram, Facebook, Twitter/X supported."} if not supported else {}),
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


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
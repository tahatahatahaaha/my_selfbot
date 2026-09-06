"""
Video downloader — reply to any message containing a link and send `..`

Supports: YouTube · Instagram · TikTok · Pinterest (and 1000+ other
sites via yt-dlp). yt-dlp is imported lazily so it does not add to
startup RAM unless this command is actually used.
"""

import asyncio
import os
import re
import tempfile

from logger import log
from telegram_layer import client

MAX_FILE_MB  = 500
_URL_RE      = re.compile(r'https?://[^\s\]\)>\"\']+', re.IGNORECASE)

# Formats ordered from best to worst — each tier is a single pre-muxed
# file that needs no ffmpeg merge. Only the last fallback ("best") can
# sometimes be a combined stream too, but yt-dlp will try it anyway.
# This completely avoids the "ffmpeg not installed" error on Railway
# without installing any extra system package.
_FORMAT = (
    "bestvideo[height<=720][ext=mp4][vcodec^=avc]+bestaudio[ext=m4a]"
    "/bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
    "/bestvideo[height<=720]+bestaudio"
    "/mp4[height<=720]"
    "/mp4"
    "/best[height<=720][ext=mp4]"
    "/best[ext=mp4]"
    "/best"
)


def _extract_url(text: str) -> str | None:
    m = _URL_RE.search(text or "")
    return m.group(0).rstrip(".,)>") if m else None


def _do_download(url: str, outdir: str) -> str:
    import yt_dlp

    # Disable automatic merging — this is what causes the "ffmpeg not
    # installed" error when yt-dlp picks separate video+audio streams and
    # then can't merge them. By setting merge_output_format to None and
    # keeping ffmpeg_location blank, we force it to pick only pre-muxed
    # single-file formats from _FORMAT above.
    opts = {
        "format":              _FORMAT,
        "outtmpl":             os.path.join(outdir, "%(title).60s.%(ext)s"),
        "merge_output_format": None,   # no merge → no ffmpeg needed
        "max_filesize":        MAX_FILE_MB * 1024 * 1024,
        "quiet":               True,
        "no_warnings":         True,
        "noplaylist":          True,
        "socket_timeout":      30,
        "retries":             3,
        # YouTube has blocked most headless downloaders since 2024.
        # Providing a recent browser User-Agent helps, but the real fix
        # is using the "cookies-from-browser" option when running locally.
        # On Railway (no browser) we use the web_embedded_player client
        # via extractor_args, which still works for most public videos.
        "extractor_args": {
            "youtube": {
                "player_client": ["web_embedded", "android"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )
        },
    }

    # If a cookies file path is set in the environment, use it. This lets
    # the user export their browser cookies once and paste the path as a
    # Railway variable, unlocking age-restricted and region-locked videos.
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE")
    if cookies_file and os.path.exists(cookies_file):
        opts["cookiefile"] = cookies_file

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise RuntimeError("yt-dlp نتیجه‌ای برنگرداند")
        path = ydl.prepare_filename(info)

    if not os.path.exists(path):
        base = os.path.splitext(path)[0]
        for ext in ("mp4", "webm", "mkv", "mov"):
            c = f"{base}.{ext}"
            if os.path.exists(c):
                return c
        files = [os.path.join(outdir, f) for f in os.listdir(outdir)]
        if files:
            return max(files, key=os.path.getsize)
        raise FileNotFoundError("فایل دانلود‌شده پیدا نشد")
    return path


def _friendly_error(e: Exception) -> str:
    msg = str(e).lower()
    if any(k in msg for k in ("private", "login", "age", "sign in", "not accessible")):
        return (
            "❌ این محتوا خصوصی است یا نیاز به لاگین دارد.\n"
            "برای ویدیوهای عمومی یوتیوب که با این خطا رد می‌شن: "
            "فایل کوکی مرورگر رو export کن و مسیرش رو تو Railway "
            "با متغیر `YTDLP_COOKIES_FILE` تنظیم کن."
        )
    if any(k in msg for k in ("ffmpeg", "merging", "merger")):
        return "❌ فرمت ویدیو نیاز به merge دارد — yt-dlp رو آپدیت کن تا فرمت بهتری انتخاب کند."
    if any(k in msg for k in ("unavailable", "not available", "removed", "deleted")):
        return "❌ این ویدیو در دسترس نیست یا حذف شده."
    if any(k in msg for k in ("filesize", "too large", "exceeds")):
        return f"❌ فایل بزرگ‌تر از {MAX_FILE_MB} MB است."
    if any(k in msg for k in ("unsupported", "no video", "no formats")):
        return "❌ این لینک پشتیبانی نمی‌شود یا ویدیویی ندارد."
    if any(k in msg for k in ("network", "timeout", "connect")):
        return "❌ خطای شبکه — چند ثانیه صبر کن و دوباره امتحان کن."
    return f"❌ خطا: {str(e)[:180]}"


async def cmd_download_video(event):
    if not event.is_reply:
        await event.edit(
            "⬇️ **دانلود ویدیو**\n"
            "روی پیامی که لینک داره ریپلای کن و `..` بزن.\n"
            "یوتیوب · اینستاگرام · تیک‌تاک · پینترست پشتیبانی می‌شن."
        )
        return

    reply = await event.get_reply_message()
    if reply is None:
        await event.edit("⚠️ پیام ریپلای پیدا نشد.")
        return

    url = _extract_url(reply.raw_text or "")
    if not url:
        await event.edit("⚠️ لینکی تو پیام پیدا نشد.")
        return

    await event.edit("⬇️ در حال دانلود…")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            path = await asyncio.to_thread(_do_download, url, tmpdir)
        except Exception as e:
            await event.edit(_friendly_error(e))
            log.warn(f"Downloader: {e}")
            return

        size_mb = os.path.getsize(path) / (1024 * 1024)
        await event.edit(f"📤 در حال ارسال… ({size_mb:.1f} MB)")

        try:
            await client.send_file(
                event.chat_id,
                path,
                supports_streaming=True,
                reply_to=reply.id,
            )
            await event.delete()
        except Exception as e:
            await event.edit(f"❌ خطا در ارسال: {str(e)[:200]}")
            log.error(f"Downloader send error: {e}")

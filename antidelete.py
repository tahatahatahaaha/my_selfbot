"""Anti-delete / anti-edit: caches recent private-chat messages (in + out)
so that when one gets deleted OR edited, a copy can be reposted to Saved
Messages — for edits, showing both the pre-edit and post-edit text.

Telegram's delete update for normal (non-channel) chats only carries the
deleted message IDs — it does NOT tell you which chat they belonged to.
That's why we need our own rolling cache built from every private message
we see, keyed by message id, so that when a delete event arrives we can
look up what it was and where it came from. The same cache doubles as the
"pre-edit" snapshot for edit tracking: when an edit event arrives, we read
the cached entry *before* overwriting it with the new version.

Cache is kept *per chat* — each private chat gets its own 500-message
window, so a busy chat can't push another chat's messages out of the cache.

Known limitation: message IDs are only guaranteed unique *within* a single
chat. Since delete events don't tell us which chat they're for, we keep a
global msg_id -> chat_id index to route lookups. If two different private
chats both have a message with the same id cached at the same time, the
index can only point to one of them — the other would be reported
incorrectly (or missed) if it's the one that gets deleted. Rare, but not a
hard guarantee.

Also known: Telegram fires an edit event when a link preview finishes
loading for a message that contains a URL, even though the visible text
didn't change. We detect that case (old text == new text) and skip
notifying for it so Saved Messages isn't spammed with false "edits".
"""

import asyncio
import io
from collections import OrderedDict, deque
from datetime import datetime as _dt
from zoneinfo import ZoneInfo

from telethon import events
from telethon.errors import FloodWaitError

from config import CLOCK_TIMEZONE, PREFIX
from logger import log
import group_inbox

CACHE_LIMIT_PER_CHAT = 500
MEDIA_MAX_BYTES = 20 * 1024 * 1024  # 20MB — skip downloading bigger media
                                     # to keep this cheap on every message
NOTIFY_DELAY = 1.0  # seconds between forwarded notifications — a bulk
                     # delete of hundreds of messages at once would
                     # otherwise blast Saved Messages and hit FloodWait
NOTIFY_MAX_RETRIES = 3

_chat_caches = {}       # chat_id -> {msg_id: entry}
_chat_cache_order = {}  # chat_id -> deque([msg_id, ...]) oldest-left
_msg_index = {}         # msg_id -> chat_id, to route delete-event lookups

# Own user id — set via set_my_id() right after client.get_me() in main.py.
# Used to skip Saved Messages (the "chat with yourself") entirely: its
# chat_id equals the account's own numeric id, so a single compare is all
# that's needed. Also used in the delete handler to silently drop entries
# that originated from Saved Messages.
_my_id: int | None = None


def set_my_id(uid: int):
    global _my_id
    _my_id = uid

# Tracks (chat_id, msg_id) for messages that WERE a `.command` you sent
# yourself. Those get edited in place by our own handlers (a `.حذف همه`
# progress bar can edit the same message a dozen times) — none of that is
# real conversation worth a 500-message cache slot, so these are skipped
# from caching entirely, from the very first edit onward. Bounded so this
# bookkeeping itself can't grow forever.
_OWN_COMMAND_ID_CAP = 1000
_own_command_ids: "OrderedDict[tuple, bool]" = OrderedDict()


def _mark_own_command(chat_id, msg_id):
    key = (chat_id, msg_id)
    _own_command_ids[key] = True
    _own_command_ids.move_to_end(key)
    if len(_own_command_ids) > _OWN_COMMAND_ID_CAP:
        _own_command_ids.popitem(last=False)


def _is_own_command(chat_id, msg_id) -> bool:
    return (chat_id, msg_id) in _own_command_ids


_control_bot_id = None          # resolved lazily, see _resolve_control_bot_id
_control_bot_id_attempted = False

# Extra bots whose messages should never be cached or reported.
# Set via ANTIDELETE_EXCLUDE_BOTS env var (comma-separated usernames),
# e.g. ANTIDELETE_EXCLUDE_BOTS=mybotname,anotherbot
# Nothing is hardcoded here — this file must not contain personal info.
from config import ANTIDELETE_EXCLUDE_BOTS as _EXTRA_EXCLUDE_USERNAMES
_excluded_bot_ids: set = set()
_excluded_resolved = False


async def _resolve_control_bot_id():
    """Resolves BOT_TOKEN's own numeric user id, once, via the Bot API's
    getMe — so anti-edit can recognize edits from *this project's own*
    control bot specifically (never other bots in general). Resolved
    lazily on first use rather than at import time: antidelete.register()
    runs before the asyncio event loop exists, so there's nothing to await
    into at that point. Failure here is non-fatal: anti-edit keeps working
    for everyone else, it just can't exclude the control bot until this
    succeeds (retried again next process start, not retried mid-run)."""
    global _control_bot_id, _control_bot_id_attempted
    _control_bot_id_attempted = True
    try:
        from telegram import Bot
        from telegram.request import HTTPXRequest

        from config import BOT_TOKEN, PROXY_HOST, PROXY_PORT

        request = HTTPXRequest(proxy=f"socks5://{PROXY_HOST}:{PROXY_PORT}") if PROXY_HOST else None
        bot = Bot(token=BOT_TOKEN, request=request) if request else Bot(token=BOT_TOKEN)
        me = await bot.get_me()
        _control_bot_id = me.id
        log.ok(f"Anti-edit: resolved control bot id ({_control_bot_id}) — its edits won't post to Saved Messages")
    except Exception as e:
        log.warn(f"Anti-edit: couldn't resolve control bot id via getMe, exclusion unavailable: {e}")


async def _resolve_excluded_bots(client):
    """Resolve extra excluded bot usernames (e.g. @TMKselfbot) to their
    numeric ids so the cache handler can skip them by id — much cheaper
    than a username string-compare on every single incoming message."""
    global _excluded_resolved
    _excluded_resolved = True
    for username in _EXTRA_EXCLUDE_USERNAMES:
        try:
            entity = await client.get_entity(username)
            _excluded_bot_ids.add(entity.id)
            log.ok(f"Anti-delete: excluding messages from @{username} (id {entity.id})")
        except Exception as e:
            log.warn(f"Anti-delete: couldn't resolve @{username}: {e}")


def _remember(chat_id, msg_id, entry):
    """Add or update a cache entry.

    Uses a deque for the per-chat insertion order so eviction is O(1)
    (deque.popleft()) instead of O(n) (list.pop(0)).  Re-caching an
    already-present message (edit case) only updates the dict value and
    does NOT move the entry to the end of the deque — FIFO eviction is
    perfectly correct here and avoids the O(n) deque.remove() call that
    the old LRU-style code needed on every edit event.
    """
    cache = _chat_caches.setdefault(chat_id, {})
    order = _chat_cache_order.setdefault(chat_id, deque())

    is_new = msg_id not in cache
    cache[msg_id] = entry
    _msg_index[msg_id] = chat_id

    if is_new:
        order.append(msg_id)
        # Evict oldest entries to stay within the per-chat cap.  Skip any
        # "ghost" ids that _pop() left behind (they're no longer in cache).
        while len(order) > CACHE_LIMIT_PER_CHAT:
            oldest = order.popleft()          # O(1) with deque
            if oldest not in cache:
                continue                       # ghost — already deleted, skip
            cache.pop(oldest, None)
            if _msg_index.get(oldest) == chat_id:
                _msg_index.pop(oldest, None)


def _pop(msg_id):
    chat_id = _msg_index.pop(msg_id, None)
    if chat_id is None:
        return None
    cache = _chat_caches.get(chat_id)
    data = cache.pop(msg_id, None) if cache else None
    # Leave the deque entry as a "ghost" — it'll be skipped during the next
    # eviction pass in _remember (which checks cache membership). This avoids
    # an O(n) deque.remove() on every deleted message; ghosts are harmless
    # because they never outnumber the CACHE_LIMIT_PER_CHAT live entries.
    return data


async def _build_cache_entry(event):
    msg = event.message

    fwd_from_name = None
    if msg.fwd_from:
        fwd_from_name = getattr(msg.fwd_from, "from_name", None) or "منبع ناشناس"

    media_bytes = None
    media_too_large = False
    media_attributes = None
    media_mime_type = None
    media_is_photo = False
    media_file_name = None
    if msg.media:
        size = None
        try:
            size = msg.file.size if msg.file else None
        except Exception:
            size = None

        if size is not None and size > MEDIA_MAX_BYTES:
            media_too_large = True
        else:
            try:
                buf = io.BytesIO()
                await msg.download_media(file=buf)
                media_bytes = buf.getvalue()
                # Re-uploading later with NO attributes/mime-type at all
                # forces Telegram to guess the file type from scratch —
                # in practice that usually means videos land as a generic
                # document with no supports_streaming flag, so they can't
                # be played inline and have to fully download first (slow
                # or outright fails to open on some phones). Keeping the
                # ORIGINAL attributes (resolution, duration, streaming
                # flag, voice/round-video flags, etc.) and passing them
                # straight through on resend fixes that completely.
                if msg.photo:
                    media_is_photo = True
                elif msg.document:
                    media_attributes = msg.document.attributes
                    media_mime_type = msg.document.mime_type
                    media_file_name = getattr(msg.file, "name", None)
            except Exception as e:
                log.warn(f"Anti-delete: couldn't cache media for msg {msg.id}: {e}")

    # Only cheap, synchronous data goes in the cache — no entity resolution
    # here, since this runs on every single private message. Names are
    # resolved later, only for the rare message that actually gets deleted.
    return {
        "chat_id": event.chat_id,
        "sender_id": msg.sender_id,
        "out": bool(msg.out),
        "text": msg.raw_text or "",
        "date": msg.date,
        "fwd_from_name": fwd_from_name,
        "media_bytes": media_bytes,
        "media_too_large": media_too_large,
        "media_attributes": media_attributes,
        "media_mime_type": media_mime_type,
        "media_is_photo": media_is_photo,
        "media_file_name": media_file_name,
    }


async def _resolve_label(client, entity_id):
    if entity_id is None:
        return "نامشخص"
    try:
        entity = await client.get_entity(entity_id)
        return getattr(entity, "first_name", None) or getattr(entity, "title", None) or str(entity_id)
    except Exception:
        return str(entity_id)


async def _send_cached_media(client, entity, data, caption):
    """Resends cached media in a form Telegram can actually stream/preview
    properly on both desktop and mobile.

    The mobile-not-opening bug: when we pass a bare BytesIO with no name,
    Telethon has no way to know the file is a photo even when the original
    was one — so it uploads it as a generic document. Telegram then sends
    a 'file' icon, not an inline image, and the phone's viewer has to
    download the whole thing before even deciding what to do with it.
    Setting bio.name to a real image extension tells Telethon what type to
    use without needing to re-infer it from the raw bytes."""
    bio = io.BytesIO(data["media_bytes"])

    if data.get("media_is_photo"):
        bio.name = "photo.jpg"
        await client.send_file(entity, bio, caption=caption[:1024], force_document=False)
        return

    file_name = data.get("media_file_name")
    mime = data.get("media_mime_type") or ""
    if not file_name:
        if mime.startswith("video/"):
            file_name = "video.mp4"
        elif mime.startswith("audio/"):
            file_name = "audio.ogg"
        elif mime.startswith("image/"):
            ext = mime.split("/")[-1] or "jpg"
            file_name = f"image.{ext}"
    if file_name:
        bio.name = file_name

    await client.send_file(
        entity,
        bio,
        caption=caption[:1024],
        attributes=data.get("media_attributes"),
        mime_type=mime or None,
        force_document=False,
    )


async def _notify_to(client, dest, data):
    """Core send: puts one cached message into `dest` (a group id or 'me')."""
    sender_label = "خودت" if data["out"] else await _resolve_label(client, data["sender_id"])
    date_label = (
        data["date"].astimezone(ZoneInfo(CLOCK_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
        if data["date"]
        else "?"
    )
    header = f"👤 {sender_label}\n🕒 {date_label}"

    if data["media_bytes"]:
        caption = header + (f"\n\n{data['text']}" if data["text"] else "")
        await _send_cached_media(client, dest, data, caption)
    elif data["media_too_large"]:
        await client.send_message(dest, header + "\n\n⚠️ رسانه بزرگ‌تر از حد مجاز بود.")
    else:
        body = header + (f"\n\n{data['text']}" if data["text"] else "")
        await client.send_message(dest, body)


async def _notify(client, data):
    try:
        dest = await group_inbox.get_inbox(client)
    except Exception:
        dest = "me"
    await _notify_to(client, dest, data)


async def _notify_with_retry(client, data, msg_id):
    """A bulk delete of hundreds of messages fires hundreds of these back to
    back. Telegram *will* FloodWait us if we blast them all at once — retry
    with the server-told wait time instead of just dropping the message."""
    for attempt in range(NOTIFY_MAX_RETRIES):
        try:
            await _notify(client, data)
            log.ok(f"Anti-delete: reposted deleted message {msg_id} to Saved Messages")
            return
        except FloodWaitError as e:
            log.warn(f"Anti-delete: flood wait {e.seconds}s, retrying message {msg_id}")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            log.error(f"Anti-delete notify error for message {msg_id}: {e}")
            return
    log.error(f"Anti-delete: gave up on message {msg_id} after {NOTIFY_MAX_RETRIES} flood-wait retries")


async def _notify_edit(client, old_entry, new_text, new_date):
    sender_label = "خودت" if old_entry["out"] else await _resolve_label(client, old_entry["sender_id"])
    date_label = (
        new_date.astimezone(ZoneInfo(CLOCK_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
        if new_date
        else "?"
    )
    old_text = old_entry.get("text") or "(بدون متن)"
    new_text_label = new_text or "(بدون متن)"

    body = (
        f"✏️ {sender_label}\n🕒 {date_label}\n\n"
        f"🔴 {old_text}\n🟢 {new_text_label}"
    )

    try:
        dest = await group_inbox.get_inbox(client)
    except Exception:
        dest = "me"

    if old_entry.get("media_bytes"):
        await _send_cached_media(client, dest, old_entry, body)
    else:
        await client.send_message(dest, body)


async def _notify_edit_with_retry(client, old_entry, new_text, new_date, msg_id):
    for attempt in range(NOTIFY_MAX_RETRIES):
        try:
            await _notify_edit(client, old_entry, new_text, new_date)
            log.ok(f"Anti-edit: reposted pre-edit version of message {msg_id} to Saved Messages")
            return
        except FloodWaitError as e:
            log.warn(f"Anti-edit: flood wait {e.seconds}s, retrying message {msg_id}")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            log.error(f"Anti-edit notify error for message {msg_id}: {e}")
            return
    log.error(f"Anti-edit: gave up on message {msg_id} after {NOTIFY_MAX_RETRIES} flood-wait retries")


async def _cache_in_background(event):
    try:
        entry = await _build_cache_entry(event)
        _remember(event.chat_id, event.message.id, entry)
    except Exception as e:
        log.error(f"Anti-delete cache error: {e}")


_MSGS_PER_PAGE = 25  # messages rendered per chat-screenshot image


def _render_page(entries, my_name, other_name, page_label: str, tz: str) -> bytes:
    """Render a batch of cache entries as a dark-theme chat-screenshot PNG.

    Uses PIL (already a project dependency for quote.py) and arabic-reshaper/
    bidi (same). Imported lazily so startup RAM isn't affected when this
    feature is never triggered.

    Returns raw PNG bytes ready to pass to client.send_file().
    """
    from PIL import Image, ImageDraw, ImageFont
    import arabic_reshaper
    from bidi.algorithm import get_display
    import os

    W            = 1080
    MARGIN       = 26
    BUBBLE_MAX_W = int(W * 0.73)
    F_MSG        = 27
    F_NAME       = 20
    F_TIME       = 18
    PAD          = 16
    GAP          = 10

    BG          = (13, 13, 13)
    C_ME        = (22, 97, 57)
    C_THEM      = (36, 36, 44)
    C_TEXT      = (228, 228, 228)
    C_NAME_ME   = (90, 200, 120)
    C_NAME_THEM = (90, 150, 215)
    C_TIME      = (115, 115, 125)
    C_HEAD      = (80, 130, 200)
    C_NOTE      = (140, 140, 150)

    font_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "font.ttf")
    try:
        fm = ImageFont.truetype(font_path, F_MSG)
        fn = ImageFont.truetype(font_path, F_NAME)
        ft = ImageFont.truetype(font_path, F_TIME)
        fh = ImageFont.truetype(font_path, F_NAME)
    except Exception:
        fm = fn = ft = fh = ImageFont.load_default()

    def _bidi(txt: str) -> str:
        if not txt:
            return ""
        try:
            return get_display(arabic_reshaper.reshape(txt))
        except Exception:
            return txt

    def _wrap(txt: str, max_w: int, draw, f) -> list:
        if not txt:
            return []
        lines, cur = [], ""
        for word in txt.split():
            probe = (cur + " " + word).strip()
            if draw.textlength(probe, font=f) <= max_w:
                cur = probe
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines or [""]

    # ── measure all bubbles on a throw-away canvas ───────────────────────
    dummy = Image.new("RGB", (W, 1))
    d0    = ImageDraw.Draw(dummy)
    rows  = []
    total_h = MARGIN + 50

    for _, data in entries:
        text    = data.get("text") or ""
        date_obj = data.get("date")
        try:
            ts = (date_obj.astimezone(ZoneInfo(tz)).strftime("%H:%M")
                  if date_obj else "?")
        except Exception:
            ts = "?"

        shaped = _bidi(text)
        is_out = data["out"]
        sender = my_name if is_out else other_name
        lines  = _wrap(shaped, BUBBLE_MAX_W - 2 * PAD, d0, fm)

        fwd_note = _bidi(f"↪ {data['fwd_from_name']}") if data.get("fwd_from_name") else ""
        fwd_h    = (F_NAME + 4) if fwd_note else 0

        if data.get("media_too_large"):
            media_note = _bidi("📎 رسانه بزرگ‌تر از ۲۰MB")
        elif data.get("media_bytes"):
            mime = (data.get("media_mime_type") or "").lower()
            is_vid = mime.startswith("video/") or any(
                hasattr(a, "duration") for a in (data.get("media_attributes") or [])
            )
            media_note = _bidi("🎬 ویدیو — جداگانه ارسال شد") if is_vid else _bidi("📎 رسانه")
        else:
            media_note = ""
        note_h = (F_MSG + 4) if media_note else 0

        txt_h = len(lines) * (F_MSG + 4)
        bh    = PAD + F_NAME + 4 + fwd_h + txt_h + note_h + 4 + F_TIME + PAD

        rows.append(dict(is_out=is_out, sender=sender, lines=lines,
                         ts=ts, bh=bh, fwd_note=fwd_note, media_note=media_note))
        total_h += bh + GAP

    total_h += MARGIN

    # ── draw ─────────────────────────────────────────────────────────────
    img  = Image.new("RGB", (W, total_h), BG)
    draw = ImageDraw.Draw(img)

    draw.text((W // 2, MARGIN + 10), _bidi(page_label),
              fill=C_HEAD, font=fh, anchor="mm")

    y = MARGIN + 50
    for row in rows:
        is_out = row["is_out"]
        mw = max((draw.textlength(l, font=fm) for l in row["lines"]), default=60)
        nw = draw.textlength(row["sender"], font=fn)
        tw = draw.textlength(row["ts"],     font=ft)
        bw = min(BUBBLE_MAX_W, int(max(mw, nw, tw)) + 2 * PAD)
        x  = (W - MARGIN - bw) if is_out else MARGIN
        draw.rounded_rectangle([x, y, x + bw, y + row["bh"]], radius=14,
                                fill=(C_ME if is_out else C_THEM))

        cy = y + PAD
        draw.text((x + PAD, cy), row["sender"],
                  fill=(C_NAME_ME if is_out else C_NAME_THEM), font=fn)
        cy += F_NAME + 4

        if row["fwd_note"]:
            draw.text((x + PAD, cy), row["fwd_note"], fill=C_NOTE, font=ft)
            cy += F_NAME + 4

        for ln in row["lines"]:
            draw.text((x + PAD, cy), ln, fill=C_TEXT, font=fm)
            cy += F_MSG + 4

        if row["media_note"]:
            draw.text((x + PAD, cy), row["media_note"], fill=C_NOTE, font=ft)

        draw.text((x + bw - PAD, y + row["bh"] - PAD - F_TIME),
                  row["ts"], fill=C_TIME, font=ft, anchor="rt")

        y += row["bh"] + GAP

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True, compress_level=6)
    return buf.getvalue()


async def _resend_full_conversation(client, chat_id, all_entries):
    """Saves a bilateral-delete event as chat-screenshot PNG images sent to
    a dedicated group. Images open natively in Telegram on every device
    (no download, no browser, no external app). Videos are sent as native
    video files in the same group so they play inline too."""
    all_sorted = sorted(
        all_entries,
        key=lambda item: item[1].get("date") or _dt.min.replace(tzinfo=None),
    )
    chat_label = await _resolve_label(client, chat_id)
    me_entity  = await client.get_me()
    my_name    = getattr(me_entity, "first_name", None) or "من"
    now_str    = _dt.now(ZoneInfo(CLOCK_TIMEZONE)).strftime("%Y-%m-%d %H:%M")
    count      = len(all_sorted)

    try:
        dest = await group_inbox.create_bilateral_group(client, chat_label)
    except Exception as e:
        log.error(f"Anti-delete: couldn't create bilateral group: {e}")
        try:
            dest = await group_inbox.get_inbox(client)
        except Exception:
            dest = "me"

    await client.send_message(
        dest,
        f"🗑 **دیلیت دوطرفه با {chat_label}**\n"
        f"📝 {count} پیام · {now_str}",
    )
    await asyncio.sleep(0.5)

    # ── render conversation as chat-screenshot images ─────────────────────
    pages       = [all_sorted[i:i + _MSGS_PER_PAGE]
                   for i in range(0, len(all_sorted), _MSGS_PER_PAGE)]
    total_pages = len(pages)

    for pi, page in enumerate(pages, 1):
        label = f"مکالمه با {chat_label}"
        if total_pages > 1:
            label += f" — صفحه {pi} از {total_pages}"
        try:
            png = await asyncio.to_thread(
                _render_page, page, my_name, chat_label, label, CLOCK_TIMEZONE
            )
            buf      = io.BytesIO(png)
            buf.name = f"chat_p{pi:02d}.png"
            await client.send_file(dest, buf, force_document=False)
            await asyncio.sleep(0.8)
        except Exception as e:
            log.error(f"Anti-delete: page {pi} render failed: {e}")

    # ── send videos as native playable files ──────────────────────────────
    for _, data in all_sorted:
        mime     = (data.get("media_mime_type") or "").lower()
        is_video = mime.startswith("video/") or any(
            hasattr(a, "duration") for a in (data.get("media_attributes") or [])
        )
        if is_video and data.get("media_bytes"):
            try:
                await _send_cached_media(client, dest, data, "")
                await asyncio.sleep(NOTIFY_DELAY)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
                try:
                    await _send_cached_media(client, dest, data, "")
                except Exception as e2:
                    log.error(f"Anti-delete: video resend failed: {e2}")
            except Exception as e:
                log.error(f"Anti-delete: video resend failed: {e}")

    log.ok(
        f"Anti-delete: bilateral delete with {chat_label} saved "
        f"({count} messages, {total_pages} screenshot images)"
    )


def register(client):
    """Call once with the running TelegramClient to enable anti-delete.
    Call set_my_id(me.id) after login so Saved Messages is excluded."""

    @client.on(events.NewMessage())
    async def _cache_handler(event):
        if not event.is_private:
            return
        # Skip Saved Messages (chat_id == own user id) — deleting there is
        # deliberate housekeeping, not something worth saving.
        if _my_id is not None and event.chat_id == _my_id:
            return
        if event.message.out and (event.message.raw_text or "").startswith(PREFIX):
            # This is you sending a `.command` — the selfbot is about to
            # edit this very message in place as its reply (and possibly
            # re-edit it several times, e.g. a delete-progress counter).
            # None of that is conversation worth a cache slot, so skip
            # caching it from the start and remember its id so every
            # future edit skips too.
            _mark_own_command(event.chat_id, event.message.id)
            return

        if not event.message.out:
            if not _control_bot_id_attempted:
                await _resolve_control_bot_id()
            if _control_bot_id is not None and event.sender_id == _control_bot_id:
                return

            if not _excluded_resolved:
                await _resolve_excluded_bots(client)
            if event.sender_id in _excluded_bot_ids:
                # e.g. @TMKselfbot — its messages are bot-generated UI
                # chatter, not real conversation worth caching or reporting.
                return

        # Building the cache entry can involve downloading media, which is
        # network-bound and was previously awaited right here — meaning
        # every private photo/video briefly stalled this handler before it
        # could return. Fire it off as a background task instead so the
        # handler itself is instant regardless of media size/network speed.
        asyncio.create_task(_cache_in_background(event))

    @client.on(events.MessageEdited())
    async def _edit_handler(event):
        if not event.is_private:
            return
        if _my_id is not None and event.chat_id == _my_id:
            return
        chat_id = event.chat_id
        msg_id = event.message.id
        new_text = event.message.raw_text or ""

        if _is_own_command(chat_id, msg_id):
            # A self-issued command's own reply being edited (e.g. a
            # `.حذف همه` progress update) — never cached, never reported.
            return

        if event.message.out:
            # Outgoing edits — you edited your own message. Nothing to report;
            # just refresh the cache so if the message later gets deleted the
            # most recent text is stored (not the stale original).
            asyncio.create_task(_cache_in_background(event))
            return

        if not _control_bot_id_attempted:
            await _resolve_control_bot_id()

        if _control_bot_id is not None and event.sender_id == _control_bot_id:
            return

        if not _excluded_resolved:
            await _resolve_excluded_bots(client)
        if event.sender_id in _excluded_bot_ids:
            return

        cache = _chat_caches.get(chat_id)
        old_entry = cache.get(msg_id) if cache else None

        if old_entry is None:
            # We never cached a pre-edit version (e.g. the bot started
            # after this message was sent) — nothing to compare against.
            asyncio.create_task(_cache_in_background(event))
            return

        if new_text == old_entry.get("text", ""):
            # Same text, e.g. a link preview just attached — Telegram fires
            # an edit event for that too, but there's nothing to report.
            asyncio.create_task(_cache_in_background(event))
            return

        asyncio.create_task(
            _notify_edit_with_retry(client, old_entry, new_text, event.message.date, msg_id)
        )
        # Refresh the cache to the edited version so a *second* edit (or a
        # later delete) compares against/reports the latest text, not the
        # original one.
        asyncio.create_task(_cache_in_background(event))

    # Debounce state for bilateral deletes: Telegram fires multiple
    # MessageDeleted batches for a single "delete for everyone" — sometimes
    # spread over 10+ seconds for large chats — so we accumulate all batches
    # in a window and call _resend_full_conversation exactly once per event.
    _bilateral_pending:   dict = {}  # chat_id -> {msg_id: data}
    _bilateral_tasks:     dict = {}  # chat_id -> asyncio.Task
    _bilateral_processed: dict = {}  # chat_id -> loop.time() of last processing
    BILATERAL_DEBOUNCE_SECS = 10.0   # was 3 s — Telegram can spread batches over 10 s+
    BILATERAL_COOLDOWN_SECS = 90.0   # ignore late batches for 90 s after processing

    async def _flush_bilateral(chat_id):
        await asyncio.sleep(BILATERAL_DEBOUNCE_SECS)
        entries = list(_bilateral_pending.pop(chat_id, {}).items())
        _bilateral_tasks.pop(chat_id, None)
        if entries:
            # Stamp BEFORE launching the task so any late-arriving batches
            # that slip in while rendering is still in progress are blocked
            # by the cooldown check in _delete_handler.
            _bilateral_processed[chat_id] = asyncio.get_event_loop().time()
            asyncio.create_task(_resend_full_conversation(client, chat_id, entries))

    @client.on(events.MessageDeleted())
    async def _delete_handler(event):
        if not event.deleted_ids:
            return

        hits = []
        for msg_id in event.deleted_ids:
            data = _pop(msg_id)
            if data is not None:
                hits.append((msg_id, data))

        if not hits:
            return

        # Skip anything from Saved Messages or from the control bot.
        if _my_id is not None:
            hits = [(mid, d) for mid, d in hits if d["chat_id"] != _my_id]
        if _control_bot_id is not None:
            hits = [(mid, d) for mid, d in hits if d.get("sender_id") != _control_bot_id]

        if not hits:
            return

        own_hits   = [(mid, d) for mid, d in hits if     d["out"]]
        other_hits = [(mid, d) for mid, d in hits if not d["out"]]

        # ── Full 2-sided PV deletion ──────────────────────────────────────
        # Accumulate batches in a debounce window; the flush task creates
        # exactly ONE group after the window closes.
        if own_hits and other_hits:
            chat_id = hits[0][1]["chat_id"]

            # If we already processed a bilateral delete for this chat
            # recently, this is just a late-arriving batch from the same
            # event — skip it entirely so no second group gets created.
            now  = asyncio.get_event_loop().time()
            last = _bilateral_processed.get(chat_id, 0.0)
            if now - last < BILATERAL_COOLDOWN_SECS:
                return

            bucket = _bilateral_pending.setdefault(chat_id, {})
            for mid, d in hits:
                bucket[mid] = d
            # Also grab any remaining cached messages for this chat
            for mid, d in _chat_caches.get(chat_id, {}).items():
                bucket.setdefault(mid, d)
            # (Re)start the debounce timer — cancels the previous one so
            # a new batch resets the 10 s countdown from scratch.
            if chat_id in _bilateral_tasks:
                _bilateral_tasks[chat_id].cancel()
            _bilateral_tasks[chat_id] = asyncio.create_task(_flush_bilateral(chat_id))
            return

        if own_hits and not other_hits:
            return  # You deleted your own message — nothing to report.

        for msg_id, data in other_hits:
            await _notify_with_retry(client, data, msg_id)
            await asyncio.sleep(NOTIFY_DELAY)

    log.ok(f"Anti-delete / anti-edit enabled (PV only, {CACHE_LIMIT_PER_CHAT} messages cached per chat)")


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
from collections import OrderedDict
from datetime import datetime as _dt
from zoneinfo import ZoneInfo

from telethon import events
from telethon.errors import FloodWaitError

from config import CLOCK_TIMEZONE, PREFIX
from logger import log

CACHE_LIMIT_PER_CHAT = 500
MEDIA_MAX_BYTES = 20 * 1024 * 1024  # 20MB — skip downloading bigger media
                                     # to keep this cheap on every message
NOTIFY_DELAY = 1.0  # seconds between forwarded notifications — a bulk
                     # delete of hundreds of messages at once would
                     # otherwise blast Saved Messages and hit FloodWait
NOTIFY_MAX_RETRIES = 3

_chat_caches = {}       # chat_id -> {msg_id: entry}
_chat_cache_order = {}  # chat_id -> [msg_id, ...] oldest -> newest
_msg_index = {}         # msg_id -> chat_id, to route delete-event lookups

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


def _remember(chat_id, msg_id, entry):
    cache = _chat_caches.setdefault(chat_id, {})
    order = _chat_cache_order.setdefault(chat_id, [])

    if msg_id in cache:
        order.remove(msg_id)
    cache[msg_id] = entry
    order.append(msg_id)
    _msg_index[msg_id] = chat_id

    while len(order) > CACHE_LIMIT_PER_CHAT:
        oldest = order.pop(0)
        cache.pop(oldest, None)
        if _msg_index.get(oldest) == chat_id:
            _msg_index.pop(oldest, None)


def _pop(msg_id):
    chat_id = _msg_index.pop(msg_id, None)
    if chat_id is None:
        return None
    cache = _chat_caches.get(chat_id)
    order = _chat_cache_order.get(chat_id)
    data = cache.pop(msg_id, None) if cache else None
    if order and msg_id in order:
        order.remove(msg_id)
    return data


async def _build_cache_entry(event):
    msg = event.message

    fwd_from_name = None
    if msg.fwd_from:
        fwd_from_name = getattr(msg.fwd_from, "from_name", None) or "منبع ناشناس"

    media_bytes = None
    media_too_large = False
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
    }


async def _resolve_label(client, entity_id):
    if entity_id is None:
        return "نامشخص"
    try:
        entity = await client.get_entity(entity_id)
        return getattr(entity, "first_name", None) or getattr(entity, "title", None) or str(entity_id)
    except Exception:
        return str(entity_id)


async def _notify(client, data):
    chat_label = await _resolve_label(client, data["chat_id"])
    sender_label = "خودت" if data["out"] else await _resolve_label(client, data["sender_id"])
    date_label = (
        data["date"].astimezone(ZoneInfo(CLOCK_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
        if data["date"]
        else "?"
    )

    header = (
        "🗑 **یه پیام تو PV حذف شد**\n"
        f"👤 چت: {chat_label}\n"
        f"✍️ فرستنده: {sender_label}\n"
        f"🕒 زمان ارسال: {date_label}"
    )
    if data["fwd_from_name"]:
        header += f"\n↪️ فوروارد شده از: {data['fwd_from_name']}"
    if data["media_too_large"]:
        header += "\n⚠️ رسانه بزرگ‌تر از حد مجاز بود، فقط متن کش شد."

    if data["media_bytes"]:
        caption = header + (f"\n\n{data['text']}" if data["text"] else "")
        await client.send_file("me", io.BytesIO(data["media_bytes"]), caption=caption[:1024])
    else:
        body = header + (f"\n\n{data['text']}" if data["text"] else "\n\n(بدون متن)")
        await client.send_message("me", body)


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
    chat_label = await _resolve_label(client, old_entry["chat_id"])
    sender_label = "خودت" if old_entry["out"] else await _resolve_label(client, old_entry["sender_id"])
    date_label = (
        new_date.astimezone(ZoneInfo(CLOCK_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
        if new_date
        else "?"
    )
    old_text = old_entry.get("text") or "(بدون متن)"
    new_text_label = new_text or "(بدون متن)"

    header = (
        "✏️ **یه پیام تو PV ویرایش شد**\n"
        f"👤 چت: {chat_label}\n"
        f"✍️ فرستنده: {sender_label}\n"
        f"🕒 زمان ویرایش: {date_label}"
    )
    body = header + f"\n\n🔴 قبل از ویرایش:\n{old_text}" + f"\n\n🟢 بعد از ویرایش:\n{new_text_label}"

    if old_entry.get("media_bytes"):
        await client.send_file("me", io.BytesIO(old_entry["media_bytes"]), caption=body[:1024])
    else:
        await client.send_message("me", body)


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


async def _save_full_conversation(client, chat_id, all_entries):
    """Saves the full cached conversation to a .txt file and sends it to
    Saved Messages. Called only on a detected full 2-sided PV deletion."""
    try:
        # Sort by message date (oldest first)
        def _date_key(item):
            d = item[1].get("date")
            return d if d is not None else _dt.min.replace(tzinfo=None)

        all_entries_sorted = sorted(all_entries, key=_date_key)

        chat_label = await _resolve_label(client, chat_id)
        me_entity = await client.get_me()
        my_name = getattr(me_entity, "first_name", None) or "من"
        now_str = _dt.now(ZoneInfo(CLOCK_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            f"مکالمه‌ی حذف‌شده با {chat_label}",
            f"تاریخ ذخیره‌سازی: {now_str}",
            f"تعداد پیام: {len(all_entries_sorted)}",
            "─" * 40,
            "",
        ]

        for _mid, entry in all_entries_sorted:
            date_obj = entry.get("date")
            if date_obj is not None:
                try:
                    time_str = date_obj.astimezone(ZoneInfo(CLOCK_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    time_str = str(date_obj)
            else:
                time_str = "?"

            sender = my_name if entry["out"] else chat_label
            text = entry.get("text") or ""

            media_note = ""
            if entry.get("media_bytes"):
                media_note = " [رسانه]"
            elif entry.get("media_too_large"):
                media_note = " [رسانه — بزرگ‌تر از حد کش]"

            fwd_note = ""
            if entry.get("fwd_from_name"):
                fwd_note = f" [فوروارد از: {entry['fwd_from_name']}]"

            body = text or "(بدون متن)"
            lines.append(f"[{time_str}] {sender}{fwd_note}{media_note}:")
            lines.append(f"  {body}")
            lines.append("")

        content = "\n".join(lines)
        buf = io.BytesIO(content.encode("utf-8"))
        filename = f"chat_{chat_label}_{now_str[:10]}.txt".replace(" ", "_")

        await client.send_file(
            "me",
            buf,
            caption=(
                f"🗂 **مکالمه‌ی PV با {chat_label} دوطرفه حذف شد**\n"
                f"📝 {len(all_entries_sorted)} پیام در فایل ذخیره شد\n"
                f"🕒 {now_str}"
            ),
            file_name=filename,
        )
        log.ok(
            f"Anti-delete: full conversation with {chat_label} "
            f"({len(all_entries_sorted)} messages) saved to file"
        )
    except Exception as e:
        log.error(f"Anti-delete: failed to save full conversation for chat {chat_id}: {e}")


def register(client):
    """Call once with the running TelegramClient to enable anti-delete."""

    @client.on(events.NewMessage())
    async def _cache_handler(event):
        if not event.is_private:
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
                # A message FROM our own control bot (panel menus, status
                # replies, help text) — same reasoning as the .command skip
                # above: it's the project's own UI chatter, not real
                # conversation, so it never enters the cache at all (not
                # just "not reported" — genuinely never stored).
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
            # Edits from THIS project's own control bot (its status/menu
            # messages get edited constantly as panel buttons are pressed)
            # were never cached in the first place (see _cache_handler
            # above) — so there's nothing to refresh or report here either.
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

    @client.on(events.MessageDeleted())
    async def _delete_handler(event):
        if not event.deleted_ids:
            return

        # Pop every deleted id from the cache.
        hits = []
        for msg_id in event.deleted_ids:
            data = _pop(msg_id)
            if data is not None:
                hits.append((msg_id, data))

        if not hits:
            return

        own_hits   = [(mid, d) for mid, d in hits if     d["out"]]
        other_hits = [(mid, d) for mid, d in hits if not d["out"]]

        # ── Full 2-sided PV deletion ──────────────────────────────────────
        # The only way a single delete-event batch can contain messages from
        # BOTH sides is a "delete for everyone" on the whole chat. In that
        # case we save the entire cached conversation to a .txt file instead
        # of sending individual Saved-Messages notifications.
        if own_hits and other_hits:
            chat_id = hits[0][1]["chat_id"]
            # Grab any remaining messages still in the cache for this chat
            # (the batch may not cover all 500 cached entries — pull the rest).
            remaining = list(_chat_caches.get(chat_id, {}).items())
            all_entries = hits + remaining
            asyncio.create_task(_save_full_conversation(client, chat_id, all_entries))
            return

        # ── User deleted their own message ────────────────────────────────
        # You consciously chose to delete it — nothing to report.
        if own_hits and not other_hits:
            return

        # ── Other person deleted their message ────────────────────────────
        # Report each one to Saved Messages, with the usual flood-wait guard.
        for msg_id, data in other_hits:
            await _notify_with_retry(client, data, msg_id)
            await asyncio.sleep(NOTIFY_DELAY)

    log.ok(f"Anti-delete / anti-edit enabled (PV only, {CACHE_LIMIT_PER_CHAT} messages cached per chat)")


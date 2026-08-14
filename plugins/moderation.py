"""Moderation plugins: bulk-deleting the account's own messages."""

import asyncio

from telethon.errors import FloodWaitError

from logger import log
from telegram_layer import client

DELETE_MAX_COUNT = 2000  # safety cap so a typo can't nuke an entire chat history


async def cmd_delete(event, arg):
    """Deletes the last N messages *you* sent in this chat (revoked for
    everyone). Only your own messages — deleting other people's messages
    needs admin rights and isn't what most people mean by this command.
    `arg == "همه"`/"all" wipes every message you've ever sent in this
    chat, looping in small batches instead of one fixed count."""
    from config import PREFIX

    arg_norm = (arg or "").strip().lower()
    if arg_norm in ("همه", "all"):
        await _delete_all(event)
        return

    if not arg or not arg.isdigit():
        await event.edit(f"استفاده: `{PREFIX}حذف <تعداد>` یا `{PREFIX}حذف همه`  (مثلاً `{PREFIX}حذف 100`)")
        return

    count = int(arg)
    if count <= 0:
        await event.edit("عدد باید بزرگ‌تر از صفر باشه.")
        return
    count = min(count, DELETE_MAX_COUNT)

    chat_id = event.chat_id

    ids_to_delete = []
    async for msg in client.iter_messages(chat_id, from_user="me", limit=count + 1):
        if msg.id == event.id:
            continue  # keep the command message itself until we've shown the result
        ids_to_delete.append(msg.id)
        if len(ids_to_delete) >= count:
            break

    if ids_to_delete:
        try:
            await client.delete_messages(chat_id, ids_to_delete, revoke=True)
        except FloodWaitError as e:
            await event.edit(f"⏳ محدودیت تلگرام؛ {e.seconds} ثانیه صبر کن و دوباره امتحان کن.")
            return
        except Exception as e:
            log.error(f"Delete command error: {e}")
            await event.edit(f"⚠️ خطا تو حذف: {e}")
            return

    await event.edit(f"🗑 {len(ids_to_delete)} پیام حذف شد.")
    await asyncio.sleep(3)
    try:
        await event.delete()
    except Exception:
        pass


DELETE_ALL_BATCH = 200      # small enough to comfortably avoid FloodWait
                             # even on a chat with tens of thousands of messages
DELETE_ALL_BATCH_PAUSE = 1.5  # seconds between batches


async def _delete_all(event):
    """Repeats the same from_user='me' fetch-and-delete as cmd_delete, in
    a loop, until a batch comes back empty — i.e. nothing of yours is left
    in this chat. Reports running progress since a chat with thousands of
    messages takes a while. Same safety scope as the normal command: only
    ever your own messages, always revoke=True."""
    chat_id = event.chat_id
    total_deleted = 0

    while True:
        ids_to_delete = []
        async for msg in client.iter_messages(chat_id, from_user="me", limit=DELETE_ALL_BATCH + 1):
            if msg.id == event.id:
                continue
            ids_to_delete.append(msg.id)
            if len(ids_to_delete) >= DELETE_ALL_BATCH:
                break

        if not ids_to_delete:
            break

        try:
            await client.delete_messages(chat_id, ids_to_delete, revoke=True)
        except FloodWaitError as e:
            try:
                await event.edit(f"⏳ محدودیت تلگرام؛ {e.seconds} ثانیه صبر می‌کنم و ادامه می‌دم… (تا الان {total_deleted} پیام حذف شد)")
            except Exception:
                pass
            await asyncio.sleep(e.seconds)
            continue
        except Exception as e:
            log.error(f"Delete-all error after {total_deleted} messages: {e}")
            await event.edit(f"⚠️ خطا بعد از حذف {total_deleted} پیام: {e}")
            return

        total_deleted += len(ids_to_delete)
        try:
            await event.edit(f"🗑 در حال پاک‌سازی… {total_deleted} پیام حذف شد.")
        except Exception:
            pass  # a rate-limited edit shouldn't stop the actual delete loop
        await asyncio.sleep(DELETE_ALL_BATCH_PAUSE)

    await event.edit(f"✅ پاک‌سازی کامل شد — {total_deleted} پیام حذف شد. اثری ازت تو این چت نموند.")
    await asyncio.sleep(4)
    try:
        await event.delete()
    except Exception:
        pass

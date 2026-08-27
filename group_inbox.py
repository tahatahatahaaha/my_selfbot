"""
group_inbox.py — persistent per-contact group inboxes for anti-delete/edit.

Instead of spamming Saved Messages (which becomes a mess fast), deleted
and edited messages go into dedicated private groups, one per contact:

  "📥 حذف‌شده‌ها — <contact name>"   → single persistent group, for ongoing
                                        one-sided deletes and edits from/to
                                        this contact. Created once, reused.

  "🗑 دیلیت دوطرفه — <contact name>" → NEW group every time the whole
                                        conversation is wiped (both sides
                                        deleted at once). Ephemeral snapshot.

Groups are created as a "megagroup" (proper Telegram group, not a channel)
with no other members — only the account owner is in them. That means they
show up in the chat list like normal chats and all content opens natively
(inline photos, streamable video, etc.) with no extra viewer involved.

The mapping (contact display-name → group id) is persisted to a tiny JSON
file ("group_inbox_map.json") next to the bot files, so the bot survives
restarts without creating duplicate groups.
"""

import asyncio
import json
import os

from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.messages import CreateChatRequest

from logger import log

_MAP_FILE = os.path.join(os.path.dirname(__file__), "group_inbox_map.json")
_lock = asyncio.Lock()

# In-memory map: display_name → telegram group/channel id (int)
_inbox_map: dict[str, int] = {}
_loaded = False


def _load():
    global _loaded, _inbox_map
    if _loaded:
        return
    _loaded = True
    if os.path.exists(_MAP_FILE):
        try:
            _inbox_map = json.loads(open(_MAP_FILE, encoding="utf-8").read())
        except Exception:
            _inbox_map = {}


def _save():
    try:
        with open(_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(_inbox_map, f, ensure_ascii=False)
    except Exception as e:
        log.error(f"group_inbox: couldn't save map: {e}")


async def _create_group(client, title: str) -> int:
    """Creates a new private megagroup with the given title and returns
    its numeric chat id. Only the account owner is a member."""
    try:
        # CreateChannel with megagroup=True is the most reliable way to
        # create a group with no other members — CreateChatRequest requires
        # at least one other user_id, which we'd then have to kick.
        result = await client(CreateChannelRequest(
            title=title,
            about="",
            megagroup=True,
        ))
        group_id = result.chats[0].id
        log.ok(f"group_inbox: created group «{title}» (id={group_id})")
        return group_id
    except Exception as e:
        log.error(f"group_inbox: couldn't create group «{title}»: {e}")
        raise


async def get_persistent_inbox(client, contact_label: str) -> int:
    """Returns the id of the one persistent delete/edit inbox group for
    this contact, creating it the first time it's needed."""
    async with _lock:
        _load()
        title = f"📥 حذف‌شده‌ها — {contact_label}"
        key = f"persistent:{contact_label}"
        if key in _inbox_map:
            return _inbox_map[key]
        gid = await _create_group(client, title)
        _inbox_map[key] = gid
        _save()
        return gid


async def create_snapshot_group(client, contact_label: str) -> int:
    """Creates a fresh snapshot group for a full 2-sided deletion event
    (a new group every time — no reuse)."""
    title = f"🗑 دیلیت دوطرفه — {contact_label}"
    return await _create_group(client, title)

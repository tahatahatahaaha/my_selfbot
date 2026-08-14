"""
Command Router — plain exact-command/alias dispatch. No AI, no agent, no
natural-language fallback: if the first word after the prefix isn't one of
the commands below, the message is silently ignored (same as a `.` typo in
normal chat always did).
"""

from telethon.errors import FloodWaitError

from help_text import HELP_TEXT
from logger import log
from plugins import ai_chat, core, media, moderation, panel, social, translate


async def route(event, cmd: str, arg: str, body: str):
    try:
        if cmd == "ping":
            await core.cmd_ping(event)
        elif cmd == "status":
            await core.cmd_status(event)
        elif cmd == "clock":
            await core.cmd_clock(event, arg)
        elif cmd == "font":
            await core.cmd_font(event, arg)
        elif cmd == "quote":
            await media.cmd_quote(event)
        elif cmd == "اسکرین":
            await media.cmd_screen(event)
        elif cmd == "حذف":
            await moderation.cmd_delete(event, arg)
        elif cmd == "تگ":
            if arg == "اعضا":
                await social.cmd_tag_members(event)
            elif arg == "ادمین":
                await social.cmd_tag_admins(event)
            else:
                from config import PREFIX
                await event.edit(f"استفاده: `{PREFIX}تگ اعضا` یا `{PREFIX}تگ ادمین`")
        elif cmd == "پنل":
            await panel.cmd_panel(event)
        elif cmd == "بستن":
            await panel.cmd_panel_close(event)
        elif cmd in ("بلاک", "block"):
            await social.cmd_block(event, arg)
        elif cmd in ("آنبلاک", "unblock"):
            await social.cmd_unblock(event, arg)
        elif cmd == "تاریخ":
            await core.cmd_date(event)
        elif cmd in ("help", "راهنما"):
            await event.edit(HELP_TEXT)
        elif cmd == "ai":
            await ai_chat.cmd_ai(event, arg)
        elif cmd in ("ایدی", "id"):
            await social.cmd_get_id(event, arg)
        elif cmd in ("ترجمه", "translate"):
            # arg is lowercased by the parser (fine for the language word),
            # but body preserves original case — needed so English text to
            # translate doesn't get silently lowercased before sending.
            rest = body.split(maxsplit=1)[1] if len(body.split(maxsplit=1)) > 1 else ""
            rest_parts = rest.split(maxsplit=1)
            lang_word = rest_parts[0] if rest_parts else ""
            text_body = rest_parts[1] if len(rest_parts) > 1 else ""
            await translate.cmd_translate(event, lang_word, text_body)
        # Nothing else matches -> ignored on purpose, same as a `.` typo
        # in normal chat always was. No AI fallback anymore.

    except FloodWaitError as e:
        log.warn(f"Rate limited, wait {e.seconds}s")
    except Exception as e:
        log.error(f"Handler error ({cmd}): {e}")

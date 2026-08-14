import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon.errors import FloodWaitError
from telethon.tl.functions.account import UpdateProfileRequest

from config import CLOCK_INTERVAL_SECONDS, CLOCK_TIMEZONE
from logger import log

STATE_FILE = "clock_state.json"

clock_active = False
_task = None


def _save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"active": clock_active}, f)
    except Exception as e:
        log.error(f"Could not save clock state: {e}")


def _load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"active": False}


async def _set_last_name(client, last_name: str) -> bool:
    """Returns True only if the name was actually updated. Retries once
    after a FloodWait using the exact wait time Telegram gave us — the
    previous version logged "waiting Xs before retrying" but never actually
    retried the request, so updates silently vanished under rate limiting
    (which happens routinely with frequent profile-name changes)."""
    for attempt in (1, 2):
        try:
            await client(UpdateProfileRequest(last_name=last_name))
            return True
        except FloodWaitError as e:
            if attempt == 1:
                log.warn(f"Rate limited by Telegram, waiting {e.seconds}s and retrying once")
                await asyncio.sleep(e.seconds)
                continue
            log.warn(f"Rate limited by Telegram again ({e.seconds}s) — giving up on this update")
            return False
        except Exception as e:
            log.error(f"Could not update last name: {e}")
            return False
    return False


async def start_clock(client) -> tuple[bool, bool]:
    """Returns (started, first_update_ok). started=False means it was
    already on. first_update_ok=False means the clock is now active but
    Telegram rate-limited the very first name update — the loop will keep
    retrying on its own, but the name may not visibly change right away."""
    global clock_active, _task

    if clock_active:
        return False, True

    clock_active = True
    _save_state()

    now = datetime.now(ZoneInfo(CLOCK_TIMEZONE)).strftime("%H:%M")
    first_ok = await _set_last_name(client, now)

    _task = asyncio.create_task(_clock_loop(client, initial_last_time=now if first_ok else None))
    log.ok("Clock turned ON" if first_ok else "Clock turned ON (name update pending — rate limited)")
    return True, first_ok


async def stop_clock(client) -> bool:
    """Disables the clock AND clears the last name field entirely —
    doesn't try to restore whatever name was there before."""
    global clock_active, _task

    was_active = clock_active
    clock_active = False

    if _task:
        _task.cancel()
        _task = None

    cleared = await _set_last_name(client, "")
    _save_state()

    if cleared:
        log.ok("Clock turned OFF, last name cleared")
    else:
        log.warn("Clock turned OFF, but last name couldn't be cleared right now (rate limited) — it may still show the last time")

    return was_active


async def on_selfbot_start(client):
    """Call once when the selfbot process starts — always turns the clock
    ON, regardless of whether it was on before."""
    global clock_active, _task

    clock_active = True
    _save_state()

    now = datetime.now(ZoneInfo(CLOCK_TIMEZONE)).strftime("%H:%M")
    first_ok = await _set_last_name(client, now)

    _task = asyncio.create_task(_clock_loop(client, initial_last_time=now if first_ok else None))
    log.ok("Clock turned ON (auto, selfbot startup)" if first_ok else "Clock turned ON (auto) — first update rate limited, will retry")


async def on_selfbot_stop(client):
    """Call once when the selfbot process is shutting down — always turns
    the clock OFF and clears the last name."""
    global clock_active, _task

    if _task:
        _task.cancel()
        _task = None

    await _set_last_name(client, "")
    clock_active = False
    _save_state()
    log.ok("Clock turned OFF (auto, selfbot shutdown), last name cleared")


async def _clock_loop(client, initial_last_time=None):
    global clock_active

    last_time = initial_last_time
    try:
        while clock_active:
            try:
                now = datetime.now(ZoneInfo(CLOCK_TIMEZONE)).strftime("%H:%M")
            except Exception as e:
                # Common cause on Windows: the IANA timezone database isn't
                # bundled with Python there, so ZoneInfo needs the separate
                # 'tzdata' package (pip install tzdata). Previously this
                # exception was unhandled and silently killed the whole
                # loop — the clock looked "on" but the name never updated.
                log.error(
                    f"Clock: couldn't resolve timezone '{CLOCK_TIMEZONE}': {e}. "
                    "If you're on Windows, run: pip install tzdata"
                )
                await asyncio.sleep(CLOCK_INTERVAL_SECONDS)
                continue

            if now != last_time:
                updated = await _set_last_name(client, now)
                if updated:
                    last_time = now
                # If the update failed (rate limited), we deliberately don't
                # mark last_time as done, so the next tick retries with the
                # same target time instead of silently giving up forever.
            await asyncio.sleep(CLOCK_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        pass


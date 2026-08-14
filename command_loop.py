"""Polls COMMAND_FILE for instructions written by control_bot.py — a
lightweight side channel separate from the Telegram event stream, so the
Bot-API control panel can steer the self-account's plugins."""

import asyncio
import os

import clock
import fontstyle
from config import COMMAND_FILE
from logger import log
from telegram_layer import client


async def run():
    while True:
        try:
            if os.path.exists(COMMAND_FILE):
                with open(COMMAND_FILE, "r", encoding="utf-8") as f:
                    command = f.read().strip()

                if command:
                    log.info(f"Received external command: {command}")

                    if command == "clock_on":
                        await clock.start_clock(client)
                    elif command == "clock_off":
                        await clock.stop_clock(client)
                    elif command.startswith("font:"):
                        key = command.split(":", 1)[1]
                        if key == "off":
                            fontstyle.set_font(None)
                            log.ok("Font turned OFF (via control panel)")
                        elif key in fontstyle.FONTS:
                            fontstyle.set_font(key)
                            log.ok(f"Font set to {key} (via control panel)")
                        else:
                            log.warn(f"Unknown font key from control panel: {key}")

                    with open(COMMAND_FILE, "w", encoding="utf-8") as f:
                        f.write("")
        except Exception as e:
            log.error(f"Command loop error: {e}")

        await asyncio.sleep(0.5)

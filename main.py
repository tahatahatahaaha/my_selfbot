import asyncio

import antidelete
import clock
import command_loop
import event_layer
import plugins  # noqa: F401 -- importing this registers every plugin
from logger import log
from telegram_layer import client

# وارد کردن تابع استارت ربات کنترل (اگر اسم فایلت چیزی غیر از bot است تغییرش بده)
from bot import start_control_bot

antidelete.register(client)
event_layer.register_handlers()


async def main():
    await client.start()
    me = await client.get_me()
    log.ok(f"Connected as: {me.first_name}")
    log.ok("SelfBot started")

    # استارت هم‌زمان کنترل‌بات
    try:
        await start_control_bot()
    except Exception as e:
        log.error(f"Failed to start Control Bot: {e}")

    await clock.on_selfbot_start(client)
    asyncio.create_task(command_loop.run())

    try:
        await client.run_until_disconnected()
    finally:
        await clock.on_selfbot_stop(client)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user")
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from config import BOT_TOKEN, COMMAND_FILE
from fontstyle import FONTS
from help_text import HELP_TEXT
from logger import log


def send_command(command: str):
    with open(COMMAND_FILE, "w", encoding="utf-8") as f:
        f.write(command)


def main_menu_markup():
    keyboard = [
        [
            InlineKeyboardButton("📊 Status", callback_data="status"),
            InlineKeyboardButton("🏓 Ping", callback_data="ping"),
        ],
        [
            InlineKeyboardButton("⏰ Clock ON", callback_data="clock_on"),
            InlineKeyboardButton("⏰ Clock OFF", callback_data="clock_off"),
        ],
        [InlineKeyboardButton("🔤 فونت", callback_data="menu:font")],
        [InlineKeyboardButton("📖 راهنما", callback_data="help")],
    ]
    return InlineKeyboardMarkup(keyboard)


def font_menu_markup():
    keys = list(FONTS.keys())
    rows = []
    for i in range(0, len(keys), 2):
        row = [
            InlineKeyboardButton(FONTS[key][0], callback_data=f"font:{key}")
            for key in keys[i:i + 2]
        ]
        rows.append(row)
    rows.append([InlineKeyboardButton("🚫 خاموش", callback_data="font:off")])
    rows.append([InlineKeyboardButton("« بازگشت", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 پنل کنترل سلف\n\n"
        "ℹ️ حذف پیام و تگ اعضا/ادمین چون باید تو یه گروه/چت خاص اجرا بشن، "
        f"از خودِ سلف با دستورات .حذف و .تگ (یا .پنل) قابل استفاده‌ان، نه از اینجا.",
        reply_markup=main_menu_markup(),
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start = time.monotonic()
    msg = await update.message.reply_text("🏓 …")
    latency_ms = (time.monotonic() - start) * 1000
    await msg.edit_text(f"🏓 Pong! {latency_ms:.0f}ms", parse_mode="Markdown")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🟢 Control Bot فعال است")


async def clock_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    send_command("clock_on")
    await update.message.reply_text("⏰ Clock ON ارسال شد ✅")


async def clock_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    send_command("clock_off")
    await update.message.reply_text("⏰ Clock OFF ارسال شد ✅")


async def font_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔤 یه فونت انتخاب کن:", reply_markup=font_menu_markup())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown", reply_markup=main_menu_markup())


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
        await _handle_callback(query, data)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


async def _handle_callback(query, data):
    if data == "menu:font":
        await query.edit_message_text("🔤 یه فونت انتخاب کن:", reply_markup=font_menu_markup())
        return

    if data == "menu:main":
        await query.edit_message_text("🤖 پنل کنترل سلف", reply_markup=main_menu_markup())
        return

    if data == "help":
        await query.edit_message_text(HELP_TEXT, parse_mode="Markdown", reply_markup=main_menu_markup())
        return

    if data == "status":
        await query.edit_message_text("🟢 Control Bot فعال است", reply_markup=main_menu_markup())
        return
    if data == "ping":
        start = time.monotonic()
        await query.edit_message_text("🏓 …")
        latency_ms = (time.monotonic() - start) * 1000
        await query.edit_message_text(
            f"🏓 Pong! {latency_ms:.0f}ms",
            reply_markup=main_menu_markup(),
            parse_mode="Markdown",
        )
        return

    if data == "clock_on":
        send_command("clock_on")
        await query.edit_message_text("⏰ Clock ON ارسال شد ✅", reply_markup=main_menu_markup())
        return

    if data == "clock_off":
        send_command("clock_off")
        await query.edit_message_text("⏰ Clock OFF ارسال شد ✅", reply_markup=main_menu_markup())
        return

    if data == "font:off":
        send_command("font:off")
        await query.edit_message_text("🔤 استایل فونت خاموش شد ✅", reply_markup=main_menu_markup())
        return

    if data.startswith("font:"):
        key = data.split(":", 1)[1]
        if key in FONTS:
            send_command(f"font:{key}")
            label = FONTS[key][0]
            await query.edit_message_text(f"🔤 فونت روی {label} تنظیم شد ✅", reply_markup=main_menu_markup())
        else:
            await query.edit_message_text("⚠️ فونت نامعتبر بود.", reply_markup=font_menu_markup())
        return


async def error_handler(update, context):
    log.error(f"Bot error: {context.error}")


def build_app():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("clock_on", clock_on))
    app.add_handler(CommandHandler("clock_off", clock_off))
    app.add_handler(CommandHandler("font", font_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(error_handler)

    return app


async def start_control_bot():
    """Starts the control bot asynchronously alongside selfbot."""
    app = build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    log.ok("Control Bot started")
    return app
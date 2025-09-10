# -*- coding: utf-8 -*-
import asyncio
import logging
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CommandHandler,
)
from telegram import Update

import config
from handlers import (
    ai_handlers,
    auth_handler,
    browse_handler,
    task_handler,
    admin_handler,
    admin_package_handler,
    admin_payment_handler,
    admin_user_handler, # Import the new handler
)
from webhook_server import run_webhook_server
import database

# --- راه‌اندازی سیستم لاگینگ ---
def setup_logging():
    """سیستم لاگینگ را با فرمت و سطح مناسب تنظیم می‌کند."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler()
        ]
    )
    # کاهش لاگ‌های اضافی از کتابخانه‌ها
    for logger_name in ["httpx", "telegram", "appwrite", "urllib3"]:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """خطاهای ایجاد شده در حین پردازش آپدیت‌ها را لاگ می‌کند و به کاربر پیام مناسب می‌دهد."""
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
    
    active_conversations = [
        auth_handler.get_auth_handler(),
        task_handler.get_create_task_conv_handler(),
        task_handler.get_edit_task_conv_handler(),
        admin_package_handler.get_new_package_conv_handler(),
        admin_package_handler.get_edit_package_conv_handler()
    ]
    if isinstance(update, Update):
        for conv_handler in active_conversations:
            # This is a simplified check. A more robust solution might be needed
            # if conversations get stuck, but it helps prevent further errors.
            if conv_handler.check_update(update):
                # Try to end the conversation gracefully
                await conv_handler.handle_update(update, context.application, check_result=None, context=context)
                break

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ متأسفم، یک خطای غیرمنتظره رخ داد. لطفاً با ارسال /start دوباره تلاش کنید.")
        except Exception as e:
            logger.error(f"خطای ناشناخته هنگام ارسال پیام خطا به کاربر: {e}", exc_info=True)


async def run_bot() -> None:
    """ربات تلگرام را راه‌اندازی و اجرا می‌کند."""
    application = Application.builder().token(config.BOT_TOKEN).build()

    # --- Conversation Handlers ---
    auth_conv_handler = auth_handler.get_auth_handler()
    create_task_conv_handler = task_handler.get_create_task_conv_handler()
    edit_task_conv_handler = task_handler.get_edit_task_conv_handler()
    new_package_conv_handler = admin_package_handler.get_new_package_conv_handler()
    edit_package_conv_handler = admin_package_handler.get_edit_package_conv_handler()

    # Group 0: Conversations must have the highest priority
    application.add_handler(auth_conv_handler, group=0)
    application.add_handler(create_task_conv_handler, group=0)
    application.add_handler(edit_task_conv_handler, group=0)
    application.add_handler(new_package_conv_handler, group=0)
    application.add_handler(edit_package_conv_handler, group=0)

    # Group 1: Regular commands and messages
    
    # --- Admin Commands ---
    application.add_handler(CommandHandler("resync", admin_handler.resync_command), group=1)
    application.add_handler(CommandHandler("reviewpayments", admin_payment_handler.review_payments_command), group=1)

    # --- Main Menu Buttons ---
    application.add_handler(MessageHandler(filters.Regex('^🔍 مرور پروژه‌ها$'), browse_handler.browse_projects_entry), group=1)
    
    admin_menu_filter = filters.Regex('^(📦 مدیریت پکیج‌ها|📊 مدیریت کاربران|📈 گزارشات|⚙️ تنظیمات ربات)$')
    application.add_handler(MessageHandler(admin_menu_filter, admin_handler.admin_panel_entry), group=1)
    
    # --- CallbackQueryHandlers ---
    application.add_handler(CallbackQueryHandler(browse_handler.button_handler, pattern=r'^(browse_|view_|refresh_|delete_|confirm_delete_)'), group=1)
    application.add_handler(CallbackQueryHandler(admin_package_handler.admin_package_button_handler, pattern=r'^admin_pkg_'), group=1)
    application.add_handler(CallbackQueryHandler(admin_payment_handler.admin_payment_button_handler, pattern=r'^admin_payment_'), group=1)
    
    # --- AI Handler (Last Priority) ---
    ai_text_filter = filters.TEXT & ~filters.COMMAND & ~filters.Regex(r'^(🔍 مرور پروژه‌ها|➕ ساخت تسک جدید)$') & ~admin_menu_filter
    application.add_handler(MessageHandler(ai_text_filter, ai_handlers.ai_handler_entry), group=1)

    application.add_error_handler(error_handler)

    # Run the bot
    try:
        logger.info("ربات تلگرام در حال راه‌اندازی است...")
        await application.initialize()
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("ربات تلگرام با موفقیت اجرا شد.")
        await asyncio.Event().wait()
    finally:
        logger.info("در حال خاموش کردن ربات تلگرام...")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("ربات تلگرام خاموش شد.")

async def run_concurrently():
    """Starts the database setup, then runs the bot and webhook server concurrently."""
    logger.info("شروع بررسی و تنظیم ساختار دیتابیس...")
    await database.setup_database_schemas()
    logger.info("بررسی ساختار دیتابیس کامل شد.")

    bot_task = asyncio.create_task(run_bot())
    webhook_task = asyncio.create_task(run_webhook_server())
    
    await asyncio.gather(bot_task, webhook_task)

if __name__ == "__main__":
    setup_logging()
    try:
        asyncio.run(run_concurrently())
    except KeyboardInterrupt:
        logger.info("برنامه توسط کاربر متوقف شد.")
    except Exception as e:
        logger.critical(f"خطای اصلی در اجرای برنامه: {e}", exc_info=True)


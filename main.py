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
    common,
)
from webhook_server import run_webhook_server
import database

# --- راه‌اندازی سیستم لاگینگ ---
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler()
        ]
    )
    # کاهش لاگ‌های اضافی از کتابخانه‌ها
    for logger_name in ["httpx", "telegram", "urllib3", "appwrite"]:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            # اگر در یک مکالمه هستیم، آن را لغو می‌کنیم تا از حالت قفل خارج شویم
            if context.user_data:
                context.user_data.clear()
            if context.chat_data:
                context.chat_data.clear()
            
            # برای جلوگیری از خطای "Message is not modified"
            error_message = f"⚠️ متأسفم، یک خطای غیرمنتظره رخ داد.\n\n`{context.error}`\n\nلطفاً با ارسال /start دوباره تلاش کنید."
            if update.callback_query:
                await update.callback_query.message.edit_text(error_message)
            else:
                await update.effective_message.reply_text(error_message)

        except Exception as e:
            logger.error(f"خطای ناشناخته هنگام ارسال پیام خطا به کاربر: {e}", exc_info=True)

async def run_bot() -> None:
    """ربات تلگرام را راه‌اندازی و اجرا می‌کند."""
    application = Application.builder().token(config.BOT_TOKEN).build()

    # --- ثبت Handler‌ها ---
    
    # مکالمات (بالاترین اولویت)
    application.add_handler(auth_handler.get_auth_handler())
    application.add_handler(task_handler.get_create_task_conv_handler())
    application.add_handler(task_handler.get_edit_task_conv_handler())
    application.add_handler(admin_handler.get_new_package_conv_handler())
    application.add_handler(admin_handler.get_edit_package_conv_handler())
    
    # دستورات ادمین
    application.add_handler(CommandHandler("resync", admin_handler.resync_command))
    application.add_handler(CommandHandler("reviewpayments", admin_handler.review_payments_command))

    # دکمه‌های شیشه‌ای (CallbackQueryHandlers)
    application.add_handler(CallbackQueryHandler(browse_handler.button_handler, pattern='^(browse_|view_|refresh_|delete_|confirm_delete_)'))
    application.add_handler(CallbackQueryHandler(admin_handler.admin_package_button_handler, pattern='^admin_pkg_'))
    application.add_handler(CallbackQueryHandler(admin_handler.admin_payment_button_handler, pattern='^admin_payment_'))
    
    # پیام‌های متنی (کمترین اولویت)
    application.add_handler(MessageHandler(filters.Regex('^🔍 مرور پروژه‌ها$'), browse_handler.browse_projects_entry))
    application.add_handler(MessageHandler(filters.Regex('^➕ ساخت تسک جدید$'), task_handler.new_task_entry))
    application.add_handler(MessageHandler(filters.Regex('^(📊 مدیریت کاربران|📦 مدیریت پکیج‌ها|📈 گزارشات|⚙️ تنظیمات ربات)$'), admin_handler.admin_panel_entry))

    # هوش مصنوعی (آخرین اولویت برای پیام‌های متنی عمومی)
    ai_text_filter = filters.TEXT & ~filters.COMMAND & ~filters.Regex('^(🔍 مرور پروژه‌ها|➕ ساخت تسک جدید|📊 مدیریت کاربران|📦 مدیریت پکیج‌ها|📈 گزارشات|⚙️ تنظیمات ربات)$')
    application.add_handler(MessageHandler(ai_text_filter, ai_handlers.ai_handler_entry))
    
    application.add_error_handler(error_handler)

    # اجرای ربات
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
    """ابتدا ساختار دیتابیس را بررسی و سپس ربات و وب‌سرور را اجرا می‌کند."""
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


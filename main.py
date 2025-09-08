import asyncio
import logging
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from telegram import Update

import config
from handlers import standard_handlers, ai_handlers
from webhook_server import run_webhook_server

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
    for logger_name in ["httpx", "telegram", "appwrite", "urllib3"]:
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
            await update.effective_message.reply_text("⚠️ متأسفم، یک خطای غیرمنتظره رخ داد. لطفاً با ارسال /start دوباره تلاش کنید.")
        except Exception as e:
            logger.error(f"خطای ناشناخته هنگام ارسال پیام خطا به کاربر: {e}", exc_info=True)

async def run_bot() -> None:
    """ربات تلگرام را راه‌اندازی و اجرا می‌کند."""
    application = Application.builder().token(config.BOT_TOKEN).build()

    # --- ثبت Handler‌ها با اولویت‌بندی صحیح ---
    
    # گروه 0: مکالمه احراز هویت (بالاترین اولویت)
    # این handler به عنوان دروازه اصلی عمل کرده و دستور /start را مدیریت می‌کند.
    auth_handler = standard_handlers.get_auth_handler()
    application.add_handler(auth_handler, group=0)

    # گروه 1: سایر مکالمات و دستورات (برای کاربران احراز هویت شده)
    create_conv_handler = standard_handlers.get_create_task_conv_handler()
    edit_conv_handler = standard_handlers.get_edit_task_conv_handler()
    application.add_handler(create_conv_handler, group=1)
    application.add_handler(edit_conv_handler, group=1)

    application.add_handler(MessageHandler(filters.Regex('^🔍 مرور پروژه‌ها$'), standard_handlers.browse_projects_entry), group=1)
    
    application.add_handler(CallbackQueryHandler(standard_handlers.button_handler), group=1)
    
    # هوش مصنوعی (آخرین اولویت برای پیام‌های متنی)
    ai_text_filter = filters.TEXT & ~filters.COMMAND & ~filters.Regex('^🔍 مرور پروژه‌ها$') & ~filters.Regex('^➕ ساخت تسک جدید$')
    application.add_handler(MessageHandler(ai_text_filter, ai_handlers.ai_handler_entry), group=1)

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
    """ربات و وب‌سرور را به صورت همزمان اجرا می‌کند."""
    # await database.create_bot_users_collection_if_not_exists()
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


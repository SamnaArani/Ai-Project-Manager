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
    TypeHandler,
)
from telegram import Update
from telegram.ext.filters import BaseFilter
from telegram.error import Forbidden

import config
from handlers import (
    auth_handler, 
    ai_handlers, 
    browse_handler, 
    task_handler,
    admin_handler,
    admin_package_handler,
    admin_payment_handler,
    admin_user_handler,
)
from webhook_server import run_webhook_server
import database
from handlers.common import is_user_admin

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
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    # این خط هشدار Appwrite را حذف می‌کند
    logging.getLogger("appwrite").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


async def check_user_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    یک "فایروال" که قبل از همه هندلرها اجرا می‌شود.
    دسترسی کاربر را بر اساس وضعیت is_active او کنترل می‌کند.
    """
    # اگر دستور start باشد، همیشه اجازه عبور می‌دهیم تا کاربر بتواند ثبت نام کند
    if isinstance(update, Update) and update.message and update.message.text == '/start':
        return

    user = update.effective_user
    if not user:
        return # برای آپدیت‌هایی که کاربر ندارند (مثل channel_post)

    user_id = str(user.id)
    
    # ادمین‌ها هرگز مسدود نمی‌شوند
    if await is_user_admin(user_id):
        return

    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )
    
    # اگر کاربر وجود ندارد یا غیرفعال است، جلوی ادامه کار را می‌گیریم
    if not user_doc or not user_doc.get('is_active', False):
        try:
            await update.effective_message.reply_text(
                f"❌ حساب کاربری شما مسدود یا غیرفعال است.\n"
                f"اگر فکر می‌کنید اشتباهی رخ داده، لطفاً با ادمین (@{config.ADMIN_USERNAME}) تماس بگیرید."
            )
        except Forbidden:
             logger.warning(f"کاربر {user_id} ربات را بلاک کرده است. امکان ارسال پیام وجود ندارد.")
        except Exception as e:
            logger.error(f"خطای ناشناخته در ارسال پیام مسدودی به کاربر {user_id}: {e}")
        
        # این دستور مهم، از اجرای سایر هندلرها جلوگیری می‌کند
        raise ApplicationHandlerStop

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # اگر خطا از نوع ApplicationHandlerStop بود (که خودمان برای مسدودسازی ایجاد کردیم)، آن را نادیده می‌گیریم
    if isinstance(context.error, ApplicationHandlerStop):
        return
        
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
    
    # گروه -1: فایروال (بالاترین اولویت برای همه آپدیت‌ها)
    application.add_handler(TypeHandler(Update, check_user_status), group=-1)
    
    # مکالمات (گروه 0)
    auth_conv = auth_handler.get_auth_handler()
    create_task_conv = task_handler.get_create_task_conv_handler()
    edit_task_conv = task_handler.get_edit_task_conv_handler()
    new_pkg_conv = admin_package_handler.get_new_package_conv_handler()
    edit_pkg_conv = admin_package_handler.get_edit_package_conv_handler()

    application.add_handler(auth_conv, group=0)
    application.add_handler(create_task_conv, group=0)
    application.add_handler(edit_task_conv, group=0)
    application.add_handler(new_pkg_conv, group=0)
    application.add_handler(edit_pkg_conv, group=0)
    
    # دستورات ادمین (گروه 1)
    application.add_handler(CommandHandler("resync", admin_handler.resync_command), group=1)
    application.add_handler(CommandHandler("reviewpayments", admin_payment_handler.review_payments_command), group=1)

    # دکمه‌های منوی اصلی (گروه 1)
    application.add_handler(MessageHandler(filters.Regex('^🔍 مرور پروژه‌ها$'), browse_handler.browse_projects_entry), group=1)
    application.add_handler(MessageHandler(filters.Regex('^📊 مدیریت کاربران$'), admin_user_handler.manage_users_entry), group=1)
    application.add_handler(MessageHandler(filters.Regex('^📦 مدیریت پکیج‌ها$'), admin_package_handler.manage_packages_entry), group=1)
    
    # CallbackQueryHandlers (گروه 1)
    application.add_handler(CallbackQueryHandler(browse_handler.button_handler, pattern='^(browse|view|refresh|delete|confirm)_'), group=1)
    application.add_handler(CallbackQueryHandler(admin_package_handler.admin_package_button_handler, pattern=r'^admin_pkg_'), group=1)
    application.add_handler(CallbackQueryHandler(admin_payment_handler.admin_payment_button_handler, pattern=r'^admin_payment_'), group=1)
    application.add_handler(CallbackQueryHandler(admin_user_handler.admin_user_button_handler, pattern=r'^admin_user_'), group=1)

    # هوش مصنوعی (آخرین اولویت برای پیام‌های متنی)
    ai_text_filter = filters.TEXT & ~filters.COMMAND
    application.add_handler(MessageHandler(ai_text_filter, ai_handlers.ai_handler_entry), group=2)

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

class ApplicationHandlerStop(Exception):
    """Exception to stop further handlers from processing an update."""
    pass


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


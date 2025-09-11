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
from telegram.error import Forbidden, BadRequest

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
    support_handler,
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
    logging.getLogger("appwrite").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


class ApplicationHandlerStop(Exception):
    """Exception to stop further handlers from processing an update."""
    pass

async def check_user_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    یک "فایروال" که قبل از همه هندلرها اجرا می‌شود.
    دسترسی کاربر را بر اساس وضعیت is_active او کنترل می‌کند.
    """
    # برای هر آپدیت جدید، فلگ را ریست می‌کنیم تا از تأثیر آن بر آپدیت‌های بعدی جلوگیری شود
    context.chat_data.pop('block_message_sent', None)

    user = update.effective_user
    if not user:
        return

    user_id = str(user.id)

    # ادمین‌ها هرگز مسدود نمی‌شوند
    if await is_user_admin(user_id):
        return

    # دستور /start توسط هندلر خودش مدیریت می‌شود، پس اینجا به آن کاری نداریم
    if update.message and update.message.text and update.message.text.startswith('/start'):
        return
    
    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )
    
    if not user_doc or not user_doc.get('is_active', False):
        logger.warning(f"دسترسی برای کاربر {user_id} رد شد (is_active: {user_doc.get('is_active') if user_doc else 'N/A'}).")
        
        # یک فلگ تنظیم می‌کنیم تا توابع دیگر (مثل get_user_token) پیام تکراری ارسال نکنند
        context.chat_data['block_message_sent'] = True
        
        if update.effective_message:
            try:
                # اگر کاربر روی دکمه‌ای کلیک کرده، آن را با پیام خطا جایگزین می‌کنیم
                if update.callback_query:
                    await update.callback_query.edit_message_text(
                        "❌ شما دسترسی ندارید. حساب کاربری شما مسدود شده است."
                    )
                # پیام اصلی مسدودیت را در چت خصوصی ارسال می‌کنیم
                await context.bot.send_message(
                    chat_id=user.id,
                    text=(
                        f"❌ حساب کاربری شما مسدود یا غیرفعال است.\n"
                        f"اگر فکر می‌کنید اشتباهی رخ داده، لطفاً با ادمین (@{config.ADMIN_USERNAME}) تماس بگیرید."
                    )
                )
            except Exception:
                # از ثبت لاگ‌های تکراری برای خطاهای قابل پیش‌بینی جلوگیری می‌کنیم
                pass
        
        raise ApplicationHandlerStop
    

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, ApplicationHandlerStop):
        return
        
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            if context.user_data: context.user_data.clear()
            if context.chat_data: context.chat_data.clear()
            await update.effective_message.reply_text("⚠️ متأسفم، یک خطای غیرمنتظره رخ داد. لطفاً با ارسال /start دوباره تلاش کنید.")
        except Exception as e:
            logger.error(f"خطای ناشناخته هنگام ارسال پیام خطا به کاربر: {e}", exc_info=True)


async def run_bot() -> None:
    """ربات تلگرام را راه‌اندازی و اجرا می‌کند."""
    application = Application.builder().token(config.BOT_TOKEN).build()
    
    # --- ثبت Handler‌ها با اولویت‌بندی صحیح ---
    
    # گروه -1: فایروال (بالاترین اولویت)
    application.add_handler(TypeHandler(Update, check_user_status), group=-1)

    # گروه 0: مکالمات (بعد از فایروال)
    application.add_handler(auth_handler.get_auth_handler(), group=0)
    application.add_handler(task_handler.get_create_task_conv_handler(), group=0)
    application.add_handler(task_handler.get_edit_task_conv_handler(), group=0)
    application.add_handler(admin_package_handler.get_new_package_conv_handler(), group=0)
    application.add_handler(admin_package_handler.get_edit_package_conv_handler(), group=0)
    application.add_handler(support_handler.get_user_support_conv_handler(), group=0)
    application.add_handler(support_handler.get_admin_reply_conv_handler(), group=0)
    application.add_handler(admin_user_handler.get_send_direct_message_conv_handler(), group=0)
    
    # گروه 1: دستورات و دکمه‌ها
    application.add_handler(CommandHandler("resync", admin_handler.resync_command), group=1)
    application.add_handler(CommandHandler("reviewpayments", admin_payment_handler.review_payments_command), group=1)
    
    # دکمه‌های منوی اصلی کاربر و ادمین
    application.add_handler(MessageHandler(filters.Regex('^🔍 مرور پروژه‌ها$'), browse_handler.browse_projects_entry), group=1)
    application.add_handler(MessageHandler(filters.Regex('^📞 پشتیبانی$'), support_handler.support_entry), group=1)
    application.add_handler(MessageHandler(filters.Regex('^📊 مدیریت کاربران$'), admin_user_handler.manage_users_entry), group=1)
    application.add_handler(MessageHandler(filters.Regex('^📦 مدیریت پکیج‌ها$'), admin_package_handler.manage_packages_entry), group=1)
    application.add_handler(MessageHandler(filters.Regex(r'^✉️ پیام‌ها'), admin_handler.admin_panel_entry), group=1)
    application.add_handler(MessageHandler(filters.Regex('^📈 گزارشات$'), admin_handler.admin_panel_entry), group=1)

    # هندلرهای مربوط به کلیک روی دکمه‌های شیشه‌ای
    application.add_handler(CallbackQueryHandler(browse_handler.button_handler, pattern='^(browse|view|refresh|delete|confirm)_'), group=1)
    application.add_handler(CallbackQueryHandler(admin_package_handler.admin_package_button_handler, pattern=r'^admin_pkg_'), group=1)
    application.add_handler(CallbackQueryHandler(admin_payment_handler.admin_payment_button_handler, pattern=r'^admin_payment_'), group=1)
    application.add_handler(CallbackQueryHandler(admin_user_handler.admin_user_button_handler, pattern=r'^admin_user_(page|view|toggle|delete|confirm|back)_'), group=1)
    application.add_handler(CallbackQueryHandler(support_handler.admin_button_handler, pattern=r'^support_admin_'), group=1)

    # گروه 2: هوش مصنوعی (آخرین اولویت)
    menu_button_texts = [
        '^🔍 مرور پروژه‌ها$', '^📞 پشتیبانی$', '^📊 مدیریت کاربران$',
        '^📦 مدیریت پکیج‌ها$', r'^✉️ پیام‌ها', '^📈 گزارشات$',
        '^➕ ساخت تسک جدید$',
    ]
    menu_filters = filters.Regex('|'.join(menu_button_texts))
    ai_text_filter = filters.TEXT & ~filters.COMMAND & ~menu_filters
    application.add_handler(MessageHandler(ai_text_filter, ai_handlers.ai_handler_entry), group=2)

    application.add_error_handler(error_handler)

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


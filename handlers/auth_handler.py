# -*- coding: utf-8 -*-
import asyncio
import logging
from datetime import datetime, timezone

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

import config
import database
import clickup_api
from . import common

logger = logging.getLogger(__name__)

# --- وضعیت‌های مکالمه جدید برای ثبت نام ---
SELECTING_PACKAGE, AWAITING_PAYMENT_DETAILS, GET_CLICKUP_TOKEN = range(11, 14)

async def _send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بر اساس نقش کاربر (ادمین/عادی)، منوی اصلی مناسب را ارسال می‌کند."""
    user_id = str(update.effective_user.id)
    
    if await common.is_user_admin(user_id):
        # منوی ادمین
        admin_menu_keyboard = [
            [KeyboardButton("📊 مدیریت کاربران"), KeyboardButton("📦 مدیریت پکیج‌ها")],
            [KeyboardButton("📈 گزارشات"), KeyboardButton("⚙️ تنظیمات ربات")]
        ]
        reply_markup = ReplyKeyboardMarkup(admin_menu_keyboard, resize_keyboard=True)
        await update.message.reply_text("سلام ادمین! به پنل مدیریت خوش آمدید.", reply_markup=reply_markup)
    else:
        # منوی کاربر عادی
        main_menu_keyboard = [[KeyboardButton("🔍 مرور پروژه‌ها")], [KeyboardButton("➕ ساخت تسک جدید")]]
        reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True, one_time_keyboard=False)
        await update.message.reply_text("سلام مجدد! به PIXEELL خوش آمدید. لطفاً یک گزینه را انتخاب کنید:", reply_markup=reply_markup)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """جریان ثبت نام را با نمایش پکیج‌ها شروع می‌کند و ادمین را مستقیماً به پنل هدایت می‌کند."""
    user_id = str(update.effective_user.id)
    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )

    # اولویت اول: بررسی اینکه آیا کاربر ادمین است یا خیر
    if user_doc and user_doc.get('is_admin'):
        await _send_main_menu(update, context)
        return ConversationHandler.END

    # اولویت دوم: بررسی کاربر عادی که قبلاً ثبت نام کرده
    if user_doc and user_doc.get('clickup_token') and user_doc.get('is_active'):
        await _send_main_menu(update, context)
        return ConversationHandler.END

    # اگر هیچکدام از موارد بالا نبود، فرآیند ثبت نام جدید را شروع کن
    if not user_doc:
        await asyncio.to_thread(
            database.create_document,
            config.APPWRITE_DATABASE_ID,
            config.BOT_USERS_COLLECTION_ID,
            {
                'telegram_id': user_id, 
                'is_active': False, 
                'is_admin': False,
                'created_at': datetime.now(timezone.utc).isoformat()
            },
            doc_id=user_id
        )

    packages = await asyncio.to_thread(
        database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, [database.Query.equal("is_active", [True])]
    )

    if not packages:
        await update.message.reply_text("👋 سلام! در حال حاضر پکیج فعالی برای ثبت نام وجود ندارد. لطفاً بعداً دوباره تلاش کنید.")
        return ConversationHandler.END

    keyboard = []
    text = "👋 سلام! به ربات مدیریت پروژه PIXEELL خوش آمدید.\n\nلطفاً یکی از پکیج‌های زیر را انتخاب کنید:\n\n"
    for pkg in packages:
        price = "رایگان" if pkg['monthly_price'] == 0 else f"{pkg['monthly_price']:,} تومان/ماه"
        text += f"🔹 *{pkg['package_name']}* ({price})\n{pkg.get('package_description', '')}\n\n"
        keyboard.append([InlineKeyboardButton(f"{pkg['package_name']} ({price})", callback_data=f"select_pkg_{pkg['$id']}")])
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECTING_PACKAGE

async def package_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """پردازش پکیج انتخاب شده توسط کاربر."""
    query = update.callback_query
    await query.answer()
    
    package_id = query.data.split('_')[-1]
    context.user_data['selected_package_id'] = package_id

    package_doc_list = await asyncio.to_thread(
        database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, [database.Query.equal("$id", [package_id])]
    )
    if not package_doc_list:
        await query.edit_message_text("❌ خطایی رخ داد: پکیج انتخاب شده یافت نشد.")
        return ConversationHandler.END
    
    package = package_doc_list[0]

    if package['monthly_price'] == 0:
        await query.edit_message_text(
            "شما پکیج رایگان را انتخاب کردید.\n\n"
            "برای ادامه، لطفاً توکن API کلیک‌اپ (ClickUp API Token) خود را ارسال کنید."
        )
        return GET_CLICKUP_TOKEN
    else:
        await query.edit_message_text(
            f"شما پکیج *{package['package_name']}* را انتخاب کردید.\n\n"
            "لطفاً پس از واریز، اطلاعات پرداخت (مانند شماره تراکنش یا کد رهگیری) را در همینجا ارسال کنید تا درخواست شما توسط ادمین بررسی شود.",
            parse_mode='Markdown'
        )
        return AWAITING_PAYMENT_DETAILS

async def payment_details_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """اطلاعات پرداخت را دریافت و در دیتابیس ذخیره می‌کند."""
    user_id = str(update.effective_user.id)
    package_id = context.user_data.get('selected_package_id')
    receipt_details = update.message.text

    if not package_id:
        await update.message.reply_text("خطا: پکیجی انتخاب نشده است. لطفاً با /start مجدداً شروع کنید.")
        return ConversationHandler.END

    payment_data = {
        'telegram_id': user_id,
        'package_id': package_id,
        'receipt_details': receipt_details,
        'request_date': datetime.now(timezone.utc).isoformat(),
        'status': 'pending'
    }
    
    try:
        await asyncio.to_thread(
            database.create_document,
            config.APPWRITE_DATABASE_ID,
            config.PAYMENT_REQUESTS_COLLECTION_ID,
            payment_data
        )
        await update.message.reply_text(
            "✅ اطلاعات پرداخت شما با موفقیت ثبت شد.\n\n"
            "درخواست شما به زودی توسط ادمین بررسی و نتیجه از طریق همین ربات به شما اطلاع داده خواهد شد."
        )
    except Exception as e:
        logger.error(f"خطا در ثبت اطلاعات پرداخت برای کاربر {user_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ متاسفانه در ثبت اطلاعات شما خطایی رخ داد. لطفاً با پشتیبانی تماس بگیرید.")

    context.user_data.clear()
    return ConversationHandler.END

async def token_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """توکن را برای پکیج رایگان دریافت و پردازش می‌کند."""
    token = update.message.text.strip()
    user_id = str(update.effective_user.id)
    package_id = context.user_data.get('selected_package_id')

    placeholder_message = await update.message.reply_text("در حال بررسی توکن...")

    is_valid = await asyncio.to_thread(clickup_api.validate_token, token)
    if is_valid:
        await placeholder_message.edit_text("توکن معتبر است. در حال فعال‌سازی حساب و همگام‌سازی اولیه...")
        
        # اگر کاربر پکیج رایگان را در همین مکالمه انتخاب کرده باشد، آن را ثبت می‌کنیم
        update_data = {'clickup_token': token, 'is_active': True}
        if package_id:
            update_data['package_id'] = package_id

        await asyncio.to_thread(
            database.upsert_document,
            config.APPWRITE_DATABASE_ID,
            config.BOT_USERS_COLLECTION_ID,
            'telegram_id',
            user_id,
            update_data
        )
        
        sync_success = await asyncio.to_thread(clickup_api.sync_all_user_data, token, user_id)
        if not sync_success:
            await placeholder_message.edit_text("⚠️ حساب شما فعال شد اما در همگام‌سازی اولیه خطایی رخ داد. لطفاً با دستور /resync مجدداً تلاش کنید.")
        else:
            await placeholder_message.edit_text("✅ حساب شما با موفقیت فعال و همگام‌سازی شد!")
        
        await _send_main_menu(update, context)
        
        context.user_data.clear()
        return ConversationHandler.END
    else:
        await placeholder_message.edit_text("❌ توکن ارسال شده نامعتبر است. لطفاً دوباره ارسال کنید یا با /cancel لغو کنید.")
        return GET_CLICKUP_TOKEN

async def cancel_auth_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """فرآیند ثبت نام را لغو می‌کند."""
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.message.edit_text("عملیات ثبت نام لغو شد.")
    else:
        await update.message.reply_text("عملیات ثبت نام لغو شد. برای شروع مجدد /start را ارسال کنید.")
    return ConversationHandler.END

def get_auth_handler() -> ConversationHandler:
    """هندلر مکالمه برای کل فرآیند ثبت نام (انتخاب پکیج، پرداخت، دریافت توکن)."""
    return ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            SELECTING_PACKAGE: [
                CallbackQueryHandler(package_selected, pattern='^select_pkg_')
            ],
            AWAITING_PAYMENT_DETAILS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payment_details_received)
            ],
            GET_CLICKUP_TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, token_received)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_auth_conversation)],
        per_message=False
    )


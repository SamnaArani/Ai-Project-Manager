# -*- coding: utf-8 -*-
import asyncio
import logging
from datetime import datetime, timezone

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
import database
import clickup_api

logger = logging.getLogger(__name__)

# --- وضعیت مکالمه ---
GET_CLICKUP_TOKEN = 0

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ورودی اصلی ربات، کاربر را بررسی و در صورت نیاز فرآیند ثبت‌نام را آغاز می‌کند."""
    user_id = str(update.effective_user.id)
    user_doc = await asyncio.to_thread(
        database.get_single_document,
        config.APPWRITE_DATABASE_ID,
        config.BOT_USERS_COLLECTION_ID,
        'telegram_id',
        user_id,
    )

    if user_doc and user_doc.get('clickup_token') and user_doc.get('is_active'):
        main_menu_keyboard = [
            [KeyboardButton("🔍 مرور پروژه‌ها")],
            [KeyboardButton("➕ ساخت تسک جدید")],
        ]
        reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True, one_time_keyboard=False)
        await update.message.reply_text("سلام مجدد! به PIXEELL خوش آمدید. لطفاً یک گزینه را انتخاب کنید:", reply_markup=reply_markup)
        return ConversationHandler.END
    else:
        if not user_doc:
            await asyncio.to_thread(
                database.create_document,
                config.APPWRITE_DATABASE_ID,
                config.BOT_USERS_COLLECTION_ID,
                {
                    'telegram_id': user_id,
                    'is_active': False,
                    'is_admin': False,
                    'created_at': datetime.now(timezone.utc).isoformat(),
                },
            )
        await update.message.reply_text(
            "👋 سلام! به ربات مدیریت پروژه PIXEELL خوش آمدید.\n\n"
            "برای شروع، لطفاً توکن API کلیک‌اپ (ClickUp API Token) خود را ارسال کنید."
        )
        return GET_CLICKUP_TOKEN

async def token_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """توکن دریافتی را اعتبارسنجی، ذخیره و اطلاعات کاربر را همگام‌سازی می‌کند."""
    token = update.message.text.strip()
    user_id = str(update.effective_user.id)

    placeholder_message = await update.message.reply_text("در حال بررسی توکن...")

    is_valid = await asyncio.to_thread(clickup_api.validate_token, token)

    if is_valid:
        await placeholder_message.edit_text("توکن معتبر است. در حال ذخیره اطلاعات...")
        await asyncio.to_thread(
            database.upsert_document,
            config.APPWRITE_DATABASE_ID,
            config.BOT_USERS_COLLECTION_ID,
            'telegram_id',
            user_id,
            {'clickup_token': token, 'is_active': True},
        )
        
        await placeholder_message.edit_text("توکن شما با موفقیت ذخیره شد. در حال همگام‌سازی اولیه اطلاعات... ⏳")
        sync_success = await asyncio.to_thread(clickup_api.sync_all_user_data, token, user_id)

        if not sync_success:
            await placeholder_message.edit_text("❌ در همگام‌سازی اولیه اطلاعات خطایی رخ داد. لطفاً با دستور /resync مجدداً تلاش کنید.")
            return ConversationHandler.END

        await placeholder_message.edit_text("✅ همگام‌سازی با موفقیت انجام شد!")
        main_menu_keyboard = [[KeyboardButton("🔍 مرور پروژه‌ها")], [KeyboardButton("➕ ساخت تسک جدید")]]
        reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)
        await update.message.reply_text("حالا می‌توانید از تمام امکانات ربات استفاده کنید:", reply_markup=reply_markup)
        return ConversationHandler.END
    else:
        await placeholder_message.edit_text(
            "❌ توکن ارسال شده نامعتبر است. لطفاً دوباره ارسال کنید یا با /cancel لغو کنید."
        )
        return GET_CLICKUP_TOKEN

async def cancel_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """فرآیند احراز هویت را لغو می‌کند."""
    await update.message.reply_text("عملیات ثبت نام لغو شد. برای شروع مجدد /start را ارسال کنید.")
    return ConversationHandler.END

def get_auth_handler() -> ConversationHandler:
    """ConversationHandler مربوط به احراز هویت را برمی‌گرداند."""
    token_filter = (
        filters.TEXT & 
        ~filters.COMMAND & 
        ~filters.Regex('^🔍 مرور پروژه‌ها$') & 
        ~filters.Regex('^➕ ساخت تسک جدید$')
    )
    return ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            GET_CLICKUP_TOKEN: [MessageHandler(token_filter, token_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel_auth)],
    )

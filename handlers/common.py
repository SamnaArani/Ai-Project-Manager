# -*- coding: utf-8 -*-
import asyncio
import logging
from telegram import Update, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest
import config
import database

logger = logging.getLogger(__name__)

async def generic_cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """یک تابع عمومی برای لغو هر گونه مکالمه فعال."""
    message_text = "عملیات لغو شد."
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.message.edit_text(message_text)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                await update.effective_chat.send_message(message_text)
    elif update.message:
        await update.message.reply_text(message_text)

    # پاک کردن داده‌های مربوط به مکالمه
    context.user_data.clear()
    context.chat_data.clear()

    return ConversationHandler.END

async def get_user_token(user_id: str, update: Update, context: ContextTypes.DEFAULT_TYPE, notify_user: bool = True) -> str | None:
    """
    توکن کلیک‌اپ کاربر را از دیتابیس دریافت می‌کند.
    در صورت عدم وجود توکن، می‌تواند به کاربر اطلاع دهد.
    """
    if 'clickup_token' in context.user_data:
        return context.user_data['clickup_token']
        
    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )
    if user_doc and user_doc.get('clickup_token'):
        context.user_data['clickup_token'] = user_doc['clickup_token']
        return user_doc['clickup_token']
    else:
        if notify_user:
            target = update.callback_query.message if update.callback_query else update.message
            if target:
                await target.reply_text("توکن ClickUp شما یافت نشد یا حساب شما غیرفعال است. لطفاً با دستور /start ثبت نام کنید.")
        return None

async def is_user_admin(user_id: str) -> bool:
    """بررسی می‌کند که آیا کاربر ادمین است یا خیر."""
    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )
    return user_doc and user_doc.get('is_admin', False)

async def send_or_edit(update: Update, text: str, reply_markup: InlineKeyboardMarkup = None, parse_mode='Markdown'):
    """
    یک پیام را در صورت امکان ویرایش می‌کند، در غیر این صورت به عنوان یک پیام جدید ارسال می‌کند.
    """
    # اگر آپدیت از نوع پیام متنی باشد، به آن پاسخ می‌دهیم
    if isinstance(update, Update) and update.message:
        target = update.message
        await target.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return
        
    # اگر آپدیت از نوع دکمه شیشه‌ای باشد، پیام را ویرایش می‌کنیم
    try:
        target = update.callback_query.message if hasattr(update, 'callback_query') and update.callback_query else update.message
        if hasattr(update, 'callback_query') and update.callback_query:
            await target.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await target.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning(f"Could not send or edit message: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred in send_or_edit: {e}")
        # Fallback to sending a new message if editing fails for other reasons
        if hasattr(update, 'effective_chat') and update.effective_chat:
            await update.effective_chat.send_message(text, reply_markup=reply_markup, parse_mode=parse_mode)


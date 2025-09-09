# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timezone
import asyncio

from telegram import Update, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import BadRequest

import config
import database

logger = logging.getLogger(__name__)

async def get_user_token(user_id: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """توکن کاربر را از دیتابیس دریافت می‌کند و در context ذخیره می‌کند."""
    if 'clickup_token' in context.user_data:
        return context.user_data['clickup_token']
        
    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )
    if user_doc and user_doc.get('clickup_token'):
        context.user_data['clickup_token'] = user_doc['clickup_token']
        return user_doc['clickup_token']
    else:
        target = update.callback_query.message if update.callback_query else update.message
        if target:
            await target.reply_text("توکن ClickUp شما یافت نشد یا حساب شما غیرفعال است. لطفاً با دستور /start ثبت نام کنید.")
        return None

def parse_due_date(due_date_str: str) -> int | None:
    """رشته تاریخ را به فرمت timestamp کلیک‌اپ تبدیل می‌کند."""
    try:
        date_obj = datetime.strptime(due_date_str, "%Y-%m-%d")
        date_obj_utc = date_obj.replace(hour=12, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        return int(date_obj_utc.timestamp() * 1000)
    except (ValueError, TypeError):
        logger.warning(f"Could not parse date string: {due_date_str}")
        return None

async def send_or_edit(update: Update, text: str, reply_markup: InlineKeyboardMarkup = None, parse_mode='Markdown'):
    """یک پیام را در صورت امکان ویرایش و در غیر این صورت ارسال می‌کند."""
    try:
        target = update.callback_query.message if update.callback_query else update.message
        if update.callback_query:
            await target.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await target.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning(f"Could not send or edit message: {e}")

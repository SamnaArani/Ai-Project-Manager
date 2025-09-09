# -*- coding: utf-8 -*-
import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

import clickup_api
from . import common

logger = logging.getLogger(__name__)

async def resync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """همگام‌سازی مجدد و کامل اطلاعات کاربر از کلیک‌اپ را به صورت دستی فعال می‌کند."""
    user_id = str(update.effective_user.id)
    token = await common.get_user_token(user_id, update, context)
    if not token:
        await update.message.reply_text("ابتدا باید با دستور /start ثبت نام کنید.")
        return

    await update.message.reply_text("شروع همگام‌سازی مجدد اطلاعات از ClickUp... این فرآیند ممکن است چند لحظه طول بکشد. ⏳")
    try:
        sync_success = await asyncio.to_thread(clickup_api.sync_all_user_data, token, user_id)
        if sync_success:
            await update.message.reply_text("✅ همگام‌سازی مجدد با موفقیت انجام شد. اکنون همه چیز باید به درستی کار کند.")
        else:
            await update.message.reply_text("❌ در هنگام همگام‌سازی مجدد خطایی رخ داد.")
    except Exception as e:
        logger.error(f"خطا در اجرای دستور /resync برای کاربر {user_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ یک خطای غیرمنتظره در حین همگام‌سازی رخ داد.")

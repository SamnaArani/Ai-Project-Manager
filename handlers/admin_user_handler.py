# -*- coding: utf-8 -*-
import logging
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

async def manage_users_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for the user management section."""
    await update.message.reply_text("شما وارد بخش مدیریت کاربران شدید. (این قابلیت در حال توسعه است)")

# -*- coding: utf-8 -*-
import asyncio
import logging
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes

import clickup_api
from . import common, admin_package_handler, admin_user_handler

logger = logging.getLogger(__name__)

# --- Admin Panel Entry ---

async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the main admin menu with custom buttons."""
    admin_keyboard = [
        [KeyboardButton("📦 مدیریت پکیج‌ها"), KeyboardButton("📊 مدیریت کاربران")],
        [KeyboardButton("📈 گزارشات"), KeyboardButton("⚙️ تنظیمات ربات")]
    ]
    reply_markup = ReplyKeyboardMarkup(admin_keyboard, resize_keyboard=True)
    await update.message.reply_text("پنل مدیریت:", reply_markup=reply_markup)

async def admin_panel_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Acts as a router for the main admin menu buttons, delegating tasks
    to the appropriate specialized handlers.
    """
    user_id = str(update.effective_user.id)
    if not await common.is_user_admin(user_id):
        return

    text = update.message.text
    if text == "📦 مدیریت پکیج‌ها":
        await admin_package_handler.manage_packages_entry(update, context)
    elif text == "📊 مدیریت کاربران":
        await admin_user_handler.manage_users_entry(update, context) 
    elif text == "📈 گزارشات":
        await update.message.reply_text("شما وارد بخش گزارشات شدید. (در حال توسعه)")
    elif text == "⚙️ تنظیمات ربات":
        await update.message.reply_text("شما وارد بخش تنظیمات ربات شدید. (در حال توسعه)")

async def resync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually triggers a full data sync for the user from ClickUp."""
    user_id = str(update.effective_user.id)
    token = await common.get_user_token(user_id, update, context, notify_user=True)
    if not token:
        # get_user_token already notified the user if needed
        return

    await update.message.reply_text("شروع همگام‌سازی مجدد اطلاعات از ClickUp... ⏳")
    try:
        sync_success = await asyncio.to_thread(clickup_api.sync_all_user_data, token, user_id)
        if sync_success:
            await update.message.reply_text("✅ همگام‌سازی مجدد با موفقیت انجام شد.")
        else:
            await update.message.reply_text("❌ در هنگام همگام‌سازی مجدد خطایی رخ داد.")
    except Exception as e:
        logger.error(f"Error during /resync for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ یک خطای غیرمنتظره در حین همگام‌سازی رخ داد.")


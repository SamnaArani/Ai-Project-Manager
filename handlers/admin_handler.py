# -*- coding: utf-8 -*-
import asyncio
import logging
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes
from appwrite.query import Query

import config
import database
import clickup_api
from . import common, admin_package_handler, admin_user_handler, support_handler, admin_payment_handler

logger = logging.getLogger(__name__)

# --- Admin Panel ---

async def show_admin_panel(admin_id: str, context: ContextTypes.DEFAULT_TYPE):
    """
    Displays or sends the main admin menu with dynamic buttons.
    This function is designed to be called from anywhere, including for live updates.
    """
    unread_tickets = await asyncio.to_thread(
        database.get_documents,
        config.APPWRITE_DATABASE_ID,
        config.SUPPORT_TICKETS_COLLECTION_ID,
        [Query.equal("status", ["unread"])]
    )
    unread_count = len(unread_tickets)
    
    messages_button_text = "✉️ پیام‌ها"
    if unread_count > 0:
        messages_button_text += f" ({unread_count})"

    admin_keyboard = [
        [KeyboardButton("📦 مدیریت پکیج‌ها"), KeyboardButton("📊 مدیریت کاربران")],
        [KeyboardButton(messages_button_text), KeyboardButton("💳 بررسی پرداخت‌ها")],
        [KeyboardButton("📈 گزارشات")]
    ]
    reply_markup = ReplyKeyboardMarkup(admin_keyboard, resize_keyboard=True, one_time_keyboard=False)
    
    try:
        await context.bot.send_message(
            chat_id=admin_id,
            text="پنل مدیریت:",
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Could not send admin panel to {admin_id}: {e}")

async def start_for_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for an admin using /start or similar commands."""
    admin_id = str(update.effective_user.id)
    await show_admin_panel(admin_id, context)

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
    elif text.startswith("✉️ پیام‌ها"):
        await support_handler.manage_messages_entry(update, context)
    elif text == "💳 بررسی پرداخت‌ها":
        await admin_payment_handler.manage_payments_entry(update, context)
    elif text == "📈 گزارشات":
        await update.message.reply_text("شما وارد بخش گزارشات شدید. (در حال توسعه)")

async def resync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually triggers a full data sync for the user from ClickUp."""
    user_id = str(update.effective_user.id)
    token = await common.get_user_token(user_id, update, context, notify_user=True)
    if not token:
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

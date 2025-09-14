# -*- coding: utf-8 -*-
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import config
import database
from . import common

logger = logging.getLogger(__name__)

def _format_usage(used: int, limit: int) -> str:
    """Formats the usage string."""
    if limit == 0:
        return f"{used} / نامحدود"
    return f"{used} / {limit}"

async def profile_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the user's profile information."""
    user_id = str(update.effective_user.id)
    
    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )

    if not user_doc:
        await update.message.reply_text("خطا: اطلاعات کاربری شما یافت نشد.")
        return

    package_name = "پکیج پایه (رایگان)"
    package_details = []
    
    if package_id := user_doc.get('package_id'):
        pkg_doc = await asyncio.to_thread(
            database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_id
        )
        if pkg_doc:
            package_name = pkg_doc.get('package_name', 'نامشخص')
            
            if pkg_doc.get('allow_ai_chat') or pkg_doc.get('allow_ai_commands'):
                package_details.append("\n📊 *مصرف هوش مصنوعی:*\n")
                
                # Chat Usage
                daily_chat_used = user_doc.get('daily_chat_usage', 0)
                daily_chat_limit = pkg_doc.get('daily_chat_limit', 0)
                monthly_chat_used = user_doc.get('monthly_chat_usage', 0)
                monthly_chat_limit = pkg_doc.get('monthly_chat_limit', 0)
                package_details.append(f"💬 *چت:*")
                package_details.append(f" - روزانه: {_format_usage(daily_chat_used, daily_chat_limit)}")
                package_details.append(f" - ماهانه: {_format_usage(monthly_chat_used, monthly_chat_limit)}\n")

                # Command Usage
                daily_cmd_used = user_doc.get('daily_command_usage', 0)
                daily_cmd_limit = pkg_doc.get('daily_command_limit', 0)
                monthly_cmd_used = user_doc.get('monthly_command_usage', 0)
                monthly_cmd_limit = pkg_doc.get('monthly_command_limit', 0)
                package_details.append(f"🤖 *دستورات هوشمند:*")
                package_details.append(f" - روزانه: {_format_usage(daily_cmd_used, daily_cmd_limit)}")
                package_details.append(f" - ماهانه: {_format_usage(monthly_cmd_used, monthly_cmd_limit)}")


    full_name = common.escape_markdown(user_doc.get('full_name', 'ثبت نشده'))
    username = common.escape_markdown(user_doc.get('telegram_username', 'ندارد'))
    activation_date = common.format_datetime_field(user_doc.get('package_activation_date'))
    expiry_date = common.format_datetime_field(user_doc.get('package_expiry_date'))

    text_lines = [
        f"👤 *پروفایل شما*",
        "---",
        f"▫️ *نام:* {full_name}",
        f"▫️ *نام کاربری:* @{username}",
        f"▫️ *شناسه تلگرام:* `{user_doc['telegram_id']}`",
        "---",
        f"📦 *پکیج فعلی:* {common.escape_markdown(package_name)}",
        f"▫️ *تاریخ فعال‌سازی:* {activation_date}",
        f"▫️ *تاریخ انقضا:* {expiry_date}",
    ]
    
    text_lines.extend(package_details)

    keyboard = [[InlineKeyboardButton("🚀 ارتقای پلن", callback_data="upgrade_plan")]]

    await update.message.reply_text(
        text="\n".join(text_lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


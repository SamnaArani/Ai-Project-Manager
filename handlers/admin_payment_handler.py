# -*- coding: utf-8 -*-
import asyncio
import logging
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import config
import database
from . import common

logger = logging.getLogger(__name__)

# --- Payment Management Functions ---

async def review_payments_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the list of pending payments to the admin."""
    if not await common.is_user_admin(str(update.effective_user.id)):
        await update.message.reply_text("⛔️ شما دسترسی لازم برای اجرای این دستور را ندارید.")
        return

    pending_payments = await asyncio.to_thread(
        database.get_documents,
        config.APPWRITE_DATABASE_ID,
        config.PAYMENT_REQUESTS_COLLECTION_ID,
        [database.Query.equal("status", ["pending"])]
    )
    if not pending_payments:
        await update.message.reply_text("هیچ درخواست پرداخت در حال انتظاری وجود ندارد.")
        return
    
    context.user_data['pending_payments'] = pending_payments
    context.user_data['payment_index'] = 0
    await display_pending_payment(update, context)

async def display_pending_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays a specific pending payment with management buttons."""
    index = context.user_data.get('payment_index', 0)
    payments = context.user_data.get('pending_payments', [])
    
    if not payments or index >= len(payments):
        await common.send_or_edit(update, "تمام درخواست‌ها بررسی شدند.")
        context.user_data.clear()
        return

    payment = payments[index]
    payment_id = payment['$id']
    user_id = payment['telegram_id']
    package_id = payment['package_id']
    
    user_doc = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id)
    package_info_list = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, [database.Query.equal("$id", [package_id])])
    
    text = f"درخواست پرداخت ({index + 1}/{len(payments)})\n\n"
    user_display_name = user_doc.get('clickup_username', user_id) if user_doc else user_id
    text += f"👤 *کاربر:* `{user_id}` (نام کاربری: {user_display_name})\n"
    if package_info_list:
        text += f"📦 *پکیج:* {package_info_list[0]['package_name']}\n"
    text += f"📄 *اطلاعات واریز:*\n`{payment['receipt_details']}`\n\n"
    text += "لطفاً اقدام مورد نظر را انتخاب کنید:"

    keyboard = [[InlineKeyboardButton("✅ تایید", callback_data=f"admin_payment_approve_{payment_id}"),
                 InlineKeyboardButton("❌ رد", callback_data=f"admin_payment_reject_{payment_id}")], []]
    if index > 0:
        keyboard[1].append(InlineKeyboardButton("◀️ قبلی", callback_data="admin_payment_prev"))
    if index < len(payments) - 1:
        keyboard[1].append(InlineKeyboardButton("▶️ بعدی", callback_data="admin_payment_next"))
    
    await common.send_or_edit(update, text, InlineKeyboardMarkup(keyboard))

async def admin_payment_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles admin buttons for payments (approve/reject/navigate)."""
    query = update.callback_query
    await query.answer()
    
    data = query.data.split('_')
    action = data[2]

    if action in ["next", "prev"]:
        index = context.user_data.get('payment_index', 0)
        new_index = index + 1 if action == "next" else index - 1
        context.user_data['payment_index'] = new_index
        await display_pending_payment(update, context)
        return

    payment_id = data[3]
    payment_doc_list = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID, [database.Query.equal("$id", [payment_id])])
    if not payment_doc_list:
        await query.edit_message_text("خطا: این درخواست پرداخت دیگر وجود ندارد.")
        return
    payment = payment_doc_list[0]
    new_status = "approved" if action == "approve" else "rejected"
    
    await asyncio.to_thread(
        database.upsert_document,
        config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID,
        '$id', payment_id,
        {'status': new_status, 'review_date': datetime.now(timezone.utc).isoformat()}
    )
    
    user_telegram_id = payment['telegram_id']
    if new_status == "approved":
        package_info_list = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, [database.Query.equal("$id", [payment['package_id']])])
        if package_info_list:
            pkg = package_info_list[0]
            await asyncio.to_thread(
                database.upsert_document,
                config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID,
                'telegram_id', user_telegram_id,
                {'package_id': payment['package_id'], 'usage_limit': pkg.get('ai_call_limit', 0), 'used_count': 0}
            )
        try:
            await context.bot.send_message(chat_id=user_telegram_id, text="✅ پرداخت شما تایید شد! حساب شما آماده فعال‌سازی است.\n\nلطفاً برای تکمیل فرآیند، توکن API کلیک‌اپ خود را ارسال کنید.")
            await query.edit_message_text(f"✅ پرداخت برای کاربر {user_telegram_id} تایید شد.")
        except Exception as e:
            logger.error(f"Failed to send message to user {user_telegram_id}: {e}")
            await query.edit_message_text(f"✅ پرداخت تایید شد، اما ارسال پیام به کاربر ناموفق بود.")
    else: # Rejected
        try:
            await context.bot.send_message(chat_id=user_telegram_id, text="❌ متاسفانه پرداخت شما رد شد. لطفاً برای اطلاعات بیشتر با پشتیبانی تماس بگیرید.")
            await query.edit_message_text(f"❌ پرداخت برای کاربر {user_telegram_id} رد شد.")
        except Exception as e:
            logger.error(f"Failed to send message to user {user_telegram_id}: {e}")
            await query.edit_message_text(f"❌ پرداخت رد شد، اما ارسال پیام به کاربر ناموفق بود.")

    payments = context.user_data.get('pending_payments', [])
    current_index = context.user_data.get('payment_index', 0)
    payments.pop(current_index)
    
    if not payments:
        await common.send_or_edit(update, "تمام درخواست‌ها بررسی شدند.")
        context.user_data.clear()
        return

    if current_index >= len(payments):
        context.user_data['payment_index'] = max(0, len(payments) - 1)

    await display_pending_payment(update, context)


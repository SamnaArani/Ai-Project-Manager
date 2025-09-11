# -*- coding: utf-8 -*-
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from appwrite.query import Query
import config
import database
from . import common

logger = logging.getLogger(__name__)

# Conversation states
AWAITING_REJECTION_REASON = range(1)

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
        [Query.equal("status", ["pending"])]
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
    package_doc = await asyncio.to_thread(database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_id)
    
    text = f"درخواست پرداخت ({index + 1}/{len(payments)})\n\n"
    user_display_name = user_doc.get('full_name', user_id) if user_doc else user_id
    text += f"👤 *کاربر:* `{user_id}` ({common.escape_markdown(user_display_name)})\n"
    if package_doc:
        text += f"📦 *پکیج:* {common.escape_markdown(package_doc['package_name'])}\n"
    text += f"📄 *اطلاعات واریز:*\n`{payment['receipt_details']}`\n\n"
    text += "لطفاً اقدام مورد نظر را انتخاب کنید:"

    keyboard = [[InlineKeyboardButton("✅ تایید", callback_data=f"admin_payment_approve_{payment_id}"),
                 InlineKeyboardButton("❌ رد", callback_data=f"admin_payment_reject_{payment_id}")], []]
    if index > 0:
        keyboard[1].append(InlineKeyboardButton("◀️ قبلی", callback_data="admin_payment_prev"))
    if index < len(payments) - 1:
        keyboard[1].append(InlineKeyboardButton("▶️ بعدی", callback_data="admin_payment_next"))
    
    await common.send_or_edit(update, text, InlineKeyboardMarkup(keyboard))

async def admin_payment_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        return ConversationHandler.END

    payment_id = data[3]
    payment_doc = await asyncio.to_thread(database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID, payment_id)
    if not payment_doc:
        await query.edit_message_text("خطا: این درخواست پرداخت دیگر وجود ندارد.")
        return ConversationHandler.END
    
    if action == "approve":
        await approve_payment(query, context, payment_doc)
        # Remove from local list and redisplay
        payments = context.user_data.get('pending_payments', [])
        current_index = context.user_data.get('payment_index', 0)
        if payments and current_index < len(payments):
            payments.pop(current_index)
        if current_index >= len(payments) and payments:
            context.user_data['payment_index'] = len(payments) - 1
        await display_pending_payment(update, context)
        return ConversationHandler.END
    
    elif action == "reject":
        context.user_data['rejecting_payment_doc'] = payment_doc
        await query.message.edit_text("لطفاً دلیل رد کردن پرداخت را تایپ و ارسال کنید.")
        return AWAITING_REJECTION_REASON

async def approve_payment(query: Update, context: ContextTypes.DEFAULT_TYPE, payment_doc: dict):
    """Logic to approve a payment."""
    payment_id = payment_doc['$id']
    user_telegram_id = payment_doc['telegram_id']
    package_id = payment_doc['package_id']
    
    await asyncio.to_thread(
        database.upsert_document,
        config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID,
        '$id', payment_id,
        {'status': 'approved', 'review_date': datetime.now(timezone.utc).isoformat()}
    )
    
    pkg_doc = await asyncio.to_thread(database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_id)
    if pkg_doc:
        activation_date = datetime.now(timezone.utc)
        expiry_date = activation_date + timedelta(days=pkg_doc.get('package_duration_days', 30))
        
        await asyncio.to_thread(
            database.upsert_document,
            config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID,
            'telegram_id', user_telegram_id,
            {
                'package_id': package_id,
                'package_activation_date': activation_date.isoformat(),
                'package_expiry_date': expiry_date.isoformat(),
                'daily_chat_usage': 0, 'monthly_chat_usage': 0,
                'daily_command_usage': 0, 'monthly_command_usage': 0,
            }
        )
        try:
            await context.bot.send_message(
                chat_id=user_telegram_id, 
                text=f"✅ پرداخت شما برای پکیج *{common.escape_markdown(pkg_doc['package_name'])}* تایید و حساب شما فعال شد!",
                parse_mode='Markdown'
                )
            await query.edit_message_text(f"✅ پرداخت برای کاربر {user_telegram_id} تایید شد.")
        except Exception as e:
            logger.error(f"Failed to send approval message to user {user_telegram_id}: {e}")
            await query.edit_message_text(f"✅ پرداخت تایید شد، اما ارسال پیام به کاربر ناموفق بود.")
    else:
        await query.edit_message_text("❌ خطا: پکیج مربوط به این پرداخت یافت نشد. پرداخت تایید نشد.")


async def rejection_reason_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the rejection reason provided by the admin."""
    reason = update.message.text
    payment_doc = context.user_data.pop('rejecting_payment_doc', None)

    if not payment_doc:
        await update.message.reply_text("خطا: اطلاعات پرداخت برای رد کردن یافت نشد.")
        return ConversationHandler.END

    await asyncio.to_thread(
        database.upsert_document,
        config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID,
        '$id', payment_doc['$id'],
        {'status': 'rejected', 'review_date': datetime.now(timezone.utc).isoformat(), 'admin_notes': reason}
    )

    try:
        rejection_message = (
            f"❌ پرداخت شما رد شد.\n\n"
            f"*دلیل:* {common.escape_markdown(reason)}"
        )
        await context.bot.send_message(
            chat_id=payment_doc['telegram_id'],
            text=rejection_message,
            parse_mode='Markdown'
            )
        await update.message.reply_text(f"❌ پرداخت برای کاربر {payment_doc['telegram_id']} رد شد و به کاربر اطلاع داده شد.")
    except Exception as e:
        logger.error(f"Failed to send rejection message to user {payment_doc['telegram_id']}: {e}")
        await update.message.reply_text("❌ پرداخت رد شد، اما ارسال پیام به کاربر ناموفق بود.")

    # Refresh the view
    payments = context.user_data.get('pending_payments', [])
    current_index = context.user_data.get('payment_index', 0)
    if payments and current_index < len(payments):
        payments.pop(current_index)
    if current_index >= len(payments) and payments:
        context.user_data['payment_index'] = len(payments) - 1
    
    # We need a callback query update object to call display_pending_payment
    # For simplicity, we just end and ask the admin to run the command again.
    await update.message.reply_text("برای مشاهده درخواست بعدی، لطفاً دستور /reviewpayments را مجدداً اجرا کنید.")
    context.user_data.clear()
    
    return ConversationHandler.END


def get_payment_review_conv_handler():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_payment_button_handler, pattern=r'^admin_payment_')],
        states={
            AWAITING_REJECTION_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, rejection_reason_received)]
        },
        fallbacks=[CommandHandler("cancel", common.generic_cancel_conversation)]
    )

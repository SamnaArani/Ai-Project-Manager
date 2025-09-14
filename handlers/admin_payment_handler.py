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

# --- Conversation States & Constants ---
AWAITING_REJECTION_REASON = range(1)
PAGE_SIZE = 5

# --- Utility Functions ---

def format_payment_details(payment, user_doc, package_doc):
    """Formats the details of a single payment for display."""
    user_display_name = user_doc.get('full_name', payment['telegram_id']) if user_doc else payment['telegram_id']
    package_name = package_doc.get('package_name', 'نامشخص') if package_doc else 'نامشخص'
    
    request_date_str = common.format_datetime_field(payment.get('request_date'))
    review_date_str = common.format_datetime_field(payment.get('review_date'))

    details = [
        f"👤 *کاربر:* `{payment['telegram_id']}` ({common.escape_markdown(user_display_name)})",
        f"📦 *پکیج:* {common.escape_markdown(package_name)}",
        f"🗓️ *تاریخ درخواست:* {request_date_str}",
    ]
    if payment['status'] == 'pending':
        details.append(f"📄 *اطلاعات واریز:*\n`{payment['receipt_details']}`")
    elif payment['status'] == 'rejected':
        details.append(f"❌ *رد شده در:* {review_date_str}")
        details.append(f"📝 *دلیل:* `{payment.get('admin_notes', 'ثبت نشده')}`")
    elif payment['status'] == 'approved':
        details.append(f"✅ *تایید شده در:* {review_date_str}")

    return "\n".join(details)


# --- Main Menu & List Views ---

async def manage_payments_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main entry point for payment management. Shows a menu with payment statuses."""
    if not await common.is_user_admin(str(update.effective_user.id)):
        await common.send_or_edit(update, "⛔️ شما دسترسی لازم را ندارید.")
        return

    pending_payments = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID, [Query.equal("status", ["pending"])])
    approved_count = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID, [Query.equal("status", ["approved"])])
    rejected_count = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID, [Query.equal("status", ["rejected"])])

    pending_count = len(pending_payments)

    text = "💳 *مدیریت پرداخت‌ها*\n\nلطفاً بخش مورد نظر را برای مدیریت انتخاب کنید:"
    keyboard = [
        [InlineKeyboardButton(f"⏳ بررسی درخواست‌های جدید ({pending_count})", callback_data="admin_payment_review_pending")],
        [InlineKeyboardButton(f"✅ مشاهده تایید شده‌ها ({len(approved_count)})", callback_data="admin_payment_list_approved_0")],
        [InlineKeyboardButton(f"❌ مشاهده رد شده‌ها ({len(rejected_count)})", callback_data="admin_payment_list_rejected_0")],
    ]
    await common.send_or_edit(update, text, InlineKeyboardMarkup(keyboard))


async def list_reviewed_payments(update: Update, context: ContextTypes.DEFAULT_TYPE, status: str, page: int = 0):
    """Displays a paginated list of users with payments of a specific status (approved/rejected)."""
    query = update.callback_query
    if query: await query.answer()

    payments = await asyncio.to_thread(
        database.get_documents,
        config.APPWRITE_DATABASE_ID,
        config.PAYMENT_REQUESTS_COLLECTION_ID,
        [Query.equal("status", [status]), Query.order_desc("review_date")]
    )

    if not payments:
        await common.send_or_edit(update, "هیچ پرداخت بررسی شده‌ای در این بخش یافت نشد.")
        return

    user_payments = {}
    for p in payments:
        user_id = p['telegram_id']
        if user_id not in user_payments:
            user_doc = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id)
            display_name = user_doc.get('full_name', user_id) if user_doc else user_id
            user_payments[user_id] = {'name': display_name, 'count': 0}
        user_payments[user_id]['count'] += 1
    
    status_map = {"approved": "تایید شده", "rejected": "رد شده"}
    text = f"لیست کاربرانی که پرداخت *{status_map.get(status, '')}* دارند:"

    sorted_users = sorted(user_payments.items(), key=lambda item: item[1]['name'])
    
    start_index = page * PAGE_SIZE
    end_index = start_index + PAGE_SIZE
    users_on_page = sorted_users[start_index:end_index]

    keyboard = []
    for user_id, data in users_on_page:
        keyboard.append([InlineKeyboardButton(f"{data['name']} ({data['count']} مورد)", callback_data=f"admin_payment_history_{user_id}_{status}")])
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"admin_payment_list_{status}_{page - 1}"))
    if end_index < len(sorted_users):
        nav_buttons.append(InlineKeyboardButton("▶️ بعدی", callback_data=f"admin_payment_list_{status}_{page + 1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("🔙 بازگشت به منوی پرداخت", callback_data="admin_payment_back_menu")])
    await common.send_or_edit(update, text, InlineKeyboardMarkup(keyboard))


async def view_user_payment_history(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str, status: str):
    """Shows the full payment history for a specific user with a given status."""
    query = update.callback_query; await query.answer()

    payments = await asyncio.to_thread(
        database.get_documents,
        config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID,
        [Query.equal("telegram_id", [user_id]), Query.equal("status", [status]), Query.order_desc("review_date")]
    )
    
    if not payments:
        await common.send_or_edit(update, "تاریخچه پرداختی برای این کاربر یافت نشد.")
        return

    user_doc = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id)
    display_name = user_doc.get('full_name', user_id) if user_doc else user_id

    full_text = f"تاریخچه پرداخت‌های کاربر: *{common.escape_markdown(display_name)}*\n\n"
    for p in payments:
        package_doc = await asyncio.to_thread(database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, p['package_id'])
        full_text += format_payment_details(p, user_doc, package_doc) + "\n\n---\n\n"
        
    keyboard = [[InlineKeyboardButton("🔙 بازگشت به لیست کاربران", callback_data=f"admin_payment_list_{status}_0")]]
    await common.send_or_edit(update, full_text, InlineKeyboardMarkup(keyboard))


# --- Pending Payment Review Flow ---

async def review_pending_payments_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts the one-by-one review of pending payments."""
    pending_payments = await asyncio.to_thread(
        database.get_documents,
        config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID,
        [Query.equal("status", ["pending"]), Query.order_asc("request_date")]
    )
    if not pending_payments:
        await common.send_or_edit(update, "هیچ درخواست پرداخت در حال انتظاری وجود ندارد.")
        return

    context.user_data['pending_payments'] = pending_payments
    context.user_data['payment_index'] = 0
    await display_pending_payment(update, context)


async def display_pending_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays a specific pending payment with management buttons."""
    index = context.user_data.get('payment_index', 0)
    payments = context.user_data.get('pending_payments', [])
    
    if not payments or index >= len(payments):
        await common.send_or_edit(update, "تمام درخواست‌های جدید بررسی شدند.")
        context.user_data.clear()
        await manage_payments_entry(update, context) # Go back to main menu
        return

    payment = payments[index]
    payment_id = payment['$id']
    user_doc = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', payment['telegram_id'])
    package_doc = await asyncio.to_thread(database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, payment['package_id'])
    
    text = f"درخواست پرداخت ({index + 1}/{len(payments)})\n\n"
    text += format_payment_details(payment, user_doc, package_doc)
    text += "\n\nلطفاً اقدام مورد نظر را انتخاب کنید:"

    keyboard = [[InlineKeyboardButton("✅ تایید", callback_data=f"admin_payment_action_approve_{payment_id}"),
                 InlineKeyboardButton("❌ رد", callback_data=f"admin_payment_action_reject_{payment_id}")], 
                [InlineKeyboardButton("🔙 بازگشت به منوی پرداخت", callback_data="admin_payment_back_menu")],
                []]

    if index > 0:
        keyboard[2].append(InlineKeyboardButton("◀️ قبلی", callback_data="admin_payment_action_prev"))
    if index < len(payments) - 1:
        keyboard[2].append(InlineKeyboardButton("▶️ بعدی", callback_data="admin_payment_action_next"))
    
    await common.send_or_edit(update, text, InlineKeyboardMarkup(keyboard))


# --- Action Handlers & Conversation ---

async def approve_payment(query: Update, context: ContextTypes.DEFAULT_TYPE, payment_doc: dict):
    """Logic to approve a payment."""
    user_telegram_id = payment_doc['telegram_id']
    package_id = payment_doc['package_id']
    
    await asyncio.to_thread(
        database.upsert_document,
        config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID, '$id', payment_doc['$id'],
        {'status': 'approved', 'review_date': datetime.now(timezone.utc).isoformat()}
    )
    
    pkg_doc = await asyncio.to_thread(database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_id)
    if pkg_doc:
        activation_date = datetime.now(timezone.utc)
        
        # BUG FIX: Ensure duration_days is an integer before using it.
        duration_days = pkg_doc.get('package_duration_days')
        if not isinstance(duration_days, int):
            logger.warning(f"Package {package_id} has invalid 'package_duration_days' ({duration_days}). Defaulting to 30.")
            duration_days = 30
        
        expiry_date = activation_date + timedelta(days=duration_days)
        
        await asyncio.to_thread(
            database.upsert_document,
            config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_telegram_id,
            {'package_id': package_id, 'package_activation_date': activation_date.isoformat(), 'package_expiry_date': expiry_date.isoformat()}
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
    context.chat_data['conversation_handled'] = True
    reason = update.message.text
    payment_doc = context.user_data.pop('rejecting_payment_doc', None)

    if not payment_doc:
        await update.message.reply_text("خطا: اطلاعات پرداخت برای رد کردن یافت نشد.")
        return ConversationHandler.END

    await asyncio.to_thread(
        database.upsert_document,
        config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID, '$id', payment_doc['$id'],
        {'status': 'rejected', 'review_date': datetime.now(timezone.utc).isoformat(), 'admin_notes': reason}
    )

    try:
        await context.bot.send_message(
            chat_id=payment_doc['telegram_id'],
            text=f"❌ پرداخت شما رد شد.\n\n*دلیل:* {common.escape_markdown(reason)}",
            parse_mode='Markdown'
        )
        await update.message.reply_text(f"❌ پرداخت برای کاربر {payment_doc['telegram_id']} رد شد و به کاربر اطلاع داده شد.")
    except Exception as e:
        logger.error(f"Failed to send rejection message to user {payment_doc['telegram_id']}: {e}")
        await update.message.reply_text("❌ پرداخت رد شد، اما ارسال پیام به کاربر ناموفق بود.")

    # Refresh the pending payments view
    context.user_data.pop('pending_payments', None)
    context.user_data.pop('payment_index', None)
    await review_pending_payments_entry(update, context)
    
    return ConversationHandler.END

async def admin_payment_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Main router for all buttons in the payment management section."""
    query = update.callback_query
    await query.answer()
    
    data_parts = query.data.split('_')
    action = data_parts[2]

    # Menu navigation
    if action == "review" and data_parts[3] == "pending":
        return await review_pending_payments_entry(update, context)
    if action == "list":
        status, page = data_parts[3], int(data_parts[4])
        return await list_reviewed_payments(update, context, status, page)
    if action == "history":
        user_id, status = data_parts[3], data_parts[4]
        return await view_user_payment_history(update, context, user_id, status)
    if action == "back" and data_parts[3] == "menu":
        return await manage_payments_entry(update, context)

    # Actions within the pending review flow
    if action == "action":
        sub_action = data_parts[3]
        if sub_action in ["next", "prev"]:
            index = context.user_data.get('payment_index', 0)
            context.user_data['payment_index'] = index + 1 if sub_action == "next" else index - 1
            await display_pending_payment(update, context)
            return ConversationHandler.END

        payment_id = data_parts[4]
        payment_doc = await asyncio.to_thread(database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID, payment_id)
        if not payment_doc:
            await query.edit_message_text("خطا: این درخواست پرداخت دیگر وجود ندارد.")
            return ConversationHandler.END
        
        if sub_action == "approve":
            await approve_payment(query, context, payment_doc)
            payments = context.user_data.get('pending_payments', [])
            if payments: payments.pop(context.user_data.get('payment_index', 0))
            await display_pending_payment(update, context)
            return ConversationHandler.END
        
        elif sub_action == "reject":
            context.user_data['rejecting_payment_doc'] = payment_doc
            await query.message.edit_text("لطفاً دلیل رد کردن پرداخت را تایپ و ارسال کنید.")
            return AWAITING_REJECTION_REASON


def get_payment_review_conv_handler():
    """Returns the ConversationHandler for the rejection reason flow."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_payment_button_handler, pattern=r'^admin_payment_action_reject_')],
        states={
            AWAITING_REJECTION_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, rejection_reason_received)]
        },
        fallbacks=[CommandHandler("cancel", common.generic_cancel_conversation)]
    )

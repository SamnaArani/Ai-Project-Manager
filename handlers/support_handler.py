# -*- coding: utf-8 -*-
import asyncio
import logging
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, 
    ConversationHandler, 
    CommandHandler, 
    MessageHandler, 
    filters,
    CallbackQueryHandler,
)
from appwrite.query import Query
import config
import database
from . import common
from . import admin_handler as admin_panel_handler

logger = logging.getLogger(__name__)

# --- Conversation States ---
(AWAITING_USER_MESSAGE, AWAITING_ADMIN_REPLY) = range(2)

# --- User Section ---

async def support_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User's entry point to the support section."""
    escaped_admin_username = common.escape_markdown(config.ADMIN_USERNAME)
    text = (f"📞 *بخش پشتیبانی*\n\n"
            f"در صورت بروز هرگونه مشکل یا سوال، می‌توانید مستقیماً با ادمین در ارتباط باشید.\n"
            f"شناسه ادمین: @{escaped_admin_username}\n\n"
            f"همچنین می‌توانید پیام خود را از طریق دکمه زیر در ربات ثبت کنید تا پس از بررسی به شما پاسخ داده شود.")
    
    keyboard = [[InlineKeyboardButton("✉️ ارسال پیام به پشتیبانی", callback_data="support_start_conv")]]
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return ConversationHandler.END

async def start_support_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation to get the user's message."""
    query = update.callback_query
    await query.answer()
    
    keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="support_cancel")]]
    await query.message.edit_text(
        "لطفاً پیام خود را به صورت کامل تایپ و ارسال کنید.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return AWAITING_USER_MESSAGE

async def user_message_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives, saves the user's message, and notifies admin."""
    context.chat_data['conversation_handled'] = True
    user = update.effective_user
    user_message = update.message.text

    ticket_data = {
        'telegram_id': str(user.id),
        'telegram_username': user.username or "",
        'full_name': user.full_name,
        'user_message': user_message,
        'status': 'unread',
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    
    new_ticket = await asyncio.to_thread(
        database.create_document, 
        config.APPWRITE_DATABASE_ID, 
        config.SUPPORT_TICKETS_COLLECTION_ID, 
        ticket_data
    )
    
    await update.message.reply_text(
        "✅ پیام شما با موفقیت ثبت شد. پس از بررسی توسط ادمین، پاسخ برای شما ارسال خواهد شد."
    )

    # --- Live Notification for Admins ---
    admins = await asyncio.to_thread(
        database.get_documents,
        config.APPWRITE_DATABASE_ID,
        config.BOT_USERS_COLLECTION_ID,
        [Query.equal("is_admin", [True])]
    )
    
    notification_text = f"✉️ پیام پشتیبانی جدیدی از طرف *{common.escape_markdown(user.full_name)}* دریافت شد."
    notification_keyboard = [[InlineKeyboardButton("مشاهده پیام", callback_data=f"support_admin_ticket_{new_ticket['$id']}")]]

    for admin in admins:
        try:
            await context.bot.send_message(
                chat_id=admin['telegram_id'],
                text=notification_text,
                reply_markup=InlineKeyboardMarkup(notification_keyboard),
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to send new message notification to admin {admin['telegram_id']}: {e}")
        # Also refresh their main menu to update the counter
        await admin_panel_handler.show_admin_panel(admin['telegram_id'], context)


    return ConversationHandler.END

async def cancel_user_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the user support conversation."""
    return await common.generic_cancel_conversation(update, context)


# --- Admin Section ---

async def manage_messages_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin's entry point to the message management section."""
    all_tickets = await asyncio.to_thread(
        database.get_documents, 
        config.APPWRITE_DATABASE_ID, 
        config.SUPPORT_TICKETS_COLLECTION_ID
    )
    
    text = "✉️ *صندوق ورودی پیام‌ها*\n\n"
    keyboard = []
    
    if not all_tickets:
        text += "هیچ پیامی برای نمایش وجود ندارد."
    else:
        user_conversations = {}
        for ticket in all_tickets:
            user_id = ticket['telegram_id']
            if user_id not in user_conversations:
                user_conversations[user_id] = {'unread_count': 0, 'total_count': 0, 'user_info': ticket}
            
            user_conversations[user_id]['total_count'] += 1
            if ticket['status'] == 'unread':
                user_conversations[user_id]['unread_count'] += 1
                
        text += "لیست کاربرانی که پیام ارسال کرده‌اند:"
        
        sorted_users = sorted(user_conversations.items(), key=lambda item: item[1]['unread_count'], reverse=True)

        for user_id, data in sorted_users:
            user_info = data['user_info']
            display_name = user_info.get('full_name') or f"@{user_info.get('telegram_username')}" or user_id
            
            button_text = f"{display_name}"
            if data['unread_count'] > 0:
                button_text += f"   newMessage({data['unread_count']})"
                
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"support_admin_view_{user_id}")])
    
    await common.send_or_edit(update, text, InlineKeyboardMarkup(keyboard))


async def view_user_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id_override: str = None):
    """Displays all tickets from a specific user. Can be called from a message or callback query."""
    query = update.callback_query
    user_id = ""

    if user_id_override:
        user_id = user_id_override
    elif query:
        await query.answer()
        user_id = query.data.split('_')[-1]

    if not user_id:
        logger.error("view_user_tickets called without a user_id.")
        await common.send_or_edit(update, "خطا: شناسه کاربر برای نمایش پیام‌ها یافت نشد.")
        return

    user_tickets = await asyncio.to_thread(
        database.get_documents,
        config.APPWRITE_DATABASE_ID,
        config.SUPPORT_TICKETS_COLLECTION_ID,
        [Query.equal("telegram_id", [user_id]), Query.order_desc("created_at")]
    )
    
    if not user_tickets:
        await common.send_or_edit(update, "پیامی از این کاربر یافت نشد.")
        return

    user_info = user_tickets[0]
    display_name = user_info.get('full_name') or f"@{user_info.get('telegram_username')}" or user_id
    escaped_display_name = common.escape_markdown(display_name)
    text = f"📬 *تاریخچه پیام‌های {escaped_display_name}*\n"
    keyboard = []
    
    for ticket in user_tickets:
        status_icon = "🔵" if ticket['status'] == 'unread' else ("⚫️" if ticket['status'] == 'read' else "✅")
        ticket_preview = (ticket['user_message'][:25] + '...') if len(ticket['user_message']) > 25 else ticket['user_message']
        button_text = f"{status_icon} {ticket_preview}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"support_admin_ticket_{ticket['$id']}")])
            
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به صندوق ورودی", callback_data="support_admin_back_inbox")])
    
    await common.send_or_edit(update, text, InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def view_single_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Displays details of a single ticket and marks it as read."""
    query = update.callback_query
    await query.answer()
    ticket_id = query.data.split('_')[-1]
    
    ticket = await asyncio.to_thread(
        database.get_single_document_by_id,
        config.APPWRITE_DATABASE_ID, config.SUPPORT_TICKETS_COLLECTION_ID, ticket_id
    )
    
    if not ticket:
        await query.message.edit_text("خطا: این پیام یافت نشد.")
        return ConversationHandler.END

    # Mark as read upon viewing
    if ticket['status'] == 'unread':
        await asyncio.to_thread(
            database.upsert_document,
            config.APPWRITE_DATABASE_ID, config.SUPPORT_TICKETS_COLLECTION_ID,
            '$id', ticket_id, {'status': 'read'}
        )
        ticket['status'] = 'read' # Update local copy

    context.user_data['reply_ticket_id'] = ticket_id
    
    created_date = datetime.fromisoformat(ticket['created_at']).strftime('%Y-%m-%d %H:%M')
    escaped_full_name = common.escape_markdown(ticket.get('full_name', 'کاربر ناشناس'))
    
    text = (f"💬 *مشاهده پیام*\n\n"
            f"👤 *از طرف:* {escaped_full_name}\n"
            f"🗓️ *تاریخ ارسال:* {created_date}\n\n"
            f"✉️ *متن پیام کاربر:*\n`{ticket['user_message']}`\n\n")
            
    keyboard = [[InlineKeyboardButton("🔙 بازگشت به پیام‌های کاربر", callback_data=f"support_admin_back_ticket_{ticket['telegram_id']}")]]
    
    if ticket.get('admin_reply'):
        replied_date = datetime.fromisoformat(ticket['replied_at']).strftime('%Y-%m-%d %H:%M')
        text += (f"✅ *پاسخ شما* (در تاریخ {replied_date}):\n"
                 f"`{ticket['admin_reply']}`")
    else:
        text += "برای پاسخ به این پیام، متن پاسخ را تایپ و ارسال کنید."

    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    
    return AWAITING_ADMIN_REPLY


async def admin_reply_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives, saves, and sends the admin's reply."""
    context.chat_data['conversation_handled'] = True
    ticket_id = context.user_data.get('reply_ticket_id')
    admin_reply_text = update.message.text
    
    if not ticket_id:
        await update.message.reply_text("خطا: مشخص نیست به کدام پیام پاسخ می‌دهید.")
        return ConversationHandler.END

    ticket = await asyncio.to_thread(
        database.get_single_document_by_id,
        config.APPWRITE_DATABASE_ID, config.SUPPORT_TICKETS_COLLECTION_ID, ticket_id
    )

    if not ticket:
        await update.message.reply_text("خطا: پیام اصلی یافت نشد.")
        return ConversationHandler.END
        
    updated_data = {
        'admin_reply': admin_reply_text,
        'status': 'replied',
        'replied_at': datetime.now(timezone.utc).isoformat()
    }
    await asyncio.to_thread(
        database.upsert_document,
        config.APPWRITE_DATABASE_ID, config.SUPPORT_TICKETS_COLLECTION_ID,
        '$id', ticket_id, updated_data
    )
    
    user_telegram_id = ticket['telegram_id']
    reply_to_user_text = (
        f"📩 *پاسخ پشتیبانی به پیام شما:*\n\n"
        f"💬 *پیام شما:*\n`{ticket['user_message']}`\n\n"
        f"✅ *پاسخ ادمین:*\n`{admin_reply_text}`"
    )
    
    try:
        await context.bot.send_message(
            chat_id=user_telegram_id, 
            text=reply_to_user_text, 
            parse_mode='Markdown'
        )
        await update.message.reply_text("✅ پاسخ شما با موفقیت برای کاربر ارسال شد.")
    except Exception as e:
        logger.error(f"Failed to send support reply to {user_telegram_id}: {e}")
        await update.message.reply_text("⚠️ پاسخ در سیستم ثبت شد، اما ارسال آن به کاربر ناموفق بود.")
        
    context.user_data.clear()
    await view_user_tickets(update, context, user_id_override=user_telegram_id)
    return ConversationHandler.END

async def back_from_ticket_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the 'back' button from a single ticket view, ending the conversation."""
    query = update.callback_query
    user_id = query.data.split('_')[-1]
    await view_user_tickets(update, context, user_id_override=user_id)
    return ConversationHandler.END

async def admin_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles buttons in the message management section."""
    query = update.callback_query
    await query.answer()
    
    action = query.data.split('_')[2]
    
    if action == "view":
        await view_user_tickets(update, context)
    elif action == "back" and query.data.split('_')[3] == "inbox":
        await query.message.delete()
        await manage_messages_entry(update, context)

# --- Handlers ---
def get_user_support_conv_handler():
    """ConversationHandler for user message submission."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(start_support_conversation, pattern='^support_start_conv$')],
        states={
            AWAITING_USER_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_message_received)],
        },
        fallbacks=[CallbackQueryHandler(cancel_user_support, pattern='^support_cancel$')],
        block=True,
    )

def get_admin_reply_conv_handler():
    """ConversationHandler for admin replies."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(view_single_ticket, pattern='^support_admin_ticket_')],
        states={
            AWAITING_ADMIN_REPLY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_reply_received),
                CallbackQueryHandler(back_from_ticket_view, pattern=r'^support_admin_back_ticket_')
            ],
        },
        fallbacks=[CommandHandler("cancel", common.generic_cancel_conversation)],
        block=True,
    )


# -*- coding: utf-8 -*-
import asyncio
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, 
    ConversationHandler, 
    MessageHandler, 
    filters,
    CallbackQueryHandler,
    CommandHandler
)
import config
import database
from . import common

logger = logging.getLogger(__name__)

# --- States ---
PAGE_SIZE = 5
AWAITING_DIRECT_MESSAGE = range(PAGE_SIZE + 1, PAGE_SIZE + 2)

def format_datetime_field(dt_string):
    """Formats an ISO datetime string into a readable format."""
    if not dt_string:
        return "ثبت نشده"
    try:
        if isinstance(dt_string, str) and dt_string.endswith('Z'):
            dt_string = dt_string[:-1] + '+00:00'
        return datetime.fromisoformat(dt_string).strftime('%Y-%m-%d %H:%M')
    except (ValueError, TypeError):
        logger.warning(f"Could not parse datetime string: {dt_string}")
        return "نامعتبر"

async def manage_users_entry(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Entry point for user management. Displays stats and a paginated list of users."""
    all_users = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID)
    all_packages = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID)
    
    package_map = {pkg['$id']: pkg['package_name'] for pkg in all_packages}
    
    admins = [u for u in all_users if u.get('is_admin')]
    regular_users = [u for u in all_users if not u.get('is_admin')]
    
    summary_lines = ["👑 *گزارش ادمین‌ها*"]
    if not admins:
        summary_lines.append("هیچ ادمینی تعریف نشده است.")
    else:
        summary_lines.append(f"تعداد کل ادمین‌ها: {len(admins)} نفر")
        admin_usernames = [f"@{common.escape_markdown(admin['telegram_username'])}" for admin in admins if admin.get('telegram_username')]
        if admin_usernames:
            summary_lines.append(" ".join(admin_usernames))

    summary_lines.append("\n" + "📊 *گزارش کاربران عادی*")
    
    total_regular_users = len(regular_users)
    users_with_no_package = 0
    package_counts = {pkg['$id']: 0 for pkg in all_packages}
    
    for user in regular_users:
        pkg_id = user.get('package_id')
        if pkg_id and pkg_id in package_counts:
            package_counts[pkg_id] += 1
        else:
            users_with_no_package += 1
            
    summary_lines.extend([
        f"👥 *تعداد کل کاربران عادی:* {total_regular_users} نفر",
        f"▫️ کاربران بدون پکیج: {users_with_no_package} نفر",
    ])
    for pkg_id, count in package_counts.items():
        if count > 0:
            pkg_name = common.escape_markdown(package_map.get(pkg_id, 'پکیج حذف شده'))
            summary_lines.append(f"▫️ {pkg_name}: {count} نفر")
            
    summary_text = "\n".join(summary_lines) + "\n\n" + "لیست کاربران (ادمین‌ها در ابتدا):"
    
    sorted_users = sorted(all_users, key=lambda u: (not u.get('is_admin', False), u.get('created_at', '')), reverse=True)
    
    start_index = page * PAGE_SIZE
    end_index = start_index + PAGE_SIZE
    users_on_page = sorted_users[start_index:end_index]
    
    keyboard = []
    for user in users_on_page:
        display_name = (user.get('telegram_username')
                        or user.get('full_name') 
                        or user.get('clickup_username') 
                        or f"ID: {user['telegram_id']}")
        status = "✅" if user.get('is_active') else "❌"
        admin_marker = "👑 " if user.get('is_admin') else ""
        keyboard.append([InlineKeyboardButton(f"{admin_marker}{status} {display_name}", callback_data=f"admin_user_view_{user['telegram_id']}")])
        
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"admin_user_page_{page - 1}"))
    if end_index < len(sorted_users):
        nav_buttons.append(InlineKeyboardButton("▶️ بعدی", callback_data=f"admin_user_page_{page + 1}"))
        
    if nav_buttons:
        keyboard.append(nav_buttons)
        
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به پنل ادمین", callback_data="admin_user_back_panel")])

    await common.send_or_edit(update, summary_text, InlineKeyboardMarkup(keyboard))

async def view_user_details(update: Update, context: ContextTypes.DEFAULT_TYPE, user_telegram_id: str):
    """Displays full details for a specific user with management buttons."""
    user_doc = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_telegram_id)
    if not user_doc:
        await common.send_or_edit(update, "❌ کاربر مورد نظر یافت نشد.")
        return

    package_name = "بدون پکیج"
    if package_id := user_doc.get('package_id'):
        pkg_doc = await asyncio.to_thread(database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_id)
        if pkg_doc:
            package_name = pkg_doc.get('package_name', 'نامشخص')

    user_display_name = (user_doc.get('telegram_username') 
                         or user_doc.get('full_name') 
                         or user_doc.get('clickup_username') 
                         or f"ID: {user_doc['telegram_id']}")
                         
    admin_marker = "👑 (ادمین)" if user_doc.get('is_admin') else ""
    status_text = "✅ فعال" if user_doc.get('is_active', True) else "❌ مسدود"
    toggle_text = "مسدود کردن" if user_doc.get('is_active', True) else "رفع مسدودی"

    usage_limit = user_doc.get('usage_limit') or 0
    used_count = user_doc.get('used_count') or 0
    
    ai_usage_text = f"{used_count} / {usage_limit if usage_limit > 0 else 'نامحدود'}"
    remaining_text = f"{usage_limit - used_count if usage_limit > 0 else 'نامحدود'} باقیمانده"

    escaped_display_name = common.escape_markdown(user_display_name)
    escaped_full_name = common.escape_markdown(user_doc.get('full_name', 'ثبت نشده'))
    escaped_username = common.escape_markdown(user_doc.get('telegram_username', 'ندارد'))
    escaped_package_name = common.escape_markdown(package_name)

    text = (f"👤 *مشخصات کاربر: {escaped_display_name} {admin_marker}*\n\n"
            f"🆔 *شناسه تلگرام:* `{user_doc['telegram_id']}`\n"
            f"🗣️ *نام تلگرام:* {escaped_full_name}\n"
            f"🌐 *یوزرنیم تلگرام:* @{escaped_username}\n"
            f"📊 *وضعیت حساب:* {status_text}\n"
            f"📦 *پکیج فعلی:* {escaped_package_name}\n"
            f"🗓️ *تاریخ فعال‌سازی:* {format_datetime_field(user_doc.get('package_activation_date'))}\n"
            f"⏳ *تاریخ انقضا:* {format_datetime_field(user_doc.get('expiry_date'))}\n"
            f"🤖 *مصرف هوش مصنوعی:* {ai_usage_text} ({remaining_text})")

    keyboard = [
        [
            InlineKeyboardButton(f"🔄 {toggle_text}", callback_data=f"admin_user_toggle_{user_telegram_id}"),
            InlineKeyboardButton("✉️ ارسال پیام", callback_data=f"admin_user_send_message_{user_telegram_id}")
        ],
        [InlineKeyboardButton("🗑️ حذف کاربر", callback_data=f"admin_user_delete_{user_telegram_id}")],
        [InlineKeyboardButton("🔙 بازگشت به لیست کاربران", callback_data="admin_user_page_0")]
    ]

    await common.send_or_edit(update, text, InlineKeyboardMarkup(keyboard))


async def admin_user_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button clicks in the user management section."""
    query = update.callback_query
    await query.answer()

    data_parts = query.data.split('_')
    action = data_parts[2]

    if action == "page":
        page_num = int(data_parts[3])
        await manage_users_entry(update, context, page=page_num)
    
    elif action == "view":
        user_telegram_id = data_parts[3]
        await view_user_details(update, context, user_telegram_id)
    
    elif action == "toggle":
        user_telegram_id = data_parts[3]
        user_doc = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_telegram_id)
        if user_doc:
            current_status = user_doc.get('is_active', True)
            new_status = not current_status
            await asyncio.to_thread(database.upsert_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_telegram_id, {'is_active': new_status})
            
            status_text = "فعال" if new_status else "مسدود"
            try:
                await context.bot.send_message(
                    chat_id=user_telegram_id,
                    text=f"ℹ️ وضعیت حساب کاربری شما توسط ادمین به حالت *{status_text}* تغییر یافت.",
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.warning(f"Could not send status change notification to user {user_telegram_id}: {e}")
                
            await view_user_details(update, context, user_telegram_id)
            
    elif action == "delete":
        user_telegram_id = data_parts[3]
        keyboard = [[InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"admin_user_confirm_delete_{user_telegram_id}")],
                    [InlineKeyboardButton("❌ خیر، بازگشت", callback_data=f"admin_user_view_{user_telegram_id}")]]
        await query.message.edit_text("⚠️ آیا از حذف این کاربر مطمئن هستید؟ این عمل تمام اطلاعات کاربر را پاک می‌کند و غیرقابل بازگشت است.", reply_markup=InlineKeyboardMarkup(keyboard))

    elif action == "confirm" and data_parts[3] == "delete":
        user_telegram_id = data_parts[4]
        user_doc = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_telegram_id)
        if user_doc:
            await asyncio.to_thread(database.delete_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, user_doc['$id'])
            await query.message.edit_text(f"✅ کاربر با شناسه `{user_telegram_id}` با موفقیت حذف شد.")
            await manage_users_entry(update, context, page=0)
        else:
            await query.message.edit_text("❌ کاربر یافت نشد. ممکن است قبلا حذف شده باشد.")

    elif action == "back" and data_parts[3] == "panel":
        from . import admin_handler
        await query.message.delete()
        await admin_handler.start_for_admin(update, context)


# --- Direct Message Conversation ---
async def send_direct_message_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation for an admin to send a direct message to a user."""
    query = update.callback_query
    await query.answer()
    
    user_telegram_id = query.data.split('_')[-1]
    context.user_data['direct_message_user_id'] = user_telegram_id
    
    keyboard = [[InlineKeyboardButton("لغو ❌", callback_data=f"cancel_direct_message_{user_telegram_id}")]]
    await query.message.edit_text(
        "لطفاً پیام خود را برای ارسال به این کاربر تایپ کنید.",
        reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return AWAITING_DIRECT_MESSAGE

async def direct_message_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sends the admin's message to the user and ends the conversation."""
    admin_message = update.message.text
    user_telegram_id = context.user_data.pop('direct_message_user_id', None)
    
    if not user_telegram_id:
        await update.message.reply_text("خطا: شناسه کاربر برای ارسال پیام یافت نشد.")
        return ConversationHandler.END

    message_to_user = f" Jawab: پیام جدیدی از طرف پشتیبانی دریافت کردید:\n\n`{admin_message}`"
    
    try:
        await context.bot.send_message(chat_id=user_telegram_id, text=message_to_user, parse_mode='Markdown')
        await update.message.reply_text("✅ پیام شما با موفقیت برای کاربر ارسال شد.")
    except Exception as e:
        logger.error(f"Failed to send direct message to user {user_telegram_id}: {e}")
        await update.message.reply_text("❌ ارسال پیام به کاربر ناموفق بود.")
        
    await view_user_details(update, context, user_telegram_id)
    return ConversationHandler.END

async def cancel_direct_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the direct message process and returns to user details."""
    query = update.callback_query
    await query.answer()
    user_telegram_id = query.data.split('_')[-1]
    context.user_data.pop('direct_message_user_id', None)
    await view_user_details(update, context, user_telegram_id)
    return ConversationHandler.END

def get_send_direct_message_conv_handler():
    """Creates the ConversationHandler for sending direct messages."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(send_direct_message_start, pattern='^admin_user_send_message_')],
        states={
            AWAITING_DIRECT_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, direct_message_received)]
        },
        fallbacks=[
            CallbackQueryHandler(cancel_direct_message, pattern='^cancel_direct_message_'),
            CommandHandler("cancel", common.generic_cancel_conversation)
            ]
    )


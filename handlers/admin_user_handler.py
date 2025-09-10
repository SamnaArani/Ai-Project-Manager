# -*- coding: utf-8 -*-
import asyncio
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import config
import database
from . import common

logger = logging.getLogger(__name__)

PAGE_SIZE = 5 # تعداد کاربران در هر صفحه

def format_datetime_field(dt_string):
    """تاریخ را از فرمت ISO به فرمت خوانا تبدیل می‌کند."""
    if not dt_string:
        return "ثبت نشده"
    try:
        if dt_string.endswith('Z'):
            dt_string = dt_string[:-1] + '+00:00'
        return datetime.fromisoformat(dt_string).strftime('%Y-%m-%d %H:%M')
    except (ValueError, TypeError):
        logger.warning(f"Could not parse datetime string: {dt_string}")
        return "نامعتبر"

async def manage_users_entry(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """
    نقطه ورود به بخش مدیریت کاربران.
    یک گزارش آماری به همراه لیست صفحه‌بندی شده کاربران را نمایش می‌دهد.
    """
    all_users = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID)
    all_packages = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID)
    
    package_map = {pkg['$id']: pkg['package_name'] for pkg in all_packages}
    
    # --- Separate admins from regular users ---
    admins = [u for u in all_users if u.get('is_admin')]
    regular_users = [u for u in all_users if not u.get('is_admin')]
    
    # --- Build Admin Stats ---
    summary_lines = ["👑 *گزارش ادمین‌ها*"]
    if not admins:
        summary_lines.append("هیچ ادمینی تعریف نشده است.")
    else:
        summary_lines.append(f"تعداد کل ادمین‌ها: {len(admins)} نفر")
        admin_usernames = [f"@{admin['telegram_username']}" for admin in admins if admin.get('telegram_username')]
        if admin_usernames:
            summary_lines.append(" ".join(admin_usernames))

    summary_lines.append("\n" + "📊 *گزارش کاربران عادی*")
    
    # --- Build Regular User Stats ---
    total_regular_users = len(regular_users)
    users_with_no_package = 0
    package_counts = {pkg['$id']: 0 for pkg in all_packages}
    
    for user in regular_users:
        if user.get('package_id') and user['package_id'] in package_counts:
            package_counts[user['package_id']] += 1
        else:
            users_with_no_package += 1
            
    summary_lines.extend([
        f"👥 *تعداد کل کاربران عادی:* {total_regular_users} نفر",
        f"▫️ کاربران بدون پکیج: {users_with_no_package} نفر",
    ])
    for pkg_id, count in package_counts.items():
        if count > 0:
            summary_lines.append(f"▫️ {package_map.get(pkg_id, 'پکیج حذف شده')}: {count} نفر")
            
    summary_text = "\n".join(summary_lines) + "\n\n" + "لیست کاربران (ادمین‌ها در ابتدا):"
    
    # --- Sort users to show admins first, then by creation date ---
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
    if end_index < len(all_users):
        nav_buttons.append(InlineKeyboardButton("▶️ بعدی", callback_data=f"admin_user_page_{page + 1}"))
        
    if nav_buttons:
        keyboard.append(nav_buttons)
        
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به پنل ادمین", callback_data="admin_user_back_panel")])

    await common.send_or_edit(update, summary_text, InlineKeyboardMarkup(keyboard))

async def view_user_details(update: Update, context: ContextTypes.DEFAULT_TYPE, user_telegram_id: str):
    """جزئیات کامل یک کاربر را به همراه دکمه‌های مدیریتی نمایش می‌دهد."""
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
    status_text = "✅ فعال" if user_doc.get('is_active') else "❌ مسدود"
    toggle_text = "مسدود کردن" if user_doc.get('is_active') else "رفع مسدودی"

    usage_limit = user_doc.get('usage_limit') or 0
    used_count = user_doc.get('used_count') or 0
    
    ai_usage_text = f"{used_count} / {usage_limit if usage_limit > 0 else 'نامحدود'}"
    remaining_text = f"{usage_limit - used_count if usage_limit > 0 else 'نامحدود'} باقیمانده"

    text = (f"👤 *مشخصات کاربر: {user_display_name} {admin_marker}*\n\n"
            f"🆔 *شناسه تلگرام:* `{user_doc['telegram_id']}`\n"
            f"🗣️ *نام تلگرام:* {user_doc.get('full_name', 'ثبت نشده')}\n"
            f"🌐 *یوزرنیم تلگرام:* @{user_doc.get('telegram_username', 'ندارد')}\n"
            f"📊 *وضعیت حساب:* {status_text}\n"
            f"📦 *پکیج فعلی:* {package_name}\n"
            f"🗓️ *تاریخ فعال‌سازی:* {format_datetime_field(user_doc.get('package_activation_date'))}\n"
            f"⏳ *تاریخ انقضا:* {format_datetime_field(user_doc.get('expiry_date'))}\n"
            f"🤖 *مصرف هوش مصنوعی:* {ai_usage_text} ({remaining_text})")

    keyboard = [
        [InlineKeyboardButton(f"🔄 {toggle_text}", callback_data=f"admin_user_toggle_{user_telegram_id}")],
        [InlineKeyboardButton("🗑️ حذف کاربر", callback_data=f"admin_user_delete_{user_telegram_id}")],
        [InlineKeyboardButton("🔙 بازگشت به لیست کاربران", callback_data="admin_user_page_0")]
    ]

    await common.send_or_edit(update, text, InlineKeyboardMarkup(keyboard))


async def admin_user_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دکمه‌های کلیک شده در بخش مدیریت کاربران را مدیریت می‌کند."""
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
            current_status = user_doc.get('is_active', False)
            await asyncio.to_thread(database.upsert_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_telegram_id, {'is_active': not current_status})
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
        await admin_handler.show_admin_panel(update, context)


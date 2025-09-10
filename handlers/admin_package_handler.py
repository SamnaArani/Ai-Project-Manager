# -*- coding: utf-8 -*-
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, 
    ConversationHandler, 
    CommandHandler, 
    MessageHandler, 
    filters,
    CallbackQueryHandler,
)
import config
import database
from . import common

logger = logging.getLogger(__name__)

# --- Conversation States ---
(PKG_NAME, PKG_DESCRIPTION, PKG_AI_LIMIT, PKG_PRICE, 
 EDIT_PKG_SELECT_FIELD, EDIT_PKG_TYPING_VALUE) = range(6)

# --- Package Management Functions ---

async def manage_packages_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the main package management menu."""
    packages = await asyncio.to_thread(
        database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID
    )
    text = "📦 *مدیریت پکیج‌ها*\n\nدر این بخش می‌توانید پکیج‌ها را مشاهده، ویرایش یا غیرفعال کنید.\n"
    keyboard = []
    if not packages:
        text += "\nهیچ پکیجی تاکنون ساخته نشده است."
    else:
        for pkg in packages:
            status = "✅ فعال" if pkg.get('is_active') else "⭕️ غیرفعال"
            keyboard.append([InlineKeyboardButton(f"{pkg['package_name']} ({status})", callback_data=f"admin_pkg_view_{pkg['$id']}")])
    keyboard.append([InlineKeyboardButton("➕ افزودن پکیج جدید", callback_data="admin_pkg_add")])
    await common.send_or_edit(update, text, InlineKeyboardMarkup(keyboard))

async def view_package_details(update: Update, context: ContextTypes.DEFAULT_TYPE, package_id: str = None):
    """Displays details of a specific package."""
    query = update.callback_query
    if package_id is None:
        if not query: return
        package_id = query.data.split('_')[-1]
    if query: await query.answer()

    pkg_list = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, [database.Query.equal("$id", [package_id])])
    if not pkg_list:
        await common.send_or_edit(update, "❌ پکیج مورد نظر یافت نشد.")
        return
    pkg = pkg_list[0]
    
    active_users = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, [database.Query.equal("package_id", [package_id]), database.Query.equal("is_active", [True])])
    user_count = len(active_users)

    price = "رایگان" if pkg['monthly_price'] == 0 else f"{pkg['monthly_price']:,} تومان/ماه"
    ai_limit = "نامحدود" if pkg['ai_call_limit'] == 0 else f"{pkg['ai_call_limit']} تماس/ماه"
    status_text = "✅ فعال" if pkg.get('is_active') else "⭕️ غیرفعال"
    toggle_text = "غیرفعال کردن" if pkg.get('is_active') else "فعال کردن"

    text = (f"📦 *جزئیات پکیج: {pkg['package_name']}*\n\n"
            f"▫️ *قیمت:* {price}\n"
            f"▫️ *محدودیت AI:* {ai_limit}\n"
            f"▫️ *وضعیت:* {status_text}\n"
            f"▫️ *تعداد کاربران فعال:* {user_count} نفر\n\n"
            f"📜 *توضیحات:*\n{pkg.get('package_description', 'ندارد')}")
    keyboard = [[InlineKeyboardButton("✏️ ویرایش", callback_data=f"admin_pkg_edit_{package_id}"),
                 InlineKeyboardButton(f"🔄 {toggle_text}", callback_data=f"admin_pkg_toggle_{package_id}"),
                 InlineKeyboardButton("🗑️ حذف", callback_data=f"admin_pkg_delete_{package_id}")],
                [InlineKeyboardButton("🔙 بازگشت به لیست پکیج‌ها", callback_data="admin_pkg_back")]]
    await common.send_or_edit(update, text, InlineKeyboardMarkup(keyboard))

async def admin_package_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles buttons related to package management."""
    query = update.callback_query
    await query.answer()
    
    data_parts = query.data.split('_')
    action = data_parts[2]
    package_id = data_parts[-1] if len(data_parts) > 3 else None
    
    if action == "view":
        await view_package_details(update, context)
    elif action == "back":
        await manage_packages_entry(update, context)
    elif action == "toggle":
        pkg_list = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, [database.Query.equal("$id", [package_id])])
        if not pkg_list: return
        current_status = pkg_list[0].get('is_active', False)
        await asyncio.to_thread(database.upsert_document, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, '$id', package_id, {'is_active': not current_status})
        await view_package_details(update, context, package_id=package_id)
    elif action == "delete":
        keyboard = [[InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"admin_pkg_confirm_delete_{package_id}")],
                    [InlineKeyboardButton("❌ خیر، بازگشت", callback_data=f"admin_pkg_view_{package_id}")]]
        await query.message.edit_text("⚠️ آیا از حذف این پکیج مطمئن هستید؟\n\nاین عمل غیرقابل بازگشت است.", reply_markup=InlineKeyboardMarkup(keyboard))
    elif action == "confirm" and data_parts[3] == "delete":
        active_users = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, [database.Query.equal("package_id", [package_id])])
        if active_users:
            await query.message.edit_text(f"❌ امکان حذف این پکیج وجود ندارد زیرا {len(active_users)} کاربر در حال استفاده از آن هستند.")
            return
        await asyncio.to_thread(database.delete_document, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_id)
        await query.message.edit_text("✅ پکیج با موفقیت حذف شد.")
        await manage_packages_entry(update, context)

# --- New Package Conversation ---

async def new_package_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]
    await common.send_or_edit(update, "شما در حال ساخت یک پکیج جدید هستید.\n\nلطفاً نام پکیج را وارد کنید:", InlineKeyboardMarkup(keyboard))
    return PKG_NAME

async def pkg_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_package'] = {'package_name': update.message.text}
    keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]
    await update.message.reply_text("نام ذخیره شد. لطفاً توضیحات پکیج را وارد کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    return PKG_DESCRIPTION

async def pkg_description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_package']['package_description'] = update.message.text
    keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]
    await update.message.reply_text("توضیحات ذخیره شد. لطفاً تعداد مجاز تماس با AI در ماه را به صورت عدد وارد کنید (برای نامحدود عدد 0 را بزنید):", reply_markup=InlineKeyboardMarkup(keyboard))
    return PKG_AI_LIMIT

async def pkg_ai_limit_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]
    try:
        context.user_data['new_package']['ai_call_limit'] = int(update.message.text)
        await update.message.reply_text("تعداد تماس ذخیره شد. لطفاً قیمت ماهانه پکیج را به تومان (فقط عدد) وارد کنید (برای رایگان عدد 0 را بزنید):", reply_markup=InlineKeyboardMarkup(keyboard))
        return PKG_PRICE
    except ValueError:
        await update.message.reply_text("❌ لطفاً فقط یک عدد صحیح وارد کنید.", reply_markup=InlineKeyboardMarkup(keyboard))
        return PKG_AI_LIMIT

async def pkg_price_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        package_data = context.user_data['new_package']
        package_data['monthly_price'] = int(update.message.text)
        package_data['is_active'] = True
        await asyncio.to_thread(database.create_document, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_data)
        await update.message.reply_text(f"✅ پکیج '{package_data['package_name']}' با موفقیت ایجاد شد.")
        context.user_data.pop('new_package', None)
        await manage_packages_entry(update, context)
        return ConversationHandler.END
    except ValueError:
        keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]
        await update.message.reply_text("❌ لطفاً قیمت را فقط به صورت عدد صحیح وارد کنید.", reply_markup=InlineKeyboardMarkup(keyboard))
        return PKG_PRICE
    except Exception as e:
        logger.error(f"خطا در ذخیره پکیج جدید: {e}", exc_info=True)
        await update.message.reply_text("❌ خطایی در هنگام ذخیره پکیج رخ داد.")
        context.user_data.pop('new_package', None)
        return ConversationHandler.END

def get_new_package_conv_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(new_package_start, pattern='^admin_pkg_add$')],
        states={
            PKG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_name_received)],
            PKG_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_description_received)],
            PKG_AI_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_ai_limit_received)],
            PKG_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_price_received)],
        },
        fallbacks=[CallbackQueryHandler(common.generic_cancel_conversation, pattern='^generic_cancel$')],
        block=True
    )

# --- Edit Package Conversation ---

async def edit_package_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    package_id = query.data.split('_')[-1]
    context.user_data['edit_package_id'] = package_id
    keyboard = [[InlineKeyboardButton("نام پکیج", callback_data="edit_pkg_field_package_name")],
                [InlineKeyboardButton("توضیحات", callback_data="edit_pkg_field_package_description")],
                [InlineKeyboardButton("محدودیت AI", callback_data="edit_pkg_field_ai_call_limit")],
                [InlineKeyboardButton("قیمت ماهانه", callback_data="edit_pkg_field_monthly_price")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data=f"admin_pkg_view_{package_id}")]]
    await query.message.edit_text("کدام بخش از پکیج را می‌خواهید ویرایش کنید؟", reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_PKG_SELECT_FIELD

async def edit_pkg_field_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    package_id = context.user_data.get('edit_package_id')
    if not package_id: return ConversationHandler.END
    pkg_list = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, [database.Query.equal("$id", [package_id])])
    if not pkg_list: return ConversationHandler.END
    pkg = pkg_list[0]
    field_to_edit = query.data.replace('edit_pkg_field_', '')
    context.user_data['field_to_edit'] = field_to_edit
    current_value = pkg.get(field_to_edit, 'تعیین نشده')
    field_map = {"package_name": "نام پکیج", "package_description": "توضیحات",
                 "ai_call_limit": "محدودیت AI", "monthly_price": "قیمت ماهانه"}
    prompt_text = (f"در حال ویرایش: *{field_map.get(field_to_edit, '')}*\n"
                   f"مقدار فعلی: `{current_value}`\n\n"
                   f"لطفاً مقدار جدید را وارد کنید:")
    keyboard = [[InlineKeyboardButton("❌ لغو ویرایش", callback_data="generic_cancel")]]
    await query.message.edit_text(prompt_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return EDIT_PKG_TYPING_VALUE

async def edit_pkg_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    field = context.user_data.get('field_to_edit')
    package_id = context.user_data.get('edit_package_id')
    if not field or not package_id:
        await update.message.reply_text("❌ خطایی در فرآیند ویرایش رخ داد.")
        context.user_data.clear()
        return ConversationHandler.END
    new_value = update.message.text
    if field in ['ai_call_limit', 'monthly_price']:
        try:
            new_value = int(new_value)
        except ValueError:
            await update.message.reply_text("❌ لطفاً فقط یک عدد صحیح وارد کنید.")
            return EDIT_PKG_TYPING_VALUE
    await asyncio.to_thread(database.upsert_document, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, '$id', package_id, {field: new_value})
    await update.message.reply_text("✅ پکیج با موفقیت به‌روزرسانی شد.")
    await view_package_details(update, context, package_id=package_id)
    context.user_data.clear()
    return ConversationHandler.END

def get_edit_package_conv_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_package_start, pattern='^admin_pkg_edit_')],
        states={
            EDIT_PKG_SELECT_FIELD: [CallbackQueryHandler(edit_pkg_field_selected, pattern='^edit_pkg_field_')],
            EDIT_PKG_TYPING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_pkg_value_received)],
        },
        fallbacks=[CallbackQueryHandler(common.generic_cancel_conversation, pattern='^generic_cancel$')],
        block=True
    )


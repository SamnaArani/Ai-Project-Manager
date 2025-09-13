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
from appwrite.query import Query


logger = logging.getLogger(__name__)

# --- Conversation States ---
(PKG_NAME, PKG_DESCRIPTION, PKG_DURATION, PKG_PRICE, 
 PKG_ALLOW_CHAT, PKG_DAILY_CHAT_LIMIT, PKG_MONTHLY_CHAT_LIMIT,
 PKG_ALLOW_COMMANDS, PKG_DAILY_CMD_LIMIT, PKG_MONTHLY_CMD_LIMIT,
 EDIT_PKG_SELECT_FIELD, EDIT_PKG_TYPING_VALUE) = range(12)


# --- Helper for conversation robustness ---
async def _check_conv_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks if 'new_package' exists in user_data. If not, cancels the conversation."""
    if 'new_package' not in context.user_data:
        target = update.message or (update.callback_query and update.callback_query.message)
        if target:
            await target.reply_text("متاسفانه فرآیند ساخت پکیج به دلیل وقفه لغو شد. لطفاً دوباره شروع کنید.")
        context.user_data.clear()
        return False
    return True

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

def format_limit(limit_val):
    return "نامحدود" if limit_val == 0 else f"{limit_val:,}"

async def view_package_details(update: Update, context: ContextTypes.DEFAULT_TYPE, package_id: str = None):
    """Displays details of a specific package."""
    query = update.callback_query
    if package_id is None:
        if not query: return
        package_id = query.data.split('_')[-1]
    if query: await query.answer()

    pkg = await asyncio.to_thread(database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_id)
    if not pkg:
        await common.send_or_edit(update, "❌ پکیج مورد نظر یافت نشد.")
        return
    
    active_users_query = [Query.equal("package_id", [package_id]), Query.equal("is_active", [True])]
    active_users = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, active_users_query)
    user_count = len(active_users)

    price = "رایگان" if pkg.get('monthly_price', 0) == 0 else f"{pkg.get('monthly_price', 0):,} تومان/ماه"
    status_text = "✅ فعال" if pkg.get('is_active') else "⭕️ غیرفعال"
    toggle_text = "غیرفعال کردن" if pkg.get('is_active') else "فعال کردن"
    
    escaped_pkg_name = common.escape_markdown(pkg['package_name'])
    escaped_description = common.escape_markdown(pkg.get('package_description', 'ندارد'))
    
    chat_status = "✅ فعال" if pkg.get('allow_ai_chat') else "❌ غیرفعال"
    cmd_status = "✅ فعال" if pkg.get('allow_ai_commands') else "❌ غیرفعال"

    text = (f"📦 *جزئیات پکیج: {escaped_pkg_name}*\n\n"
            f"💰 *قیمت:* {price}\n"
            f"⏳ *مدت زمان:* {pkg.get('package_duration_days', 30)} روز\n"
            f"📊 *وضعیت:* {status_text}\n"
            f"👥 *تعداد کاربران فعال:* {user_count} نفر\n\n"
            f"💬 *چت با هوش مصنوعی:* {chat_status}\n"
            f" - محدودیت روزانه: {format_limit(pkg.get('daily_chat_limit', 0))}\n"
            f" - محدودیت ماهانه: {format_limit(pkg.get('monthly_chat_limit', 0))}\n\n"
            f"🤖 *دستورات هوشمند:* {cmd_status}\n"
            f" - محدودیت روزانه: {format_limit(pkg.get('daily_command_limit', 0))}\n"
            f" - محدودیت ماهانه: {format_limit(pkg.get('monthly_command_limit', 0))}\n\n"
            f"📜 *توضیحات:*\n{escaped_description}")

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
        pkg = await asyncio.to_thread(database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_id)
        if not pkg: return
        current_status = pkg.get('is_active', False)
        await asyncio.to_thread(database.upsert_document, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, '$id', package_id, {'is_active': not current_status})
        await view_package_details(update, context, package_id=package_id)
    elif action == "delete":
        keyboard = [[InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"admin_pkg_confirm_delete_{package_id}")],
                    [InlineKeyboardButton("❌ خیر، بازگشت", callback_data=f"admin_pkg_view_{package_id}")]]
        await query.message.edit_text("⚠️ آیا از حذف این پکیج مطمئن هستید؟ این عمل غیرقابل بازگشت است.", reply_markup=InlineKeyboardMarkup(keyboard))
    elif action == "confirm" and data_parts[3] == "delete":
        active_users = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, [Query.equal("package_id", [package_id])])
        if active_users:
            await query.message.edit_text(f"❌ امکان حذف این پکیج وجود ندارد زیرا {len(active_users)} کاربر در حال استفاده از آن هستند.")
            return
        await asyncio.to_thread(database.delete_document, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_id)
        await query.message.edit_text("✅ پکیج با موفقیت حذف شد.")
        await manage_packages_entry(update, context)

# --- New Package Conversation ---

async def new_package_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_package'] = {}
    await common.send_or_edit(update, "شما در حال ساخت یک پکیج جدید هستید.\n\nلطفاً نام پکیج را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]))
    return PKG_NAME

async def pkg_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_conv_state(update, context): return ConversationHandler.END
    context.user_data['new_package']['package_name'] = update.message.text
    await update.message.reply_text("نام ذخیره شد. لطفاً توضیحات پکیج را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]))
    return PKG_DESCRIPTION

async def pkg_description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_conv_state(update, context): return ConversationHandler.END
    context.user_data['new_package']['package_description'] = update.message.text
    await update.message.reply_text("توضیحات ذخیره شد. لطفاً مدت زمان پکیج به روز را وارد کنید (مثلا: 30):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]))
    return PKG_DURATION

async def pkg_duration_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_conv_state(update, context): return ConversationHandler.END
    try:
        context.user_data['new_package']['package_duration_days'] = int(update.message.text)
        await update.message.reply_text("مدت زمان ذخیره شد. لطفاً قیمت ماهانه پکیج را به تومان (فقط عدد) وارد کنید (برای رایگان عدد 0 را بزنید):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]))
        return PKG_PRICE
    except ValueError:
        await update.message.reply_text("❌ لطفاً فقط یک عدد صحیح وارد کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]))
        return PKG_DURATION

async def pkg_price_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_conv_state(update, context): return ConversationHandler.END
    try:
        context.user_data['new_package']['monthly_price'] = int(update.message.text)
        keyboard = [
            [InlineKeyboardButton("✅ بله", callback_data="pkg_bool_true")],
            [InlineKeyboardButton("❌ خیر", callback_data="pkg_bool_false")]
        ]
        await update.message.reply_text("قیمت ذخیره شد. آیا چت با هوش مصنوعی در این پکیج فعال باشد؟", reply_markup=InlineKeyboardMarkup(keyboard))
        return PKG_ALLOW_CHAT
    except ValueError:
        await update.message.reply_text("❌ لطفاً قیمت را فقط به صورت عدد صحیح وارد کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]))
        return PKG_PRICE

async def _ask_allow_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Helper function to ask for AI command permission."""
    keyboard = [[InlineKeyboardButton("✅ بله", callback_data="pkg_bool_true")], [InlineKeyboardButton("❌ خیر", callback_data="pkg_bool_false")]]
    await common.send_or_edit(update, "ذخیره شد. آیا دستورات هوشمند در این پکیج فعال باشد؟", reply_markup=InlineKeyboardMarkup(keyboard))

async def pkg_allow_chat_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_conv_state(update, context): return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    
    allow_chat = (query.data == 'pkg_bool_true')
    context.user_data['new_package']['allow_ai_chat'] = allow_chat

    if allow_chat:
        await query.message.edit_text("تنظیمات ذخیره شد. لطفاً محدودیت روزانه چت را وارد کنید (عدد 0 برای نامحدود):")
        return PKG_DAILY_CHAT_LIMIT
    else:
        # Skip chat limit questions if chat is disabled
        context.user_data['new_package']['daily_chat_limit'] = 0
        context.user_data['new_package']['monthly_chat_limit'] = 0
        await query.message.delete() # Remove the previous message before sending the new one
        await _ask_allow_commands(update, context)
        return PKG_ALLOW_COMMANDS
    
async def pkg_daily_chat_limit_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_conv_state(update, context): return ConversationHandler.END
    try:
        context.user_data['new_package']['daily_chat_limit'] = int(update.message.text)
        await update.message.reply_text("ذخیره شد. لطفاً محدودیت ماهانه چت را وارد کنید (عدد 0 برای نامحدود):")
        return PKG_MONTHLY_CHAT_LIMIT
    except ValueError:
        await update.message.reply_text("❌ لطفاً فقط یک عدد صحیح وارد کنید.")
        return PKG_DAILY_CHAT_LIMIT

async def pkg_monthly_chat_limit_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_conv_state(update, context): return ConversationHandler.END
    try:
        context.user_data['new_package']['monthly_chat_limit'] = int(update.message.text)
        await _ask_allow_commands(update, context)
        return PKG_ALLOW_COMMANDS
    except ValueError:
        await update.message.reply_text("❌ لطفاً فقط یک عدد صحیح وارد کنید.")
        return PKG_MONTHLY_CHAT_LIMIT

async def _finalize_package_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Helper to save the new package to the database and end the conversation."""
    context.chat_data['conversation_handled'] = True
    try:
        package_data = context.user_data['new_package']
        package_data['is_active'] = True
        
        await asyncio.to_thread(database.create_document, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_data)
        
        # Use a consistent way to send the final message
        final_message_target = update.message or (update.callback_query and update.callback_query.message)
        if final_message_target:
             await final_message_target.reply_text(f"✅ پکیج '{package_data['package_name']}' با موفقیت ایجاد شد.")

        context.user_data.pop('new_package', None)
        await manage_packages_entry(update, context)
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"خطا در ذخیره پکیج جدید: {e}", exc_info=True)
        await common.send_or_edit(update, "❌ خطایی در هنگام ذخیره پکیج رخ داد.")
        context.user_data.pop('new_package', None)
        return ConversationHandler.END

async def pkg_allow_commands_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_conv_state(update, context): return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    
    allow_commands = (query.data == 'pkg_bool_true')
    context.user_data['new_package']['allow_ai_commands'] = allow_commands

    if allow_commands:
        await query.message.edit_text("تنظیمات ذخیره شد. لطفاً محدودیت روزانه دستورات هوشمند را وارد کنید (عدد 0 برای نامحدود):")
        return PKG_DAILY_CMD_LIMIT
    else:
        # Skip command limit questions and finalize
        context.user_data['new_package']['daily_command_limit'] = 0
        context.user_data['new_package']['monthly_command_limit'] = 0
        await query.message.delete()
        return await _finalize_package_creation(update, context)


async def pkg_daily_cmd_limit_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_conv_state(update, context): return ConversationHandler.END
    try:
        context.user_data['new_package']['daily_command_limit'] = int(update.message.text)
        await update.message.reply_text("ذخیره شد. لطفاً محدودیت ماهانه دستورات هوشمند را وارد کنید (عدد 0 برای نامحدود):")
        return PKG_MONTHLY_CMD_LIMIT
    except ValueError:
        await update.message.reply_text("❌ لطفاً فقط یک عدد صحیح وارد کنید.")
        return PKG_DAILY_CMD_LIMIT

async def pkg_monthly_cmd_limit_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_conv_state(update, context): return ConversationHandler.END
    try:
        context.user_data['new_package']['monthly_command_limit'] = int(update.message.text)
        return await _finalize_package_creation(update, context)
    except ValueError:
        await update.message.reply_text("❌ لطفاً فقط یک عدد صحیح وارد کنید.")
        return PKG_MONTHLY_CMD_LIMIT


def get_new_package_conv_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(new_package_start, pattern='^admin_pkg_add$')],
        states={
            PKG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_name_received)],
            PKG_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_description_received)],
            PKG_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_duration_received)],
            PKG_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_price_received)],
            PKG_ALLOW_CHAT: [CallbackQueryHandler(pkg_allow_chat_received, pattern='^pkg_bool_')],
            PKG_DAILY_CHAT_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_daily_chat_limit_received)],
            PKG_MONTHLY_CHAT_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_monthly_chat_limit_received)],
            PKG_ALLOW_COMMANDS: [CallbackQueryHandler(pkg_allow_commands_received, pattern='^pkg_bool_')],
            PKG_DAILY_CMD_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_daily_cmd_limit_received)],
            PKG_MONTHLY_CMD_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_monthly_cmd_limit_received)],
        },
        fallbacks=[CallbackQueryHandler(common.generic_cancel_conversation, pattern='^generic_cancel$')],
    )

# --- Edit Package Conversation ---

async def edit_package_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    package_id = query.data.split('_')[-1]
    context.user_data['edit_package_id'] = package_id
    
    fields = {
        "نام پکیج": "package_name",
        "توضیحات": "package_description",
        "مدت زمان (روز)": "package_duration_days",
        "قیمت ماهانه": "monthly_price",
        "فعال‌سازی چت AI": "allow_ai_chat",
        "محدودیت روزانه چت": "daily_chat_limit",
        "محدودیت ماهانه چت": "monthly_chat_limit",
        "فعال‌سازی دستورات AI": "allow_ai_commands",
        "محدودیت روزانه دستور": "daily_command_limit",
        "محدودیت ماهانه دستور": "monthly_command_limit",
    }
    
    keyboard = [[InlineKeyboardButton(name, callback_data=f"edit_pkg_field_{key}")] for name, key in fields.items()]
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"admin_pkg_view_{package_id}")])
    
    await query.message.edit_text("کدام بخش از پکیج را می‌خواهید ویرایش کنید؟", reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_PKG_SELECT_FIELD

async def edit_pkg_field_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    package_id = context.user_data.get('edit_package_id')
    if not package_id: return ConversationHandler.END
    
    pkg = await asyncio.to_thread(database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_id)
    if not pkg: return ConversationHandler.END

    field_to_edit = query.data.replace('edit_pkg_field_', '')
    context.user_data['field_to_edit'] = field_to_edit
    
    current_value = pkg.get(field_to_edit, 'تعیین نشده')
    
    field_map = {
        "package_name": "نام پکیج", "package_description": "توضیحات", "package_duration_days": "مدت زمان",
        "monthly_price": "قیمت ماهانه", "allow_ai_chat": "فعال‌سازی چت AI", "daily_chat_limit": "محدودیت روزانه چت",
        "monthly_chat_limit": "محدودیت ماهانه چت", "allow_ai_commands": "فعال‌سازی دستورات AI",
        "daily_command_limit": "محدودیت روزانه دستور", "monthly_command_limit": "محدودیت ماهانه دستور"
    }

    if field_to_edit in ['allow_ai_chat', 'allow_ai_commands']:
        keyboard = [
            [InlineKeyboardButton("✅ فعال", callback_data=f"edit_pkg_val_True")],
            [InlineKeyboardButton("❌ غیرفعال", callback_data=f"edit_pkg_val_False")],
            [InlineKeyboardButton("🔙 لغو", callback_data=f"admin_pkg_edit_{package_id}")],
        ]
        await query.message.edit_text(f"در حال ویرایش: *{field_map.get(field_to_edit, '')}*\nمقدار فعلی: `{current_value}`\n\nلطفاً مقدار جدید را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        prompt_text = (f"در حال ویرایش: *{field_map.get(field_to_edit, '')}*\n"
                       f"مقدار فعلی: `{current_value}`\n\n"
                       f"لطفاً مقدار جدید را وارد کنید:")
        keyboard = [[InlineKeyboardButton("❌ لغو ویرایش", callback_data=f"admin_pkg_edit_{package_id}")]]
        await query.message.edit_text(prompt_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        
    return EDIT_PKG_TYPING_VALUE

async def edit_pkg_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['conversation_handled'] = True
    field = context.user_data.get('field_to_edit')
    package_id = context.user_data.get('edit_package_id')
    
    if not field or not package_id:
        await common.send_or_edit(update, "❌ خطایی در فرآیند ویرایش رخ داد.")
        context.user_data.clear()
        return ConversationHandler.END

    if update.message:
        new_value = update.message.text
    elif update.callback_query:
        await update.callback_query.answer()
        new_value = update.callback_query.data.split('_')[-1]
    else:
        return EDIT_PKG_TYPING_VALUE

    if field in ['package_duration_days', 'monthly_price', 'daily_chat_limit', 'monthly_chat_limit', 'daily_command_limit', 'monthly_command_limit']:
        try:
            new_value = int(new_value)
        except ValueError:
            await common.send_or_edit(update, "❌ لطفاً فقط یک عدد صحیح وارد کنید.")
            return EDIT_PKG_TYPING_VALUE
    elif field in ['allow_ai_chat', 'allow_ai_commands']:
        new_value = (new_value == 'True')

    await asyncio.to_thread(database.upsert_document, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, '$id', package_id, {field: new_value})
    
    await common.send_or_edit(update, "✅ پکیج با موفقیت به‌روزرسانی شد.")
    await view_package_details(update, context, package_id=package_id)
    
    context.user_data.clear()
    return ConversationHandler.END

def get_edit_package_conv_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_package_start, pattern='^admin_pkg_edit_')],
        states={
            EDIT_PKG_SELECT_FIELD: [CallbackQueryHandler(edit_pkg_field_selected, pattern='^edit_pkg_field_')],
            EDIT_PKG_TYPING_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_pkg_value_received),
                CallbackQueryHandler(edit_pkg_value_received, pattern='^edit_pkg_val_')
            ],
        },
        fallbacks=[
            CallbackQueryHandler(common.generic_cancel_conversation, pattern='^generic_cancel$'),
            # Go back to the edit menu if they cancel typing
            CallbackQueryHandler(edit_package_start, pattern='^admin_pkg_edit_')
            ],
    )


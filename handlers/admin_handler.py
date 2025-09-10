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

import config
import database
from . import common

logger = logging.getLogger(__name__)

# --- وضعیت‌های مکالمه ---
(PKG_NAME, PKG_DESCRIPTION, PKG_AI_LIMIT, PKG_PRICE, 
 EDIT_PKG_SELECT_FIELD, EDIT_PKG_TYPING_VALUE) = range(6)

# --- توابع ورودی پنل ادمین ---

async def admin_panel_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ورودی اصلی برای دکمه‌های پنل ادمین."""
    user_id = str(update.effective_user.id)
    if not await common.is_user_admin(user_id):
        return

    text = update.message.text
    if text == "📊 مدیریت کاربران":
        await update.message.reply_text("شما وارد بخش مدیریت کاربران شدید. (در حال توسعه)")
    elif text == "📦 مدیریت پکیج‌ها":
        await manage_packages_entry(update, context)
    elif text == "📈 گزارشات":
        await update.message.reply_text("شما وارد بخش گزارشات شدید. (در حال توسعه)")
    elif text == "⚙️ تنظیمات ربات":
        await update.message.reply_text("شما وارد بخش تنظیمات ربات شدید. (در حال توسعه)")


async def resync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """همگام‌سازی مجدد و کامل اطلاعات کاربر از کلیک‌اپ را به صورت دستی فعال می‌کند."""
    user_id = str(update.effective_user.id)
    token = await common.get_user_token(user_id, update, context)
    if not token:
        await update.message.reply_text("ابتدا باید با دستور /start ثبت نام کنید.")
        return

    await update.message.reply_text("شروع همگام‌سازی مجدد اطلاعات از ClickUp... این فرآیند ممکن است چند لحظه طول بکشد. ⏳")
    try:
        sync_success = await asyncio.to_thread(clickup_api.sync_all_user_data, token, user_id)
        if sync_success:
            await update.message.reply_text("✅ همگام‌سازی مجدد با موفقیت انجام شد. اکنون همه چیز باید به درستی کار کند.")
        else:
            await update.message.reply_text("❌ در هنگام همگام‌سازی مجدد خطایی رخ داد.")
    except Exception as e:
        logger.error(f"خطا در اجرای دستور /resync برای کاربر {user_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ یک خطای غیرمنتظره در حین همگام‌سازی رخ داد.")


# --- مدیریت پکیج‌ها ---

async def manage_packages_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """منوی اصلی مدیریت پکیج‌ها را با لیستی از پکیج‌های موجود نمایش می‌دهد."""
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
    """جزئیات یک پکیج خاص را به همراه تعداد کاربران فعال آن نمایش می‌دهد."""
    query = update.callback_query
    
    if package_id is None:
        if not query:
            logger.warning("view_package_details called without package_id and callback_query.")
            return
        package_id = query.data.split('_')[-1]

    if query: await query.answer()

    pkg_list = await asyncio.to_thread(
        database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, [database.Query.equal("$id", [package_id])]
    )
    if not pkg_list:
        await common.send_or_edit(update, "❌ پکیج مورد نظر یافت نشد.")
        return

    pkg = pkg_list[0]
    
    active_users = await asyncio.to_thread(
        database.get_documents,
        config.APPWRITE_DATABASE_ID,
        config.BOT_USERS_COLLECTION_ID,
        [database.Query.equal("package_id", [package_id]), database.Query.equal("is_active", [True])]
    )
    user_count = len(active_users)

    price = "رایگان" if pkg['monthly_price'] == 0 else f"{pkg['monthly_price']:,} تومان/ماه"
    ai_limit = "نامحدود" if pkg['ai_call_limit'] == 0 else f"{pkg['ai_call_limit']} تماس/ماه"
    status_text = "✅ فعال" if pkg.get('is_active') else "⭕️ غیرفعال"
    toggle_text = "غیرفعال کردن" if pkg.get('is_active') else "فعال کردن"

    text = (
        f"📦 *جزئیات پکیج: {pkg['package_name']}*\n\n"
        f"▫️ *قیمت:* {price}\n"
        f"▫️ *محدودیت AI:* {ai_limit}\n"
        f"▫️ *وضعیت:* {status_text}\n"
        f"▫️ *تعداد کاربران فعال:* {user_count} نفر\n\n"
        f"📜 *توضیحات:*\n{pkg.get('package_description', 'ندارد')}"
    )

    keyboard = [
        [
            InlineKeyboardButton("✏️ ویرایش", callback_data=f"admin_pkg_edit_{package_id}"),
            InlineKeyboardButton(f"🔄 {toggle_text}", callback_data=f"admin_pkg_toggle_{package_id}"),
            InlineKeyboardButton("🗑️ حذف", callback_data=f"admin_pkg_delete_{package_id}")
        ],
        [InlineKeyboardButton("🔙 بازگشت به لیست پکیج‌ها", callback_data="admin_pkg_back")]
    ]
    await common.send_or_edit(update, text, InlineKeyboardMarkup(keyboard))

async def admin_package_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دکمه‌های مربوط به مدیریت پکیج‌ها را مدیریت می‌کند."""
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
        keyboard = [
            [InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"admin_pkg_confirm_delete_{package_id}")],
            [InlineKeyboardButton("❌ خیر، بازگشت", callback_data=f"admin_pkg_view_{package_id}")]
        ]
        await query.message.edit_text("⚠️ آیا از حذف این پکیج مطمئن هستید؟\n\nاین عمل غیرقابل بازگشت است.", reply_markup=InlineKeyboardMarkup(keyboard))
    elif action == "confirm" and data_parts[3] == "delete":
        active_users = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, [database.Query.equal("package_id", [package_id])])
        if active_users:
            await query.message.edit_text(f"❌ امکان حذف این پکیج وجود ندارد زیرا {len(active_users)} کاربر در حال استفاده از آن هستند. لطفاً ابتدا کاربران را به پکیج دیگری منتقل کنید.")
            return
        await asyncio.to_thread(database.delete_document, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_id)
        await query.message.edit_text("✅ پکیج با موفقیت حذف شد.")
        await manage_packages_entry(update, context)

# --- مکالمه ساخت پکیج جدید ---

async def new_package_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """شروع فرآیند ساخت پکیج جدید توسط ادمین."""
    user_id = str(update.effective_user.id)
    if not await common.is_user_admin(user_id):
        return ConversationHandler.END
    
    keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await common.send_or_edit(update, "شما در حال ساخت یک پکیج جدید هستید.\n\nلطفاً نام پکیج را وارد کنید:", reply_markup)
    return PKG_NAME

async def pkg_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_package'] = {'package_name': update.message.text}
    keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("نام ذخیره شد. لطفاً توضیحات پکیج را وارد کنید:", reply_markup=reply_markup)
    return PKG_DESCRIPTION

async def pkg_description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_package']['package_description'] = update.message.text
    keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "توضیحات ذخیره شد. لطفاً تعداد مجاز تماس با AI در ماه را به صورت عدد وارد کنید (برای نامحدود عدد 0 را بزنید):",
        reply_markup=reply_markup
    )
    return PKG_AI_LIMIT

async def pkg_ai_limit_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        limit = int(update.message.text)
        context.user_data['new_package']['ai_call_limit'] = limit
        await update.message.reply_text(
            "تعداد تماس ذخیره شد. لطفاً قیمت ماهانه پکیج را به تومان (فقط عدد) وارد کنید (برای رایگان عدد 0 را بزنید):",
            reply_markup=reply_markup
        )
        return PKG_PRICE
    except ValueError:
        await update.message.reply_text("❌ لطفاً فقط یک عدد صحیح وارد کنید.", reply_markup=reply_markup)
        return PKG_AI_LIMIT

async def pkg_price_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = int(update.message.text)
        package_data = context.user_data['new_package']
        package_data['monthly_price'] = price
        package_data['is_active'] = True

        await asyncio.to_thread(
            database.create_document,
            config.APPWRITE_DATABASE_ID,
            config.PACKAGES_COLLECTION_ID,
            package_data
        )
        await update.message.reply_text(f"✅ پکیج '{package_data['package_name']}' با موفقیت ایجاد شد.")
        
        context.user_data.pop('new_package', None)
        await manage_packages_entry(update, context)
        return ConversationHandler.END
    except ValueError:
        keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("❌ لطفاً قیمت را فقط به صورت عدد صحیح وارد کنید.", reply_markup=reply_markup)
        return PKG_PRICE
    except Exception as e:
        logger.error(f"خطا در ذخیره پکیج جدید: {e}", exc_info=True)
        await update.message.reply_text("❌ خطایی در هنگام ذخیره پکیج رخ داد.")
        context.user_data.pop('new_package', None)
        return ConversationHandler.END

def get_new_package_conv_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("newpackage", new_package_start),
            CallbackQueryHandler(new_package_start, pattern='^admin_pkg_add$')
        ],
        states={
            PKG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_name_received)],
            PKG_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_description_received)],
            PKG_AI_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_ai_limit_received)],
            PKG_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, pkg_price_received)],
        },
        fallbacks=[
            CommandHandler("cancel", common.generic_cancel_conversation),
            CallbackQueryHandler(common.generic_cancel_conversation, pattern='^generic_cancel$')
        ],
        block=True
    )

# --- مکالمه ویرایش پکیج ---

async def edit_package_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    package_id = query.data.split('_')[-1]
    context.user_data['edit_package_id'] = package_id
    
    keyboard = [
        [InlineKeyboardButton("نام پکیج", callback_data="edit_pkg_field_package_name")],
        [InlineKeyboardButton("توضیحات", callback_data="edit_pkg_field_package_description")],
        [InlineKeyboardButton("محدودیت AI", callback_data="edit_pkg_field_ai_call_limit")],
        [InlineKeyboardButton("قیمت ماهانه", callback_data="edit_pkg_field_monthly_price")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"admin_pkg_view_{package_id}")]
    ]
    await query.message.edit_text("کدام بخش از پکیج را می‌خواهید ویرایش کنید؟", reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_PKG_SELECT_FIELD

async def edit_pkg_field_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    package_id = context.user_data.get('edit_package_id')
    if not package_id:
        await common.send_or_edit(update, "❌ خطایی رخ داد. لطفاً دوباره تلاش کنید.")
        return ConversationHandler.END

    pkg_list = await asyncio.to_thread(
        database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, [database.Query.equal("$id", [package_id])]
    )
    if not pkg_list:
        await common.send_or_edit(update, "❌ پکیج مورد نظر یافت نشد.")
        return ConversationHandler.END
    pkg = pkg_list[0]

    field_to_edit = query.data.replace('edit_pkg_field_', '')
    context.user_data['field_to_edit'] = field_to_edit
    
    current_value = pkg.get(field_to_edit, 'تعیین نشده')

    field_map = {
        "package_name": "نام پکیج",
        "package_description": "توضیحات",
        "ai_call_limit": "محدودیت AI",
        "monthly_price": "قیمت ماهانه"
    }

    prompt_text = (
        f"در حال ویرایش: *{field_map.get(field_to_edit, 'فیلد نامشخص')}*\n"
        f"مقدار فعلی: `{current_value}`\n\n"
        f"لطفاً مقدار جدید را وارد کنید:"
    )
    
    keyboard = [[InlineKeyboardButton("❌ لغو ویرایش", callback_data="generic_cancel")]]
    await query.message.edit_text(prompt_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return EDIT_PKG_TYPING_VALUE

async def edit_pkg_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    field = context.user_data.get('field_to_edit')
    package_id = context.user_data.get('edit_package_id')

    if not field or not package_id:
        await update.message.reply_text("❌ خطایی در فرآیند ویرایش رخ داد. لطفاً دوباره تلاش کنید.")
        await manage_packages_entry(update, context)
        context.user_data.clear()
        return ConversationHandler.END

    new_value = update.message.text

    if field in ['ai_call_limit', 'monthly_price']:
        try:
            new_value = int(new_value)
        except ValueError:
            await update.message.reply_text("❌ لطفاً فقط یک عدد صحیح وارد کنید. عملیات ویرایش لغو شد.")
            await view_package_details(update, context, package_id=package_id)
            context.user_data.clear()
            return ConversationHandler.END
            
    await asyncio.to_thread(
        database.upsert_document,
        config.APPWRITE_DATABASE_ID,
        config.PACKAGES_COLLECTION_ID,
        '$id',
        package_id,
        {field: new_value}
    )
    
    await update.message.reply_text("✅ پکیج با موفقیت به‌روزرسانی شد.")
    
    # After editing, show the details again. We need to simulate a callback query.
    # The message in the update object is the one with the new value. We can reuse it.
    if update.message:
        # We create a new "dummy" update object that looks like it came from a callback button
        # so we can reuse the view_package_details function.
        class DummyQuery:
            def __init__(self, message, from_user, data):
                self.message = message
                self.from_user = from_user
                self.data = data
            async def answer(self): pass
        
        dummy_update = Update(update.update_id)
        dummy_update.callback_query = DummyQuery(update.message, update.effective_user, f"admin_pkg_view_{package_id}")
        await view_package_details(dummy_update, context)

    else: # Fallback if update.message is not available
        await manage_packages_entry(update, context)

    context.user_data.clear()
    return ConversationHandler.END

def get_edit_package_conv_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_package_start, pattern='^admin_pkg_edit_')],
        states={
            EDIT_PKG_SELECT_FIELD: [CallbackQueryHandler(edit_pkg_field_selected, pattern='^edit_pkg_field_')],
            EDIT_PKG_TYPING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_pkg_value_received)],
        },
        fallbacks=[
            CommandHandler("cancel", common.generic_cancel_conversation),
            CallbackQueryHandler(common.generic_cancel_conversation, pattern='^generic_cancel$')
        ],
        block=True
    )

# --- مدیریت پرداخت‌ها ---

async def review_payments_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لیست پرداخت‌های در انتظار تایید را برای ادمین نمایش می‌دهد."""
    user_id = str(update.effective_user.id)
    if not await common.is_user_admin(user_id):
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
    """یک درخواست پرداخت مشخص را به همراه دکمه‌های مدیریت نمایش می‌دهد."""
    index = context.user_data.get('payment_index', 0)
    payments = context.user_data.get('pending_payments', [])
    
    if not payments or index >= len(payments):
        await common.send_or_edit(update, "تمام درخواست‌ها بررسی شدند.")
        context.user_data.pop('pending_payments', None)
        context.user_data.pop('payment_index', None)
        return

    payment = payments[index]
    payment_id = payment['$id']
    user_id = payment['telegram_id']
    package_id = payment['package_id']
    
    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )
    package_info_list = await asyncio.to_thread(
        database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, [database.Query.equal("$id", [package_id])]
    )
    
    text = f"درخواست پرداخت ({index + 1}/{len(payments)})\n\n"
    user_display_name = user_doc.get('clickup_username', user_id) if user_doc else user_id
    text += f"👤 *کاربر:* `{user_id}` (نام کاربری: {user_display_name})\n"
    if package_info_list:
        text += f"📦 *پکیج:* {package_info_list[0]['package_name']}\n"
    text += f"📄 *اطلاعات واریز:*\n`{payment['receipt_details']}`\n\n"
    text += "لطفاً اقدام مورد نظر را انتخاب کنید:"

    keyboard = [
        [
            InlineKeyboardButton("✅ تایید", callback_data=f"admin_payment_approve_{payment_id}"),
            InlineKeyboardButton("❌ رد", callback_data=f"admin_payment_reject_{payment_id}")
        ],
        []
    ]
    if index > 0:
        keyboard[1].append(InlineKeyboardButton("◀️ قبلی", callback_data="admin_payment_prev"))
    if index < len(payments) - 1:
        keyboard[1].append(InlineKeyboardButton("▶️ بعدی", callback_data="admin_payment_next"))
    
    await common.send_or_edit(update, text, InlineKeyboardMarkup(keyboard))

async def admin_payment_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دکمه‌های مربوط به پنل ادمین (تایید/رد/پیمایش) را مدیریت می‌کند."""
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
    payment_doc_list = await asyncio.to_thread(
        database.get_documents, config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID, [database.Query.equal("$id", [payment_id])]
    )
    if not payment_doc_list:
        await query.edit_message_text("خطا: این درخواست پرداخت دیگر وجود ندارد.")
        return
    payment = payment_doc_list[0]

    new_status = "approved" if action == "approve" else "rejected"
    
    await asyncio.to_thread(
        database.upsert_document,
        config.APPWRITE_DATABASE_ID,
        config.PAYMENT_REQUESTS_COLLECTION_ID,
        '$id',
        payment_id,
        {'status': new_status, 'review_date': datetime.now(timezone.utc).isoformat()}
    )
    
    user_telegram_id = payment['telegram_id']
    
    if new_status == "approved":
        package_info_list = await asyncio.to_thread(
            database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, [database.Query.equal("$id", [payment['package_id']])]
        )
        if package_info_list:
            pkg = package_info_list[0]
            await asyncio.to_thread(
                database.upsert_document,
                config.APPWRITE_DATABASE_ID,
                config.BOT_USERS_COLLECTION_ID,
                'telegram_id',
                user_telegram_id,
                {
                    'package_id': payment['package_id'],
                    'usage_limit': pkg.get('ai_call_limit', 0),
                    'used_count': 0, # Reset usage on new approval
                }
            )
        try:
            await context.bot.send_message(
                chat_id=user_telegram_id,
                text="✅ پرداخت شما تایید شد! حساب شما آماده فعال‌سازی است.\n\n"
                     "لطفاً برای تکمیل فرآیند، توکن API کلیک‌اپ خود را ارسال کنید."
            )
            await query.edit_message_text(f"✅ پرداخت برای کاربر {user_telegram_id} تایید شد.")
        except Exception as e:
            logger.error(f"Failed to send message to user {user_telegram_id}: {e}")
            await query.edit_message_text(f"✅ پرداخت برای کاربر {user_telegram_id} تایید شد، اما ارسال پیام به او ناموفق بود.")
    else:
        try:
            await context.bot.send_message(
                chat_id=user_telegram_id,
                text="❌ متاسفانه پرداخت شما رد شد. لطفاً برای اطلاعات بیشتر با پشتیبانی تماس بگیرید."
            )
            await query.edit_message_text(f"❌ پرداخت برای کاربر {user_telegram_id} رد شد.")
        except Exception as e:
            logger.error(f"Failed to send message to user {user_telegram_id}: {e}")
            await query.edit_message_text(f"❌ پرداخت برای کاربر {user_telegram_id} رد شد، اما ارسال پیام به او ناموفق بود.")

    payments = context.user_data.get('pending_payments', [])
    current_index = context.user_data.get('payment_index', 0)
    if payments and current_index < len(payments):
        payments.pop(current_index)
    
    if not payments:
        await common.send_or_edit(update, "تمام درخواست‌ها بررسی شدند.")
        context.user_data.pop('pending_payments', None)
        context.user_data.pop('payment_index', None)
        return

    if current_index >= len(payments):
        context.user_data['payment_index'] = max(0, len(payments) - 1)

    await display_pending_payment(update, context)


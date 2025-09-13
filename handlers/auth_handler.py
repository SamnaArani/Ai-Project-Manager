# -*- coding: utf-8 -*-
import asyncio
import logging
from datetime import datetime, timezone, timedelta
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
import clickup_api
from . import common
from . import admin_handler

logger = logging.getLogger(__name__)

# New conversation states reflecting the new flow
(SELECTING_PACKAGE, AWAITING_CLICKUP_TOKEN, AWAITING_PAYMENT_DETAILS) = range(3)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the /start command, checking user status and directing them."""
    user_id = str(update.effective_user.id)
    user_info = update.effective_user
    
    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )
    
    if user_doc and not user_doc.get('is_active', True):
        await update.message.reply_text(
            f"❌ حساب کاربری شما مسدود است.\n"
            f"اگر فکر می‌کنید اشتباهی رخ داده، لطفاً با ادمین (@{config.ADMIN_USERNAME}) تماس بگیرید."
        )
        return ConversationHandler.END

    # Create or update user's basic info
    full_name = user_info.full_name
    telegram_username = user_info.username or ""
    user_data_payload = { 'telegram_id': user_id, 'full_name': full_name, 'telegram_username': telegram_username }
    if not user_doc:
        user_data_payload.update({
            'is_active': True, 'is_admin': False, 'created_at': datetime.now(timezone.utc).isoformat()
        })
    
    await asyncio.to_thread(
        database.upsert_document,
        config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID,
        'telegram_id', user_id, user_data_payload
    )
    
    # Re-fetch the document after upsert to have the latest data
    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )

    # --- User Flow Logic ---
    if user_doc and user_doc.get('is_admin'):
        await admin_handler.start_for_admin(update, context)
        return ConversationHandler.END

    # Flow 1: Fully registered and functional user
    is_fully_registered = user_doc and user_doc.get('clickup_token') and user_doc.get('package_id')
    if is_fully_registered:
        await common.show_main_menu(update, "سلام مجدد! به PIXEELL خوش آمدید. لطفاً یک گزینه را انتخاب کنید:")
        return ConversationHandler.END

    # Flow 2: New user (no package selected yet). This is the main entry point for new users.
    if not user_doc.get('package_id'):
        await update.message.reply_text(
            "👋 سلام! به ربات مدیریت پروژه PIXEELL خوش آمدید."
        )
        await show_packages_for_selection(update, context)
        return SELECTING_PACKAGE

    # Flow 3: User HAS a package but is missing a token. This is an edge case.
    if user_doc.get('package_id') and not user_doc.get('clickup_token'):
        context.chat_data['auth_flow_active'] = True # Re-apply lock
        await common.show_limited_menu(update, "⚠️ برای ادامه ثبت نام و فعال‌سازی حساب، لطفاً توکن ClickUp معتبری را ارسال کنید.")
        return AWAITING_CLICKUP_TOKEN

    # Fallback for any other unexpected state, guide them back to the start.
    await update.message.reply_text(
        "👋 سلام! به ربات مدیریت پروژه PIXEELL خوش آمدید."
    )
    await show_packages_for_selection(update, context)
    return SELECTING_PACKAGE


async def show_packages_for_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays available packages as inline buttons."""
    packages = await asyncio.to_thread(
        database.get_documents,
        config.APPWRITE_DATABASE_ID,
        config.PACKAGES_COLLECTION_ID,
        [Query.equal("is_active", [True])]
    )

    if not packages:
        await common.send_or_edit(update, "متاسفانه در حال حاضر هیچ پکیج فعالی برای انتخاب وجود ندارد.")
        return

    keyboard = []
    for pkg in packages:
        price = "رایگان" if pkg.get('monthly_price', 0) == 0 else f"{pkg['monthly_price']:,} تومان"
        button_text = f"{pkg['package_name']} - {price}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"select_pkg_{pkg['$id']}")])

    # Combined intro and details text
    details_text = "برای شروع، لطفاً یکی از پکیج‌های زیر را انتخاب کنید:\n\n"
    details_text += "📜 *راهنمای پکیج‌ها:*\n\n"
    for pkg in packages:
        price = "رایگان" if pkg.get('monthly_price', 0) == 0 else f"{pkg['monthly_price']:,} تومان"
        details_text += (f"🔹 *{pkg['package_name']}* ({price})\n"
                         f"{pkg.get('package_description', 'توضیحات ندارد.')}\n\n")

    await common.send_or_edit(update, details_text, InlineKeyboardMarkup(keyboard))


async def package_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles package selection and asks for ClickUp token first."""
    query = update.callback_query
    await query.answer()
    package_id = query.data.split('_')[-1]
    
    pkg_doc = await asyncio.to_thread(
        database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_id
    )

    if not pkg_doc:
        await query.message.edit_text("❌ پکیج انتخاب شده یافت نشد. لطفاً دوباره تلاش کنید.")
        return SELECTING_PACKAGE

    context.user_data['selected_package_id'] = package_id
    context.user_data['is_free_package'] = (pkg_doc.get('monthly_price', 0) == 0)
    
    # Activate the auth flow lock
    context.chat_data['auth_flow_active'] = True
    
    await query.message.edit_text(
        f"شما پکیج *{pkg_doc['package_name']}* را انتخاب کردید.\n\n"
        "قدم بعدی: لطفاً توکن API کلیک‌اپ خود را ارسال کنید تا اعتبارسنجی شود."
    )
    return AWAITING_CLICKUP_TOKEN


async def clickup_token_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validates the ClickUp token, syncs data, and decides the next step."""
    if context.chat_data.get('in_support_flow'):
        return AWAITING_CLICKUP_TOKEN

    context.chat_data['conversation_handled'] = True
    token = update.message.text.strip()
    user_id = str(update.effective_user.id)
    
    placeholder_message = await update.message.reply_text("در حال بررسی توکن...")

    # --- Duplicate Token Check ---
    existing_user_with_token = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'clickup_token', token
    )
    if existing_user_with_token and existing_user_with_token['telegram_id'] != user_id:
        await placeholder_message.edit_text("❌ این توکن قبلاً توسط کاربر دیگری ثبت شده است. لطفاً از یک توکن دیگر استفاده کنید.")
        return AWAITING_CLICKUP_TOKEN

    # --- Token Validation & Sync ---
    await placeholder_message.edit_text("در حال اعتبارسنجی توکن در ClickUp...")
    is_valid = await asyncio.to_thread(clickup_api.validate_token, token)
    if not is_valid:
        await placeholder_message.edit_text("❌ توکن نامعتبر است. لطفاً دوباره ارسال کنید یا با /cancel لغو کنید.")
        return AWAITING_CLICKUP_TOKEN
    
    await placeholder_message.edit_text("✅ توکن معتبر است. در حال همگام‌سازی اولیه اطلاعات... ⏳")
    sync_success = await asyncio.to_thread(clickup_api.sync_all_user_data, token, user_id)
    if not sync_success:
        await placeholder_message.edit_text("❌ در همگام‌سازی اولیه اطلاعات خطایی رخ داد. لطفاً با پشتیبانی تماس بگیرید.")
        context.chat_data.pop('auth_flow_active', None)
        return ConversationHandler.END
    
    await asyncio.to_thread(database.upsert_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id, {'clickup_token': token})
    await placeholder_message.edit_text("✅ همگام‌سازی با موفقیت انجام شد!")

    # --- Decide next step based on package type ---
    if context.user_data.get('is_free_package'):
        # --- FREE PACKAGE: Finalize registration ---
        package_id = context.user_data.get('selected_package_id')
        pkg_doc = await asyncio.to_thread(database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_id)
        if pkg_doc:
            duration_days = pkg_doc.get('package_duration_days') or 30
            expiry_date = datetime.now(timezone.utc) + timedelta(days=duration_days)
            user_update_data = {
                'package_id': package_id, 'is_active': True,
                'package_activation_date': datetime.now(timezone.utc).isoformat(),
                'package_expiry_date': expiry_date.isoformat()
            }
            await asyncio.to_thread(database.upsert_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id, user_update_data)

        context.chat_data.pop('auth_flow_active', None)
        await common.show_main_menu(update, "ثبت نام شما تکمیل شد. حالا می‌توانید از امکانات ربات استفاده کنید:")
        context.user_data.clear()
        return ConversationHandler.END
    else:
        # --- PAID PACKAGE: Proceed to payment ---
        await update.message.reply_text(
            "لطفاً پس از واریز، اطلاعات پرداخت (مانند شماره تراکنش، اسکرین‌شات یا کد رهگیری) را در قالب یک پیام متنی ارسال کنید."
        )
        return AWAITING_PAYMENT_DETAILS


async def payment_details_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Saves payment details, notifies admin, and activates a temporary free package."""
    context.chat_data['conversation_handled'] = True
    user_id = str(update.effective_user.id)
    package_id = context.user_data.get('selected_package_id')
    receipt_details = update.message.text

    if not package_id:
        await update.message.reply_text("خطایی رخ داد. لطفاً با دستور /start مجدداً تلاش کنید.")
        return ConversationHandler.END

    # Save payment request
    payment_data = {
        'telegram_id': user_id, 'package_id': package_id,
        'receipt_details': receipt_details, 'status': 'pending',
        'request_date': datetime.now(timezone.utc).isoformat()
    }
    await asyncio.to_thread(database.create_document, config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID, payment_data)

    # Notify admins
    admins = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, [Query.equal("is_admin", [True])])
    user_display_name = update.effective_user.full_name or f"@{update.effective_user.username}" or user_id
    notification_text = f"💳 درخواست پرداخت جدیدی از طرف *{common.escape_markdown(user_display_name)}* ثبت شد."
    keyboard = [[InlineKeyboardButton("بررسی درخواست", callback_data="admin_payment_review_pending")]]
    for admin in admins:
        try:
            await context.bot.send_message(chat_id=admin['telegram_id'], text=notification_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Failed to send new payment notification to admin {admin['telegram_id']}: {e}")

    # Activate a temporary free package
    free_packages = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, [Query.equal("monthly_price", [0]), Query.equal("is_active", [True])])
    if free_packages:
        free_pkg_id = free_packages[0]['$id']
        await asyncio.to_thread(database.upsert_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id, {'package_id': free_pkg_id, 'is_active': True})

    await update.message.reply_text("✅ اطلاعات پرداخت شما ثبت و برای ادمین ارسال شد.")
    
    # Unlock the flow and end conversation
    context.chat_data.pop('auth_flow_active', None)
    await common.show_main_menu(update, "تا زمان تایید پرداخت توسط ادمین، می‌توانید از امکانات پایه ربات (غیر از هوش مصنوعی) استفاده کنید.")
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_and_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Clears conversation data and restarts the start command."""
    context.user_data.clear()
    context.chat_data.pop('auth_flow_active', None)
    await update.message.reply_text("فرآیند فعلی لغو شد. ربات مجدداً راه‌اندازی می‌شود...")
    return await start_command(update, context)

def get_auth_handler() -> ConversationHandler:
    """Creates and returns the main authentication and registration conversation handler."""
    token_filter = filters.TEXT & ~filters.COMMAND & ~filters.Regex('^📞 پشتیبانی$')

    return ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            SELECTING_PACKAGE: [CallbackQueryHandler(package_selected, pattern='^select_pkg_')],
            AWAITING_CLICKUP_TOKEN: [MessageHandler(token_filter, clickup_token_received)],
            AWAITING_PAYMENT_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_details_received)],
        },
        fallbacks=[
            CommandHandler("start", cancel_and_restart),
            CommandHandler("cancel", common.generic_cancel_conversation)
            ],
    )


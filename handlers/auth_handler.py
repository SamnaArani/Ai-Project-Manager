# -*- coding: utf-8 -*-
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
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

# Conversation states
(SELECTING_PACKAGE, AWAITING_PAYMENT_DETAILS, AWAITING_CLICKUP_TOKEN) = range(3)

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

    if not user_doc:
         user_doc = await asyncio.to_thread(
            database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
        )

    if user_doc and user_doc.get('is_admin'):
        await admin_handler.start_for_admin(update, context)
        return ConversationHandler.END

    if user_doc and user_doc.get('clickup_token') and user_doc.get('package_id'):
        await show_main_menu(update, "سلام مجدد! به PIXEELL خوش آمدید. لطفاً یک گزینه را انتخاب کنید:")
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 سلام! به ربات مدیریت پروژه PIXEELL خوش آمدید.\n\n"
        "برای شروع، لطفاً یکی از پکیج‌های زیر را انتخاب کنید:"
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
        price = "رایگان" if pkg['monthly_price'] == 0 else f"{pkg['monthly_price']:,} تومان"
        button_text = f"{pkg['package_name']} - {price}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"select_pkg_{pkg['$id']}")])

    details_text = "📜 *راهنمای پکیج‌ها:*\n\n"
    for pkg in packages:
        price = "رایگان" if pkg['monthly_price'] == 0 else f"{pkg['monthly_price']:,} تومان"
        details_text += (f"🔹 *{pkg['package_name']}* ({price})\n"
                         f"{pkg.get('package_description', 'توضیحات ندارد.')}\n\n")

    await common.send_or_edit(update, details_text, InlineKeyboardMarkup(keyboard))

async def package_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles user's package selection."""
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
    
    if pkg_doc['monthly_price'] == 0:
        context.user_data['is_free_package'] = True
        await query.message.edit_text(
            "شما پکیج رایگان را انتخاب کردید. برای فعال‌سازی، لطفاً توکن API کلیک‌اپ خود را ارسال کنید."
        )
        return AWAITING_CLICKUP_TOKEN
    else:
        context.user_data['is_free_package'] = False
        await query.message.edit_text(
            f"شما پکیج *{pkg_doc['package_name']}* را انتخاب کردید.\n\n"
            "لطفاً پس از واریز، اطلاعات پرداخت (مانند شماره تراکنش، اسکرین‌شات یا کد رهگیری) را در قالب یک پیام متنی ارسال کنید."
        )
        return AWAITING_PAYMENT_DETAILS

async def payment_details_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Saves payment details and proceeds to ask for ClickUp token."""
    user_id = str(update.effective_user.id)
    package_id = context.user_data.get('selected_package_id')
    receipt_details = update.message.text

    if not package_id:
        await update.message.reply_text("خطایی رخ داد. لطفاً با دستور /start مجدداً تلاش کنید.")
        return ConversationHandler.END

    payment_data = {
        'telegram_id': user_id, 'package_id': package_id,
        'receipt_details': receipt_details, 'status': 'pending',
        'request_date': datetime.now(timezone.utc).isoformat()
    }
    await asyncio.to_thread(
        database.create_document, config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID, payment_data
    )

    await update.message.reply_text(
        "✅ اطلاعات پرداخت شما ثبت شد و در انتظار تایید ادمین است.\n\n"
        "برای استفاده از امکانات پایه تا زمان تایید، لطفاً توکن API کلیک‌اپ خود را ارسال کنید."
    )
    return AWAITING_CLICKUP_TOKEN

async def clickup_token_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validates the ClickUp token and finalizes registration."""
    token = update.message.text.strip()
    user_id = str(update.effective_user.id)
    
    placeholder_message = await update.message.reply_text("در حال اعتبارسنجی توکن...")
    is_valid = await asyncio.to_thread(clickup_api.validate_token, token)

    if not is_valid:
        await placeholder_message.edit_text("❌ توکن نامعتبر است. لطفاً دوباره ارسال کنید یا با /cancel لغو کنید.")
        return AWAITING_CLICKUP_TOKEN

    await asyncio.to_thread(
        database.upsert_document,
        config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID,
        'telegram_id', user_id, {'clickup_token': token}
    )
    
    # Activate free package immediately, or give pending users the free package temporarily
    package_to_activate_id = None
    if context.user_data.get('is_free_package'):
        package_to_activate_id = context.user_data.get('selected_package_id')
    else: # It's a pending paid user, give them free access for now
        free_packages = await asyncio.to_thread(
            database.get_documents, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID,
            [Query.equal("monthly_price", [0])]
        )
        if free_packages:
            package_to_activate_id = free_packages[0]['$id']

    if package_to_activate_id:
        pkg_doc = await asyncio.to_thread(database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_to_activate_id)
        if pkg_doc:
            expiry_date = datetime.now(timezone.utc) + timedelta(days=pkg_doc.get('package_duration_days', 30))
            user_update_data = {
                'package_id': package_to_activate_id,
                'is_active': True,
                'package_activation_date': datetime.now(timezone.utc).isoformat(),
                'package_expiry_date': expiry_date.isoformat()
            }
            await asyncio.to_thread(
                database.upsert_document,
                config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID,
                'telegram_id', user_id, user_update_data
            )
    
    await placeholder_message.edit_text("توکن شما با موفقیت ذخیره شد. در حال همگام‌سازی اولیه اطلاعات... ⏳")
    sync_success = await asyncio.to_thread(clickup_api.sync_all_user_data, token, user_id)

    if not sync_success:
        await placeholder_message.edit_text("❌ در همگام‌سازی اولیه اطلاعات خطایی رخ داد. لطفاً با دستور /resync مجدداً تلاش کنید.")
    else:
        await placeholder_message.edit_text("✅ همگام‌سازی با موفقیت انجام شد!")

    await show_main_menu(update, "ثبت نام شما تکمیل شد. حالا می‌توانید از امکانات ربات استفاده کنید:")
    context.user_data.clear()
    return ConversationHandler.END

async def show_main_menu(update: Update, text: str):
    """Displays the main menu for authenticated users."""
    main_menu_keyboard = [
        [KeyboardButton("🔍 مرور پروژه‌ها")], 
        [KeyboardButton("➕ ساخت تسک جدید")],
        [KeyboardButton("📞 پشتیبانی")]
    ]
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True, one_time_keyboard=False)
    await update.message.reply_text(text, reply_markup=reply_markup)

def get_auth_handler() -> ConversationHandler:
    """Creates and returns the main authentication and registration conversation handler."""
    return ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            SELECTING_PACKAGE: [CallbackQueryHandler(package_selected, pattern='^select_pkg_')],
            AWAITING_PAYMENT_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_details_received)],
            AWAITING_CLICKUP_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, clickup_token_received)],
        },
        fallbacks=[CommandHandler("cancel", common.generic_cancel_conversation)],
    )


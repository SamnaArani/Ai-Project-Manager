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
from dateutil.parser import parse as dateutil_parse
import config
import database
import clickup_api
from . import common
from . import admin_handler

logger = logging.getLogger(__name__)

# --- States ---
(SELECTING_PACKAGE, AWAITING_CLICKUP_TOKEN, AWAITING_PAYMENT_DETAILS, 
 AWAITING_RESYNC_CONFIRMATION) = range(4)


async def _proceed_to_next_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Helper function to decide the next step after token validation/sync.
    Checks if the package is free to finalize registration, or ends the conversation 
    prompting the user to submit payment details via a persistent button.
    """
    user_id = str(update.effective_user.id)
    
    user_doc = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id)
    package_id = user_doc.get('package_id')
    if not package_id:
        logger.error(f"Cannot proceed to next step for user {user_id}: package_id not found.")
        await update.effective_chat.send_message("خطایی در بازیابی پکیج شما رخ داد. لطفاً با /start دوباره شروع کنید.")
        return ConversationHandler.END

    pkg_doc = await asyncio.to_thread(database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_id)
    is_free = (pkg_doc.get('monthly_price', 0) == 0) if pkg_doc else False

    if is_free:
        duration_days = pkg_doc.get('package_duration_days', 30) if pkg_doc else 30
        expiry_date = datetime.now(timezone.utc) + timedelta(days=duration_days)
        user_update_data = {
            'package_activation_date': datetime.now(timezone.utc).isoformat(),
            'package_expiry_date': expiry_date.isoformat()
        }
        await asyncio.to_thread(database.upsert_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id, user_update_data)
        
        context.chat_data.pop('auth_flow_active', None)
        await common.show_main_menu(update, "ثبت نام شما تکمیل شد. حالا می‌توانید از امکانات ربات استفاده کنید:")
        context.user_data.clear()
        return ConversationHandler.END
    else:
        # For paid packages, end the conversation but prompt for payment.
        await common.show_main_menu(update, "✅ ثبت نام اولیه شما با موفقیت انجام شد. می‌توانید از امکانات پایه ربات استفاده کنید.")
        keyboard = [[InlineKeyboardButton("تکمیل ثبت نام و پرداخت", callback_data="start_payment_submission")]]
        await update.effective_chat.send_message(
            "برای فعال‌سازی کامل حساب و دسترسی به هوش مصنوعی، لطفاً فرآیند پرداخت را تکمیل کنید.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.chat_data.pop('auth_flow_active', None)
        context.user_data.clear()
        return ConversationHandler.END

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
    user_data_payload = {'telegram_id': user_id, 'full_name': full_name, 'telegram_username': telegram_username}
    if not user_doc:
        user_data_payload.update({
            'is_active': True, 'is_admin': False, 'created_at': datetime.now(timezone.utc).isoformat()
        })
    
    await asyncio.to_thread(
        database.upsert_document,
        config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID,
        'telegram_id', user_id, user_data_payload
    )
    
    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )

    if user_doc and user_doc.get('is_admin'):
        await admin_handler.start_for_admin(update, context)
        return ConversationHandler.END

    if user_doc and user_doc.get('clickup_token') and user_doc.get('package_expiry_date'):
        expiry_date = dateutil_parse(user_doc['package_expiry_date']).replace(tzinfo=timezone.utc)
        if expiry_date > datetime.now(timezone.utc):
            await common.show_main_menu(update, "سلام مجدد! به PIXEELL خوش آمدید. لطفاً یک گزینه را انتخاب کنید:")
            return ConversationHandler.END
        else:
            await update.message.reply_text("❗️ اعتبار پکیج شما به پایان رسیده است. لطفاً برای ادامه یک پکیج جدید انتخاب کنید.")
    
    if user_doc and user_doc.get('clickup_token') and user_doc.get('package_id') and not user_doc.get('package_expiry_date'):
        pkg_doc = await asyncio.to_thread(database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, user_doc['package_id'])
        if pkg_doc and pkg_doc.get('monthly_price', 0) > 0:
            await common.show_main_menu(update, "✅ ثبت نام اولیه شما انجام شده. می‌توانید از امکانات پایه استفاده کنید.")
            keyboard = [[InlineKeyboardButton("تکمیل ثبت نام و پرداخت", callback_data="start_payment_submission")]]
            await update.message.reply_text(
                "برای فعال‌سازی کامل حساب و دسترسی به هوش مصنوعی، لطفاً فرآیند پرداخت را تکمیل کنید.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return ConversationHandler.END

    if user_doc and user_doc.get('package_id') and not user_doc.get('clickup_token'):
        context.chat_data['auth_flow_active'] = True 
        await common.show_limited_menu(update, "⚠️ برای ادامه ثبت نام، لطفاً توکن ClickUp خود را ارسال کنید.")
        return AWAITING_CLICKUP_TOKEN

    await update.message.reply_text("👋 سلام! به ربات مدیریت پروژه PIXEELL خوش آمدید.")
    await show_packages_for_selection(update, context)
    return SELECTING_PACKAGE


async def show_packages_for_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, send_new: bool = False):
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

    details_text = "برای شروع، لطفاً یکی از پکیج‌های زیر را انتخاب کنید:\n\n"
    details_text += "📜 *راهنمای پکیج‌ها:*\n\n"
    for pkg in packages:
        price = "رایگان" if pkg.get('monthly_price', 0) == 0 else f"{pkg['monthly_price']:,} تومان"
        details_text += (f"🔹 *{pkg['package_name']}* ({price})\n"
                         f"{pkg.get('package_description', 'توضیحات ندارد.')}\n\n")

    if send_new and update.effective_chat:
        await update.effective_chat.send_message(details_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await common.send_or_edit(update, details_text, InlineKeyboardMarkup(keyboard))


async def package_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles package selection, saves it to DB, and asks for ClickUp token."""
    query = update.callback_query
    await query.answer()
    package_id = query.data.split('_')[-1]
    
    pkg_doc = await asyncio.to_thread(
        database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, package_id
    )

    if not pkg_doc:
        await query.message.edit_text("❌ پکیج انتخاب شده یافت نشد. لطفاً دوباره تلاش کنید.")
        return SELECTING_PACKAGE
    
    user_id = str(update.effective_user.id)
    await asyncio.to_thread(
        database.upsert_document,
        config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID,
        'telegram_id', user_id, {'package_id': package_id, 'package_expiry_date': None} # Clear expiry date on new selection
    )

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
    
    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )
    if user_doc and user_doc.get('clickup_token') == token:
        keyboard = [
            [InlineKeyboardButton("✅ بله، همگام‌سازی مجدد", callback_data="resync_confirm_yes")],
            [InlineKeyboardButton("❌ خیر، ادامه", callback_data="resync_confirm_no")]
        ]
        await update.message.reply_text(
            "این توکن قبلاً برای شما ثبت شده است.\n"
            "آیا می‌خواهید اطلاعات خود را مجدداً با ClickUp همگام‌سازی کنید؟",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return AWAITING_RESYNC_CONFIRMATION

    placeholder_message = await update.message.reply_text("در حال بررسی توکن...")

    existing_user_with_token = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'clickup_token', token
    )
    if existing_user_with_token and existing_user_with_token['telegram_id'] != user_id:
        await placeholder_message.edit_text("❌ این توکن قبلاً توسط کاربر دیگری ثبت شده است. لطفاً از یک توکن دیگر استفاده کنید.")
        return AWAITING_CLICKUP_TOKEN

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

    if context.user_data.pop('is_upgrading', False):
        await show_packages_for_selection(update, context, send_new=True)
        return SELECTING_PACKAGE
    
    return await _proceed_to_next_step(update, context)


async def handle_resync_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the user's choice for re-syncing data."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    if query.data == 'resync_confirm_yes':
        await query.message.edit_text("بسیار خب. در حال همگام‌سازی مجدد اطلاعات... ⏳")
        user_doc = await asyncio.to_thread(
            database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
        )
        token = user_doc.get('clickup_token')
        
        if not token:
             await query.message.edit_text("❌ توکن شما یافت نشد. لطفاً با /start مجدداً تلاش کنید.")
             return ConversationHandler.END

        sync_success = await asyncio.to_thread(clickup_api.sync_all_user_data, token, user_id)
        if not sync_success:
            await query.message.edit_text("❌ در همگام‌سازی مجدد اطلاعات خطایی رخ داد. لطفاً با پشتیبانی تماس بگیرید.")
            context.chat_data.pop('auth_flow_active', None)
            return ConversationHandler.END
            
        await query.message.edit_text("✅ همگام‌سازی مجدد با موفقیت انجام شد!")
    else: # resync_confirm_no
        await query.message.edit_text("بسیار خب، از همگام‌سازی مجدد صرف نظر شد. در حال ادامه فرآیند...")

    return await _proceed_to_next_step(update, context)


async def payment_details_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Saves payment details and notifies admin."""
    context.chat_data['conversation_handled'] = True
    user_id = str(update.effective_user.id)
    
    user_doc = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id)
    package_id = user_doc.get('package_id') if user_doc else None
    
    if not package_id:
        await update.message.reply_text("خطایی رخ داد. پکیج شما یافت نشد. لطفاً با دستور /start مجدداً تلاش کنید.")
        return ConversationHandler.END

    payment_data = {
        'telegram_id': user_id, 'package_id': package_id,
        'receipt_details': update.message.text, 'status': 'pending',
        'request_date': datetime.now(timezone.utc).isoformat()
    }
    await asyncio.to_thread(database.create_document, config.APPWRITE_DATABASE_ID, config.PAYMENT_REQUESTS_COLLECTION_ID, payment_data)

    admins = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, [Query.equal("is_admin", [True])])
    user_display_name = update.effective_user.full_name or f"@{update.effective_user.username}" or user_id
    notification_text = f"💳 درخواست پرداخت جدیدی از طرف *{common.escape_markdown(user_display_name)}* ثبت شد."
    keyboard = [[InlineKeyboardButton("بررسی درخواست", callback_data="admin_payment_review_pending")]]
    for admin in admins:
        try:
            await context.bot.send_message(chat_id=admin['telegram_id'], text=notification_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Failed to send new payment notification to admin {admin['telegram_id']}: {e}")

    await update.message.reply_text("✅ اطلاعات پرداخت شما ثبت و برای ادمین ارسال شد. پس از تایید، تمام امکانات پکیج برای شما فعال خواهد شد.")
    
    context.chat_data.pop('auth_flow_active', None)
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_and_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Clears conversation data and restarts the start command."""
    context.user_data.clear()
    context.chat_data.pop('auth_flow_active', None)
    await update.message.reply_text("فرآیند فعلی لغو شد. ربات مجدداً راه‌اندازی می‌شود...")
    return await start_command(update, context)


async def start_payment_or_upgrade_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    A new entry point to handle payment or upgrade requests. 
    This function is called by a standalone handler in main.py and starts the conversation.
    """
    query = update.callback_query
    await query.answer()
    
    context.user_data.clear()
        
    action = query.data
    user_id = str(update.effective_user.id)

    if action == 'upgrade_plan':
        if query.message:
            try:
                await query.message.delete()
            except Exception as e:
                logger.warning(f"Could not delete message on starting upgrade flow: {e}")
        user_doc = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id)
        if user_doc and user_doc.get('clickup_token'):
            await show_packages_for_selection(update, context, send_new=True)
            return SELECTING_PACKAGE
        else:
            context.chat_data['auth_flow_active'] = True
            context.user_data['is_upgrading'] = True
            await update.effective_chat.send_message(
                "برای ارتقای پلن، ابتدا باید حساب کلیک‌اپ خود را متصل کنید.\n\n"
                "لطفاً توکن API کلیک‌اپ خود را ارسال کنید."
            )
            return AWAITING_CLICKUP_TOKEN
            
    elif action == 'start_payment_submission':
        if query.message:
            await query.message.edit_text(
                "بسیار خب. لطفاً اطلاعات پرداخت (مانند شماره تراکنش یا کد رهگیری) را در قالب یک پیام متنی ارسال کنید.",
                reply_markup=None
            )
        else: # Fallback if message is somehow gone
            await update.effective_chat.send_message(
                "بسیار خب. لطفاً اطلاعات پرداخت (مانند شماره تراکنش یا کد رهگیری) را در قالب یک پیام متنی ارسال کنید."
             )
        return AWAITING_PAYMENT_DETAILS

    return ConversationHandler.END


def get_auth_handler() -> ConversationHandler:
    """Creates and returns the main authentication and registration conversation handler."""
    token_filter = filters.TEXT & ~filters.COMMAND & ~filters.Regex('^📞 پشتیبانی$')
    
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command),
            # This is the NEW entry point for buttons clicked outside the initial flow.
            CallbackQueryHandler(start_payment_or_upgrade_flow, pattern=r'^(start_payment_submission|upgrade_plan)$')
            ],
        states={
            SELECTING_PACKAGE: [CallbackQueryHandler(package_selected, pattern='^select_pkg_')],
            AWAITING_CLICKUP_TOKEN: [MessageHandler(token_filter, clickup_token_received)],
            AWAITING_PAYMENT_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_details_received)],
            AWAITING_RESYNC_CONFIRMATION: [
                CallbackQueryHandler(handle_resync_confirmation, pattern=r'^resync_confirm_')
            ],
        },
        fallbacks=[
            CommandHandler("start", cancel_and_restart),
            CommandHandler("cancel", common.generic_cancel_conversation)
            ],
        # Allow reentry for the new entry point to work correctly
        allow_reentry=True
    )


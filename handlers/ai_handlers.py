# -*- coding: utf-8 -*-
import asyncio
import telegram
import json
import logging
import inspect
from typing import Dict, Any, Tuple
from datetime import datetime, date, timezone
from dateutil.parser import parse as dateutil_parse
from langchain.memory import ConversationSummaryMemory
from langchain_ollama import ChatOllama
from langchain_core.messages import SystemMessage, HumanMessage
from httpx import ConnectError
from functools import partial

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import config
from ai import prompts, tools
import database
from . import common
import clickup_api

logger = logging.getLogger(__name__)

memories = {}

def get_memory(user_id: str) -> ConversationSummaryMemory:
    if user_id not in memories:
        memories[user_id] = ConversationSummaryMemory(llm=ChatOllama(model=config.OLLAMA_MODEL, base_url=config.OLLAMA_BASE_URL))
    return memories[user_id]

# --- AI Access Control ---

async def check_ai_access(user_id: str, request_type: str) -> Tuple[bool, str, dict, dict]:
    """
    Checks if a user has access to AI features based on their package.
    Returns: (has_access, reason_code, user_doc, package_doc)
    """
    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )
    if not user_doc or not user_doc.get('package_id'):
        return False, "no_package", None, None

    package_doc = await asyncio.to_thread(
        database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, user_doc['package_id']
    )
    if not package_doc:
        return False, "package_not_found", user_doc, None

    is_paid_package = package_doc.get('monthly_price', 0) > 0
    has_expiry_date = 'package_expiry_date' in user_doc and user_doc['package_expiry_date']

    if is_paid_package and not has_expiry_date:
        return False, "pending_payment", user_doc, package_doc

    if has_expiry_date:
        try:
            expiry_date = dateutil_parse(user_doc['package_expiry_date']).replace(tzinfo=timezone.utc)
            if expiry_date < datetime.now(timezone.utc):
                return False, "expired", user_doc, package_doc
        except (ValueError, TypeError):
             logger.warning(f"Could not parse expiry date '{user_doc['package_expiry_date']}' for user {user_id}")

    today_str = date.today().isoformat()
    last_usage_date_val = user_doc.get('last_usage_date')
    last_usage_str = (last_usage_date_val or "").split("T")[0]
    
    if last_usage_str != today_str:
        await asyncio.to_thread(
            database.upsert_document,
            config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id,
            {'daily_chat_usage': 0, 'daily_command_usage': 0}
        )
        user_doc.update({'daily_chat_usage': 0, 'daily_command_usage': 0})
    
    if request_type == 'chat':
        if not package_doc.get('allow_ai_chat'):
            return False, "no_ai_chat_permission", user_doc, package_doc
        if package_doc.get('daily_chat_limit', 0) > 0 and user_doc.get('daily_chat_usage', 0) >= package_doc['daily_chat_limit']:
            return False, "daily_chat_limit_exceeded", user_doc, package_doc
        if package_doc.get('monthly_chat_limit', 0) > 0 and user_doc.get('monthly_chat_usage', 0) >= package_doc['monthly_chat_limit']:
            return False, "monthly_chat_limit_exceeded", user_doc, package_doc
    
    elif request_type == 'command':
        if not package_doc.get('allow_ai_commands'):
            return False, "no_ai_command_permission", user_doc, package_doc
        if package_doc.get('daily_command_limit', 0) > 0 and user_doc.get('daily_command_usage', 0) >= package_doc['daily_command_limit']:
            return False, "daily_command_limit_exceeded", user_doc, package_doc
        if package_doc.get('monthly_command_limit', 0) > 0 and user_doc.get('monthly_command_usage', 0) >= package_doc['monthly_command_limit']:
            return False, "monthly_command_limit_exceeded", user_doc, package_doc

    return True, "ok", user_doc, package_doc

async def increment_usage_counters(user_id: str, request_type: str, user_doc: dict):
    """Increments the relevant usage counters for the user."""
    update_data = {'last_usage_date': datetime.now(timezone.utc).isoformat()}
    if request_type == 'chat':
        update_data['daily_chat_usage'] = user_doc.get('daily_chat_usage', 0) + 1
        update_data['monthly_chat_usage'] = user_doc.get('monthly_chat_usage', 0) + 1
    elif request_type == 'command':
        update_data['daily_command_usage'] = user_doc.get('daily_command_usage', 0) + 1
        update_data['monthly_command_usage'] = user_doc.get('monthly_command_usage', 0) + 1
    
    await asyncio.to_thread(
        database.upsert_document,
        config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id, update_data
    )

async def _execute_tool_and_handle_response(tool_name: str, args: dict, update: Update, context: ContextTypes.DEFAULT_TYPE, placeholder_message_id: int):
    """A centralized function to call a tool and process its response."""
    try:
        tool_function = tools.TOOL_MAPPING.get(tool_name)
        if not tool_function:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=placeholder_message_id, text=f"⚠️ ابزار '{tool_name}' یافت نشد.")
            return

        sig = inspect.signature(tool_function)
        tool_params = sig.parameters
        
        # Filter args from the LLM plan to only include what the function accepts
        filtered_args = {k: v for k, v in args.items() if k in tool_params}
        
        # Add the required telegram objects if the function needs them
        if 'update' in tool_params: filtered_args['update'] = update
        if 'context' in tool_params: filtered_args['context'] = context
        
        result = await tool_function(**filtered_args)
        
        if result is not None:
            final_message = result.get('message', 'عملیات با موفقیت انجام شد.')
            if result.get('url'): final_message += f"\n\n🔗 *لینک تسک:* {result['url']}"
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=placeholder_message_id, text=final_message, parse_mode='Markdown')
            logger.info(f"ابزار '{tool_name}' اجرا شد و پاسخ مستقیم ارسال گردید.")
        else:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=placeholder_message_id)
            logger.info(f"ابزار تعاملی '{tool_name}' اجرا شد. پیام موقت حذف گردید.")

    except Exception as e:
        logger.error(f"خطا در اجرای ابزار '{tool_name}': {e}", exc_info=True)
        try:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=placeholder_message_id, text=f"❌ در اجرای دستور شما خطا رخ داد: {str(e)}")
        except Exception as inner_e:
            logger.error(f"Could not even edit the placeholder to show error: {inner_e}")

def log_chat_to_db(user_id: str, user_name: str, user_message: str, bot_response: str, success: bool, error_message: str = None):
    data = {'user_id': user_id, 'user_name': user_name, 'user_message': user_message, 'bot_response': bot_response, 'success': success, 'error_message': error_message, 'timestamp': datetime.now().isoformat()}
    try:
        database.create_document(config.APPWRITE_CHAT_DATABASE_ID, config.CHAT_LOGS_COLLECTION_ID, data)
    except Exception as e:
        logger.error(f"خطا در ذخیره لاگ مکالمه در Appwrite: {e}", exc_info=True)

async def ai_handler_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if context.chat_data.get('auth_flow_active') or context.chat_data.pop('conversation_handled', False):
        return
    if await common.is_user_admin(user_id) or (update.message and update.message.text.startswith('pk_')):
        return
        
    context.chat_data.pop('ai_correction_context', None)
        
    has_access, reason_code, user_doc, _ = await check_ai_access(user_id, 'chat')
    if not has_access:
        reason_map = {
            "no_package": "شما برای استفاده از هوش مصنوعی نیاز به یک پکیج فعال دارید.",
            "pending_payment": "برای فعال‌سازی کامل حساب و دسترسی به هوش مصنوعی، لطفاً فرآیند پرداخت را تکمیل کنید.",
            "no_ai_chat_permission": "پکیج فعلی شما اجازه‌ی چت با هوش مصنوعی را نمی‌دهد. برای دسترسی، پلن خود را ارتقا دهید.",
            "expired": "اعتبار پکیج شما به پایان رسیده است. لطفاً پلن خود را تمدید یا ارتقا دهید.",
            "package_not_found": "خطا: پکیج شما یافت نشد. لطفاً با پشتیبانی تماس بگیرید.",
            "daily_chat_limit_exceeded": "محدودیت چت روزانه شما به پایان رسیده است.",
            "monthly_chat_limit_exceeded": "محدودیت چت ماهانه شما به پایان رسیده است.",
        }
        message_text = reason_map.get(reason_code, f"❌ شما دسترسی لازم را ندارید.")
        
        keyboard = None
        if reason_code in ["no_package", "no_ai_chat_permission", "expired"]:
            keyboard = [[InlineKeyboardButton("🚀 ارتقای پلن", callback_data="upgrade_plan")]]
        elif reason_code == "pending_payment":
            keyboard = [[InlineKeyboardButton("💳 تکمیل ثبت نام و پرداخت", callback_data="start_payment_submission")]]
        
        await update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
        logger.info(f"AI access blocked for user {user_id}. Reason: {reason_code}")
        return

    placeholder_message = await context.bot.send_message(chat_id=update.effective_chat.id, text="در حال پردازش درخواست شما... ⏳")
    
    user_input = update.message.text
    user_name = update.message.from_user.username or "Unknown"
    logger.info(f"درخواست هوش مصنوعی جدید از '{user_name}' ({user_id}): '{user_input}'")
    
    memory = get_memory(user_id)
    history = memory.chat_memory.messages
    
    await update.message.chat.send_action(action='typing')
    routing_messages = [SystemMessage(content=prompts.TOOL_ROUTER_PROMPT)] + history + [HumanMessage(content=user_input)]
    llm_router = ChatOllama(model=config.OLLAMA_MODEL, base_url=config.OLLAMA_BASE_URL, format="json", temperature=0)
    
    try:
        response = await llm_router.ainvoke(routing_messages)
        plan = json.loads(response.content)
        tool_name = plan.get('steps', [{}])[0].get('tool_name', 'no_op')
        
        if tool_name != 'no_op':
            has_access, reason, user_doc, _ = await check_ai_access(user_id, 'command')
            if not has_access:
                reason_map = {
                    "no_ai_command_permission": "پکیج شما اجازه استفاده از دستورات هوشمند را نمی‌دهد.",
                    "daily_command_limit_exceeded": "محدودیت دستورات هوشمند روزانه شما به پایان رسیده است.",
                    "monthly_command_limit_exceeded": "محدودیت دستورات هوشمند ماهانه شما به پایان رسیده است."
                }
                error_message = reason_map.get(reason, f"❌ {reason}")
                await placeholder_message.edit_text(error_message)
                return
            
            tool_args = plan['steps'][0].get('arguments', {})
            await _execute_tool_and_handle_response(tool_name, tool_args, update, context, placeholder_message.message_id)
            await increment_usage_counters(user_id, 'command', user_doc)
            log_chat_to_db(user_id, user_name, user_input, json.dumps(plan), True)

        else: # It's a general chat message
            llm_chat = ChatOllama(model=config.OLLAMA_MODEL, base_url=config.OLLAMA_BASE_URL, temperature=0.7)
            chat_response = await llm_chat.ainvoke([SystemMessage(content=prompts.CHAT_PROMPT)] + history + [HumanMessage(content=user_input)])
            await placeholder_message.edit_text(chat_response.content)
            memory.save_context({"input": user_input}, {"output": chat_response.content})
            await increment_usage_counters(user_id, 'chat', user_doc)
            log_chat_to_db(user_id, user_name, user_input, chat_response.content, True)

    except ConnectError as e:
        logger.error(f"Could not connect to Ollama server: {e}")
        await placeholder_message.edit_text("🚨 متاسفانه در حال حاضر امکان ارتباط با سرور هوش مصنوعی وجود ندارد. لطفاً بعداً تلاش کنید.")
        log_chat_to_db(user_id, user_name, user_input, "Connection Error", False, str(e))
    except Exception as e:
        logger.critical(f"خطای غیرمنتظره در پردازش هوشمند: {e}", exc_info=True)
        await placeholder_message.edit_text(f"🚨 یک خطای غیرمنتظره رخ داد.")
        log_chat_to_db(user_id, user_name, user_input, str(e), False, str(e))

async def handle_ai_delete_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'Yes' or 'No' buttons for an AI-initiated task deletion."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_delete_ai":
        await query.message.edit_text("عملیات حذف تسک لغو شد.")
        return

    task_id = query.data.replace("confirm_delete_ai_", "")
    
    user_id = str(query.from_user.id)
    token = await common.get_user_token(user_id, update, context)
    if not token:
        await query.message.edit_text("خطا: توکن شما برای انجام این عملیات یافت نشد.")
        return

    await query.message.edit_text("در حال حذف تسک از ClickUp و دیتابیس محلی... ⏳")
    
    delete_call = partial(clickup_api.delete_task_in_clickup, task_id, token=token)
    clickup_success = await asyncio.to_thread(delete_call)
    
    if clickup_success:
        db_delete_call = partial(
            database.delete_document_by_clickup_id, 
            config.APPWRITE_DATABASE_ID, 
            config.TASKS_COLLECTION_ID, 
            'clickup_task_id', 
            task_id
        )
        await asyncio.to_thread(db_delete_call)
        await query.message.edit_text("✅ تسک با موفقیت حذف شد.")
    else:
        await query.message.edit_text("❌ حذف تسک از ClickUp ناموفق بود. ممکن است تسک قبلاً حذف شده باشد یا دسترسی لازم را نداشته باشید.")

async def handle_ai_correction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user's selection from a correction keyboard."""
    query = update.callback_query
    await query.answer()

    if query.data == "ai_correction_cancel":
        await query.message.edit_text("عملیات لغو شد.")
        context.chat_data.pop('ai_correction_context', None)
        return

    correction_context = context.chat_data.pop('ai_correction_context', None)
    if not correction_context:
        await query.message.edit_text("خطا: اطلاعات زمینه برای تصحیح یافت نشد. لطفاً دوباره تلاش کنید.")
        return

    parts = query.data.split('_')
    correction_type = parts[2]  # 'list' or 'task'
    corrected_value = '_'.join(parts[3:])

    new_args = correction_context['original_args']
    if correction_type == 'list':
        new_args['list_name'] = corrected_value
    elif correction_type == 'task':
        new_args['task_name'] = corrected_value

    tool_name = correction_context['tool_name']
    
    placeholder = await query.message.edit_text("در حال پردازش مجدد با اطلاعات صحیح... ⏳")
    
    await _execute_tool_and_handle_response(tool_name, new_args, update, context, placeholder.message_id)


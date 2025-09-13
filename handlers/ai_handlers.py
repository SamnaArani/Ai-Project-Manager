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

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

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
    Returns: (has_access, reason, user_doc, package_doc)
    """
    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )
    if not user_doc or not user_doc.get('package_id'):
        return False, "شما پکیج فعالی ندارید.", None, None

    package_doc = await asyncio.to_thread(
        database.get_single_document_by_id, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, user_doc['package_id']
    )
    if not package_doc:
        return False, "پکیج شما یافت نشد.", user_doc, None

    expiry_str = user_doc.get('package_expiry_date')
    if expiry_str:
        try:
            expiry_date = dateutil_parse(expiry_str).date()
            if expiry_date < date.today():
                return False, "اعتبار پکیج شما به پایان رسیده است.", user_doc, package_doc
        except (ValueError, TypeError):
             logger.warning(f"Could not parse expiry date '{expiry_str}' for user {user_id}")


    today_str = date.today().isoformat()
    last_usage_str = user_doc.get('last_usage_date', "").split("T")[0]
    if last_usage_str != today_str:
        await asyncio.to_thread(
            database.upsert_document,
            config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id,
            {'daily_chat_usage': 0, 'daily_command_usage': 0}
        )
        user_doc.update({'daily_chat_usage': 0, 'daily_command_usage': 0})
    
    if request_type == 'chat':
        if not package_doc.get('allow_ai_chat'):
            return False, "پکیج شما اجازه چت با هوش مصنوعی را نمی‌دهد.", user_doc, package_doc
        if package_doc.get('daily_chat_limit', 0) > 0 and user_doc.get('daily_chat_usage', 0) >= package_doc['daily_chat_limit']:
            return False, "محدودیت چت روزانه شما به پایان رسیده است.", user_doc, package_doc
        if package_doc.get('monthly_chat_limit', 0) > 0 and user_doc.get('monthly_chat_usage', 0) >= package_doc['monthly_chat_limit']:
            return False, "محدودیت چت ماهانه شما به پایان رسیده است.", user_doc, package_doc
    
    elif request_type == 'command':
        if not package_doc.get('allow_ai_commands'):
            return False, "پکیج شما اجازه استفاده از دستورات هوشمند را نمی‌دهد.", user_doc, package_doc
        if package_doc.get('daily_command_limit', 0) > 0 and user_doc.get('daily_command_usage', 0) >= package_doc['daily_command_limit']:
            return False, "محدودیت دستورات هوشمند روزانه شما به پایان رسیده است.", user_doc, package_doc
        if package_doc.get('monthly_command_limit', 0) > 0 and user_doc.get('monthly_command_usage', 0) >= package_doc['monthly_command_limit']:
            return False, "محدودیت دستورات هوشمند ماهانه شما به پایان رسیده است.", user_doc, package_doc

    return True, "دسترسی مجاز است.", user_doc, package_doc

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

async def execute_plan(plan: Dict[str, Any], user_input: str, update: Update, context: ContextTypes.DEFAULT_TYPE, placeholder_message_id: int) -> bool:
    tool_name = "unknown"
    try:
        if not plan or 'steps' not in plan or not plan['steps']:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=placeholder_message_id, text="متاسفانه نقشه‌ای برای اجرا دریافت نشد.")
            return False

        first_step = plan['steps'][0]
        tool_name = first_step.get("tool_name")
        tool_function = tools.TOOL_MAPPING.get(tool_name)

        if not tool_function:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=placeholder_message_id, text=f"⚠️ ابزار '{tool_name}' یافت نشد.")
            return False

        arguments = first_step.get("arguments", {})
        
        logger.info(f"شروع اجرای گام ۱/۱: ابزار='{tool_name}', آرگومان‌ها={arguments}")

        sig = inspect.signature(tool_function)
        tool_args = arguments.copy()

        if 'update' in sig.parameters: tool_args['update'] = update
        if 'context' in sig.parameters: tool_args['context'] = context

        result = await tool_function(**tool_args)
        if result is not None:
            final_message = result.get('message', 'عملیات با موفقیت انجام شد.')
            if result.get('url'): final_message += f"\n\n🔗 *لینک تسک:* {result['url']}"
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=placeholder_message_id, text=final_message, parse_mode='Markdown')
            logger.info(f"عملیات تکی با ابزار '{tool_name}' با موفقیت انجام شد.")
        else:
            logger.info(f"ابزار تعاملی '{tool_name}' اجرا شد و منتظر ورودی کاربر است.")

        return True
    
    except Exception as e:
        logger.error(f"خطا در اجرای ابزار '{tool_name}': {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=placeholder_message_id, text=f"❌ در اجرای دستور شما خطا رخ داد: {str(e)}")
        return False

def log_chat_to_db(user_id: str, user_name: str, user_message: str, bot_response: str, success: bool, error_message: str = None):
    data = {'user_id': user_id, 'user_name': user_name, 'user_message': user_message, 'bot_response': bot_response, 'success': success, 'error_message': error_message, 'timestamp': datetime.now().isoformat()}
    try:
        database.create_document(config.APPWRITE_CHAT_DATABASE_ID, config.CHAT_LOGS_COLLECTION_ID, data)
    except Exception as e:
        logger.error(f"خطا در ذخیره لاگ مکالمه در Appwrite: {e}", exc_info=True)

async def ai_handler_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # --- Conversation Locks ---
    if context.chat_data.get('auth_flow_active'):
        logger.info(f"AI handler blocked for user {user_id} because auth flow is active.")
        return
    if context.chat_data.pop('conversation_handled', False):
        logger.info(f"AI handler blocked for user {user_id} because the update was handled by a conversation.")
        return

    # --- Ignore Admins and Tokens ---
    if await common.is_user_admin(user_id):
        return
    if update.message and update.message.text and update.message.text.startswith('pk_'):
        logger.info(f"Ignoring likely ClickUp token from user {user_id}.")
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
        
        request_type = 'chat' if tool_name == 'no_op' else 'command'
        has_access, reason, user_doc, _ = await check_ai_access(user_id, request_type)

        if not has_access:
            await placeholder_message.edit_text(f"❌ {reason}")
            return
            
        if tool_name == 'no_op':
            llm_chat = ChatOllama(model=config.OLLAMA_MODEL, base_url=config.OLLAMA_BASE_URL, temperature=0.7)
            chat_response = await llm_chat.ainvoke([SystemMessage(content=prompts.CHAT_PROMPT)] + history + [HumanMessage(content=user_input)])
            await placeholder_message.edit_text(chat_response.content)
            memory.save_context({"input": user_input}, {"output": chat_response.content})
            await increment_usage_counters(user_id, 'chat', user_doc)
            log_chat_to_db(user_id, user_name, user_input, chat_response.content, True)
        else:
            await execute_plan(plan, user_input, update, context, placeholder_message.message_id)
            await increment_usage_counters(user_id, 'command', user_doc)
            log_chat_to_db(user_id, user_name, user_input, json.dumps(plan), True)

    except ConnectError as e:
        logger.error(f"Could not connect to Ollama server: {e}")
        await placeholder_message.edit_text("🚨 متاسفانه در حال حاضر امکان ارتباط با سرور هوش مصنوعی وجود ندارد. لطفاً بعداً تلاش کنید.")
        log_chat_to_db(user_id, user_name, user_input, "Connection Error", False, str(e))
    except Exception as e:
        logger.critical(f"خطای غیرمنتظره در پردازش هوشمند: {e}", exc_info=True)
        await placeholder_message.edit_text(f"🚨 یک خطای غیرمنتظره رخ داد.")
        log_chat_to_db(user_id, user_name, user_input, str(e), False, str(e))


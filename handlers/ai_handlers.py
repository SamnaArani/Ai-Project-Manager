# -*- coding: utf-8 -*-
import asyncio
import telegram
import json
import logging
import inspect
from typing import Dict, Any
from datetime import datetime
from langchain.memory import ConversationSummaryMemory
from langchain_ollama import ChatOllama
from langchain_core.messages import SystemMessage, HumanMessage
from httpx import ConnectError

from telegram import Update
from telegram.ext import ContextTypes

import config
from ai import prompts, tools
import database
from handlers import standard_handlers

logger = logging.getLogger(__name__)

# حافظه برای هر کاربر (کلید: user_id)
memories = {}

def get_memory(user_id: str) -> ConversationSummaryMemory:
    """حافظه خلاصه‌شده مکالمه هر کاربر رو برمی‌گردونه یا ایجاد می‌کنه."""
    if user_id not in memories:
        memories[user_id] = ConversationSummaryMemory(llm=ChatOllama(model=config.OLLAMA_MODEL, base_url=config.OLLAMA_BASE_URL))
    return memories[user_id]

async def execute_plan(plan: Dict[str, Any], user_input: str, update: Update, context: ContextTypes.DEFAULT_TYPE, placeholder_message_id: int) -> bool:
    """نقشه را به صورت هوشمند اجرا می‌کند."""
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
        if 'update' in sig.parameters: arguments['update'] = update
        if 'context' in sig.parameters: arguments['context'] = context

        result = await tool_function(**arguments)

        if result is not None:
            final_message = result.get('message', 'عملیات با موفقیت انجام شد.')
            if url := result.get('url'):
                final_message += f"\n\n🔗 *لینک تسک:* {url}"
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=placeholder_message_id, text=final_message, parse_mode='Markdown')
        else:
            logger.info(f"ابزار تعاملی '{tool_name}' اجرا شد و منتظر ورودی کاربر است.")

        return True
    
    except Exception as e:
        logger.error(f"خطا در اجرای ابزار '{tool_name}': {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=placeholder_message_id, text=f"❌ در اجرای دستور شما خطا رخ داد: {str(e)}")
        return False

def log_chat_to_db(user_id: str, user_name: str, user_message: str, bot_response: str, success: bool, error_message: str = None):
    """مکالمه‌ها رو توی دیتابیس چت Appwrite ذخیره می‌کنه."""
    data = {'user_id': user_id, 'user_name': user_name, 'user_message': user_message, 'bot_response': bot_response, 'success': success, 'error_message': error_message, 'timestamp': datetime.now().isoformat()}
    try:
        database.create_document(config.APPWRITE_CHAT_DATABASE_ID, config.CHAT_LOGS_COLLECTION_ID, data)
        logger.info(f"لاگ مکالمه برای کاربر {user_id} در دیتابیس ذخیره شد.")
    except Exception as e:
        logger.error(f"خطا در ذخیره لاگ مکالمه در Appwrite: {e}", exc_info=True)

async def ai_handler_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    # The primary authentication check. This function checks memory first, then DB.
    # It also sets the 'is_authenticated' flag in user_data and messages the user if they are not found.
    clickup_token = await standard_handlers._get_user_token(user_id, update, context)
    if not clickup_token:
        # The _get_user_token function already sent a message, so we just exit.
        logger.warning(f"AI handler: User {user_id} is not authenticated. Aborting.")
        return

    user_input = update.message.text
    user_name = update.message.from_user.username or "Unknown"
    
    placeholder_message = await context.bot.send_message(chat_id=update.effective_chat.id, text="در حال پردازش درخواست شما... ⏳")
    
    logger.info(f"درخواست هوش مصنوعی جدید از کاربر '{user_name}' ({user_id}): '{user_input}'")
    
    memory = get_memory(user_id)
    history = memory.chat_memory.messages
    await update.message.chat.send_action(action='typing')
    routing_messages = [SystemMessage(content=prompts.TOOL_ROUTER_PROMPT)] + history + [HumanMessage(content=user_input)]

    llm_router = ChatOllama(model=config.OLLAMA_MODEL, base_url=config.OLLAMA_BASE_URL, format="json", temperature=0)
    try:
        response = await llm_router.ainvoke(routing_messages)
        logger.info(f"پاسخ خام از مسیریاب LLM:\n{response.content}")
        
        plan = json.loads(response.content)
        tool_name = plan.get('steps', [{}])[0].get('tool_name', 'no_op')
        logger.info(f"ابزار انتخاب شده توسط مسیریاب: '{tool_name}'")
        
        if tool_name == 'no_op':
            logger.info("تولید پاسخ محاوره‌ای...")
            chat_prompt_text = prompts.CHAT_PROMPT.format(user_input=user_input)
            llm_chat = ChatOllama(model=config.OLLAMA_MODEL, base_url=config.OLLAMA_BASE_URL, temperature=0.7)
            chat_response = await llm_chat.ainvoke([SystemMessage(content=chat_prompt_text)] + history + [HumanMessage(content=user_input)])
            final_response_text = chat_response.content
            logger.info(f"پاسخ نهایی محاوره‌ای:\n{final_response_text}")
            
            await placeholder_message.edit_text(final_response_text)
            memory.save_context({"input": user_input}, {"output": chat_response.content})
            log_chat_to_db(user_id, user_name, user_input, chat_response.content, True)
        else:
            logger.info(f"اجرای نقشه برای ابزار '{tool_name}'...")
            success = await execute_plan(plan, user_input, update, context, placeholder_message.message_id)
            if not inspect.iscoroutinefunction(tools.TOOL_MAPPING.get(tool_name)):
                 memory.save_context({"input": user_input}, {"output": response.content})
                 log_chat_to_db(user_id, user_name, user_input, response.content, success)

    except ConnectError:
        logger.critical("عدم امکان اتصال به سرویس Ollama. آیا سرویس در حال اجرا است؟", exc_info=True)
        await placeholder_message.edit_text("❌ امکان اتصال به سرویس هوش مصنوعی وجود ندارد. لطفاً مطمئن شوید که Ollama در حال اجرا است.")
        log_chat_to_db(user_id, user_name, user_input, "ConnectError to Ollama", False, "Ollama not running or accessible")
    except json.JSONDecodeError:
        logger.error(f"پاسخ JSON نامعتبر از LLM", exc_info=True)
        await placeholder_message.edit_text("🚨 خطا: پاسخ نامعتبر از هوش مصنوعی دریافت شد.")
        log_chat_to_db(user_id, user_name, user_input, "Invalid JSON Response", False, "JSONDecodeError")
    except Exception as e:
        logger.critical(f"خطای غیرمنتظره در پردازش هوشمند: {e}", exc_info=True)
        await placeholder_message.edit_text(f"🚨 یک خطای غیرمنتظره رخ داد: {str(e)}")
        log_chat_to_db(user_id, user_name, user_input, str(e), False, str(e))


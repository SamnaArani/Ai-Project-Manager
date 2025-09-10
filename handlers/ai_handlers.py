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
from . import common
import clickup_api

logger = logging.getLogger(__name__)

memories = {}

def get_memory(user_id: str) -> ConversationSummaryMemory:
    if user_id not in memories:
        memories[user_id] = ConversationSummaryMemory(llm=ChatOllama(model=config.OLLAMA_MODEL, base_url=config.OLLAMA_BASE_URL))
    return memories[user_id]

async def execute_plan(plan: Dict[str, Any], user_input: str, update: Update, context: ContextTypes.DEFAULT_TYPE, placeholder_message_id: int) -> bool:
    """
    نقشه را به صورت هوشمند اجرا می‌کند و به طور خودکار آبجکت‌های تلگرام را در صورت نیاز پاس می‌دهد.
    """
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

        # اجرای ابزار برای تسک‌های چندگانه یا تکی
        if 'task_names' in tool_args and isinstance(tool_args['task_names'], list):
            final_message = ""
            tasks_to_process = tool_args.pop('task_names')
            logger.info(f"پردازش گروهی برای {len(tasks_to_process)} تسک آغاز شد.")
            for task_name in tasks_to_process:
                single_task_args = {**tool_args, 'task_name': task_name}
                logger.info(f"  -> در حال پردازش تسک '{task_name}'...")
                try:
                    result = await tool_function(**single_task_args)
                    if result:
                        final_message += result.get('message', 'عملیات با موفقیت انجام شد.') + "\n\n"
                        if result.get('url'): final_message += f"🔗 *لینک تسک:* {result['url']}\n\n"
                        logger.info(f"  -->> عملیات روی تسک '{task_name}' موفق بود.")
                except Exception as e:
                    final_message += f"❌ *خطا در تسک «{task_name}»*: {str(e)}\n\n"
            
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=placeholder_message_id, text=final_message, parse_mode='Markdown')
            logger.info("پردازش گروهی تسک‌ها به پایان رسید.")
        else:
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
    
    # از ارسال پیام‌هایی که شبیه توکن هستند، جلوگیری می‌کنیم
    if update.message and update.message.text and update.message.text.startswith('pk_'):
        logger.info(f"Ignoring likely ClickUp token from user {user_id}.")
        return
        
    # توکن را به صورت بی‌صدا دریافت می‌کنیم. اگر کاربر ادمین باشد و توکن نداشته باشد، مشکلی پیش نمی‌آید
    clickup_token = await common.get_user_token(user_id, update, context, notify_user=False)
    if not clickup_token:
        logger.info(f"AI handler: User {user_id} does not have a token. Silently aborting AI processing.")
        return

    user_input = update.message.text
    user_name = update.message.from_user.username or "Unknown"
    
    placeholder_message = await context.bot.send_message(chat_id=update.effective_chat.id, text="در حال پردازش درخواست شما... ⏳")
    
    logger.info(f"درخواست هوش مصنوعی جدید از '{user_name}' ({user_id}): '{user_input}'")
    
    memory = get_memory(user_id)
    conversation_state = context.chat_data.get('conversation_state')
    
    if conversation_state == 'awaiting_delete_confirmation':
        logger.info(f"کاربر در وضعیت 'awaiting_delete_confirmation' است. پردازش پاسخ...")
        pending_deletion_info = context.chat_data.pop('pending_deletion', None)
        context.chat_data.pop('conversation_state', None)
        if not pending_deletion_info:
             logger.error("خطای داخلی: pending_deletion_info در حالی که در وضعیت تایید حذف بود، یافت نشد.")
             await placeholder_message.edit_text("خطای داخلی: اطلاعات تسک برای حذف یافت نشد.")
             return
        
        if user_input.lower() in ['بله', 'آره', 'yes', 'y']:
            logger.info("کاربر حذف را تایید کرد.")
            task_id, task_name = pending_deletion_info['task_id'], pending_deletion_info['task_name']
            await placeholder_message.edit_text(f"در حال حذف تسک '{task_name}'...")
            
            if await asyncio.to_thread(clickup_api.delete_task_in_clickup, task_id, clickup_token):
                logger.info(f"تسک '{task_name}' ({task_id}) با موفقیت از ClickUp حذف شد.")
                db_deleted = await asyncio.to_thread(
                    database.delete_document_by_clickup_id, 
                    config.APPWRITE_DATABASE_ID, 
                    config.TASKS_COLLECTION_ID, 
                    'clickup_task_id', 
                    task_id
                )
                if db_deleted:
                    logger.info(f"تسک '{task_name}' ({task_id}) با موفقیت از دیتابیس محلی حذف شد.")
                    await placeholder_message.edit_text(f"✅ تسک '{task_name}' با موفقیت حذف شد.")
                else:
                    logger.warning(f"تسک '{task_name}' ({task_id}) از دیتابیس محلی حذف نشد (احتمالا از قبل وجود نداشت).")
                    await placeholder_message.edit_text(f"⚠️ تسک از ClickUp حذف شد، اما از دیتابیس محلی حذف نشد.")
            else:
                logger.error(f"حذف تسک '{task_name}' ({task_id}) از ClickUp ناموفق بود.")
                await placeholder_message.edit_text("❌ حذف تسک از ClickUp ناموفق بود.")
        else:
            logger.info("کاربر حذف را لغو کرد.")
            await placeholder_message.edit_text(f"عملیات حذف تسک '{pending_deletion_info.get('task_name', 'مورد نظر')}' لغو شد.")
        return

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
            logger.info("مسیریاب 'no_op' را انتخاب کرد. تولید پاسخ محاوره‌ای...")
            chat_prompt_text = prompts.CHAT_PROMPT.format(user_input=user_input)
            llm_chat = ChatOllama(model=config.OLLAMA_MODEL, base_url=config.OLLAMA_BASE_URL, temperature=0.7)
            chat_response = await llm_chat.ainvoke([SystemMessage(content=chat_prompt_text)] + history + [HumanMessage(content=user_input)])
            logger.info(f"پاسخ نهایی محاوره‌ای:\n{chat_response.content}")
            await placeholder_message.edit_text(chat_response.content)
            memory.save_context({"input": user_input}, {"output": chat_response.content})
            log_chat_to_db(user_id, user_name, user_input, chat_response.content, True)
        else:
            success = await execute_plan(plan, user_input, update, context, placeholder_message.message_id)
            if tool_name not in ['confirm_and_delete_task', 'ask_user']: # Don't log interactive tool starts
                 memory.save_context({"input": user_input}, {"output": response.content})
                 log_chat_to_db(user_id, user_name, user_input, response.content, success)

    except ConnectError:
        await placeholder_message.edit_text("❌ امکان اتصال به سرویس هوش مصنوعی وجود ندارد.")
        log_chat_to_db(user_id, user_name, user_input, "ConnectError to Ollama", False, "Ollama not running")
    except json.JSONDecodeError:
        await placeholder_message.edit_text("🚨 خطا: پاسخ نامعتبر از هوش مصنوعی دریافت شد.")
        log_chat_to_db(user_id, user_name, user_input, "Invalid JSON Response", False, "JSONDecodeError")
    except Exception as e:
        logger.critical(f"خطای غیرمنتظره در پردازش هوشمند: {e}", exc_info=True)
        await placeholder_message.edit_text(f"🚨 یک خطای غیرمنتظره رخ داد: {str(e)}")
        log_chat_to_db(user_id, user_name, user_input, str(e), False, str(e))


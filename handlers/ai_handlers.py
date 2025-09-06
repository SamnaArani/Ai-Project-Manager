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
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import BadRequest

import config
from ai import prompts, tools
import clickup_api
import database

logger = logging.getLogger(__name__)

# حافظه برای هر کاربر (کلید: user_id)
memories = {}

def get_memory(user_id: str) -> ConversationSummaryMemory:
    """حافظه خلاصه‌شده مکالمه هر کاربر رو برمی‌گردونه یا ایجاد می‌کنه."""
    if user_id not in memories:
        memories[user_id] = ConversationSummaryMemory(llm=ChatOllama(model=config.OLLAMA_MODEL, base_url=config.OLLAMA_BASE_URL))
    return memories[user_id]

async def execute_plan(plan: Dict[str, Any], user_input: str, update: Update, context: ContextTypes.DEFAULT_TYPE, placeholder_message_id: int) -> bool:
    """
    نقشه را به صورت هوشمند اجرا می‌کند.
    به طور خودکار تشخیص می‌دهد که کدام ابزارها به آبجکت‌های تلگرام نیاز دارند و آن‌ها را پاس می‌دهد.
    """
    try:
        if not plan or 'steps' not in plan or not plan['steps']:
            logger.warning("نقشه اجرایی نامعتبر یا خالی است.")
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=placeholder_message_id,
                text="متاسفانه نقشه‌ای برای اجرا دریافت نشد."
            )
            return False

        first_step = plan['steps'][0]
        tool_name = first_step.get("tool_name")

        if not tool_name or tool_name not in tools.TOOL_MAPPING or tools.TOOL_MAPPING[tool_name] is None:
            logger.error(f"ابزار نامعتبر یا غیرفعال '{tool_name}' در نقشه یافت شد.")
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=placeholder_message_id,
                text=f"⚠️ ابزار '{tool_name}' یافت نشد یا در حال حاضر غیرفعال است."
            )
            return False

        raw_arguments = first_step.get("arguments", {})
        tool_function = tools.TOOL_MAPPING[tool_name]

        # اعتبارسنجی آرگومان‌ها
        final_arguments = {}
        user_input_lower = user_input.lower()
        
        # Task Names and List Name
        if 'task_name' in raw_arguments:
            final_arguments['task_name'] = raw_arguments['task_name']
        if 'task_names' in raw_arguments:
            final_arguments['task_names'] = raw_arguments['task_names']
        if 'list_name' in raw_arguments:
            final_arguments['list_name'] = raw_arguments['list_name']

        # New fields for update
        update_fields = ['new_name', 'new_description', 'new_status', 'new_priority', 'new_assignee_name', 'new_due_date']
        for field in update_fields:
            if field in raw_arguments:
                final_arguments[field] = raw_arguments[field]
        
        # Optional args for create/delete
        optional_arg_keywords = {
            'description': ['description', 'توضیح'],
            'priority': ['priority', 'اولویت'],
            'status': ['status', 'وضعیت'],
            'assignee_name': ['assign', 'assignee', 'اساین', 'مسئول', 'اختصاص'],
            'start_date': ['start', 'شروع'],
            'due_date': ['due', 'date', 'تاریخ', 'تحویل'],
            'question': ['question', 'سوال', 'چه', 'چیه', 'چی']
        }
        for arg, keywords in optional_arg_keywords.items():
            if arg in raw_arguments and (arg in ['question'] or any(keyword in user_input_lower for keyword in keywords)):
                if arg in ['start_date', 'due_date']:
                    if not any(neg_keyword in user_input_lower for neg_keyword in ['خالی', 'نزن', 'نداره']):
                        final_arguments[arg] = raw_arguments[arg]
                else:
                    final_arguments[arg] = raw_arguments[arg]

        # اطمینان از اینکه question برای ask_user همیشه وجود داشته باشه
        if tool_name == 'ask_user' and 'question' not in final_arguments and 'question' in raw_arguments:
            final_arguments['question'] = raw_arguments['question']
        elif tool_name == 'ask_user' and 'question' not in final_arguments:
            final_arguments['question'] = "لطفاً جزئیات بیشتری ارائه دهید."

        logger.info(f"آرگومان‌های خام از LLM: {raw_arguments}")
        logger.info(f"شروع اجرای گام ۱/۱: ابزار='{tool_name}', آرگومان‌های نهایی={final_arguments}")

        sig = inspect.signature(tool_function)
        tool_args = final_arguments.copy()

        # اضافه کردن update و context اگر لازم باشه
        if 'update' in sig.parameters:
            tool_args['update'] = update
        if 'context' in sig.parameters:
            tool_args['context'] = context

        # اجرای تابع ابزار
        if 'task_names' in tool_args:
            final_message = ""
            for task_name in tool_args['task_names']:
                single_task_args = {**tool_args, 'task_name': task_name}
                single_task_args.pop('task_names', None)
                try:
                    result = await tool_function(**single_task_args)
                    if result:
                        final_message += result.get('message', 'عملیات با موفقیت انجام شد.') + "\n\n"
                        if result.get('url'):
                            final_message += f"🔗 *لینک تسک:* {result['url']}\n\n"
                except Exception as e:
                    logger.error(f"خطا در اجرای ابزار '{tool_name}' برای تسک '{task_name}': {e}", exc_info=True)
                    final_message += f"❌ در اجرای دستور برای تسک '{task_name}' خطا رخ داد: {str(e)}\n\n"
            
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=placeholder_message_id,
                text=final_message,
                parse_mode='Markdown'
            )
            logger.info("عملیات با موفقیت اجرا شد.")
        else:
            result = await tool_function(**tool_args)

            if result is not None:
                final_message = result.get('message', 'عملیات با موفقیت انجام شد.')
                if result.get('url'):
                    final_message += f"\n\n🔗 *لینک تسک:* {result['url']}"
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=placeholder_message_id,
                    text=final_message,
                    parse_mode='Markdown'
                )
                logger.info("عملیات با موفقیت اجرا شد.")
            else:
                logger.info(f"ابزار تعاملی '{tool_name}' اجرا شد و منتظر ورودی کاربر است.")

        return True
    
    except Exception as e:
        logger.error(f"خطا در اجرای ابزار '{tool_name}': {e}", exc_info=True)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=placeholder_message_id,
            text=f"❌ در اجرای دستور شما خطا رخ داد: {str(e)}"
        )
        return False

def log_chat(user_id: str, user_name: str, user_message: str, bot_response: str, success: bool, error_message: str = None):
    """مکالمه‌ها رو توی دیتابیس چت Appwrite ذخیره می‌کنه."""
    data = {
        'user_id': user_id,
        'user_name': user_name,
        'user_message': user_message,
        'bot_response': bot_response,
        'success': success,
        'error_message': error_message,
        'timestamp': datetime.now().isoformat()
    }
    try:
        database.upsert_document(
            collection_id=config.CHAT_LOGS_COLLECTION_ID,
            query_key='user_id',
            query_value=user_id,
            data=data
        )
        logger.info(f"مکالمه برای کاربر {user_id} با موفقیت در دیتابیس چت ذخیره شد.")
    except Exception as e:
        logger.error(f"خطا در ذخیره مکالمه در Appwrite: {e}", exc_info=True)

async def ai_handler_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    user_id = str(update.message.from_user.id)
    user_name = update.message.from_user.username or "Unknown"
    
    # Send a quick placeholder message and store its ID
    placeholder_message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="در حال پردازش درخواست شما... لطفاً منتظر بمانید ⏳"
    )
    
    # Step 1: Check user's package and AI access
    user_doc = database.get_single_document(config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id)
    if not user_doc:
        await context.bot.edit_message_text(
            chat_id=placeholder_message.chat_id,
            message_id=placeholder_message.message_id,
            text="❌ متاسفم، شما هنوز در سیستم ثبت نام نکرده‌اید. لطفاً برای شروع، دستور /start را ارسال کنید."
        )
        return

    package_id = user_doc.get('package_id')
    package_doc = database.get_single_document(config.PACKAGES_COLLECTION_ID, '$id', package_id) if package_id else None
    
    if package_doc and package_doc.get('ai_call_limit') == 0:
        await context.bot.edit_message_text(
            chat_id=placeholder_message.chat_id,
            message_id=placeholder_message.message_id,
            text="❌ برای استفاده از امکانات هوش مصنوعی، لطفاً پکیج خود را ارتقا دهید."
        )
        return
    
    # Step 2: Proceed with AI processing
    logger.info(f"درخواست هوش مصنوعی جدید از کاربر: '{user_input}'")
    
    memory = get_memory(user_id)
    conversation_state = context.chat_data.get('conversation_state')
    
    if conversation_state == 'awaiting_delete_confirmation':
        pending_deletion_info = context.chat_data.get('pending_deletion')
        context.chat_data.pop('conversation_state', None)
        context.chat_data.pop('pending_deletion', None)
        try:
            if user_input.lower() in ['بله', 'آره', 'yes', 'y']:
                if pending_deletion_info:
                    task_id, task_name = pending_deletion_info['task_id'], pending_deletion_info['task_name']
                    await context.bot.edit_message_text(chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, text=f"در حال حذف تسک '{task_name}'...")
                    if await asyncio.to_thread(clickup_api.delete_task_in_clickup, task_id):
                        if await asyncio.to_thread(database.delete_document_by_clickup_id, config.TASKS_COLLECTION_ID, task_id):
                            await context.bot.edit_message_text(chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, text=f"✅ تسک '{task_name}' با موفقیت از ClickUp و دیتابیس محلی حذف شد.")
                        else:
                            await context.bot.edit_message_text(chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, text=f"⚠️ تسک '{task_name}' از ClickUp حذف شد، اما حذف از دیتابیس محلی ناموفق بود یا تسک قبلاً حذف شده بود.")
                    else:
                        await context.bot.edit_message_text(chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, text="❌ حذف تسک از ClickUp ناموفق بود.")
                else:
                    await context.bot.edit_message_text(chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, text="خطای داخلی: اطلاعات تسک برای حذف یافت نشد.")
            else:
                await context.bot.edit_message_text(chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, text=f"عملیات حذف تسک '{pending_deletion_info.get('task_name', 'مورد نظر')}' لغو شد.")
        except Exception as e:
            logger.error(f"خطا در حین عملیات حذف تأیید شده: {e}", exc_info=True)
            await context.bot.edit_message_text(chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, text=f"❌ خطایی در هنگام حذف رخ داد: {e}")
        return

    history = memory.chat_memory.messages[-2:] if len(memory.chat_memory.messages) > 1 else memory.chat_memory.messages
    await update.message.chat.send_action(action='typing')
    routing_messages = [SystemMessage(content=prompts.TOOL_ROUTER_PROMPT)] + history + [HumanMessage(content=user_input)]

    llm_router = ChatOllama(model=config.OLLAMA_MODEL, base_url=config.OLLAMA_BASE_URL, format="json", temperature=0)
    try:
        response = await llm_router.ainvoke(routing_messages)

        try:
            plan = json.loads(response.content)
        except json.JSONDecodeError:
            logger.error(f"پاسخ JSON نامعتبر از LLM دریافت شد: {response.content}")
            await context.bot.edit_message_text(chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, text="🚨 خطا: پاسخ نامعتبر از هوش مصنوعی دریافت شد. لطفاً دوباره تلاش کنید.")
            log_chat(user_id, user_name, user_input, response.content, False, "JSONDecodeError")
            return

        tool_name = plan.get('steps', [{}])[0].get('tool_name', 'no_op')
        
        # Check for multiple tasks and modify the plan if needed
        if 'task_names' not in plan.get('steps', [{}])[0].get('arguments', {}) and 'task_name' in plan.get('steps', [{}])[0].get('arguments', {}):
            plan['steps'][0]['arguments']['task_names'] = [plan['steps'][0]['arguments'].pop('task_name')]
        
        if tool_name not in tools.TOOL_MAPPING or tools.TOOL_MAPPING.get(tool_name) is None:
            tool_name = 'no_op'

        if tool_name == 'ask_user' and any(keyword in user_input.lower() for keyword in ['راهکار', 'مشاوره', 'مناسب', 'توضیح', 'بدونم', 'بیشتر']):
            tool_name = 'no_op'

        if tool_name == 'no_op':
            logger.info("مسیریاب 'no_op' را انتخاب کرد. تولید پاسخ محاوره‌ای...")
            chat_prompt_text = prompts.CHAT_PROMPT.format(user_input=user_input)
            llm_chat = ChatOllama(model=config.OLLAMA_MODEL, base_url=config.OLLAMA_BASE_URL, temperature=0.7)
            chat_response = await llm_chat.ainvoke([SystemMessage(content=chat_prompt_text)] + history + [HumanMessage(content=user_input)])
            final_response_text = chat_response.content
            await context.bot.edit_message_text(chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, text=final_response_text)
            memory.save_context({"input": user_input}, {"output": chat_response.content})
            log_chat(user_id, user_name, user_input, chat_response.content, True)
        else:
            logger.info(f"مسیریاب ابزار '{tool_name}' را انتخاب کرد. اجرای نقشه...")
            is_interactive = tool_name in ['ask_user', 'confirm_and_delete_task', 'create_task', 'update_task']
            
            # Pass placeholder message info to execute_plan
            success = await execute_plan(plan, user_input, update, context, placeholder_message.message_id)

            if not is_interactive:
                memory.save_context({"input": user_input}, {"output": response.content})
                log_chat(user_id, user_name, user_input, response.content, success)

    except json.JSONDecodeError:
        logger.error("پاسخ از LLM به فرمت JSON نیست.", exc_info=True)
        await context.bot.edit_message_text(chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, text="🚨 خطا: پاسخ نامعتبر از هوش مصنوعی دریافت شد.")
        log_chat(user_id, user_name, user_input, "پاسخ نامعتبر", False, "JSONDecodeError")
    except telegram.error.NetworkError as ne:
        logger.error(f"خطای شبکه در ارتباط با تلگرام: {ne}", exc_info=True)
        await context.bot.edit_message_text(chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, text="🚨 خطای شبکه: لطفاً اتصال اینترنت را بررسی کنید.")
        log_chat(user_id, user_name, user_input, str(ne), False, str(ne))
    except Exception as e:
        logger.critical(f"یک خطای غیرمنتظره در پردازش هوشمند رخ داد: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, text=f"🚨 یک خطای غیرمنتظره رخ داد: {str(e)}")
        log_chat(user_id, user_name, user_input, str(e), False, str(e))

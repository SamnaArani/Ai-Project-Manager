# -*- coding: utf-8 -*-
import asyncio
import logging
from typing import Optional, Dict, Any, Tuple, List
from datetime import datetime, timedelta
from dateutil.parser import parse as dateutil_parse
from thefuzz import process as fuzz_process
import re
from functools import partial

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from appwrite.query import Query

import config
import database
import clickup_api
from handlers import common as standard_handlers

logger = logging.getLogger(__name__)

# --- توابع کمکی ---

def parse_date(date_str: str) -> Optional[int]:
    """تاریخ را به فرمت timestamp کلیک‌اپ تبدیل می‌کند."""
    if not date_str: return None
    today = datetime.now()
    try:
        parsed_date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        try:
            parsed_date = dateutil_parse(date_str, default=today, fuzzy=True, dayfirst=False)
        except (ValueError, TypeError):
            date_str_lower = date_str.lower()
            if "امروز" in date_str_lower: parsed_date = today
            elif "فردا" in date_str_lower: parsed_date = today + timedelta(days=1)
            elif "پس فردا" in date_str_lower: parsed_date = today + timedelta(days=2)
            elif "دیروز" in date_str_lower: parsed_date = today - timedelta(days=1)
            elif "روز دیگه" in date_str_lower or "روز دیگر" in date_str_lower:
                try:
                    days_match = re.search(r'\d+', date_str)
                    if days_match:
                        days = int(days_match.group(0))
                        parsed_date = today + timedelta(days=days)
                    else: return None
                except (ValueError, IndexError): return None
            else:
                return None
    return int(parsed_date.timestamp() * 1000)

def _clean_text(text: str) -> str:
    """Removes leading/trailing whitespace and non-breaking spaces."""
    if not isinstance(text, str):
        return ""
    return text.replace('\xa0', ' ').strip()

def _find_task_in_db(task_name: str, list_name: str, user_id: str) -> Optional[Tuple[Dict[str, Any], str]]:
    """یک تسک را با جستجوی دقیق و سپس فازی برای یک کاربر مشخص پیدا کرده و خود تسک به همراه نام لیست را برمی‌گرداند."""
    user_query = [Query.equal("telegram_id", [user_id])]
    
    lists = database.get_documents(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, user_query)
    if not lists:
        raise ValueError("هیچ لیستی برای شما در دیتابیس یافت نشد.")
        
    list_choices = {lst['name']: lst['clickup_list_id'] for lst in lists}
    
    if not list_name:
        all_list_names = ", ".join(list_choices.keys())
        raise ValueError(f"نام لیست مشخص نشده است. لیست‌های موجود شما: {all_list_names}")

    clean_list_name_input = _clean_text(list_name).lower()
    best_list_match = None
    
    for name in list_choices.keys():
        if _clean_text(name).lower() == clean_list_name_input:
            best_list_match = name
            break
    
    if not best_list_match:
        match_result = fuzz_process.extractOne(list_name, list_choices.keys())
        if match_result and match_result[1] >= 80:
            best_list_match = match_result[0]
        else:
            all_list_names = ", ".join(list_choices.keys())
            raise ValueError(f"لیست '{list_name}' یافت نشد. لیست‌های موجود شما: {all_list_names}")

    list_id = list_choices[best_list_match]
    task_query = user_query + [Query.equal("list_id", [list_id])]
    tasks_in_list = database.get_documents(config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, task_query)
    
    if not tasks_in_list:
        raise ValueError(f"هیچ تسکی در لیست '{best_list_match}' یافت نشد.")

    task_titles = {task['title']: task for task in tasks_in_list if task.get('title')}
    
    clean_task_name_input = _clean_text(task_name).lower()
    best_task_match_title = None

    for title in task_titles.keys():
        if _clean_text(title).lower() == clean_task_name_input:
            best_task_match_title = title
            break
            
    if not best_task_match_title:
        match_result = fuzz_process.extractOne(task_name, task_titles.keys())
        if match_result and match_result[1] > 80:
            best_task_match_title = match_result[0]
        else:
            suggestion = f" آیا منظورتان '{match_result[0]}' بود؟" if match_result else ""
            raise ValueError(f"تسک با نام نزدیک به '{task_name}' در لیست '{best_list_match}' یافت نشد.{suggestion}")

    return task_titles[best_task_match_title], best_list_match


async def _handle_find_task_error(
    e: ValueError, 
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE, 
    tool_name: str, 
    original_args: dict
) -> Optional[Dict[str, Any]]:
    """Handles ValueError from _find_task_in_db by starting an interactive correction flow."""
    error_msg = str(e)
    user_id = str(update.effective_user.id)
    user_query = [Query.equal("telegram_id", [user_id])]
    
    target_message = update.effective_message
    if not target_message:
        logger.error("Could not find a target message in _handle_find_task_error")
        return {"message": f"❌ {error_msg}"}

    if "لیست" in error_msg and "یافت نشد" in error_msg:
        list_name_attempted = original_args.get('list_name', '')
        retry_args = {k: v for k, v in original_args.items() if k != 'list_name'}
        
        context.chat_data['ai_correction_context'] = {'tool_name': tool_name, 'original_args': retry_args}
        lists = database.get_documents(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, user_query)
        keyboard = [[InlineKeyboardButton(lst['name'], callback_data=f"ai_correct_list_{lst['name']}")] for lst in lists]
        keyboard.append([InlineKeyboardButton("❌ لغو", callback_data="ai_correction_cancel")])
        
        await target_message.reply_text(
            f"⚠️ لیست «{list_name_attempted}» یافت نشد. لطفاً لیست صحیح را انتخاب کنید:", 
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return None

    elif "تسک" in error_msg and "یافت نشد" in error_msg:
        task_name_attempted = original_args.get('task_name', '')
        list_name = original_args.get('list_name')
        retry_args = {k: v for k, v in original_args.items() if k != 'task_name'}

        lists = database.get_documents(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, user_query)
        list_choices = {lst['name']: lst['clickup_list_id'] for lst in lists}
        best_list_match_name, _ = fuzz_process.extractOne(list_name, list_choices.keys())
        list_id = list_choices[best_list_match_name]

        task_query = user_query + [Query.equal("list_id", [list_id])]
        tasks_in_list = database.get_documents(config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, task_query)
        
        context.chat_data['ai_correction_context'] = {'tool_name': tool_name, 'original_args': retry_args}
        
        keyboard = [[InlineKeyboardButton(task['title'], callback_data=f"ai_correct_task_{task['title']}")] for task in tasks_in_list if task.get('title')]
        keyboard.append([InlineKeyboardButton("❌ لغو", callback_data="ai_correction_cancel")])
        
        await target_message.reply_text(
            f"⚠️ تسک «{task_name_attempted}» در لیست «{best_list_match_name}» یافت نشد. آیا منظورتان یکی از تسک‌های زیر است؟",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return None
    
    else:
        return {"message": f"❌ {error_msg}"}


# --- ابزارهای اصلی ---
async def create_task(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE,
    task_name: str,
    list_name: str,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    assignee_name: Optional[str] = None,
    status: Optional[str] = None,
    start_date: Optional[str] = None,
    due_date: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    user_id = str(update.effective_user.id)
    token = await standard_handlers.get_user_token(user_id, update, context)
    if not token: return {"message": "خطا: توکن کاربر یافت نشد."}
    
    if not task_name or not list_name: raise ValueError("نام تسک و نام لیست الزامی است.")
    
    original_args = {k: v for k, v in locals().items() if k not in ['update', 'context', 'user_id', 'token'] and v is not None}
    
    try:
        user_query = [Query.equal("telegram_id", [user_id])]
        lists = database.get_documents(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, user_query)
        list_choices = {lst['name']: lst['clickup_list_id'] for lst in lists}
        if not list_choices: return {"message": "هیچ لیستی برای شما یافت نشد. لطفاً ابتدا از همگام‌سازی اطلاعات خود مطمئن شوید."}
        
        best_list_match, list_score = fuzz_process.extractOne(list_name, list_choices.keys())
        if list_score < 80:
            raise ValueError(f"لیست '{list_name}' یافت نشد")

    except ValueError as e:
        return await _handle_find_task_error(e, update, context, 'create_task', original_args)

    list_id = list_choices[best_list_match]
    payload = {"name": task_name}
    if description: payload["description"] = description
    
    if assignee_name:
        users = database.get_documents(config.APPWRITE_DATABASE_ID, config.CLICKUP_USERS_COLLECTION_ID, [Query.equal("telegram_id", [user_id])])
        user_choices = {user['username']: user['clickup_user_id'] for user in users}
        best_user_match, user_score = fuzz_process.extractOne(assignee_name, user_choices.keys())
        if user_score < 80: return {"message": f"کاربر '{assignee_name}' یافت نشد."}
        payload["assignees"] = [int(user_choices[best_user_match])]

    if start_date and (start_timestamp := parse_date(start_date)): payload["start_date"] = start_timestamp
    if due_date and (due_timestamp := parse_date(due_date)): payload["due_date"] = due_timestamp
    
    priority_map = {"فوری": 1, "بالا": 2, "متوسط": 3, "پایین": 4}
    if priority:
        best_priority_match, _ = fuzz_process.extractOne(priority.lower(), priority_map.keys())
        if not best_priority_match: return {"message": f"اولویت '{priority}' معتبر نیست."}
        payload['priority'] = priority_map[best_priority_match]

    if status:
        list_statuses = await asyncio.to_thread(clickup_api.get_list_statuses, list_id, token=token)
        status_name_map = {s['status'].lower(): s['status'] for s in list_statuses}
        best_status_match, status_score = fuzz_process.extractOne(status.lower(), status_name_map.keys())
        if status_score < 80: return {"message": f"وضعیت '{status}' در این لیست معتبر نیست."}
        payload['status'] = status_name_map[best_status_match]

    success, task_data = await asyncio.to_thread(clickup_api.create_task_in_clickup_api, list_id, payload, token=token)
    
    if not success: raise Exception(f"ClickUp API error: {task_data.get('err', 'Unknown error')}")
    
    task_id = task_data.get('id')
    if task_id:
        synced_task = await asyncio.to_thread(clickup_api.sync_single_task_from_clickup, task_id, token=token, telegram_id=user_id)
        if synced_task:
            def format_dt(ts): return datetime.fromtimestamp(int(ts)/1000).strftime('%Y-%m-%d') if ts else "خالی"
            details = [
                f"✅ تسک با موفقیت در لیست *{best_list_match}* ساخته شد!\n",
                f"🏷️ *عنوان:* {synced_task.get('title', 'خالی')}",
                f"👤 *مسئول:* {synced_task.get('assignee_name', 'خالی') or 'خالی'}",
                f"📊 *وضعیت:* {synced_task.get('status', 'خالی') or 'خالی'}",
                f"❗️ *اولویت:* {synced_task.get('priority', 'خالی') or 'خالی'}",
                f"🏁 *تاریخ تحویل:* {format_dt(synced_task.get('due_date'))}"
            ]
            return {"message": "\n".join(details), "url": task_data.get('url')}
    
    return {"message": f"✅ تسک '{task_name}' با موفقیت ساخته شد.", "url": task_data.get('url')}

async def update_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    task_name: str,
    list_name: str,
    new_name: Optional[str] = None,
    new_description: Optional[str] = None,
    new_status: Optional[str] = None,
    new_priority: Optional[str] = None,
    new_assignee_name: Optional[str] = None,
    new_due_date: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    user_id = str(update.effective_user.id)
    token = await standard_handlers.get_user_token(user_id, update, context)
    if not token: return {"message": "خطا: توکن کاربر یافت نشد."}
    
    original_args = {k: v for k, v in locals().items() if k not in ['update', 'context', 'user_id', 'token'] and v is not None}

    try:
        task, list_name_found = _find_task_in_db(task_name, list_name, user_id)
    except ValueError as e:
        return await _handle_find_task_error(e, update, context, 'update_task', original_args)

    payload = {}
    if new_name: payload['name'] = new_name
    if new_description: payload['description'] = new_description
    if new_assignee_name:
        user_query = [Query.equal("telegram_id", [user_id])]
        users = database.get_documents(config.APPWRITE_DATABASE_ID, config.CLICKUP_USERS_COLLECTION_ID, user_query)
        user_choices = {user['username']: user['clickup_user_id'] for user in users}
        best_user_match, user_score = fuzz_process.extractOne(new_assignee_name, user_choices.keys())
        if user_score > 80:
            payload['assignees'] = {"add": [int(user_choices[best_user_match])]}
        else:
            return {"message": f"کاربر '{new_assignee_name}' یافت نشد."}
    if new_status:
        list_statuses = await asyncio.to_thread(clickup_api.get_list_statuses, task['list_id'], token=token)
        status_name_map = {s['status'].lower(): s['status'] for s in list_statuses}
        best_status_match, status_score = fuzz_process.extractOne(new_status.lower(), status_name_map.keys())
        if status_score > 80:
            payload['status'] = status_name_map[best_status_match]
        else:
            return {"message": f"وضعیت '{new_status}' در این لیست معتبر نیست."}
    
    priority_map = {"فوری": 1, "بالا": 2, "متوسط": 3, "پایین": 4}
    if new_priority:
        best_priority_match, _ = fuzz_process.extractOne(new_priority.lower(), priority_map.keys())
        if best_priority_match:
            payload['priority'] = priority_map[best_priority_match]
        else:
            return {"message": f"اولویت '{new_priority}' معتبر نیست."}

    if new_due_date and (due_timestamp := parse_date(new_due_date)): payload["due_date"] = due_timestamp

    if not payload: raise ValueError("هیچ تغییری برای اعمال مشخص نشده است.")
    
    success, response_data = await asyncio.to_thread(clickup_api.update_task_in_clickup_api, task['clickup_task_id'], payload, token=token)
    if not success: raise Exception(f"ClickUp API error: {response_data.get('err', 'Unknown error')}")
        
    synced_task = await asyncio.to_thread(clickup_api.sync_single_task_from_clickup, task['clickup_task_id'], token=token, telegram_id=user_id)
    if synced_task:
        def format_dt(ts): return datetime.fromtimestamp(int(ts)/1000).strftime('%Y-%m-%d') if ts else "خالی"
        details = [
            f"✅ تسک '{task['title']}' با موفقیت به‌روزرسانی شد. جزئیات جدید:",
            f"🏷️ *عنوان:* {synced_task.get('title', 'خالی')}",
            f"👤 *مسئول:* {synced_task.get('assignee_name', 'خالی') or 'خالی'}",
            f"📊 *وضعیت:* {synced_task.get('status', 'خالی') or 'خالی'}",
            f"❗️ *اولویت:* {synced_task.get('priority', 'خالی') or 'خالی'}",
            f"🏁 *تاریخ تحویل:* {format_dt(synced_task.get('due_date'))}"
        ]
        return {"message": "\n".join(details), "url": response_data.get('url')}
    return {"message": f"✅ تسک '{task_name}' با موفقیت به‌روزرسانی شد. ", "url": response_data.get('url')}

async def confirm_and_delete_task(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE,
    task_name: str, 
    list_name: str
) -> Optional[Dict[str, Any]]:
    user_id = str(update.effective_user.id)
    original_args = {'task_name': task_name, 'list_name': list_name}

    try:
        task, list_name_found = _find_task_in_db(task_name, list_name, user_id)
        
        details_text = "\n".join([
            "آیا از حذف تسک زیر مطمئن هستید؟\n",
            f"🏷️ *عنوان:* {task.get('title', 'خالی')}",
            f"🗂️ *لیست:* {list_name_found}",
            f"👤 *مسئول:* {task.get('assignee_name', 'خالی') or 'خالی'}",
            f"📊 *وضعیت:* {task.get('status', 'خالی') or 'خالی'}",
        ])
        
        keyboard = [
            [InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"confirm_delete_ai_{task['clickup_task_id']}")],
            [InlineKeyboardButton("❌ خیر، لغو", callback_data="cancel_delete_ai")]
        ]
        
        target_message = update.effective_message
        if target_message:
            await target_message.reply_text(details_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        else:
            logger.error("Cannot find message to reply to in confirm_and_delete_task")
            return {"message": "خطا: پیام اصلی برای پاسخ یافت نشد."}
            
        return None # Signal interactive step
        
    except ValueError as e:
        return await _handle_find_task_error(e, update, context, 'confirm_and_delete_task', original_args)
    except Exception as e:
        logger.error(f"خطای پیش‌بینی نشده در حذف تسک: {e}", exc_info=True)
        return {"message": f"❌ یک خطای پیش‌بینی نشده رخ داد."}

async def ask_user(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE,
    question: str
) -> Optional[Dict[str, Any]]:
    """از کاربر سوالی می‌پرسد و منتظر پاسخ می‌ماند."""
    if not question: raise ValueError("متن سوال برای ابزار ask_user الزامی است.")
    context.chat_data['conversation_state'] = 'ai_is_waiting'
    context.chat_data['ai_question_asked'] = question
    
    target_message = update.effective_message
    if target_message:
        await target_message.reply_text(question)
    
    return None # Signal interactive step

# --- مپینگ ابزارها ---
TOOL_MAPPING = {
    "create_task": create_task,
    "update_task": update_task,
    "confirm_and_delete_task": confirm_and_delete_task,
    "ask_user": ask_user,
}


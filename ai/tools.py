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
from handlers import standard_handlers


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
            else: return None
    return int(parsed_date.timestamp() * 1000)

def _find_task_in_db(task_name: str, list_name: str) -> Optional[Tuple[Dict[str, Any], str]]:
    """یک تسک را با جستجوی فازی پیدا کرده و خود تسک به همراه نام لیست را برمی‌گرداند."""
    lists = database.get_documents(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID)
    if not lists: raise ValueError("هیچ لیستی در دیتابیس یافت نشد.")
        
    list_choices = {lst['name']: lst['clickup_list_id'] for lst in lists}
    
    best_list_match, list_score = fuzz_process.extractOne(list_name, list_choices.keys())
    if list_score < 85:
        all_list_names = ", ".join(list(list_choices.keys())[:10]) # Limit to 10 for readability
        raise ValueError(f"لیست '{list_name}' یافت نشد. لیست‌های موجود: {all_list_names}...")

    list_id = list_choices[best_list_match]
    tasks_in_list = database.get_documents(config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, [Query.equal("list_id", [list_id])])
    
    if not tasks_in_list: raise ValueError(f"هیچ تسکی در لیست '{best_list_match}' یافت نشد.")

    task_titles = {task['title']: task for task in tasks_in_list}
    best_match, score = fuzz_process.extractOne(task_name, task_titles.keys())

    if score > 85:
        return task_titles[best_match], best_list_match
    else:
        raise ValueError(f"تسک با نام نزدیک به '{task_name}' در لیست '{best_list_match}' یافت نشد. آیا منظورتان '{best_match}' بود؟")

# --- ابزارهای اصلی ---

async def _create_task_tool(
    update: Update, context: ContextTypes.DEFAULT_TYPE, task_name: str, list_name: str,
    description: Optional[str] = None, priority: Optional[str] = None, assignee_name: Optional[str] = None,
    status: Optional[str] = None, start_date: Optional[str] = None, due_date: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """ابزار هوشمند ساخت تسک با قابلیت اصلاح تعاملی."""
    
    user_id = str(update.effective_user.id)
    token = await standard_handlers._get_user_token(user_id, update, context)
    if not token: return {"message": "خطا: توکن کاربر یافت نشد."}

    original_args = {k: v for k, v in locals().items() if k not in ['update', 'context', 'user_id', 'token'] and v is not None}
    
    if not task_name or not list_name: raise ValueError("نام تسک و نام لیست الزامی است.")

    lists = database.get_documents(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID)
    list_choices = {lst['name']: lst['clickup_list_id'] for lst in lists}
    best_list_match, list_score = fuzz_process.extractOne(list_name, list_choices.keys())
    
    if list_score < 85:
        context.chat_data['conversation_state'] = 'awaiting_list_correction'
        context.chat_data['pending_task_payload'] = original_args
        keyboard = [[InlineKeyboardButton(name, callback_data=f"correct_list_name_{name}")] for name in list_choices.keys()]
        await standard_handlers._send_or_edit(update, f"⚠️ لیست «{list_name}» یافت نشد. لطفاً لیست صحیح را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
        return None
    
    list_id = list_choices[best_list_match]
    payload = {"name": task_name}
    if description: payload["description"] = description
    
    if assignee_name:
        users = database.get_documents(config.APPWRITE_DATABASE_ID, config.CLICKUP_USERS_COLLECTION_ID)
        user_choices = {user['username']: user['clickup_user_id'] for user in users}
        best_user_match, user_score = fuzz_process.extractOne(assignee_name, user_choices.keys())
        if user_score < 85:
            context.chat_data['pending_task_payload'] = original_args
            keyboard = [[InlineKeyboardButton(name, callback_data=f"correct_assignee_name_{name}")] for name in user_choices.keys()]
            await standard_handlers._send_or_edit(update, f"⚠️ کاربر «{assignee_name}» یافت نشد. لطفاً کاربر صحیح را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
            return None
        payload["assignees"] = [int(user_choices[best_user_match])]

    if start_date and (ts := parse_date(start_date)): payload["start_date"] = ts
    if due_date and (ts := parse_date(due_date)): payload["due_date"] = ts
    
    priority_map = {"فوری": 1, "بالا": 2, "متوسط": 3, "پایین": 4}
    if priority and (p_match := fuzz_process.extractOne(priority, priority_map.keys())[0]):
        payload['priority'] = priority_map[p_match]

    if status:
        statuses_call = partial(clickup_api.get_list_statuses, list_id, token=token)
        list_statuses = await asyncio.to_thread(statuses_call)
        valid_status_names = [s['status'] for s in list_statuses]
        if s_match := fuzz_process.extractOne(status, valid_status_names)[0]:
            payload['status'] = s_match

    create_call = partial(clickup_api.create_task_in_clickup_api, list_id, payload, token=token)
    success, task_data = await asyncio.to_thread(create_call)
    
    if not success: raise Exception(f"ClickUp API error: {task_data.get('err', 'Unknown error')}")
    
    if task_id := task_data.get('id'):
        sync_call = partial(clickup_api.sync_single_task_from_clickup, task_id, token=token)
        synced_task = await asyncio.to_thread(sync_call)
        if synced_task:
            details = [f"✅ تسک با موفقیت در لیست *{best_list_match}* ساخته شد!", f"🏷️ *عنوان:* {synced_task.get('title', 'خالی')}"]
            return {"message": "\n".join(details), "url": task_data.get('url')}
    
    return {"message": f"✅ تسک '{task_name}' با موفقیت ساخته شد.", "url": task_data.get('url')}


async def _update_task_tool(
    update: Update, context: ContextTypes.DEFAULT_TYPE, task_name: str, list_name: str,
    new_name: Optional[str] = None, new_description: Optional[str] = None, new_status: Optional[str] = None,
    new_priority: Optional[str] = None, new_assignee_name: Optional[str] = None, new_due_date: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """ابزار هوشمند به‌روزرسانی تسک با قابلیت اصلاح تعاملی."""
    
    user_id = str(update.effective_user.id)
    token = await standard_handlers._get_user_token(user_id, update, context)
    if not token: return {"message": "خطا: توکن کاربر یافت نشد."}
    
    original_args = {k: v for k, v in locals().items() if k not in ['update', 'context', 'user_id', 'token'] and v is not None}
    
    try:
        task, list_name_found = _find_task_in_db(task_name, list_name)
    except ValueError as e:
        error_msg = str(e)
        if "آیا منظورتان" in error_msg:
            context.chat_data['pending_update_payload'] = original_args
            suggested_task = re.search(r"'(.+)' بود؟", error_msg).group(1) if re.search(r"'(.+)' بود؟", error_msg) else ""
            keyboard = [[InlineKeyboardButton(suggested_task, callback_data=f"correct_update_task_{suggested_task}")]] if suggested_task else []
            await standard_handlers._send_or_edit(update, error_msg, InlineKeyboardMarkup(keyboard))
            return None
        else:
            return {"message": f"❌ {e}"}

    payload = {}
    if new_name: payload['name'] = new_name
    if new_description: payload['description'] = new_description
    if new_due_date and (ts := parse_date(new_due_date)): payload["due_date"] = ts

    if new_assignee_name:
        users = database.get_documents(config.APPWRITE_DATABASE_ID, config.CLICKUP_USERS_COLLECTION_ID)
        user_choices = {user['username']: user['clickup_user_id'] for user in users}
        if best_match := fuzz_process.extractOne(new_assignee_name, user_choices.keys())[0]:
            payload['assignees'] = [int(user_choices[best_match])]

    if new_status:
        statuses_call = partial(clickup_api.get_list_statuses, task['list_id'], token=token)
        list_statuses = await asyncio.to_thread(statuses_call)
        valid_status_names = [s['status'] for s in list_statuses]
        if best_match := fuzz_process.extractOne(new_status, valid_status_names)[0]:
            payload['status'] = best_match
    
    priority_map = {"فوری": 1, "بالا": 2, "متوسط": 3, "پایین": 4}
    if new_priority:
        if best_match := fuzz_process.extractOne(new_priority, priority_map.keys())[0]:
            payload['priority'] = priority_map[best_match]

    if not payload: return {"message": "هیچ تغییری برای اعمال مشخص نشده است."}
    
    update_call = partial(clickup_api.update_task_in_clickup_api, task['clickup_task_id'], payload, token=token)
    success, response_data = await asyncio.to_thread(update_call)
    
    if not success: raise Exception(f"ClickUp API error: {response_data.get('err', 'Unknown error')}")
        
    sync_call = partial(clickup_api.sync_single_task_from_clickup, task['clickup_task_id'], token=token)
    await asyncio.to_thread(sync_call)
    return {"message": f"✅ تسک '{task['title']}' با موفقیت به‌روزرسانی شد."}


async def _confirm_and_delete_task_tool(
    update: Update, context: ContextTypes.DEFAULT_TYPE, task_name: str, list_name: str
) -> None:
    """تسک را پیدا کرده و برای حذف از کاربر تاییدیه می‌گیرد."""
    try:
        task, _ = _find_task_in_db(task_name, list_name)
        
        details = f"آیا از حذف تسک *{task.get('title', 'N/A')}* مطمئن هستید؟\n\nبا ارسال 'بله' تایید و با 'خیر' لغو کنید."
        
        context.chat_data['conversation_state'] = 'awaiting_delete_confirmation'
        context.chat_data['pending_deletion'] = {'task_id': task['clickup_task_id'], 'task_name': task['title']}
        await standard_handlers._send_or_edit(update, details)
    except ValueError as e:
        await standard_handlers._send_or_edit(update, f"❌ {e}")
    except Exception as e:
        logger.error(f"خطای پیش‌بینی نشده در حذف تسک: {e}", exc_info=True)
        await standard_handlers._send_or_edit(update, "❌ یک خطای پیش‌بینی نشده رخ داد.")

async def ask_user_tool(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str) -> None:
    if not question: raise ValueError("متن سوال برای ابزار ask_user الزامی است.")
    await standard_handlers._send_or_edit(update, question)

# --- مپینگ ابزارها ---
TOOL_MAPPING = {
    "create_task": _create_task_tool,
    "update_task": _update_task_tool,
    "confirm_and_delete_task": _confirm_and_delete_task_tool,
    "ask_user": ask_user_tool,
}


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
        # First, try a strict format that users are asked to follow.
        parsed_date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        # If strict format fails, use a more flexible parser.
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
                    days = int(''.join(filter(str.isdigit, date_str)))
                    parsed_date = today + timedelta(days=days)
                except ValueError: return None
            else:
                return None
    return int(parsed_date.timestamp() * 1000)

def _find_task_in_db(task_name: str, list_name: str) -> Optional[Tuple[Dict[str, Any], str]]:
    """یک تسک را با جستجوی فازی پیدا کرده و خود تسک به همراه نام لیست را برمی‌گرداند."""
    lists = database.get_documents(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID)
    if not lists:
        raise ValueError("هیچ لیستی در دیتابیس یافت نشد.")
        
    list_choices = {lst['name']: lst['clickup_list_id'] for lst in lists}
    
    best_list_match, list_score = fuzz_process.extractOne(list_name, list_choices.keys())
    if list_score < 90:
        all_list_names = ", ".join(list_choices.keys())
        raise ValueError(f"لیست '{list_name}' یافت نشد. لیست‌های موجود: {all_list_names}")

    list_id = list_choices[best_list_match]
    tasks_in_list = database.get_documents(config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, [database.Query.equal("list_id", [list_id])])
    
    if not tasks_in_list:
        raise ValueError(f"هیچ تسکی در لیست '{best_list_match}' یافت نشد.")

    task_titles = {task['title']: task for task in tasks_in_list}
    best_match, score = fuzz_process.extractOne(task_name, task_titles.keys())

    if score > 90:
        return task_titles[best_match], best_list_match
    else:
        raise ValueError(f"تسک با نام نزدیک به '{task_name}' در لیست '{best_list_match}' یافت نشد. آیا منظورتان '{best_match}' بود؟")


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
    """ابزار هوشمند ساخت تسک با قابلیت اصلاح تعاملی."""
    
    user_id = str(update.effective_user.id)
    token = await standard_handlers._get_user_token(user_id, update, context)
    if not token: return {"message": "خطا: توکن کاربر یافت نشد."}
    
    original_args = {k: v for k, v in locals().items() if k not in ['update', 'context', 'user_id', 'token'] and v is not None}
    
    if not task_name or not list_name:
        raise ValueError("نام تسک و نام لیست الزامی است.")

    # --- اعتبارسنجی نام لیست ---
    lists = database.get_documents(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID)
    list_choices = {lst['name']: lst['clickup_list_id'] for lst in lists}
    best_list_match, list_score = fuzz_process.extractOne(list_name, list_choices.keys())
    
    if list_score < 85:
        logger.info(f"لیست نامعتبر '{list_name}'. درخواست انتخاب از کاربر.")
        context.chat_data['conversation_state'] = 'awaiting_list_correction'
        context.chat_data['pending_task_payload'] = original_args
        keyboard = [[InlineKeyboardButton(name, callback_data=f"correct_list_name_{name}")] for name in list_choices.keys()]
        await update.message.reply_text(f"⚠️ لیست «{list_name}» یافت نشد. لطفاً لیست صحیح را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
        return None
    
    list_id = list_choices[best_list_match]
    
    payload = {"name": task_name}
    if description: payload["description"] = description
    
    # --- اعتبارسنجی نام مسئول تسک ---
    if assignee_name:
        users = database.get_documents(config.APPWRITE_DATABASE_ID, config.CLICKUP_USERS_COLLECTION_ID)
        user_choices = {user['username']: user['clickup_user_id'] for user in users}
        best_user_match, user_score = fuzz_process.extractOne(assignee_name, user_choices.keys())
        
        if user_score < 85:
            logger.info(f"کاربر نامعتبر '{assignee_name}'. درخواست انتخاب از کاربر.")
            context.chat_data['conversation_state'] = 'awaiting_assignee_correction'
            context.chat_data['pending_task_payload'] = original_args
            keyboard = [[InlineKeyboardButton(name, callback_data=f"correct_assignee_name_{name}")] for name in user_choices.keys()]
            await update.message.reply_text(f"⚠️ کاربر «{assignee_name}» یافت نشد. لطفاً کاربر صحیح را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
            return None
        payload["assignees"] = [int(user_choices[best_user_match])]

    if start_date and (start_timestamp := parse_date(start_date)): payload["start_date"] = start_timestamp
    if due_date and (due_timestamp := parse_date(due_date)): payload["due_date"] = due_timestamp
    
    # --- اعتبارسنجی اولویت ---
    priority_map = {"فوری": 1, "بالا": 2, "متوسط": 3, "پایین": 4}
    if priority:
        best_priority_match, _ = fuzz_process.extractOne(priority, priority_map.keys())
        if best_priority_match:
            payload['priority'] = priority_map[best_priority_match]
        else:
            context.chat_data['conversation_state'] = 'awaiting_priority_correction'
            context.chat_data['pending_task_payload'] = original_args
            keyboard = [[InlineKeyboardButton(p, callback_data=f"correct_priority_{p}")] for p in priority_map.keys()]
            await update.message.reply_text(f"⚠️ اولویت «{priority}» معتبر نیست. لطفاً انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
            return None

    # --- اعتبارسنجی وضعیت ---
    if status:
        statuses_call = partial(clickup_api.get_list_statuses, list_id, token=token)
        list_statuses = await asyncio.to_thread(statuses_call)
        valid_status_names = [s['status'] for s in list_statuses]
        best_status_match, status_score = fuzz_process.extractOne(status, valid_status_names)
        if status_score > 85:
            payload['status'] = best_status_match
        else:
            context.chat_data['conversation_state'] = 'awaiting_status_correction'
            context.chat_data['pending_task_payload'] = original_args
            keyboard = [[InlineKeyboardButton(s, callback_data=f"correct_status_{s}")] for s in valid_status_names]
            await update.message.reply_text(f"⚠️ وضعیت «{status}» معتبر نیست. لطفاً انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
            return None

    create_call = partial(clickup_api.create_task_in_clickup_api, list_id, payload, token=token)
    success, task_data = await asyncio.to_thread(create_call)
    
    if not success:
        raise Exception(f"ClickUp API error: {task_data.get('err', 'Unknown error')}")
    
    task_id = task_data.get('id')
    if task_id:
        sync_call = partial(clickup_api.sync_single_task_from_clickup, task_id, token=token)
        synced_task = await asyncio.to_thread(sync_call)
        if synced_task:
            def format_dt(ts): return datetime.fromtimestamp(int(ts)/1000).strftime('%Y-%m-%d') if ts else "خالی"
            details = [
                f"✅ تسک با موفقیت در لیست *{best_list_match}* ساخته شد!\n",
                f"🏷️ *عنوان:* {synced_task.get('title', 'خالی')}",
                f"📝 *توضیحات:* {synced_task.get('content', 'خالی') or 'خالی'}",
                f"👤 *مسئول:* {synced_task.get('assignee_name', 'خالی') or 'خالی'}",
                f"📊 *وضعیت:* {synced_task.get('status', 'خالی') or 'خالی'}",
                f"❗️ *اولویت:* {synced_task.get('priority', 'خالی') or 'خالی'}",
                f"🗓️ *تاریخ شروع:* {format_dt(synced_task.get('start_date'))}",
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
    """ابزار هوشمند به‌روزرسانی تسک با قابلیت اصلاح تعاملی."""
    
    user_id = str(update.effective_user.id)
    token = await standard_handlers._get_user_token(user_id, update, context)
    if not token: return {"message": "خطا: توکن کاربر یافت نشد."}
    
    original_args = {k: v for k, v in locals().items() if k not in ['update', 'context', 'user_id', 'token'] and v is not None}
    logger.info(f"در حال تلاش برای به‌روزرسانی تسک '{task_name}' در لیست '{list_name}'")
    
    try:
        task, list_name_found = _find_task_in_db(task_name, list_name)
    except ValueError as e:
        error_msg = str(e)
        if "یافت نشد. آیا منظورتان" in error_msg:
            # Handle interactive correction for task name
            context.chat_data['conversation_state'] = 'awaiting_task_correction'
            context.chat_data['pending_update_payload'] = original_args
            
            # Extract suggested task name from error message
            match = re.search(r"منظورتان '(.+)' بود؟", error_msg)
            suggested_task = match.group(1) if match else None
            
            keyboard = [[InlineKeyboardButton(suggested_task, callback_data=f"correct_task_name_{suggested_task}")]] if suggested_task else []
            keyboard.append([InlineKeyboardButton("❌ لغو", callback_data="cancel_conv")])
            
            await update.message.reply_text(error_msg.replace("آیا منظورتان", "⚠️").replace("بود؟", "؟"), reply_markup=InlineKeyboardMarkup(keyboard))
            return None
        else:
            raise

    payload = {}
    if new_name: payload['name'] = new_name
    if new_description: payload['description'] = new_description
    if new_assignee_name:
        users = database.get_documents(config.APPWRITE_DATABASE_ID, config.CLICKUP_USERS_COLLECTION_ID)
        user_choices = {user['username']: user['clickup_user_id'] for user in users}
        best_user_match, user_score = fuzz_process.extractOne(new_assignee_name, user_choices.keys())
        if user_score > 85:
            payload['assignees'] = [int(user_choices[best_user_match])]
        else:
            context.chat_data['conversation_state'] = 'awaiting_assignee_correction_update'
            context.chat_data['pending_update_payload'] = original_args
            keyboard = [[InlineKeyboardButton(name, callback_data=f"correct_assignee_update_{name}")] for name in user_choices.keys()]
            await update.message.reply_text(f"⚠️ کاربر «{new_assignee_name}» یافت نشد. لطفاً کاربر صحیح را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
            return None
    if new_status:
        statuses_call = partial(clickup_api.get_list_statuses, task['list_id'], token=token)
        list_statuses = await asyncio.to_thread(statuses_call)
        valid_status_names = [s['status'] for s in list_statuses]
        best_status_match, status_score = fuzz_process.extractOne(new_status, valid_status_names)
        if status_score > 85:
            payload['status'] = best_status_match
        else:
            context.chat_data['conversation_state'] = 'awaiting_status_correction_update'
            context.chat_data['pending_update_payload'] = original_args
            keyboard = [[InlineKeyboardButton(s, callback_data=f"correct_status_update_{s}")] for s in valid_status_names]
            await update.message.reply_text(f"⚠️ وضعیت «{new_status}» معتبر نیست. لطفاً انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
            return None
    
    priority_map = {"فوری": 1, "بالا": 2, "متوسط": 3, "پایین": 4}
    if new_priority:
        best_priority_match, _ = fuzz_process.extractOne(new_priority, priority_map.keys())
        if best_priority_match:
            payload['priority'] = priority_map[best_priority_match]
        else:
            context.chat_data['conversation_state'] = 'awaiting_priority_correction_update'
            context.chat_data['pending_update_payload'] = original_args
            keyboard = [[InlineKeyboardButton(p, callback_data=f"correct_priority_update_{p}")] for p in priority_map.keys()]
            await update.message.reply_text(f"⚠️ اولویت «{new_priority}» معتبر نیست. لطفاً انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
            return None

    if new_due_date and (due_timestamp := parse_date(new_due_date)): payload["due_date"] = due_timestamp

    if not payload:
        raise ValueError("هیچ تغییری برای اعمال مشخص نشده است.")
    
    update_call = partial(clickup_api.update_task_in_clickup_api, task['clickup_task_id'], payload, token=token)
    success, response_data = await asyncio.to_thread(update_call)
    
    if not success:
        raise Exception(f"ClickUp API error: {response_data.get('err', 'Unknown error')}")
        
    sync_call = partial(clickup_api.sync_single_task_from_clickup, task['clickup_task_id'], token=token)
    synced_task = await asyncio.to_thread(sync_call)
    
    if synced_task:
        def format_dt(ts): 
            if not ts: return "خالی"
            if isinstance(ts, int):
                return datetime.fromtimestamp(ts/1000).strftime('%Y-%m-%d')
            return ts
            
        details = [
            f"✅ تسک '{task['title']}' با موفقیت به‌روزرسانی شد. جزئیات جدید:",
            f"🏷️ *عنوان:* {synced_task.get('title', 'خالی')}",
            f"📝 *توضیحات:* {synced_task.get('content', 'خالی') or 'خالی'}",
            f"🗂️ *لیست:* {list_name_found}",
            f"👤 *مسئول:* {synced_task.get('assignee_name', 'خالی') or 'خالی'}",
            f"📊 *وضعیت:* {synced_task.get('status', 'خالی') or 'خالی'}",
            f"❗️ *اولویت:* {synced_task.get('priority', 'خالی') or 'خالی'}",
            f"🗓️ *تاریخ شروع:* {format_dt(synced_task.get('start_date'))}",
            f"🏁 *تاریخ تحویل:* {format_dt(synced_task.get('due_date'))}"
        ]
        return {"message": "\n".join(details), "url": response_data.get('url')}

    return {"message": f"✅ تسک '{task_name}' با موفقیت به‌روزرسانی شد. ", "url": response_data.get('url')}


async def confirm_and_delete_task(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE,
    task_name: str, 
    list_name: str
) -> None:
    """تسک را پیدا کرده و برای حذف از کاربر تاییدیه می‌گیرد."""
    try:
        task, list_name_found = _find_task_in_db(task_name, list_name)
        
        def format_dt(ts): return datetime.fromtimestamp(int(ts)/1000).strftime('%Y-%m-%d') if ts else "خالی"
        
        details = [
            "آیا از حذف تسک زیر مطمئن هستید؟\n",
            f"🏷️ *عنوان:* {task.get('title', 'خالی')}",
            f"📝 *توضیحات:* {task.get('content', 'خالی') or 'خالی'}",
            f"🗂️ *لیست:* {list_name_found}",
            f"👤 *مسئول:* {task.get('assignee_name', 'خالی') or 'خالی'}",
            f"📊 *وضعیت:* {task.get('status', 'خالی') or 'خالی'}",
            f"❗️ *اولویت:* {task.get('priority', 'خالی') or 'خالی'}",
            f"🗓️ *تاریخ شروع:* {format_dt(task.get('start_date'))}",
            f"🏁 *تاریخ تحویل:* {format_dt(task.get('due_date'))}"
        ]
        details_text = "\n".join(details)
        details_text += "\n\nبا ارسال 'بله' تایید و با 'خیر' لغو کنید."
        
        context.chat_data['conversation_state'] = 'awaiting_delete_confirmation'
        context.chat_data['pending_deletion'] = {'task_id': task['clickup_task_id'], 'task_name': task['title']}
        await update.message.reply_text(details_text, parse_mode='Markdown')

    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
    except Exception as e:
        logger.error(f"خطای پیش‌بینی نشده در حذف تسک: {e}", exc_info=True)
        await update.message.reply_text(f"❌ یک خطای پیش‌بینی نشده رخ داد.")


async def ask_user(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE,
    question: str
) -> None:
    """از کاربر سوالی می‌پرسد و منتظر پاسخ می‌ماند."""
    if not question: raise ValueError("متن سوال برای ابزار ask_user الزامی است.")
    context.chat_data['conversation_state'] = 'ai_is_waiting'
    context.chat_data['ai_question_asked'] = question
    await update.message.reply_text(question)

# --- مپینگ ابزارها ---
TOOL_MAPPING = {
    "create_task": create_task,
    "update_task": update_task,
    "confirm_and_delete_task": confirm_and_delete_task,
    "ask_user": ask_user,
}


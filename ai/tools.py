import asyncio
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from dateutil.parser import parse as dateutil_parse
import difflib

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import config
import database
import clickup_api

logger = logging.getLogger(__name__)

# --- توابع کمکی ---

def parse_due_date(due_date_str: str) -> Optional[int]:
    today = datetime.now()
    try:
        parsed_date = dateutil_parse(due_date_str, default=today, fuzzy=True, dayfirst=False)
        return int(parsed_date.timestamp() * 1000)
    except (ValueError, TypeError):
        due_date_str_lower = due_date_str.lower()
        if "امروز" in due_date_str_lower: return int(today.timestamp() * 1000)
        elif "فردا" in due_date_str_lower: return int((today + timedelta(days=1)).timestamp() * 1000)
        elif "روز دیگه" in due_date_str_lower or "روز دیگر" in due_date_str_lower:
            try:
                days = int(''.join(filter(str.isdigit, due_date_str)))
                return int((today + timedelta(days=days)).timestamp() * 1000)
            except ValueError: return None
        return None

def _find_task(task_name: str, list_name: str) -> Optional[Dict[str, Any]]:
    lists = database.get_documents(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, [database.Query.equal("name", [list_name])])
    if not lists:
        all_list_names = [l.get('name', 'بدون نام') for l in database.get_documents(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID)]
        raise ValueError(f"لیست '{list_name}' یافت نشد. لیست‌های موجود: {', '.join(all_list_names)}")
    list_id = lists[0]['clickup_list_id']
    tasks = database.get_documents(config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, [database.Query.equal("list_id", [list_id]), database.Query.equal("title", [task_name])])
    if not tasks:
        raise ValueError(f"تسک '{task_name}' در لیست '{list_name}' یافت نشد.")
    return tasks[0]

# --- ابزارهای اصلی (بازطراحی شده) ---

async def _create_task_tool(
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
    """
    ابزار هوشمند ساخت تسک با قابلیت اصلاح تعاملی خطاها.
    """
    original_args = {
        'task_name': task_name, 'list_name': list_name, 'description': description,
        'priority': priority, 'assignee_name': assignee_name, 'status': status, 
        'start_date': start_date, 'due_date': due_date
    }
    original_args = {k: v for k, v in original_args.items() if v is not None}

    if not task_name or not list_name:
        raise ValueError("نام تسک و نام لیست الزامی است.")

    # بررسی اعتبار نام لیست
    lists = database.get_documents(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, [database.Query.equal("name", [list_name])])
    if not lists:
        logger.info(f"لیست نامعتبر '{list_name}'. درخواست انتخاب از کاربر.")
        context.chat_data['conversation_state'] = 'awaiting_list_correction'
        context.chat_data['pending_task_payload'] = original_args
        
        all_lists = database.get_documents(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID)
        keyboard = [[InlineKeyboardButton(l['name'], callback_data=f"correct_list_name_{l['name']}")] for l in all_lists]
        
        message_text = (f"⚠️ لیست «{list_name}» یافت نشد.\n"
                        "لطفاً لیست صحیح را از موارد زیر انتخاب کنید:")
        await update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return None

    list_id = lists[0]['clickup_list_id']

    payload = {"name": task_name}
    if description: payload["description"] = description
    
    # بررسی اعتبار نام کاربر
    if assignee_name:
        users = database.get_documents(config.APPWRITE_DATABASE_ID, config.USERS_COLLECTION_ID)
        username_map = {user.get('username', '').lower(): user for user in users}
        all_usernames = list(username_map.keys())
        
        best_match = difflib.get_close_matches(assignee_name.lower(), all_usernames, n=1, cutoff=0.6)
        
        if best_match:
            matched_user = username_map[best_match[0]]
            payload["assignees"] = [int(matched_user['clickup_user_id'])]
            logger.info(f"نام '{assignee_name}' با کاربر '{matched_user['username']}' مطابقت داده شد.")
        else:
            logger.info(f"کاربر نامعتبر '{assignee_name}'. درخواست انتخاب از کاربر.")
            context.chat_data['conversation_state'] = 'awaiting_assignee_correction'
            context.chat_data['pending_task_payload'] = original_args
            
            keyboard = [[InlineKeyboardButton(u['username'], callback_data=f"correct_assignee_name_{u['username']}")] for u in users]
            
            message_text = (f"⚠️ کاربر «{assignee_name}» یافت نشد.\n"
                            "لطفاً کاربر صحیح را از لیست زیر انتخاب کنید:")
            await update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard))
            return None
    
    if start_date:
        if start_timestamp := parse_due_date(start_date): payload["start_date"] = start_timestamp
    if due_date:
        if due_timestamp := parse_due_date(due_date): payload["due_date"] = due_timestamp
    
    # بررسی اعتبار اولویت
    priority_map = {"فوری": 1, "urgent": 1, "بالا": 2, "high": 2, "متوسط": 3, "normal": 3, "پایین": 4, "low": 4}
    if priority:
        if str(priority).lower() not in priority_map:
            logger.info(f"اولویت نامعتبر '{priority}'. درخواست انتخاب از کاربر.")
            context.chat_data['conversation_state'] = 'awaiting_priority_correction'
            context.chat_data['pending_task_payload'] = original_args
            keyboard = [
                [InlineKeyboardButton("فوری (Urgent)", callback_data="correct_priority_فوری")],
                [InlineKeyboardButton("بالا (High)", callback_data="correct_priority_بالا")],
                [InlineKeyboardButton("متوسط (Normal)", callback_data="correct_priority_متوسط")],
                [InlineKeyboardButton("پایین (Low)", callback_data="correct_priority_پایین")],
            ]
            message_text = (f"⚠️ اولویت «{priority}» معتبر نیست.\n"
                            "لطفاً یکی از اولویت‌های مجاز زیر را برای ادامه انتخاب کنید:")
            await update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard))
            return None
        else:
            payload['priority'] = priority_map[str(priority).lower()]

    # بررسی اعتبار وضعیت
    if status:
        list_statuses = clickup_api.get_list_statuses(list_id)
        valid_status_names = [s['status'].lower() for s in list_statuses]
        if status.lower() not in valid_status_names:
            logger.info(f"وضعیت نامعتبر '{status}'. درخواست انتخاب از کاربر.")
            context.chat_data['conversation_state'] = 'awaiting_status_correction'
            context.chat_data['pending_task_payload'] = original_args
            keyboard = [[InlineKeyboardButton(s['status'], callback_data=f"correct_status_{s['status']}")] for s in list_statuses]
            message_text = (f"⚠️ وضعیت «{status}» معتبر نیست.\n"
                            "لطفاً یکی از وضعیت‌های مجاز زیر را برای ادامه انتخاب کنید:")
            await update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard))
            return None
        else:
            payload['status'] = status

    success, task_data = clickup_api.create_task_in_clickup_api(list_id, payload)
    
    if not success:
        raise Exception(f"ClickUp API error: {task_data.get('err')}")
    
    task_id = task_data.get('id')
    synced_task = None
    if task_id:
        synced_task = clickup_api.sync_single_task_from_clickup(task_id)
    else:
        logger.error("Could not find task ID in the response from ClickUp API after creation.")

    if synced_task:
        def format_date(timestamp_ms):
            if not timestamp_ms: return "خالی"
            try: return datetime.fromtimestamp(int(timestamp_ms) / 1000).strftime('%Y-%m-%d')
            except (ValueError, TypeError): return "نامشخص"

        list_name_str = lists[0]['name']

        details = [
            f"✅ تسک با موفقیت ساخته شد!\n",
            f"🏷️ *عنوان:* {synced_task.get('title', 'خالی')}",
            f"📝 *توضیحات:* {synced_task.get('content', 'خالی') or 'خالی'}",
            f"🗂️ *لیست:* {list_name_str}",
            f"👤 *مسئول:* {synced_task.get('assignee_name', 'خالی')}",
            f"📊 *وضعیت:* {synced_task.get('status', 'خالی')}",
            f"❗️ *اولویت:* {synced_task.get('priority', 'خالی')}",
            f"🗓️ *تاریخ شروع:* {format_date(synced_task.get('start_date'))}",
            f"🏁 *تاریخ تحویل:* {format_date(synced_task.get('due_date'))}"
        ]
        final_message = "\n".join(details)
        return {"message": final_message, "url": task_data.get('url')}
    else:
        # Fallback
        return {"message": f"✅ تسک '{task_name}' در لیست '{list_name}' ساخته شد، اما همگام‌سازی ناموفق بود.", "url": task_data.get('url')}


async def _confirm_and_delete_task_tool(task_name: str, list_name: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task = _find_task(task_name, list_name)
    start_date_str = "تعیین نشده"
    if task.get('start_date'):
        try:
            dt_object = datetime.fromtimestamp(int(task['start_date']) / 1000)
            start_date_str = dt_object.strftime('%Y-%m-%d')
        except (ValueError, TypeError): start_date_str = "نامشخص"
    due_date_str = "تعیین نشده"
    if task.get('due_date'):
        try:
            dt_object = datetime.fromtimestamp(int(task['due_date']) / 1000)
            due_date_str = dt_object.strftime('%Y-%m-%d')
        except (ValueError, TypeError): due_date_str = "نامشخص"
    details_text = (f"آیا از حذف تسک زیر مطمئن هستید؟\n\n"
                    f"🔹 *عنوان:* {task.get('title', 'N/A')}\n"
                    f"🔸 *وضعیت:* {task.get('status', 'N/A')}\n"
                    f"🔹 *مسئول:* {task.get('assignee_name', 'ندارد')}\n"
                    f"🔸 *تاریخ شروع:* {start_date_str}\n"
                    f"🔹 *تاریخ تحویل:* {due_date_str}\n\n"
                    "لطفاً با ارسال 'بله' یا 'خیر' پاسخ دهید.")
    context.chat_data['conversation_state'] = 'awaiting_delete_confirmation'
    context.chat_data['pending_deletion'] = {'task_id': task['clickup_task_id'], 'task_name': task['title']}
    await update.message.reply_text(details_text, parse_mode='Markdown')

async def ask_user_tool(question: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not question: raise ValueError("سوال برای ابزار ask_user اجباری است.")
    context.chat_data['conversation_state'] = 'ai_is_waiting'
    context.chat_data['ai_question_asked'] = question
    await update.message.reply_text(question)

# --- مپینگ ابزارها ---
TOOL_MAPPING = {
    "create_task": _create_task_tool,
    "update_task": None, 
    "confirm_and_delete_task": _confirm_and_delete_task_tool,
    "ask_user": ask_user_tool,
}


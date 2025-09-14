# -*- coding: utf-8 -*-
import asyncio
import logging
from datetime import datetime
from functools import partial
from dateutil.parser import parse as dateutil_parse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.ext import ContextTypes
from appwrite.query import Query

import config
import database
import clickup_api
from . import common

logger = logging.getLogger(__name__)

async def browse_projects_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نقطه ورود برای مرور پروژه ها از طریق منوی اصلی."""
    user_id = str(update.effective_user.id)
    if not await common.get_user_token(user_id, update, context): return
    
    keyboard = [[InlineKeyboardButton("نمایش فضاها (Spaces)", callback_data="browse_spaces")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("برای شروع مرور، روی دکمه زیر کلیک کنید:", reply_markup=reply_markup)

async def render_task_view(query_or_update: Update | CallbackQuery, task_id: str):
    """جزئیات یک تسک مشخص را نمایش یا ویرایش می‌کند."""
    user_id = str(query_or_update.from_user.id)
    task = await asyncio.to_thread(
        database.get_single_document, 
        config.APPWRITE_DATABASE_ID, 
        config.TASKS_COLLECTION_ID, 
        'clickup_task_id', 
        task_id
    )
    
    if not task or task.get('telegram_id') != user_id:
        await common.send_or_edit(query_or_update, "تسک پیدا نشد یا شما به آن دسترسی ندارید.")
        return

    def format_date(iso_date_str: str | None) -> str:
        """Formats an ISO 8601 date string into a readable format."""
        if not iso_date_str: return "خالی"
        try:
            dt_obj = dateutil_parse(iso_date_str)
            return dt_obj.strftime('%Y-%m-%d')
        except (ValueError, TypeError):
            logger.warning(f"Could not parse date string in render_task_view: {iso_date_str}")
            return "نامشخص"
    
    list_doc = None
    if list_id := task.get('list_id'):
        list_doc = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, 'clickup_list_id', list_id)

    details = [
        f"🏷️ *عنوان:* {common.escape_markdown(task.get('title', 'خالی'))}",
        f"📝 *توضیحات:* {common.escape_markdown(task.get('content', 'خالی') or 'خالی')}",
        f"🗂️ *لیست:* {common.escape_markdown(list_doc['name'] if list_doc else 'نامشخص')}",
        f"👤 *مسئول:* {common.escape_markdown(task.get('assignee_name', 'خالی') or 'خالی')}",
        f"📊 *وضعیت:* {common.escape_markdown(task.get('status', 'خالی') or 'خالی')}",
        f"❗️ *اولویت:* {common.escape_markdown(task.get('priority', 'خالی') or 'خالی')}",
        f"🗓️ *تاریخ شروع:* {format_date(task.get('start_date'))}",
        f"🏁 *تاریخ تحویل:* {format_date(task.get('due_date'))}"
    ]
    text = "\n".join(details)
    
    keyboard = [
        [InlineKeyboardButton("✏️ ویرایش", callback_data=f"edit_task_{task_id}"), InlineKeyboardButton("🗑️ حذف", callback_data=f"delete_task_{task_id}")]
    ]
    if task.get('list_id'):
        keyboard.append([InlineKeyboardButton("↩️ بازگشت به تسک‌ها", callback_data=f"view_list_{task['list_id']}")])
    
    await common.send_or_edit(query_or_update, text, InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تمام تعاملات دکمه‌های اینلاین را مدیریت می‌کند."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    token = await common.get_user_token(user_id, update, context)
    if not token: return
    
    data = query.data
    parts = data.split('_')
    action = parts[0]

    keyboard, text, back_button = [], "لطفاً انتخاب کنید:", None
    user_query = [Query.equal("telegram_id", [user_id])]

    if action == "browse" and parts[1] == "spaces":
        docs = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.SPACES_COLLECTION_ID, user_query)
        text, keyboard = "لیست فضاها:", [[InlineKeyboardButton(s['name'], callback_data=f"view_space_{s['clickup_space_id']}")] for s in docs]
    
    elif action == "view":
        entity, entity_id = parts[1], '_'.join(parts[2:])
        if entity == "space":
            text = "لیست پوشه‌ها:"
            space_query = user_query + [Query.equal("space_id", [entity_id])]
            docs = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.FOLDERS_COLLECTION_ID, space_query)
            keyboard = [[InlineKeyboardButton(f['name'], callback_data=f"view_folder_{f['clickup_folder_id']}")] for f in docs]
            back_button = InlineKeyboardButton("↩️ بازگشت به فضاها", callback_data="browse_spaces")
        elif entity == "folder":
            text = "لیست لیست‌ها:"
            folder = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.FOLDERS_COLLECTION_ID, 'clickup_folder_id', entity_id)
            folder_query = user_query + [Query.equal("folder_id", [entity_id])]
            docs = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, folder_query)
            keyboard = [[InlineKeyboardButton(l['name'], callback_data=f"view_list_{l['clickup_list_id']}")] for l in docs]
            if folder and folder.get('space_id'): back_button = InlineKeyboardButton("↩️ بازگشت به پوشه‌ها", callback_data=f"view_space_{folder['space_id']}")
        elif entity == "list":
            text = "لیست تسک‌ها:"
            lst = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, 'clickup_list_id', entity_id)
            list_query = user_query + [Query.equal("list_id", [entity_id])]
            tasks = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, list_query)
            keyboard = [[InlineKeyboardButton(t['title'], callback_data=f"view_task_{t['clickup_task_id']}")] for t in tasks]
            keyboard.append([InlineKeyboardButton("➕ ساخت تسک جدید", callback_data=f"newtask_in_list_{entity_id}")])
            keyboard.append([InlineKeyboardButton("🔄 رفرش", callback_data=f"refresh_list_{entity_id}")]) 
            if lst and lst.get('folder_id'): back_button = InlineKeyboardButton("↩️ بازگشت به لیست‌ها", callback_data=f"view_folder_{lst['folder_id']}")
        elif entity == "task": await render_task_view(query, entity_id); return

    elif action == "refresh" and parts[1] == "list":
        list_id = '_'.join(parts[2:])
        await query.edit_message_text("در حال همگام‌سازی تسک‌ها از ClickUp... 🔄")
        try:
            sync_call = partial(clickup_api.sync_tasks_for_list, list_id, token=token, telegram_id=user_id)
            synced_count = await asyncio.to_thread(sync_call)
            text = f"همگام‌سازی کامل شد. {synced_count} تسک پردازش شد.\n\nلیست تسک‌ها:"
            list_query = user_query + [Query.equal("list_id", [list_id])]
            tasks = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, list_query)
            keyboard = [[InlineKeyboardButton(t['title'], callback_data=f"view_task_{t['clickup_task_id']}")] for t in tasks]
            keyboard.append([InlineKeyboardButton("➕ ساخت تسک جدید", callback_data=f"newtask_in_list_{list_id}")])
            keyboard.append([InlineKeyboardButton("🔄 رفرش", callback_data=f"refresh_list_{list_id}")])
            lst = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, 'clickup_list_id', list_id)
            if lst and lst.get('folder_id'): back_button = InlineKeyboardButton("↩️ بازگشت به لیست‌ها", callback_data=f"view_folder_{lst['folder_id']}")
        except Exception as e:
            logger.error(f"خطا در هنگام رفرش لیست {list_id}: {e}", exc_info=True)
            text, back_button = "❌ خطایی در هنگام همگام‌سازی رخ داد.", InlineKeyboardButton("↩️ بازگشت", callback_data=f"view_list_{list_id}")

    elif action == "delete" and parts[1] == "task":
        task_id = '_'.join(parts[2:])
        text, keyboard = "آیا از حذف این تسک مطمئن هستید؟", [[InlineKeyboardButton("✅ بله", callback_data=f"confirm_delete_{task_id}")], [InlineKeyboardButton("❌ خیر", callback_data=f"view_task_{task_id}")]]
    
    elif action == "confirm" and parts[1] == "delete":
        task_id = '_'.join(parts[2:])
        await query.edit_message_text("در حال حذف تسک...")
        task = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id)
        
        if not task or task.get('telegram_id') != user_id:
            await query.edit_message_text("خطا: تسک برای حذف یافت نشد یا شما دسترسی ندارید.")
            return

        delete_call = partial(clickup_api.delete_task_in_clickup, task_id, token=token)
        if await asyncio.to_thread(delete_call):
            db_delete_call = partial(database.delete_document_by_clickup_id, config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id)
            await asyncio.to_thread(db_delete_call)
            text = "✅ تسک با موفقیت از ClickUp و دیتابیس محلی حذف شد."
            if task and task.get('list_id'): back_button = InlineKeyboardButton("↩️ بازگشت به لیست تسک‌ها", callback_data=f"view_list_{task['list_id']}")
        else:
            text, back_button = "❌ حذف تسک از ClickUp ناموفق بود.", InlineKeyboardButton("↩️ بازگشت به تسک", callback_data=f"view_task_{task_id}")
    
    if not keyboard and not text == "لطفاً انتخاب کنید:": text = "موردی برای نمایش پیدا نشد."
    if back_button: keyboard.append([back_button])
    await common.send_or_edit(query, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

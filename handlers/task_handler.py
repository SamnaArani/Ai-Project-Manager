# -*- coding: utf-8 -*-
import asyncio
import logging
from functools import partial

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler, 
    MessageHandler, 
    filters
)
from appwrite.query import Query

import config
import database
import clickup_api
from . import common
from . import browse_handler # For rendering task view after creation/edit

logger = logging.getLogger(__name__)

# --- وضعیت‌های مکالمه ---
(CREATE_SELECTING_LIST, CREATE_TYPING_TITLE, CREATE_TYPING_DESCRIPTION,
 CREATE_SELECTING_STATUS, CREATE_SELECTING_PRIORITY, CREATE_TYPING_START_DATE, 
 CREATE_TYPING_DUE_DATE, CREATE_SELECTING_ASSIGNEE) = range(8)
(EDIT_SELECTING_FIELD, EDIT_TYPING_VALUE, EDIT_SELECTING_VALUE) = range(8, 11)


# --- مکالمه ساخت تسک دستی ---
async def new_task_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ورودی برای فرآیند ساخت تسک جدید از طریق دکمه منو."""
    user_id = str(update.effective_user.id)
    if not await common.get_user_token(user_id, update, context): return ConversationHandler.END

    user_query = [Query.equal("telegram_id", [user_id])]
    lists = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, user_query)
    
    if not lists:
        await common.send_or_edit(update, "هیچ لیستی برای ساخت تسک یافت نشد. لطفاً از همگام‌سازی اطلاعات خود مطمئن شوید.")
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(lst['name'], callback_data=f"select_list_{lst['clickup_list_id']}")] for lst in lists]
    keyboard.append([InlineKeyboardButton("لغو ❌", callback_data="cancel_conv")])
    await common.send_or_edit(update, "لطفاً لیستی که تسک باید در آن ساخته شود را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_LIST

async def new_task_in_list_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ورودی برای ساخت تسک از داخل نمای یک لیست."""
    query = update.callback_query; await query.answer()
    context.user_data['list_id'] = query.data.split('_')[-1]
    return await ask_for_title(update, context)

async def ask_for_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """از کاربر عنوان تسک را می‌پرسد."""
    list_id = context.user_data.get('list_id')
    if not list_id: return ConversationHandler.END
    lst = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, 'clickup_list_id', list_id)
    list_name = lst['name'] if lst else "انتخاب شده"
    keyboard = [[InlineKeyboardButton("↪️ بازگشت به انتخاب لیست", callback_data="back_to_list_selection")]]
    await common.send_or_edit(update, f"ساخت تسک در لیست *{list_name}*.\nلطفاً عنوان را وارد کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_TYPING_TITLE

async def title_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """عنوان تسک را دریافت و برای توضیحات سوال می‌کند."""
    context.user_data['title'] = update.message.text
    return await ask_for_description(update, context)

async def ask_for_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """از کاربر توضیحات تسک را می‌پرسد."""
    keyboard = [[InlineKeyboardButton("↪️ بازگشت به عنوان", callback_data="back_to_title"), InlineKeyboardButton("عبور ➡️", callback_data="skip_description")]]
    await common.send_or_edit(update, "عنوان ذخیره شد. حالا توضیحات را وارد کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_TYPING_DESCRIPTION

async def description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """توضیحات را دریافت و برای وضعیت سوال می‌کند."""
    context.user_data['description'] = update.message.text
    return await ask_for_status(update, context)

async def skip_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """از مرحله توضیحات عبور می‌کند."""
    await update.callback_query.answer()
    context.user_data['description'] = ""
    return await ask_for_status(update, context)

async def ask_for_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """وضعیت‌های موجود لیست را نمایش داده و از کاربر انتخاب می‌خواهد."""
    user_id = str(update.effective_user.id)
    token = await common.get_user_token(user_id, update, context)
    if not token: return ConversationHandler.END

    list_id = context.user_data['list_id']
    statuses_call = partial(clickup_api.get_list_statuses, list_id, token=token)
    statuses = await asyncio.to_thread(statuses_call)
    keyboard = [[InlineKeyboardButton(status['status'], callback_data=f"select_status_{status['status']}")] for status in statuses]
    keyboard.append([InlineKeyboardButton("↪️ بازگشت به توضیحات", callback_data="back_to_description"), InlineKeyboardButton("عبور ➡️", callback_data="select_status_skip")])
    await common.send_or_edit(update, "وضعیت تسک را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_STATUS

async def status_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """وضعیت انتخابی را ذخیره و برای اولویت سوال می‌کند."""
    await update.callback_query.answer()
    status = update.callback_query.data.split('_')[-1]
    context.user_data['status'] = status if status != 'skip' else None
    return await ask_for_priority(update, context)

async def ask_for_priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """از کاربر اولویت تسک را می‌پرسد."""
    keyboard = [
        [InlineKeyboardButton("فوری", callback_data="priority_1"), InlineKeyboardButton("بالا", callback_data="priority_2")],
        [InlineKeyboardButton("متوسط", callback_data="priority_3"), InlineKeyboardButton("پایین", callback_data="priority_4")],
        [InlineKeyboardButton("↪️ بازگشت به وضعیت", callback_data="back_to_status"), InlineKeyboardButton("عبور ➡️", callback_data="priority_skip")]
    ]
    await common.send_or_edit(update, "اولویت تسک را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_PRIORITY

async def priority_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """اولویت انتخابی را ذخیره و برای تاریخ شروع سوال می‌کند."""
    await update.callback_query.answer()
    priority = update.callback_query.data.split('_')[1]
    context.user_data['priority'] = int(priority) if priority != 'skip' else None
    return await ask_for_assignee(update, context) # Simplified flow

async def ask_for_assignee(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """از کاربر مسئول تسک را می‌پرسد."""
    user_id = str(update.effective_user.id)
    user_query = [Query.equal("telegram_id", [user_id])]
    users = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.CLICKUP_USERS_COLLECTION_ID, user_query)

    keyboard = [[InlineKeyboardButton(user['username'], callback_data=f"select_user_{user['clickup_user_id']}")] for user in users]
    keyboard.append([InlineKeyboardButton("↪️ بازگشت به اولویت", callback_data="back_to_priority"), InlineKeyboardButton("عبور ➡️", callback_data="select_user_skip")])
    await common.send_or_edit(update, "مسئول انجام تسک را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_ASSIGNEE

async def assignee_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """مسئول تسک را ذخیره کرده و تسک نهایی را در کلیک‌اپ ایجاد می‌کند."""
    query = update.callback_query; await query.answer()
    user_id_str = str(query.from_user.id)
    token = await common.get_user_token(user_id_str, update, context)
    if not token: return ConversationHandler.END

    assignee_id = query.data.split('_')[-1]
    context.user_data['assignee_id'] = assignee_id if assignee_id != 'skip' else None
    await query.edit_message_text(text="در حال ساخت تسک...")

    user_data = context.user_data
    payload = {"name": user_data['title'], "description": user_data.get('description', '')}
    if user_data.get('assignee_id'): payload["assignees"] = [int(user_data['assignee_id'])]
    if user_data.get('status'): payload["status"] = user_data['status']
    if user_data.get('priority'): payload["priority"] = user_data['priority']
    
    create_call = partial(clickup_api.create_task_in_clickup_api, user_data['list_id'], payload, token=token)
    success, task_data = await asyncio.to_thread(create_call)

    if success and (task_id := task_data.get('id')):
        sync_call = partial(clickup_api.sync_single_task_from_clickup, task_id, token=token, telegram_id=user_id_str)
        await asyncio.to_thread(sync_call)
        await browse_handler.render_task_view(query, task_id)
    else:
        await query.edit_message_text(text=f"❌ ساخت تسک ناموفق بود. خطا: {task_data.get('err', 'نامشخص')}")

    user_data.clear()
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """هر مکالمه‌ای را لغو می‌کند."""
    await common.send_or_edit(update, "عملیات لغو شد.")
    context.user_data.clear()
    return ConversationHandler.END

def get_create_task_conv_handler() -> ConversationHandler:
    """ConversationHandler مربوط به ساخت تسک را برمی‌گرداند."""
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex('^➕ ساخت تسک جدید$'), new_task_entry), 
            CallbackQueryHandler(new_task_in_list_start, pattern='^newtask_in_list_')
        ],
        states={
            CREATE_SELECTING_LIST: [CallbackQueryHandler(new_task_in_list_start, pattern='^select_list_')],
            CREATE_TYPING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, title_received), 
                                  CallbackQueryHandler(new_task_entry, pattern='^back_to_list_selection$')],
            CREATE_TYPING_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, description_received), 
                                        CallbackQueryHandler(skip_description, pattern='^skip_description$'), 
                                        CallbackQueryHandler(ask_for_title, pattern='^back_to_title$')],
            CREATE_SELECTING_STATUS: [CallbackQueryHandler(status_selected, pattern='^select_status_'), 
                                      CallbackQueryHandler(ask_for_description, pattern='^back_to_description$')],
            CREATE_SELECTING_PRIORITY: [CallbackQueryHandler(priority_selected, pattern='^priority_'), 
                                        CallbackQueryHandler(ask_for_status, pattern='^back_to_status$')],
            CREATE_SELECTING_ASSIGNEE: [CallbackQueryHandler(assignee_selected, pattern='^select_user_'), 
                                        CallbackQueryHandler(ask_for_priority, pattern='^back_to_priority$')],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation), 
            CallbackQueryHandler(cancel_conversation, pattern='^cancel_conv$')
        ],
    )


# --- مکالمه ویرایش تسک ---
async def edit_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ورودی برای فرآیند ویرایش تسک."""
    query = update.callback_query; await query.answer()
    user_id = str(query.from_user.id)
    if not await common.get_user_token(user_id, update, context): return ConversationHandler.END

    task_id = '_'.join(query.data.split('_')[2:])
    context.user_data['edit_task_id'] = task_id
    task = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id)
    
    if not task or task.get('telegram_id') != user_id:
        await common.send_or_edit(update, "خطا: تسک مورد نظر یافت نشد یا شما به آن دسترسی ندارید.")
        return ConversationHandler.END
    context.user_data['task'] = task
    return await show_edit_menu(query, context)

async def show_edit_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE, message_text: str = "کدام بخش را می‌خواهید ویرایش کنید؟") -> int:
    """منوی دکمه‌های ویرایش تسک را نمایش می‌دهد."""
    keyboard = [
        [InlineKeyboardButton("ویرایش عنوان", callback_data="edit_field_name"), InlineKeyboardButton("ویرایش توضیحات", callback_data="edit_field_description")],
        [InlineKeyboardButton("تغییر وضعیت", callback_data="edit_field_status"), InlineKeyboardButton("تغییر اولویت", callback_data="edit_field_priority")],
        [InlineKeyboardButton("تغییر مسئول تسک", callback_data="edit_field_assignees")],
        [InlineKeyboardButton("↩️ بازگشت به تسک", callback_data="back_to_task")]
    ]
    await common.send_or_edit(update_or_query, message_text, InlineKeyboardMarkup(keyboard))
    return EDIT_SELECTING_FIELD

async def edit_field_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بر اساس فیلد انتخابی، از کاربر مقدار جدید را می‌پرسد."""
    query = update.callback_query; await query.answer()
    user_id = str(query.from_user.id)
    token = await common.get_user_token(user_id, update, context)
    if not token: return ConversationHandler.END

    field_to_edit = '_'.join(query.data.split('_')[2:])
    context.user_data['field_to_edit'] = field_to_edit
    task = context.user_data['task']
    prompt_text, keyboard, next_state = "", [], EDIT_SELECTING_VALUE
    
    text_fields = {'name': 'title', 'description': 'content'}
    if field_to_edit in text_fields:
        next_state = EDIT_TYPING_VALUE
        current_value = task.get(text_fields[field_to_edit], 'خالی') or 'خالی'
        prompt_text = f"مقدار فعلی: *{current_value}*\n\nلطفاً مقدار جدید را وارد کنید:"
    elif field_to_edit == 'status':
        status_call = partial(clickup_api.get_list_statuses, task['list_id'], token=token)
        statuses = await asyncio.to_thread(status_call)
        keyboard = [[InlineKeyboardButton(s['status'], callback_data=f"edit_value_{s['status']}")] for s in statuses]
        prompt_text = f"وضعیت فعلی: *{task.get('status', 'N/A')}*\n\nوضعیت جدید را انتخاب کنید:"
    elif field_to_edit == 'priority':
        keyboard = [[InlineKeyboardButton(p_name, callback_data=f"edit_value_{p_val}")] for p_name, p_val in [("فوری",1), ("بالا",2), ("متوسط",3), ("پایین",4), ("حذف",0)]]
        prompt_text = f"اولویت فعلی: *{task.get('priority', 'N/A')}*\n\nاولویت جدید را انتخاب کنید:"
    elif field_to_edit == 'assignees':
        user_query = [Query.equal("telegram_id", [user_id])]
        users = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.CLICKUP_USERS_COLLECTION_ID, user_query)
        keyboard = [[InlineKeyboardButton(u['username'], callback_data=f"edit_value_{u['clickup_user_id']}")] for u in users]
        prompt_text = "مسئول جدید را انتخاب کنید:"

    if keyboard: keyboard.append([InlineKeyboardButton("❌ لغو", callback_data="cancel_edit_field")])
    await common.send_or_edit(update, prompt_text, InlineKeyboardMarkup(keyboard))
    return next_state

async def process_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, new_value) -> int:
    """مقدار جدید را پردازش و تسک را در کلیک‌اپ آپدیت می‌کند."""
    user_id = str(update.effective_user.id)
    token = await common.get_user_token(user_id, update, context)
    if not token: return ConversationHandler.END

    await common.send_or_edit(update, "در حال به‌روزرسانی...")
    
    task_id, field = context.user_data['edit_task_id'], context.user_data['field_to_edit']
    payload, api_value = {}, new_value
    
    if field == 'priority': api_value = int(new_value) if new_value != "0" else None
    elif field == 'assignees': api_value = {'add': [int(new_value)], 'rem': []}
    
    payload[field] = api_value
    update_call = partial(clickup_api.update_task_in_clickup_api, task_id, payload, token=token)
    success, response_data = await asyncio.to_thread(update_call)
    
    if success:
        sync_call = partial(clickup_api.sync_single_task_from_clickup, task_id, token=token, telegram_id=user_id)
        await asyncio.to_thread(sync_call)
        await browse_handler.render_task_view(update, task_id)
        context.user_data.clear()
        return ConversationHandler.END
    else:
        await show_edit_menu(update, context, f"❌ به‌روزرسانی ناموفق بود: {response_data.get('err', 'خطای نامشخص')}")
        return EDIT_SELECTING_FIELD

async def edit_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """مقدار متنی جدید برای ویرایش را دریافت می‌کند."""
    return await process_edit(update, context, update.message.text)

async def edit_value_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """مقدار انتخابی جدید برای ویرایش را از دکمه‌ها دریافت می‌کند."""
    query = update.callback_query; await query.answer()
    return await process_edit(update, context, query.data.split('_')[-1])

async def back_to_task_from_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """از منوی ویرایش به نمای تسک بازمی‌گردد."""
    query = update.callback_query; await query.answer()
    task_id = context.user_data.get('edit_task_id')
    context.user_data.clear()
    if task_id: await browse_handler.render_task_view(query, task_id)
    return ConversationHandler.END

def get_edit_task_conv_handler() -> ConversationHandler:
    """ConversationHandler مربوط به ویرایش تسک را برمی‌گرداند."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_task_start, pattern='^edit_task_')],
        states={
            EDIT_SELECTING_FIELD: [CallbackQueryHandler(edit_field_selected, pattern='^edit_field_'), 
                                   CallbackQueryHandler(back_to_task_from_edit, pattern='^back_to_task$')],
            EDIT_TYPING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value_received)],
            EDIT_SELECTING_VALUE: [CallbackQueryHandler(edit_value_selected, pattern='^edit_value_'), 
                                     CallbackQueryHandler(lambda u, c: show_edit_menu(u, c, "عملیات ویرایش لغو شد."), pattern='^cancel_edit_field$')]
        },
        fallbacks=[CommandHandler("cancel", back_to_task_from_edit)],
    )

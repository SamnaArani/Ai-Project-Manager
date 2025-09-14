# -*- coding: utf-8 -*-
import asyncio
import logging
from functools import partial

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
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
from . import browse_handler
from ai.tools import parse_date

logger = logging.getLogger(__name__)

# --- Conversation States ---
(CREATE_SELECTING_LIST, CREATE_TYPING_TITLE, CREATE_TYPING_DESCRIPTION,
 CREATE_SELECTING_STATUS, CREATE_SELECTING_PRIORITY, CREATE_TYPING_START_DATE, 
 CREATE_TYPING_DUE_DATE, CREATE_SELECTING_ASSIGNEE, AWAITING_RESTART_CONFIRMATION) = range(9)
(EDIT_SELECTING_FIELD, EDIT_TYPING_VALUE, EDIT_SELECTING_VALUE) = range(9, 12)


# --- Helper Functions for Create Task Conversation ---
async def _start_fresh_task_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Encapsulates the logic for starting the task creation process from scratch."""
    user_id = str(update.effective_user.id)
    user_query = [Query.equal("telegram_id", [user_id])]
    lists = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, user_query)
    
    if not lists:
        await common.send_or_edit(update, "هیچ لیستی برای ساخت تسک یافت نشد. لطفاً از همگام‌سازی اطلاعات خود مطمئن شوید.")
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(lst['name'], callback_data=f"select_list_{lst['clickup_list_id']}")] for lst in lists]
    keyboard.append([InlineKeyboardButton("لغو ❌", callback_data="cancel_conv")])
    await common.send_or_edit(update, "لطفاً لیستی که تسک باید در آن ساخته شود را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_LIST

async def _resume_task_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Resumes the task creation conversation based on the last known step."""
    query = update.callback_query
    await query.message.edit_text("بسیار خب، می‌توانید به ساخت تسک خود ادامه دهید. لطفاً اطلاعات بعدی را وارد کنید:")

    if 'assignee_id' in context.user_data: return await assignee_selected(update, context)
    if 'due_date' in context.user_data: return await ask_for_assignee(update, context)
    if 'start_date' in context.user_data: return await ask_for_due_date(update, context)
    if 'priority' in context.user_data: return await ask_for_start_date(update, context)
    if 'status' in context.user_data: return await ask_for_priority(update, context)
    if 'description' in context.user_data: return await ask_for_status(update, context)
    if 'title' in context.user_data: return await ask_for_description(update, context)
    if 'list_id' in context.user_data: return await ask_for_title(update, context)
    
    logger.warning(f"Could not determine resume step for user {update.effective_user.id}. Restarting.")
    return await _start_fresh_task_creation(update, context)


# --- Create Task Conversation ---
async def new_task_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for creating a new task. Asks for confirmation if another process is active."""
    user_id = str(update.effective_user.id)
    if not await common.get_user_token(user_id, update, context): return ConversationHandler.END

    if any(key in context.user_data for key in ['list_id', 'title', 'description']):
        keyboard = [
            [InlineKeyboardButton("✅ بله، ری‌استارت کن", callback_data="restart_confirm_yes")],
            [InlineKeyboardButton("❌ خیر، ادامه می‌دهم", callback_data="restart_confirm_no")]
        ]
        await update.message.reply_text(
            "شما در حال ساخت یک تسک هستید. آیا می‌خواهید فرآیند فعلی را لغو کرده و یک تسک کاملاً جدید را شروع کنید؟",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return AWAITING_RESTART_CONFIRMATION
    
    return await _start_fresh_task_creation(update, context)

async def handle_restart_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the user's response to the restart confirmation."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "restart_confirm_yes":
        # Clear only task-related data
        for key in list(context.user_data.keys()):
            if key in ['list_id', 'title', 'description', 'status', 'priority', 'assignee_id', 'start_date', 'due_date']:
                context.user_data.pop(key)
        await query.message.edit_text("فرآیند ساخت تسک قبلی لغو شد.")
        return await _start_fresh_task_creation(update, context)
    else: # restart_confirm_no
        return await _resume_task_creation(update, context)

async def new_task_in_list_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts task creation from within a list view."""
    query = update.callback_query; await query.answer()
    # Clear any previous task creation data
    for key in list(context.user_data.keys()):
            if key in ['list_id', 'title', 'description', 'status', 'priority', 'assignee_id', 'start_date', 'due_date']:
                context.user_data.pop(key)
    context.user_data['list_id'] = query.data.split('_')[-1]
    return await ask_for_title(update, context)

async def ask_for_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Asks the user for the task title."""
    if 'list_id' not in context.user_data:
        logger.error(f"KeyError: 'list_id' not found for user {update.effective_user.id} in ask_for_title. Aborting.")
        await common.send_or_edit(update, "❌ خطایی رخ داد. لطفاً با '➕ ساخت تسک جدید' دوباره شروع کنید.")
        context.user_data.clear()
        return ConversationHandler.END
        
    list_id = context.user_data.get('list_id')
    lst = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, 'clickup_list_id', list_id)
    list_name = lst['name'] if lst else "انتخاب شده"
    keyboard = [[InlineKeyboardButton("↪️ بازگشت به انتخاب لیست", callback_data="back_to_list_selection")]]
    await common.send_or_edit(update, f"ساخت تسک در لیست *{list_name}*.\nلطفاً عنوان را وارد کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_TYPING_TITLE

async def title_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives task title and asks for description."""
    context.chat_data['conversation_handled'] = True
    context.user_data['title'] = update.message.text
    return await ask_for_description(update, context)

async def ask_for_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Asks the user for the task description."""
    keyboard = [[InlineKeyboardButton("↪️ بازگشت به عنوان", callback_data="back_to_title"), InlineKeyboardButton("عبور ➡️", callback_data="skip_description")]]
    await common.send_or_edit(update, "عنوان ذخیره شد. حالا توضیحات را وارد کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_TYPING_DESCRIPTION

async def description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives description and asks for status."""
    context.chat_data['conversation_handled'] = True
    context.user_data['description'] = update.message.text
    return await ask_for_status(update, context)

async def skip_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skips the description step."""
    await update.callback_query.answer()
    context.user_data['description'] = ""
    return await ask_for_status(update, context)

async def ask_for_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Asks the user to select a status."""
    user_id = str(update.effective_user.id)
    if 'list_id' not in context.user_data:
        logger.error(f"KeyError: 'list_id' not found for user {user_id} in ask_for_status. Aborting.")
        await common.send_or_edit(update, "❌ خطایی رخ داد. لطفاً با '➕ ساخت تسک جدید' دوباره شروع کنید.")
        context.user_data.clear()
        return ConversationHandler.END

    token = await common.get_user_token(user_id, update, context)
    if not token: return ConversationHandler.END

    list_id = context.user_data['list_id']
    statuses = await asyncio.to_thread(clickup_api.get_list_statuses, list_id, token=token)
    keyboard = [[InlineKeyboardButton(status['status'], callback_data=f"select_status_{status['status']}")] for status in statuses]
    keyboard.append([InlineKeyboardButton("↪️ بازگشت به توضیحات", callback_data="back_to_description"), InlineKeyboardButton("عبور ➡️", callback_data="select_status_skip")])
    await common.send_or_edit(update, "وضعیت تسک را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_STATUS

async def status_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Saves status and asks for priority."""
    query = update.callback_query
    await query.answer()
    status = query.data.split('_')[-1]
    context.user_data['status'] = status if status != 'skip' else None
    return await ask_for_priority(update, context)

async def ask_for_priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Asks the user to select a priority."""
    keyboard = [
        [InlineKeyboardButton("فوری", callback_data="priority_1"), InlineKeyboardButton("بالا", callback_data="priority_2")],
        [InlineKeyboardButton("متوسط", callback_data="priority_3"), InlineKeyboardButton("پایین", callback_data="priority_4")],
        [InlineKeyboardButton("↪️ بازگشت به وضعیت", callback_data="back_to_status"), InlineKeyboardButton("عبور ➡️", callback_data="priority_skip")]
    ]
    await common.send_or_edit(update, "اولویت تسک را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_PRIORITY

async def priority_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Saves priority and asks for start date."""
    query = update.callback_query
    await query.answer()
    priority = query.data.split('_')[1]
    context.user_data['priority'] = int(priority) if priority != 'skip' else None
    return await ask_for_start_date(update, context)

async def ask_for_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Asks for the task start date."""
    keyboard = [
        [InlineKeyboardButton("↪️ بازگشت به اولویت", callback_data="back_to_priority")],
        [InlineKeyboardButton("عبور ➡️", callback_data="skip_start_date")]
    ]
    prompt = "اولویت ذخیره شد. لطفاً تاریخ شروع را وارد کنید (مثلاً: 2024-12-25, فردا, 2 روز دیگه)."
    await common.send_or_edit(update, prompt, InlineKeyboardMarkup(keyboard))
    return CREATE_TYPING_START_DATE

async def skip_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    context.user_data['start_date'] = None
    return await ask_for_due_date(update, context)

async def start_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['conversation_handled'] = True
    context.user_data['start_date'] = update.message.text
    return await ask_for_due_date(update, context)

async def ask_for_due_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Asks for the task due date."""
    keyboard = [
        [InlineKeyboardButton("↪️ بازگشت به تاریخ شروع", callback_data="back_to_start_date")],
        [InlineKeyboardButton("عبور ➡️", callback_data="skip_due_date")]
    ]
    prompt = "تاریخ شروع ذخیره شد. لطفاً تاریخ تحویل را وارد کنید."
    await common.send_or_edit(update, prompt, InlineKeyboardMarkup(keyboard))
    return CREATE_TYPING_DUE_DATE

async def skip_due_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    context.user_data['due_date'] = None
    return await ask_for_assignee(update, context)

async def due_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['conversation_handled'] = True
    context.user_data['due_date'] = update.message.text
    return await ask_for_assignee(update, context)

async def ask_for_assignee(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Asks the user to select an assignee."""
    user_id = str(update.effective_user.id)
    user_query = [Query.equal("telegram_id", [user_id])]
    users = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.CLICKUP_USERS_COLLECTION_ID, user_query)

    keyboard = [[InlineKeyboardButton(user['username'], callback_data=f"select_user_{user['clickup_user_id']}")] for user in users]
    keyboard.append([InlineKeyboardButton("↪️ بازگشت به تاریخ تحویل", callback_data="back_to_due_date"), InlineKeyboardButton("عبور ➡️", callback_data="select_user_skip")])
    await common.send_or_edit(update, "مسئول انجام تسک را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_ASSIGNEE

async def assignee_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Saves assignee and creates the task in ClickUp."""
    context.chat_data['conversation_handled'] = True
    query = update.callback_query; await query.answer()
    user_id_str = str(query.from_user.id)
    token = await common.get_user_token(user_id_str, update, context)
    if not token: return ConversationHandler.END

    assignee_id = query.data.split('_')[-1]
    context.user_data['assignee_id'] = assignee_id if assignee_id != 'skip' else None
    await common.send_or_edit(update, text="در حال ساخت تسک...")

    user_data = context.user_data
    payload = {"name": user_data['title'], "description": user_data.get('description', '')}
    if user_data.get('assignee_id'): payload["assignees"] = [int(user_data['assignee_id'])]
    if user_data.get('status'): payload["status"] = user_data['status']
    if user_data.get('priority'): payload["priority"] = user_data['priority']

    if start_date_str := user_data.get('start_date'):
        if start_timestamp := parse_date(start_date_str):
            payload["start_date"] = start_timestamp
            
    if due_date_str := user_data.get('due_date'):
        if due_timestamp := parse_date(due_date_str):
            payload["due_date"] = due_timestamp
    
    success, task_data = await asyncio.to_thread(
        clickup_api.create_task_in_clickup_api, user_data['list_id'], payload, token=token
    )

    if success and (task_id := task_data.get('id')):
        await asyncio.to_thread(clickup_api.sync_single_task_from_clickup, task_id, token=token, telegram_id=user_id_str)
        await browse_handler.render_task_view(query, task_id)
    else:
        err_msg = task_data.get('err', 'نامشخص')
        await common.send_or_edit(update, text=f"❌ ساخت تسک ناموفق بود. خطا: {err_msg}")

    context.user_data.clear()
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Generic conversation cancellation."""
    return await common.generic_cancel_conversation(update, context)

def get_create_task_conv_handler() -> ConversationHandler:
    """Returns the ConversationHandler for the task creation flow."""
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex('^➕ ساخت تسک جدید$'), new_task_entry), 
            CallbackQueryHandler(new_task_in_list_start, pattern='^newtask_in_list_')
        ],
        states={
            AWAITING_RESTART_CONFIRMATION: [CallbackQueryHandler(handle_restart_confirmation, pattern='^restart_confirm_')],
            CREATE_SELECTING_LIST: [CallbackQueryHandler(new_task_in_list_start, pattern='^select_list_')],
            CREATE_TYPING_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, title_received), 
                CallbackQueryHandler(lambda u,c: _start_fresh_task_creation(u,c), pattern='^back_to_list_selection$')
            ],
            CREATE_TYPING_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, description_received), 
                CallbackQueryHandler(skip_description, pattern='^skip_description$'), 
                CallbackQueryHandler(ask_for_title, pattern='^back_to_title$')
            ],
            CREATE_SELECTING_STATUS: [
                CallbackQueryHandler(status_selected, pattern='^select_status_'), 
                CallbackQueryHandler(ask_for_description, pattern='^back_to_description$')
            ],
            CREATE_SELECTING_PRIORITY: [
                CallbackQueryHandler(priority_selected, pattern='^priority_'), 
                CallbackQueryHandler(ask_for_status, pattern='^back_to_status$')
            ],
            CREATE_TYPING_START_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, start_date_received),
                CallbackQueryHandler(skip_start_date, pattern='^skip_start_date$'),
                CallbackQueryHandler(ask_for_priority, pattern='^back_to_priority$')
            ],
            CREATE_TYPING_DUE_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, due_date_received),
                CallbackQueryHandler(skip_due_date, pattern='^skip_due_date$'),
                CallbackQueryHandler(ask_for_start_date, pattern='^back_to_start_date$')
            ],
            CREATE_SELECTING_ASSIGNEE: [
                CallbackQueryHandler(assignee_selected, pattern='^select_user_'), 
                CallbackQueryHandler(ask_for_due_date, pattern='^back_to_due_date$')
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation), 
            CallbackQueryHandler(cancel_conversation, pattern='^cancel_conv$')
        ],
        allow_reentry=True
    )


# --- Edit Task Conversation ---
async def edit_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the edit task process."""
    query = update.callback_query; await query.answer()
    user_id = str(query.from_user.id)
    if not await common.get_user_token(user_id, update, context): return ConversationHandler.END

    task_id = '_'.join(query.data.split('_')[2:])
    context.user_data['edit_task_id'] = task_id
    task = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id)
    
    if not task or task.get('telegram_id') != user_id:
        await common.send_or_edit(query, "خطا: تسک مورد نظر یافت نشد یا شما به آن دسترسی ندارید.")
        return ConversationHandler.END
    context.user_data['task'] = task
    return await show_edit_menu(update, context)

async def show_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str = "کدام بخش را می‌خواهید ویرایش کنید؟") -> int:
    """Displays the task editing menu."""
    keyboard = [
        [InlineKeyboardButton("ویرایش عنوان", callback_data="edit_field_name"), InlineKeyboardButton("ویرایش توضیحات", callback_data="edit_field_description")],
        [InlineKeyboardButton("تغییر وضعیت", callback_data="edit_field_status"), InlineKeyboardButton("تغییر اولویت", callback_data="edit_field_priority")],
        [InlineKeyboardButton("تغییر تاریخ شروع", callback_data="edit_field_start_date"), InlineKeyboardButton("تغییر تاریخ تحویل", callback_data="edit_field_due_date")],
        [InlineKeyboardButton("تغییر مسئول تسک", callback_data="edit_field_assignees")],
        [InlineKeyboardButton("↩️ بازگشت به تسک", callback_data="back_to_task")]
    ]
    await common.send_or_edit(update, message_text, InlineKeyboardMarkup(keyboard))
    return EDIT_SELECTING_FIELD

async def edit_field_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Asks the user for the new value based on the selected field."""
    query = update.callback_query; await query.answer()
    user_id = str(query.from_user.id)
    token = await common.get_user_token(user_id, update, context)
    if not token: return ConversationHandler.END

    field_to_edit = query.data.replace('edit_field_', '')
    context.user_data['field_to_edit'] = field_to_edit
    task = context.user_data['task']
    prompt_text, keyboard, next_state = "", [], EDIT_SELECTING_VALUE
    
    if field_to_edit in ['name', 'description', 'start_date', 'due_date']:
        field_map = {'name': 'title', 'description': 'content', 'start_date': 'start_date', 'due_date': 'due_date'}
        next_state = EDIT_TYPING_VALUE
        current_value = task.get(field_map[field_to_edit], 'خالی') or 'خالی'
        prompt_text = f"مقدار فعلی: *{common.escape_markdown(current_value)}*\n\nلطفاً مقدار جدید را وارد کنید:"
    elif field_to_edit == 'status':
        statuses = await asyncio.to_thread(clickup_api.get_list_statuses, task['list_id'], token=token)
        keyboard = [[InlineKeyboardButton(s['status'], callback_data=f"edit_value_{s['status']}")] for s in statuses]
        prompt_text = f"وضعیت فعلی: *{common.escape_markdown(task.get('status', 'N/A'))}*\n\nوضعیت جدید را انتخاب کنید:"
    elif field_to_edit == 'priority':
        keyboard = [[InlineKeyboardButton(p_name, callback_data=f"edit_value_{p_val}")] for p_name, p_val in [("فوری",1), ("بالا",2), ("متوسط",3), ("پایین",4), ("حذف",0)]]
        prompt_text = f"اولویت فعلی: *{common.escape_markdown(task.get('priority', 'N/A'))}*\n\nاولویت جدید را انتخاب کنید:"
    elif field_to_edit == 'assignees':
        users = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.CLICKUP_USERS_COLLECTION_ID, [Query.equal("telegram_id", [user_id])])
        keyboard = [[InlineKeyboardButton(u['username'], callback_data=f"edit_value_{u['clickup_user_id']}")] for u in users]
        prompt_text = "مسئول جدید را انتخاب کنید:"

    if keyboard: keyboard.append([InlineKeyboardButton("❌ لغو", callback_data="cancel_edit_field")])
    await common.send_or_edit(update, prompt_text, InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return next_state

async def process_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, new_value) -> int:
    """Processes the new value and updates the task in ClickUp."""
    context.chat_data['conversation_handled'] = True
    user_id = str(update.effective_user.id)
    token = await common.get_user_token(user_id, update, context)
    if not token: return ConversationHandler.END

    await common.send_or_edit(update, "در حال به‌روزرسانی...")
    
    task_id, field = context.user_data['edit_task_id'], context.user_data['field_to_edit']
    payload, api_value = {}, new_value
    
    if field == 'priority': api_value = int(new_value) if new_value != "0" else None
    elif field == 'assignees': api_value = {'add': [int(new_value)], 'rem': []}
    elif field in ['start_date', 'due_date']:
        timestamp = parse_date(new_value)
        if timestamp is None:
            await common.send_or_edit(update, "فرمت تاریخ نامعتبر است. لطفاً دوباره تلاش کنید.")
            return EDIT_TYPING_VALUE
        api_value = timestamp
    
    payload[field] = api_value
    success, response_data = await asyncio.to_thread(
        clickup_api.update_task_in_clickup_api, task_id, payload, token=token
    )
    
    if success:
        await asyncio.to_thread(clickup_api.sync_single_task_from_clickup, task_id, token=token, telegram_id=user_id)
        # Check if update is a CallbackQuery or a regular Update object to call render_task_view correctly
        target_update = update.callback_query if hasattr(update, 'callback_query') and update.callback_query else update
        await browse_handler.render_task_view(target_update, task_id)
        context.user_data.clear()
        return ConversationHandler.END
    else:
        err_msg = response_data.get('err', 'خطای نامشخص')
        await show_edit_menu(update, context, f"❌ به‌روزرسانی ناموفق بود: {err_msg}")
        return EDIT_SELECTING_FIELD

async def edit_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives the new text value for editing."""
    return await process_edit(update, context, update.message.text)

async def edit_value_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives the new selected value from buttons."""
    query = update.callback_query; await query.answer()
    return await process_edit(update, context, query.data.split('_')[-1])

async def back_to_task_from_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Returns from the edit menu to the task view."""
    query = update.callback_query; await query.answer()
    task_id = context.user_data.get('edit_task_id')
    context.user_data.clear()
    if task_id: await browse_handler.render_task_view(query, task_id)
    return ConversationHandler.END

def get_edit_task_conv_handler() -> ConversationHandler:
    """Returns the ConversationHandler for the task editing flow."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_task_start, pattern='^edit_task_')],
        states={
            EDIT_SELECTING_FIELD: [
                CallbackQueryHandler(edit_field_selected, pattern='^edit_field_'), 
                CallbackQueryHandler(back_to_task_from_edit, pattern='^back_to_task$')
            ],
            EDIT_TYPING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value_received)],
            EDIT_SELECTING_VALUE: [
                CallbackQueryHandler(edit_value_selected, pattern='^edit_value_'), 
                CallbackQueryHandler(lambda u, c: show_edit_menu(u, c, "عملیات ویرایش لغو شد."), pattern='^cancel_edit_field$')
            ]
        },
        fallbacks=[CommandHandler("cancel", back_to_task_from_edit)],
    )


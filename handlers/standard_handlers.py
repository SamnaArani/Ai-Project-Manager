# -*- coding: utf-8 -*-
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters
)
from telegram import ReplyKeyboardMarkup, KeyboardButton, CallbackQuery, Message
from telegram.error import BadRequest
import config
import database
import clickup_api
from ai import tools
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# --- تعریف وضعیت‌های مکالمه ---
(CREATE_SELECTING_LIST, CREATE_TYPING_TITLE, CREATE_TYPING_DESCRIPTION,
 CREATE_SELECTING_STATUS, CREATE_SELECTING_PRIORITY, CREATE_TYPING_START_DATE,
 CREATE_TYPING_DUE_DATE, CREATE_SELECTING_ASSIGNEE) = range(8)
(EDIT_SELECTING_FIELD, EDIT_TYPING_VALUE, EDIT_SELECTING_VALUE) = range(8, 11)
(ONBOARDING_SELECTING_PACKAGE, ONBOARDING_CONFIRM_PAYMENT, ONBOARDING_GET_CLICKUP_TOKEN) = range(11, 14)

# --- توابع کمکی ---

def parse_due_date(due_date_str: str) -> int | None:
    try:
        date_obj = datetime.strptime(due_date_str, "%Y-%m-%d")
        date_obj_utc = date_obj.replace(hour=12, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        return int(date_obj_utc.timestamp() * 1000)
    except (ValueError, TypeError):
        logger.warning(f"Could not parse date string: {due_date_str}")
        return None

async def _send_or_edit(update: Update, text: str, reply_markup: InlineKeyboardMarkup = None, parse_mode='Markdown'):
    """
    یک پیام را ویرایش می‌کند اگر از دکمه باشد، در غیر این صورت پیام جدید می‌فرستد.
    """
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning(f"Could not send or edit message: {e}")
        # In case of message not modified, we just ignore the error.

async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    منوی اصلی را برای کاربر نمایش می‌دهد.
    """
    main_menu_keyboard = [
        [KeyboardButton("🔍 مرور پروژه‌ها")],
        [KeyboardButton("➕ ساخت تسک جدید")]
    ]
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True, one_time_keyboard=False)
    context.user_data.clear()
    context.chat_data.clear()
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text("سلام! لطفاً یک گزینه را انتخاب کنید یا دستور خود را تایپ کنید:", reply_markup=reply_markup)


# --- توابع اصلی ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_doc = await asyncio.to_thread(database.get_single_document, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id)

    if not user_doc:
        # User not found, start onboarding process
        await update.message.reply_text("سلام! به دستیار هوشمند مدیریت پروژه خوش آمدید.\nبرای شروع، لطفاً یک پکیج را انتخاب کنید:")

        packages = await asyncio.to_thread(database.get_documents, config.PACKAGES_COLLECTION_ID)
        keyboard = [[InlineKeyboardButton(f"{p['package_name']} ({p['monthly_price']} تومان)", callback_data=f"select_package_{p['$id']}")] for p in packages]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text("پکیج‌ها:", reply_markup=reply_markup)
        return ONBOARDING_SELECTING_PACKAGE
    else:
        # User already exists, check if ClickUp token is set
        if not user_doc.get('clickup_token'):
            await update.message.reply_text("👋 خوش آمدید! لطفا توکن API کلیک‌آپ خود را برای ادامه ارسال کنید:")
            return ONBOARDING_GET_CLICKUP_TOKEN
        else:
            await send_main_menu(update, context)
            return ConversationHandler.END


async def browse_projects_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("نمایش فضاها (Spaces)", callback_data="browse_spaces")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("برای شروع مرور، روی دکمه زیر کلیک کنید:", reply_markup=reply_markup)

async def render_task_view(query_or_update, task_id):
    target_message = query_or_update.message if isinstance(query_or_update, CallbackQuery) else query_or_update
    user_id = str(query_or_update.effective_user.id)
    task = await asyncio.to_thread(database.get_single_document_by_user, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id, user_id)

    if task:
        def format_date(timestamp_ms):
            if not timestamp_ms: return "خالی"
            try: return datetime.fromtimestamp(int(timestamp_ms) / 1000).strftime('%Y-%m-%d')
            except (ValueError, TypeError): return "نامشخص"

        list_doc = None
        if list_id := task.get('list_id'):
            list_doc = await asyncio.to_thread(database.get_single_document_by_user, config.LISTS_COLLECTION_ID, 'clickup_list_id', list_id, user_id)

        details = [
            f"🏷️ *عنوان:* {task.get('title', 'خالی')}",
            f"📝 *توضیحات:* {task.get('content', 'خالی') or 'خالی'}",
            f"🗂️ *لیست:* {list_doc['name'] if list_doc else 'نامشخص'}",
            f"👤 *مسئول:* {task.get('assignee_name', 'خالی') or 'خالی'}",
            f"📊 *وضعیت:* {task.get('status', 'خالی') or 'خالی'}",
            f"❗️ *اولویت:* {task.get('priority', 'خالی') or 'خالی'}",
            f"🗓️ *تاریخ شروع:* {format_date(task.get('start_date'))}",
            f"🏁 *تاریخ تحویل:* {format_date(task.get('due_date'))}"
        ]
        text = "\n".join(details)

        keyboard = [
            [InlineKeyboardButton("✏️ ویرایش", callback_data=f"edit_task_{task_id}"), InlineKeyboardButton("🗑️ حذف", callback_data=f"delete_task_{task_id}")]
        ]
        if task.get('list_id'):
            keyboard.append([InlineKeyboardButton("↩️ بازگشت به تسک‌ها", callback_data=f"view_list_{task['list_id']}")])

        try:
            if isinstance(query_or_update, CallbackQuery):
                 await target_message.edit_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            else:
                 await target_message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.warning(f"Error rendering task view: {e}")
    else:
        await (query_or_update.message if isinstance(query_or_update, CallbackQuery) else query_or_update).reply_text("تسک پیدا نشد.")

# --- منطق دکمه‌های عمومی و اصلاح خطا ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    logger.info(f"Callback query received: {data}")
    parts = data.split('_')
    action = parts[0]

    prefix_map = {
        "correct_status_": "status",
        "correct_priority_": "priority",
        "correct_assignee_name_": "assignee_name",
        "correct_list_name_": "list_name"
    }
    found_prefix = next((prefix for prefix in prefix_map if data.startswith(prefix)), None)

    if action == "correct" and found_prefix:
        correction_type = prefix_map[found_prefix]
        selected_value = data[len(found_prefix):]
        payload = context.chat_data.get('pending_task_payload', {})

        context.chat_data.pop('conversation_state', None)
        context.chat_data.pop('pending_task_payload', None)

        payload[correction_type] = selected_value
        task_name = payload.get('task_name', 'بدون نام')

        field_names_fa = {"status": "وضعیت", "priority": "اولویت", "assignee_name": "مسئول", "list_name": "لیست"}
        await query.edit_message_text(f"در حال ساخت تسک '{task_name}' با {field_names_fa.get(correction_type, '')} اصلاح شده: '{selected_value}'...")

        try:
            result = await tools._create_task_tool(update=update, context=context, **payload)
            if result:
                 final_message = result.get('message', 'عملیات با موفقیت انجام شد.')
                 if url := result.get('url'): final_message += f"\n\n🔗 *لینک تسک:* {url}"
                 await query.edit_message_text(final_message, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"خطا در ساخت تسک پس از اصلاح: {e}", exc_info=True)
            await query.edit_message_text(f"❌ خطای غیرمنتظره: {e}")
        return

    keyboard = []
    text = "لطفاً انتخاب کنید:"
    back_button = None
    if action == "browse" and parts[1] == "spaces":
        keyboard = [[InlineKeyboardButton(s['name'], callback_data=f"view_space_{s['clickup_space_id']}")] for s in await asyncio.to_thread(database.get_documents_by_user, config.SPACES_COLLECTION_ID, str(update.effective_user.id))]
    elif action == "view":
        entity, entity_id = parts[1], '_'.join(parts[2:])
        user_id = str(update.effective_user.id)
        if entity == "space":
            text = "لیست پوشه‌ها:"
            folders = await asyncio.to_thread(database.get_documents_by_user, config.FOLDERS_COLLECTION_ID, user_id, [database.Query.equal("space_id", [entity_id])])
            keyboard = [[InlineKeyboardButton(f['name'], callback_data=f"view_folder_{f['clickup_folder_id']}")] for f in folders]
            back_button = InlineKeyboardButton("↩️ بازگشت به فضاها", callback_data="browse_spaces")
        elif entity == "folder":
            text = "لیست لیست‌ها:"
            folder = await asyncio.to_thread(database.get_single_document_by_user, config.FOLDERS_COLLECTION_ID, 'clickup_folder_id', entity_id, user_id)
            lists = await asyncio.to_thread(database.get_documents_by_user, config.LISTS_COLLECTION_ID, user_id, [database.Query.equal("folder_id", [entity_id])])
            keyboard = [[InlineKeyboardButton(l['name'], callback_data=f"view_list_{l['clickup_list_id']}")] for l in lists]
            if folder and folder.get('space_id'): back_button = InlineKeyboardButton("↩️ بازگشت به پوشه‌ها", callback_data=f"view_space_{folder['space_id']}")
        elif entity == "list":
            text = "لیست تسک‌ها:"
            lst = await asyncio.to_thread(database.get_single_document_by_user, config.LISTS_COLLECTION_ID, 'clickup_list_id', entity_id, user_id)
            tasks = await asyncio.to_thread(database.get_documents_by_user, config.TASKS_COLLECTION_ID, user_id, [database.Query.equal("list_id", [entity_id])])
            keyboard = [[InlineKeyboardButton(t['title'], callback_data=f"view_task_{t['clickup_task_id']}")] for t in tasks]
            keyboard.append([InlineKeyboardButton("➕ ساخت تسک جدید", callback_data=f"newtask_in_list_{entity_id}")])
            keyboard.append([InlineKeyboardButton("🔄 رفرش", callback_data=f"refresh_list_{entity_id}")])
            if lst and lst.get('folder_id'): back_button = InlineKeyboardButton("↩️ بازگشت به لیست‌ها", callback_data=f"view_folder_{lst['folder_id']}")
        elif entity == "task": await render_task_view(query, entity_id); return
    elif action == "refresh" and parts[1] == "list":
        list_id = '_'.join(parts[2:])
        user_id = str(update.effective_user.id)
        await query.edit_message_text("در حال همگام‌سازی تسک‌ها از ClickUp... لطفاً صبر کنید 🔄")
        try:
            synced_count = await asyncio.to_thread(clickup_api.sync_tasks_for_list, list_id, user_id)
            text = f"همگام‌سازی کامل شد. {synced_count} تسک پردازش شد.\n\nلیست تسک‌ها:"
            tasks = await asyncio.to_thread(database.get_documents_by_user, config.TASKS_COLLECTION_ID, user_id, [database.Query.equal("list_id", [list_id])])
            keyboard = [[InlineKeyboardButton(t['title'], callback_data=f"view_task_{t['clickup_task_id']}")] for t in tasks]
            keyboard.append([InlineKeyboardButton("➕ ساخت تسک جدید", callback_data=f"newtask_in_list_{list_id}")])
            keyboard.append([InlineKeyboardButton("🔄 رفرش", callback_data=f"refresh_list_{list_id}")])
            lst = await asyncio.to_thread(database.get_single_document_by_user, config.LISTS_COLLECTION_ID, 'clickup_list_id', list_id, user_id)
            if lst and lst.get('folder_id'):
                back_button = InlineKeyboardButton("↩️ بازگشت به لیست‌ها", callback_data=f"view_folder_{lst['folder_id']}")
        except Exception as e:
            logger.error(f"خطا در هنگام رفرش لیست {list_id}: {e}", exc_info=True)
            text = f"❌ خطایی در هنگام همگام‌سازی رخ داد."
            back_button = InlineKeyboardButton("↩️ بازگشت", callback_data=f"view_list_{list_id}")
    elif action == "delete" and parts[1] == "task":
        task_id = '_'.join(parts[2:])
        text, keyboard = "آیا از حذف این تسک مطمئن هستید؟", [[InlineKeyboardButton("✅ بله", callback_data=f"confirm_delete_{task_id}")], [InlineKeyboardButton("❌ خیر", callback_data=f"view_task_{task_id}")]]
    elif action == "confirm" and parts[1] == "delete":
        task_id = '_'.join(parts[2:])
        user_id = str(update.effective_user.id)
        await query.edit_message_text("در حال حذف تسک...")
        task = await asyncio.to_thread(database.get_single_document_by_user, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id, user_id)
        if await asyncio.to_thread(clickup_api.delete_task_in_clickup, task_id, user_id):
            if await asyncio.to_thread(database.delete_document_by_clickup_id, config.TASKS_COLLECTION_ID, task_id, user_id):
                text = "✅ تسک با موفقیت از ClickUp و دیتابیس محلی حذف شد."
            else:
                text = "⚠️ تسک از ClickUp حذف شد، اما حذف از دیتابیس محلی ناموفق بود."
            if task and task.get('list_id'):
                back_button = InlineKeyboardButton("↩️ بازگشت به لیست تسک‌ها", callback_data=f"view_list_{task['list_id']}")
        else:
            text, back_button = "❌ حذف تسک از ClickUp ناموفق بود.", InlineKeyboardButton("↩️ بازگشت به تسک", callback_data=f"view_task_{task_id}")
    
    if not keyboard and not back_button: text = "موردی برای نمایش پیدا نشد."
    if back_button: keyboard.append([back_button])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# --- مکالمه ساخت تسک دستی ---

async def new_task_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    lists = await asyncio.to_thread(database.get_documents_by_user, config.LISTS_COLLECTION_ID, user_id)
    keyboard = [[InlineKeyboardButton(lst['name'], callback_data=f"select_list_{lst['clickup_list_id']}")] for lst in lists]
    keyboard.append([InlineKeyboardButton("لغو ❌", callback_data="cancel_conv")])
    await _send_or_edit(update, "لطفاً لیستی که تسک باید در آن ساخته شود را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_LIST

async def new_task_in_list_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data['list_id'] = query.data.split('_')[-1]
    return await ask_for_title(update, context)

async def ask_for_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    lst = await asyncio.to_thread(database.get_single_document_by_user, config.LISTS_COLLECTION_ID, 'clickup_list_id', context.user_data['list_id'], user_id)
    list_name = lst['name'] if lst else "انتخاب شده"
    keyboard = [[InlineKeyboardButton("↪️ بازگشت به انتخاب لیست", callback_data="back_to_list_selection")]]
    await _send_or_edit(update, f"ساخت تسک در لیست *{list_name}*.\nلطفاً عنوان را وارد کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_TYPING_TITLE

async def title_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['title'] = update.message.text
    return await ask_for_description(update, context)

async def ask_for_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("↪️ بازگشت به عنوان", callback_data="back_to_title"), InlineKeyboardButton("عبور ➡️", callback_data="skip_description")]]
    await _send_or_edit(update, "عنوان ذخیره شد. حالا توضیحات را وارد کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_TYPING_DESCRIPTION

async def description_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['description'] = update.message.text
    return await ask_for_status(update, context)

async def skip_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data['description'] = ""
    return await ask_for_status(update, context)

async def ask_for_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    list_id = context.user_data['list_id']
    statuses = await asyncio.to_thread(clickup_api.get_list_statuses, list_id, user_id)
    keyboard = [[InlineKeyboardButton(s['status'], callback_data=f"select_status_{s['status']}")] for s in statuses]
    keyboard.append([InlineKeyboardButton("↪️ بازگشت به توضیحات", callback_data="back_to_description"), InlineKeyboardButton("عبور ➡️", callback_data="select_status_skip")])
    await _send_or_edit(update, "وضعیت تسک را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_STATUS

async def status_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    status = update.callback_query.data.split('_')[-1]
    context.user_data['status'] = status if status != 'skip' else None
    return await ask_for_priority(update, context)

async def ask_for_priority(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("فوری", callback_data="priority_1"), InlineKeyboardButton("بالا", callback_data="priority_2")],
        [InlineKeyboardButton("متوسط", callback_data="priority_3"), InlineKeyboardButton("پایین", callback_data="priority_4")],
        [InlineKeyboardButton("↪️ بازگشت به وضعیت", callback_data="back_to_status"), InlineKeyboardButton("عبور ➡️", callback_data="priority_skip")]
    ]
    await _send_or_edit(update, "اولویت تسک را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_PRIORITY

async def priority_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    priority = update.callback_query.data.split('_')[1]
    context.user_data['priority'] = int(priority) if priority != 'skip' else None
    return await ask_for_start_date(update, context)

async def ask_for_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("↪️ بازگشت به اولویت", callback_data="back_to_priority")]]
    await _send_or_edit(update, "تاریخ شروع را وارد کنید (مثال: 2025-09-01) یا /skip بزنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_TYPING_START_DATE

async def start_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (parsed := parse_due_date(update.message.text)):
        await update.message.reply_text("فرمت تاریخ اشتباه است. لطفاً دوباره تلاش کنید (YYYY-MM-DD).")
        return CREATE_TYPING_START_DATE
    context.user_data['start_date'] = parsed
    return await ask_for_due_date(update, context)

async def skip_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['start_date'] = None
    return await ask_for_due_date(update, context)

async def ask_for_due_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("↪️ بازگشت به تاریخ شروع", callback_data="back_to_start_date")]]
    await _send_or_edit(update, "تاریخ پایان را وارد کنید (مثال: 2025-12-31) یا /skip بزنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_TYPING_DUE_DATE

async def due_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (parsed := parse_due_date(update.message.text)):
        await update.message.reply_text("فرمت تاریخ اشتباه است. لطفاً دوباره تلاش کنید (YYYY-MM-DD).")
        return CREATE_TYPING_DUE_DATE
    context.user_data['due_date'] = parsed
    return await ask_for_assignee(update, context)

async def skip_due_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['due_date'] = None
    return await ask_for_assignee(update, context)

async def ask_for_assignee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    users = await asyncio.to_thread(database.get_documents_by_user, config.CLICKUP_USERS_COLLECTION_ID, user_id)
    keyboard = [[InlineKeyboardButton(user['username'], callback_data=f"select_user_{user['clickup_user_id']}")] for user in users]
    keyboard.append([InlineKeyboardButton("↪️ بازگشت به تاریخ پایان", callback_data="back_to_due_date"), InlineKeyboardButton("عبور ➡️", callback_data="select_user_skip")])
    await _send_or_edit(update, "مسئول انجام تسک را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_ASSIGNEE

async def assignee_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    assignee_id = query.data.split('_')[-1]
    context.user_data['assignee_id'] = assignee_id if assignee_id != 'skip' else None
    await query.edit_message_text(text="در حال ساخت تسک...")

    user_data = context.user_data
    payload = {"name": user_data['title'], "description": user_data.get('description', '')}
    if user_data.get('assignee_id'): payload["assignees"] = [int(user_data['assignee_id'])]
    if user_data.get('status'): payload["status"] = user_data['status']
    if user_data.get('priority'): payload["priority"] = user_data['priority']
    if user_data.get('start_date'): payload["start_date"] = user_data['start_date']
    if user_data.get('due_date'): payload["due_date"] = user_data['due_date']
    
    user_id = str(update.effective_user.id)
    success, task_data = await asyncio.to_thread(clickup_api.create_task_in_clickup_api, user_data['list_id'], payload, user_id)

    if success and (task_id := task_data.get('id')):
        synced_task = await asyncio.to_thread(clickup_api.sync_single_task_from_clickup, task_id, user_id)
        if synced_task:
            def format_date(timestamp_ms):
                if not timestamp_ms: return "خالی"
                try: return datetime.fromtimestamp(int(timestamp_ms) / 1000).strftime('%Y-%m-%d')
                except: return "نامشخص"

            list_doc = await asyncio.to_thread(database.get_single_document_by_user, config.LISTS_COLLECTION_ID, 'clickup_list_id', user_data['list_id'], user_id)

            details = [
                f"✅ تسک با موفقیت ساخته شد!\n",
                f"🏷️ *عنوان:* {synced_task.get('title', 'خالی')}",
                f"📝 *توضیحات:* {synced_task.get('content', 'خالی') or 'خالی'}",
                f"🗂️ *لیست:* {list_doc['name'] if list_doc else 'نامشخص'}",
                f"👤 *مسئول:* {synced_task.get('assignee_name', 'خالی')}",
                f"📊 *وضعیت:* {synced_task.get('status', 'خالی')}",
                f"❗️ *اولویت:* {synced_task.get('priority', 'خالی')}",
                f"🗓️ *تاریخ شروع:* {format_date(synced_task.get('start_date'))}",
                f"🏁 *تاریخ تحویل:* {format_date(synced_task.get('due_date'))}"
            ]
            final_message = "\n".join(details)
            final_message += f"\n\n🔗 *لینک تسک:* {task_data.get('url')}"
            await query.edit_message_text(text=final_message, parse_mode='Markdown')
        else:
             await query.edit_message_text(f"✅ تسک ساخته شد اما همگام‌سازی ناموفق بود. لینک: {task_data.get('url')}")
    else:
        await query.edit_message_text(text=f"❌ ساخت تسک ناموفق بود. خطا: {task_data.get('err', 'نامشخص')}")

    user_data.clear()
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await _send_or_edit(update, "عملیات ساخت تسک لغو شد.")
    else:
        await update.message.reply_text("عملیات ساخت تسک لغو شد.")

    await start_command(update, context)
    context.user_data.clear()
    return ConversationHandler.END

def get_create_task_conv_handler():
    states = {
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
        CREATE_TYPING_START_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, start_date_received),
                                   CommandHandler("skip", skip_start_date),
                                   CallbackQueryHandler(ask_for_priority, pattern='^back_to_priority$')],
        CREATE_TYPING_DUE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, due_date_received),
                                 CommandHandler("skip", skip_due_date),
                                 CallbackQueryHandler(ask_for_start_date, pattern='^back_to_start_date$')],
        CREATE_SELECTING_ASSIGNEE: [CallbackQueryHandler(assignee_selected, pattern='^select_user_'),
                                    CallbackQueryHandler(ask_for_due_date, pattern='^back_to_due_date$')],
    }
    return ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^➕ ساخت تسک جدید$'), new_task_entry),
                      CallbackQueryHandler(new_task_in_list_start, pattern='^newtask_in_list_')],
        states=states,
        fallbacks=[CommandHandler("cancel", cancel_conversation),
                   CallbackQueryHandler(cancel_conversation, pattern='^cancel_conv$')],
        per_chat=True,
        per_user=True,
    )

async def show_edit_menu(update_or_message, context: ContextTypes.DEFAULT_TYPE, message_text: str = "کدام بخش را می‌خواهید ویرایش کنید؟"):
    keyboard = [
        [InlineKeyboardButton("ویرایش عنوان", callback_data="edit_field_name"), InlineKeyboardButton("ویرایش توضیحات", callback_data="edit_field_description")],
        [InlineKeyboardButton("تغییر وضعیت", callback_data="edit_field_status"), InlineKeyboardButton("تغییر اولویت", callback_data="edit_field_priority")],
        [InlineKeyboardButton("تغییر تاریخ شروع", callback_data="edit_field_start_date"), InlineKeyboardButton("تغییر تاریخ تحویل", callback_data="edit_field_due_date")],
        [InlineKeyboardButton("تغییر مسئول تسک", callback_data="edit_field_assignees")],
        [InlineKeyboardButton("↩️ بازگشت به تسک", callback_data="back_to_task")]
    ]
    target_message = update_or_message if isinstance(update_or_message, Message) else update_or_message.message
    await target_message.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return EDIT_SELECTING_FIELD

async def edit_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    task_id = '_'.join(query.data.split('_')[2:])
    context.user_data['edit_task_id'] = task_id
    user_id = str(update.effective_user.id)
    task = await asyncio.to_thread(database.get_single_document_by_user, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id, user_id)
    if not task:
        await query.edit_message_text("خطا: تسک مورد نظر یافت نشد.")
        return ConversationHandler.END
    context.user_data['task'] = task
    return await show_edit_menu(query, context)

async def edit_field_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    field_to_edit = '_'.join(query.data.split('_')[2:])
    context.user_data['field_to_edit'] = field_to_edit
    task = context.user_data['task']
    prompt_text, keyboard, next_state = "", [], EDIT_SELECTING_VALUE
    user_id = str(update.effective_user.id)
    
    if field_to_edit in ['name', 'description']:
        next_state = EDIT_TYPING_VALUE
        field_map = {'name': 'title', 'description': 'content'}
        current_value = task.get(field_map[field_to_edit], 'خالی')
        prompt_text = f"مقدار فعلی: *{current_value}*\n\nلطفاً مقدار جدید را وارد کنید:"
    elif field_to_edit == 'status':
        statuses = await asyncio.to_thread(clickup_api.get_list_statuses, task['list_id'], user_id)
        keyboard = [[InlineKeyboardButton(s['status'], callback_data=f"edit_value_{s['status']}")] for s in statuses]
        prompt_text = f"وضعیت فعلی: *{task.get('status', 'N/A')}*\n\nوضعیت جدید را انتخاب کنید:"
    elif field_to_edit == 'priority':
        keyboard = [[InlineKeyboardButton(p_name, callback_data=f"edit_value_{p_val}")] for p_name, p_val in [("فوری",1), ("بالا",2), ("متوسط",3), ("پایین",4), ("حذف",0)]]
        prompt_text = f"اولویت فعلی: *{task.get('priority', 'N/A')}*\n\nاولویت جدید را انتخاب کنید:"
    elif field_to_edit == 'assignees':
        users = await asyncio.to_thread(database.get_documents_by_user, config.CLICKUP_USERS_COLLECTION_ID, user_id)
        keyboard = [[InlineKeyboardButton(u['username'], callback_data=f"edit_value_{u['clickup_user_id']}")] for u in users]
        prompt_text = "مسئول جدید را انتخاب کنید:"
    elif field_to_edit in ['start_date', 'due_date']:
        next_state = EDIT_TYPING_VALUE
        field_map = {'start_date': 'start_date', 'due_date': 'due_date'}
        current_value_ts = task.get(field_map[field_to_edit])
        current_value = datetime.fromtimestamp(int(current_value_ts) / 1000).strftime('%Y-%m-%d') if current_value_ts else "خالی"
        prompt_text = f"مقدار فعلی: *{current_value}*\n\nلطفاً مقدار جدید را وارد کنید (مثال: YYYY-MM-DD):"

    if keyboard: keyboard.append([InlineKeyboardButton("❌ لغو", callback_data="cancel_edit_field")])
    await query.edit_message_text(prompt_text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None, parse_mode='Markdown')
    return next_state

async def process_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, new_value):
    query = update.callback_query
    editable_message = query.message if query else await update.message.reply_text("در حال پردازش...")
    await editable_message.edit_text("در حال به‌روزرسانی...")
    
    task_id, field = context.user_data['edit_task_id'], context.user_data['field_to_edit']
    user_id = str(update.effective_user.id)
    payload, api_value = {}, new_value
    
    if field == 'priority': api_value = int(new_value)
    elif field == 'assignees': api_value = {'add': [int(new_value)], 'rem': []}
    elif 'date' in field:
        if new_value.lower() in ['remove', 'حذف', 'خالی']:
            api_value = None
        elif not (api_value := parse_due_date(new_value)):
            await editable_message.edit_text("❌ فرمت تاریخ نامعتبر است. لطفاً دوباره تلاش کنید."); return EDIT_TYPING_VALUE
    
    payload[field] = api_value
    success, response_data = await asyncio.to_thread(clickup_api.update_task_in_clickup_api, task_id, payload, user_id)
    
    if success:
        synced_task = await asyncio.to_thread(clickup_api.sync_single_task_from_clickup, task_id, user_id)
        if synced_task:
            def format_date(timestamp_ms):
                if not timestamp_ms: return "خالی"
                try: return datetime.fromtimestamp(int(timestamp_ms) / 1000).strftime('%Y-%m-%d')
                except: return "نامشخص"
            
            list_doc = await asyncio.to_thread(database.get_single_document_by_user, config.LISTS_COLLECTION_ID, 'clickup_list_id', synced_task['list_id'], user_id)
            
            details = [
                f"✅ تسک با موفقیت به‌روزرسانی شد!\n",
                f"🏷️ *عنوان:* {synced_task.get('title', 'خالی')}",
                f"📝 *توضیحات:* {synced_task.get('content', 'خالی') or 'خالی'}",
                f"🗂️ *لیست:* {list_doc['name'] if list_doc else 'نامشخص'}",
                f"👤 *مسئول:* {synced_task.get('assignee_name', 'خالی')}",
                f"📊 *وضعیت:* {synced_task.get('status', 'خالی')}",
                f"❗️ *اولویت:* {synced_task.get('priority', 'خالی')}",
                f"🗓️ *تاریخ شروع:* {format_date(synced_task.get('start_date'))}",
                f"🏁 *تاریخ تحویل:* {format_date(synced_task.get('due_date'))}"
            ]
            final_message = "\n".join(details)
            await show_edit_menu(editable_message, context, f"✅ به‌روزرسانی موفق بود. مورد دیگری برای ویرایش هست؟\n\n{final_message}")
        else:
            await show_edit_menu(editable_message, context, "✅ به‌روزرسانی موفق بود اما همگام‌سازی ناموفق بود.")
    else:
        await show_edit_menu(editable_message, context, f"❌ به‌روزرسانی ناموفق بود: {response_data.get('err', 'خطای نامشخص')}")

    return EDIT_SELECTING_FIELD

async def edit_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_edit(update, context, update.message.text)

async def edit_value_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    return await process_edit(update, context, query.data.split('_')[-1])

async def back_to_task_from_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    task_id = context.user_data.get('edit_task_id')
    context.user_data.clear()
    if task_id: await render_task_view(query, task_id)
    return ConversationHandler.END

def get_edit_task_conv_handler():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_task_start, pattern='^edit_task_')],
        states={
            EDIT_SELECTING_FIELD: [CallbackQueryHandler(edit_field_selected, pattern='^edit_field_'),
                                   CallbackQueryHandler(back_to_task_from_edit, pattern='^back_to_task$')],
            EDIT_TYPING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value_received)],
            EDIT_SELECTING_VALUE: [CallbackQueryHandler(edit_value_selected, pattern='^edit_value_'),
                                     CallbackQueryHandler(show_edit_menu, pattern='^cancel_edit_field$')]
        },
        fallbacks=[CommandHandler("cancel", back_to_task_from_edit)],
        per_user=True,
        per_chat=True,
    )


# --- Onboarding Conversation ---

async def select_package_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    package_id = query.data.split('_')[-1]

    package_doc = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, '$id', package_id)
    if not package_doc:
        await query.edit_message_text("❌ پکیج مورد نظر یافت نشد. لطفاً دوباره تلاش کنید.")
        return ConversationHandler.END

    user_id = str(update.effective_user.id)
    user_data = {
        'telegram_id': user_id,
        'is_active': True,
        'is_admin': False,
        'package_id': package_id,
        'used_count': 0
    }

    if package_doc.get('monthly_price') == 0:
        # Free package, immediately activate user
        await asyncio.to_thread(database.upsert_document, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id, user_data)
        await query.edit_message_text(f"✅ پکیج رایگان برای شما فعال شد! لطفا توکن API کلیک‌آپ خود را برای ادامه ارسال کنید:")
        return ONBOARDING_GET_CLICKUP_TOKEN
    else:
        # Paid package, show payment info
        await asyncio.to_thread(database.upsert_document, config.PAYMENT_REQUESTS_COLLECTION_ID, 'user_id', user_id, {
            'user_id': user_id,
            'package_id': package_id,
            'status': 'pending',
            'payment_info': 'Awaiting user payment'
        })

        payment_info_text = f"برای فعال‌سازی پکیج '{package_doc.get('package_name')}' با مبلغ {package_doc.get('monthly_price')} تومان، لطفاً مبلغ را به یکی از روش‌های زیر واریز کنید:\n\n"
        payment_info_text += "شماره حساب:\n`1234-5678-9012-3456`\n"
        payment_info_text += "آدرس کیف پول:\n`0x123...abc`\n\n"
        payment_info_text += "پس از پرداخت، لطفا تصویر فیش واریزی را برای من ارسال کنید."

        await query.edit_message_text(payment_info_text, parse_mode='Markdown')
        context.user_data['package_id'] = package_id
        return ONBOARDING_CONFIRM_PAYMENT

async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This handler is not yet fully implemented
    await update.message.reply_text("تصویر فیش واریزی دریافت شد. به زودی درخواست شما بررسی و پاسخ داده می‌شود.")
    return ConversationHandler.END


async def get_clickup_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    clickup_token = update.message.text
    
    # Simple validation for token format (can be improved)
    if len(clickup_token) > 20: # Basic check for token length
        user_doc = await asyncio.to_thread(database.get_single_document, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id)
        if user_doc:
            await asyncio.to_thread(database.upsert_document, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id, {'clickup_token': clickup_token})
            await update.message.reply_text("✅ توکن کلیک‌آپ شما با موفقیت ذخیره شد!")
            await send_main_menu(update, context)
            return ConversationHandler.END
    
    await update.message.reply_text("❌ توکن وارد شده معتبر نیست. لطفاً توکن API کلیک‌آپ خود را به درستی وارد کنید:")
    return ONBOARDING_GET_CLICKUP_TOKEN
    
def get_onboarding_conv_handler():
    return ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            ONBOARDING_SELECTING_PACKAGE: [CallbackQueryHandler(select_package_onboarding, pattern='^select_package_')],
            ONBOARDING_CONFIRM_PAYMENT: [MessageHandler(filters.PHOTO, confirm_payment)],
            ONBOARDING_GET_CLICKUP_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_clickup_token)]
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        per_user=True,
        per_chat=True,
    )

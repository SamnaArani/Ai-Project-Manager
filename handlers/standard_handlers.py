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
from appwrite.query import Query
import config
import database
import clickup_api
from ai import tools
from datetime import datetime, timezone
from functools import partial

logger = logging.getLogger(__name__)

# --- تعریف وضعیت‌های مکالمه ---
(CREATE_SELECTING_LIST, CREATE_TYPING_TITLE, CREATE_TYPING_DESCRIPTION,
 CREATE_SELECTING_STATUS, CREATE_SELECTING_PRIORITY, CREATE_TYPING_START_DATE, 
 CREATE_TYPING_DUE_DATE, CREATE_SELECTING_ASSIGNEE) = range(8)
(EDIT_SELECTING_FIELD, EDIT_TYPING_VALUE, EDIT_SELECTING_VALUE) = range(8, 11)
GET_CLICKUP_TOKEN = 11

# --- توابع کمکی ---

async def _get_user_token(user_id: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """توکن کاربر را از دیتابیس دریافت می‌کند و در context ذخیره می‌کند."""
    if 'clickup_token' in context.user_data:
        return context.user_data['clickup_token']
        
    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )
    if user_doc and user_doc.get('clickup_token'):
        context.user_data['clickup_token'] = user_doc['clickup_token']
        return user_doc['clickup_token']
    else:
        target = update.callback_query.message if update.callback_query else update.message
        await target.reply_text("توکن ClickUp شما یافت نشد یا حساب شما غیرفعال است. لطفاً با دستور /start ثبت نام کنید.")
        return None

def parse_due_date(due_date_str: str) -> int | None:
    try:
        date_obj = datetime.strptime(due_date_str, "%Y-%m-%d")
        date_obj_utc = date_obj.replace(hour=12, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        return int(date_obj_utc.timestamp() * 1000)
    except (ValueError, TypeError):
        logger.warning(f"Could not parse date string: {due_date_str}")
        return None

async def _send_or_edit(update: Update, text: str, reply_markup: InlineKeyboardMarkup = None):
    try:
        target = update.callback_query.message if update.callback_query else update.message
        if update.callback_query:
            await target.edit_text(text, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            await target.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning(f"Could not send or edit message: {e}")

# --- مکالمه ثبت نام و احراز هویت ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id
    )

    if user_doc and user_doc.get('clickup_token') and user_doc.get('is_active'):
        main_menu_keyboard = [
            [KeyboardButton("🔍 مرور پروژه‌ها")],
            [KeyboardButton("➕ ساخت تسک جدید")]
        ]
        reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True, one_time_keyboard=False)
        await update.message.reply_text("سلام مجدد! به PIXEELL خوش آمدید. لطفاً یک گزینه را انتخاب کنید:", reply_markup=reply_markup)
        return ConversationHandler.END
    else:
        if not user_doc:
            await asyncio.to_thread(
                database.create_document,
                config.APPWRITE_DATABASE_ID,
                config.BOT_USERS_COLLECTION_ID,
                {'telegram_id': user_id, 'is_active': False, 'is_admin': False}
            )
        await update.message.reply_text(
            "👋 سلام! به ربات مدیریت پروژه PIXEELL خوش آمدید.\n\n"
            "برای شروع، لطفاً توکن API کلیک‌اپ (ClickUp API Token) خود را ارسال کنید.\n\n"
            "می‌توانید این توکن را از بخش 'Apps' در تنظیمات حساب کلیک‌اپ خود دریافت کنید."
        )
        return GET_CLICKUP_TOKEN

async def token_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    user_id = str(update.effective_user.id)

    placeholder_message = await update.message.reply_text("در حال بررسی توکن...")

    # 1. Check for duplicate token
    duplicate_user_doc = await asyncio.to_thread(
        database.get_single_document, config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'clickup_token', token
    )
    
    if duplicate_user_doc and duplicate_user_doc.get('telegram_id') != user_id:
        await placeholder_message.edit_text(
            "❌ این توکن API قبلاً توسط کاربر دیگری ثبت شده است.\n\n"
            "هر کاربر باید از توکن شخصی خود استفاده کند. اگر فکر می‌کنید این یک اشتباه است، لطفاً با پشتیبانی تماس بگیرید: @saman_arani"
        )
        return GET_CLICKUP_TOKEN

    # 2. Validate token with ClickUp API
    await placeholder_message.edit_text("توکن منحصر به فرد است. در حال اعتبارسنجی با کلیک‌اپ...")
    is_valid = await asyncio.to_thread(clickup_api.validate_token, token)

    if is_valid:
        await placeholder_message.edit_text("توکن معتبر است. در حال همگام‌سازی اولیه اطلاعات... ⏳ این ممکن است کمی طول بکشد.")
        sync_success = await asyncio.to_thread(clickup_api.sync_all_user_data, token)

        if not sync_success:
            await placeholder_message.edit_text("❌ توکن معتبر است، اما در همگام‌سازی اولیه اطلاعات خطایی رخ داد. لطفاً با پشتیبانی تماس بگیرید.")
            return GET_CLICKUP_TOKEN

        # 3. Upsert user data
        await asyncio.to_thread(
            database.upsert_document,
            config.APPWRITE_DATABASE_ID,
            config.BOT_USERS_COLLECTION_ID,
            'telegram_id',
            user_id,
            {'clickup_token': token, 'is_active': True}
        )
        await placeholder_message.edit_text("✅ توکن شما با موفقیت ذخیره و فعال شد!")
        
        main_menu_keyboard = [
            [KeyboardButton("🔍 مرور پروژه‌ها")],
            [KeyboardButton("➕ ساخت تسک جدید")]
        ]
        reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True, one_time_keyboard=False)
        await update.message.reply_text("حالا می‌توانید از تمام امکانات ربات استفاده کنید:", reply_markup=reply_markup)
        return ConversationHandler.END
    else:
        await placeholder_message.edit_text(
            "❌ توکن ارسال شده نامعتبر است.\n\n"
            "لطفاً توکن صحیح را دوباره ارسال کنید یا برای لغو /cancel را بزنید."
        )
        return GET_CLICKUP_TOKEN

async def cancel_auth_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("عملیات ثبت نام لغو شد. برای شروع مجدد /start را ارسال کنید.")
    return ConversationHandler.END

def get_auth_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            GET_CLICKUP_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, token_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel_auth_conversation)],
        per_chat=True,
        per_user=True,
    )

# --- توابع اصلی ---

async def browse_projects_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not await _get_user_token(user_id, update, context): return
    
    keyboard = [[InlineKeyboardButton("نمایش فضاها (Spaces)", callback_data="browse_spaces")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("برای شروع مرور، روی دکمه زیر کلیک کنید:", reply_markup=reply_markup)

async def render_task_view(query_or_update, task_id):
    target_message = query_or_update.message if isinstance(query_or_update, CallbackQuery) else query_or_update
    task = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id)
    
    if task:
        def format_date(timestamp_ms):
            if not timestamp_ms: return "خالی"
            try: return datetime.fromtimestamp(int(timestamp_ms) / 1000).strftime('%Y-%m-%d')
            except (ValueError, TypeError): return "نامشخص"
        
        list_doc = None
        if list_id := task.get('list_id'):
            list_doc = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, 'clickup_list_id', list_id)

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
        
        await _send_or_edit(query_or_update, text, InlineKeyboardMarkup(keyboard))

    else:
        await (query_or_update.message if isinstance(query_or_update, CallbackQuery) else query_or_update).reply_text("تسک پیدا نشد.")

async def _handle_update_correction(update: Update, context: ContextTypes.DEFAULT_TYPE, correction_type: str, selected_value: str):
    from handlers import ai_handlers
    payload = context.chat_data.pop('pending_update_payload', None)
    context.chat_data.pop('conversation_state', None)
    
    if not payload:
        await _send_or_edit(update, "خطا: اطلاعات ویرایش پیدا نشد. لطفاً دوباره تلاش کنید.")
        return

    if correction_type == 'list':
        payload['list_name'] = selected_value
    elif correction_type == 'task':
        payload['task_name'] = selected_value

    plan = {"steps": [{"tool_name": "update_task", "arguments": payload}]}
    
    placeholder_message = await update.callback_query.message.edit_text(f"در حال اعمال تغییرات با اطلاعات صحیح... لطفاً صبر کنید.")
    
    await ai_handlers.execute_plan(plan, update.callback_query.message.text, update, context, placeholder_message.id)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    token = await _get_user_token(user_id, update, context)
    if not token: return
    
    data = query.data
    logger.info(f"Callback query received: {data}")
    parts = data.split('_')
    action = parts[0]

    prefix_map = {
        "correct_status_": "status", "correct_priority_": "priority",
        "correct_assignee_name_": "assignee_name", "correct_list_name_": "list_name"
    }
    update_prefix_map = {"correct_update_list_": "list", "correct_update_task_": "task"}

    found_prefix = next((prefix for prefix in prefix_map if data.startswith(prefix)), None)
    found_update_prefix = next((prefix for prefix in update_prefix_map if data.startswith(prefix)), None)

    if action == "correct" and found_prefix:
        correction_type = prefix_map[found_prefix]
        selected_value = data[len(found_prefix):]
        payload = context.chat_data.get('pending_task_payload', {})
        
        context.chat_data.pop('conversation_state', None)
        context.chat_data.pop('pending_task_payload', None)

        payload[correction_type] = selected_value
        await query.edit_message_text(f"در حال اعمال مقدار اصلاح شده: '{selected_value}'...")

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
    
    elif found_update_prefix:
        await _handle_update_correction(update, context, update_prefix_map[found_update_prefix], data[len(found_update_prefix):])
        return

    keyboard, text, back_button = [], "لطفاً انتخاب کنید:", None
    if action == "browse" and parts[1] == "spaces":
        docs = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.SPACES_COLLECTION_ID)
        text, keyboard = "لیست فضاها:", [[InlineKeyboardButton(s['name'], callback_data=f"view_space_{s['clickup_space_id']}")] for s in docs]
    elif action == "view":
        entity, entity_id = parts[1], '_'.join(parts[2:])
        if entity == "space":
            text = "لیست پوشه‌ها:"
            docs = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.FOLDERS_COLLECTION_ID, [Query.equal("space_id", [entity_id])])
            keyboard = [[InlineKeyboardButton(f['name'], callback_data=f"view_folder_{f['clickup_folder_id']}")] for f in docs]
            back_button = InlineKeyboardButton("↩️ بازگشت به فضاها", callback_data="browse_spaces")
        elif entity == "folder":
            text = "لیست لیست‌ها:"
            folder = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.FOLDERS_COLLECTION_ID, 'clickup_folder_id', entity_id)
            docs = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, [Query.equal("folder_id", [entity_id])])
            keyboard = [[InlineKeyboardButton(l['name'], callback_data=f"view_list_{l['clickup_list_id']}")] for l in docs]
            if folder and folder.get('space_id'): back_button = InlineKeyboardButton("↩️ بازگشت به پوشه‌ها", callback_data=f"view_space_{folder['space_id']}")
        elif entity == "list":
            text = "لیست تسک‌ها:"
            lst = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, 'clickup_list_id', entity_id)
            tasks = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, [Query.equal("list_id", [entity_id])])
            keyboard = [[InlineKeyboardButton(t['title'], callback_data=f"view_task_{t['clickup_task_id']}")] for t in tasks]
            keyboard.append([InlineKeyboardButton("➕ ساخت تسک جدید", callback_data=f"newtask_in_list_{entity_id}")])
            keyboard.append([InlineKeyboardButton("🔄 رفرش", callback_data=f"refresh_list_{entity_id}")]) 
            if lst and lst.get('folder_id'): back_button = InlineKeyboardButton("↩️ بازگشت به لیست‌ها", callback_data=f"view_folder_{lst['folder_id']}")
        elif entity == "task": await render_task_view(query, entity_id); return
    elif action == "refresh" and parts[1] == "list":
        list_id = '_'.join(parts[2:])
        await query.edit_message_text("در حال همگام‌سازی تسک‌ها از ClickUp... 🔄")
        try:
            sync_call = partial(clickup_api.sync_tasks_for_list, list_id, token=token)
            synced_count = await asyncio.to_thread(sync_call)
            text = f"همگام‌سازی کامل شد. {synced_count} تسک پردازش شد.\n\nلیست تسک‌ها:"
            tasks = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, [Query.equal("list_id", [list_id])])
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
        delete_call = partial(clickup_api.delete_task_in_clickup, task_id, token=token)
        if await asyncio.to_thread(delete_call):
            db_delete_call = partial(database.delete_document_by_clickup_id, config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id)
            await asyncio.to_thread(db_delete_call)
            text = "✅ تسک با موفقیت از ClickUp و دیتابیس محلی حذف شد."
            if task and task.get('list_id'): back_button = InlineKeyboardButton("↩️ بازگشت به لیست تسک‌ها", callback_data=f"view_list_{task['list_id']}")
        else:
            text, back_button = "❌ حذف تسک از ClickUp ناموفق بود.", InlineKeyboardButton("↩️ بازگشت به تسک", callback_data=f"view_task_{task_id}")
    
    if not keyboard and not back_button: text = "موردی برای نمایش پیدا نشد."
    if back_button: keyboard.append([back_button])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# --- مکالمه ساخت تسک دستی ---
async def new_task_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not await _get_user_token(user_id, update, context): return ConversationHandler.END

    lists = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID)
    keyboard = [[InlineKeyboardButton(lst['name'], callback_data=f"select_list_{lst['clickup_list_id']}")] for lst in lists]
    keyboard.append([InlineKeyboardButton("لغو ❌", callback_data="cancel_conv")])
    await _send_or_edit(update, "لطفاً لیستی که تسک باید در آن ساخته شود را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_LIST

async def new_task_in_list_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data['list_id'] = query.data.split('_')[-1]
    return await ask_for_title(update, context)

async def ask_for_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lst = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, 'clickup_list_id', context.user_data['list_id'])
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
    token = await _get_user_token(user_id, update, context)
    if not token: return ConversationHandler.END

    list_id = context.user_data['list_id']
    statuses_call = partial(clickup_api.get_list_statuses, list_id, token=token)
    statuses = await asyncio.to_thread(statuses_call)
    keyboard = [[InlineKeyboardButton(status['status'], callback_data=f"select_status_{status['status']}")] for status in statuses]
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
    users = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.CLICKUP_USERS_COLLECTION_ID)
    keyboard = [[InlineKeyboardButton(user['username'], callback_data=f"select_user_{user['clickup_user_id']}")] for user in users]
    keyboard.append([InlineKeyboardButton("↪️ بازگشت به تاریخ پایان", callback_data="back_to_due_date"), InlineKeyboardButton("عبور ➡️", callback_data="select_user_skip")])
    await _send_or_edit(update, "مسئول انجام تسک را انتخاب کنید:", InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_ASSIGNEE

async def assignee_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    user_id_str = str(query.from_user.id)
    token = await _get_user_token(user_id_str, update, context)
    if not token: return ConversationHandler.END

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

    create_call = partial(clickup_api.create_task_in_clickup_api, user_data['list_id'], payload, token=token)
    success, task_data = await asyncio.to_thread(create_call)

    if success and (task_id := task_data.get('id')):
        sync_call = partial(clickup_api.sync_single_task_from_clickup, task_id, token=token)
        synced_task = await asyncio.to_thread(sync_call)
        if synced_task:
            list_doc = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, 'clickup_list_id', user_data['list_id'])
            
            details = [
                f"✅ تسک با موفقیت ساخته شد!\n",
                f"🏷️ *عنوان:* {synced_task.get('title', 'خالی')}",
                f"👤 *مسئول:* {synced_task.get('assignee_name', 'خالی') or 'خالی'}",
                f"📊 *وضعیت:* {synced_task.get('status', 'خالی') or 'خالی'}",
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
    await _send_or_edit(update, "عملیات لغو شد.")
    context.user_data.clear()
    return ConversationHandler.END

def get_create_task_conv_handler():
    return ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^➕ ساخت تسک جدید$'), new_task_entry), 
                      CallbackQueryHandler(new_task_in_list_start, pattern='^newtask_in_list_')],
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
            CREATE_TYPING_START_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, start_date_received), 
                                       CommandHandler("skip", skip_start_date), 
                                       CallbackQueryHandler(ask_for_priority, pattern='^back_to_priority$')],
            CREATE_TYPING_DUE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, due_date_received), 
                                     CommandHandler("skip", skip_due_date), 
                                     CallbackQueryHandler(ask_for_start_date, pattern='^back_to_start_date$')],
            CREATE_SELECTING_ASSIGNEE: [CallbackQueryHandler(assignee_selected, pattern='^select_user_'), 
                                        CallbackQueryHandler(ask_for_due_date, pattern='^back_to_due_date$')],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation), 
                   CallbackQueryHandler(cancel_conversation, pattern='^cancel_conv$')],
    )

# --- مکالمه ویرایش تسک ---
async def show_edit_menu(update_or_message, context: ContextTypes.DEFAULT_TYPE, message_text: str = "کدام بخش را می‌خواهید ویرایش کنید؟"):
    keyboard = [
        [InlineKeyboardButton("ویرایش عنوان", callback_data="edit_field_name"), InlineKeyboardButton("ویرایش توضیحات", callback_data="edit_field_description")],
        [InlineKeyboardButton("تغییر وضعیت", callback_data="edit_field_status"), InlineKeyboardButton("تغییر اولویت", callback_data="edit_field_priority")],
        [InlineKeyboardButton("تغییر تاریخ شروع", callback_data="edit_field_start_date"), InlineKeyboardButton("تغییر تاریخ تحویل", callback_data="edit_field_due_date")],
        [InlineKeyboardButton("تغییر مسئول تسک", callback_data="edit_field_assignees")],
        [InlineKeyboardButton("↩️ بازگشت به تسک", callback_data="back_to_task")]
    ]
    await _send_or_edit(update_or_message, message_text, InlineKeyboardMarkup(keyboard))
    return EDIT_SELECTING_FIELD

async def edit_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    user_id = str(query.from_user.id)
    if not await _get_user_token(user_id, update, context): return ConversationHandler.END

    task_id = '_'.join(query.data.split('_')[2:])
    context.user_data['edit_task_id'] = task_id
    task = await asyncio.to_thread(database.get_single_document, config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id)
    if not task:
        await _send_or_edit(update, "خطا: تسک مورد نظر یافت نشد.")
        return ConversationHandler.END
    context.user_data['task'] = task
    return await show_edit_menu(query, context)

async def edit_field_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    user_id = str(query.from_user.id)
    token = await _get_user_token(user_id, update, context)
    if not token: return ConversationHandler.END

    field_to_edit = '_'.join(query.data.split('_')[2:])
    context.user_data['field_to_edit'] = field_to_edit
    task = context.user_data['task']
    prompt_text, keyboard, next_state = "", [], EDIT_SELECTING_VALUE
    
    text_fields = {'name': 'title', 'description': 'content', 'start_date': 'start_date', 'due_date': 'due_date'}
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
        users = await asyncio.to_thread(database.get_documents, config.APPWRITE_DATABASE_ID, config.CLICKUP_USERS_COLLECTION_ID)
        keyboard = [[InlineKeyboardButton(u['username'], callback_data=f"edit_value_{u['clickup_user_id']}")] for u in users]
        prompt_text = "مسئول جدید را انتخاب کنید:"

    if keyboard: keyboard.append([InlineKeyboardButton("❌ لغو", callback_data="cancel_edit_field")])
    await _send_or_edit(update, prompt_text, InlineKeyboardMarkup(keyboard))
    return next_state

async def process_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, new_value):
    user_id = str(update.effective_user.id)
    token = await _get_user_token(user_id, update, context)
    if not token: return ConversationHandler.END

    await _send_or_edit(update, "در حال به‌روزرسانی...")
    
    task_id, field = context.user_data['edit_task_id'], context.user_data['field_to_edit']
    payload, api_value = {}, new_value
    
    if field == 'priority': api_value = int(new_value) if new_value != "0" else None
    elif field == 'assignees': api_value = {'add': [int(new_value)], 'rem': []}
    elif 'date' in field and not (api_value := parse_due_date(new_value)):
        await _send_or_edit(update, "❌ فرمت تاریخ نامعتبر است. لطفاً دوباره تلاش کنید."); return EDIT_TYPING_VALUE
    
    payload[field] = api_value
    update_call = partial(clickup_api.update_task_in_clickup_api, task_id, payload, token=token)
    success, response_data = await asyncio.to_thread(update_call)
    
    if success:
        sync_call = partial(clickup_api.sync_single_task_from_clickup, task_id, token=token)
        asyncio.create_task(asyncio.to_thread(sync_call))
        await show_edit_menu(update, context, "✅ به‌روزرسانی موفق بود. مورد دیگری برای ویرایش هست؟")
    else:
        await show_edit_menu(update, context, f"❌ به‌روزرسانی ناموفق بود: {response_data.get('err', 'خطای نامشخص')}")

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
                                     CallbackQueryHandler(lambda u, c: show_edit_menu(u, c), pattern='^cancel_edit_field$')]
        },
        fallbacks=[CommandHandler("cancel", back_to_task_from_edit)],
    )


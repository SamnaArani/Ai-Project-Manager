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

logger = logging.getLogger(__name__)

(CREATE_SELECTING_LIST, CREATE_TYPING_TITLE, CREATE_TYPING_DESCRIPTION,
 CREATE_SELECTING_STATUS, CREATE_SELECTING_PRIORITY, CREATE_TYPING_DUE_DATE,
 CREATE_SELECTING_ASSIGNEE) = range(7)
(EDIT_SELECTING_FIELD, EDIT_TYPING_VALUE, EDIT_SELECTING_VALUE) = range(7, 10)

def parse_due_date(due_date_str: str) -> int | None:
    try:
        date_obj = datetime.strptime(due_date_str, "%Y-%m-%d")
        date_obj_utc = date_obj.replace(hour=12, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        return int(date_obj_utc.timestamp() * 1000)
    except (ValueError, TypeError):
        logger.warning(f"Could not parse date string: {due_date_str}")
        return None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    main_menu_keyboard = [
        [KeyboardButton("🔍 مرور پروژه‌ها")],
        [KeyboardButton("➕ ساخت تسک جدید")]
    ]
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True, one_time_keyboard=False)
    context.user_data.clear()
    context.chat_data.clear()
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text("سلام! لطفاً یک گزینه را انتخاب کنید یا دستور خود را تایپ کنید:", reply_markup=reply_markup)

async def browse_projects_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("نمایش فضاها (Spaces)", callback_data="browse_spaces")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("برای شروع مرور، روی دکمه زیر کلیک کنید:", reply_markup=reply_markup)

async def render_task_view(query_or_update, task_id):
    task = await asyncio.to_thread(database.get_single_document, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id)
    target = query_or_update.message if isinstance(query_or_update, CallbackQuery) else query_or_update.effective_message
    if task:
        due_date_str = "تعیین نشده"
        if task.get('due_date'):
            try:
                dt_object = datetime.fromtimestamp(int(task['due_date']) / 1000)
                due_date_str = dt_object.strftime('%Y-%m-%d')
            except (ValueError, TypeError):
                due_date_str = "نامشخص"
        text = (f"*{task.get('title', 'بدون عنوان')}*\n\n"
                f"*وضعیت:* {task.get('status', 'N/A')}\n"
                f"*اولویت:* {task.get('priority', 'N/A')}\n"
                f"*تاریخ تحویل:* {due_date_str}\n\n"
                f"*توضیحات:*\n{task.get('content', 'توضیحاتی وجود ندارد.')}")
        keyboard = [
            [InlineKeyboardButton("✏️ ویرایش", callback_data=f"edit_task_{task_id}"), InlineKeyboardButton("🗑️ حذف", callback_data=f"delete_task_{task_id}")]
        ]
        if task.get('list_id'):
            keyboard.append([InlineKeyboardButton("↩️ بازگشت به تسک‌ها", callback_data=f"view_list_{task['list_id']}")])
        try:
            if isinstance(query_or_update, CallbackQuery):
                 await target.edit_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            else:
                 await target.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.warning(f"Error rendering task view: {e}")
    else:
        await (query_or_update.message if isinstance(query_or_update, CallbackQuery) else query_or_update).reply_text("تسک پیدا نشد.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    logger.info(f"Callback query received: {data}")
    parts = data.split('_')
    action = parts[0]
    
    if action == "correct" and parts[1] in ["status", "priority"]:
        correction_type = parts[1]
        selected_value = '_'.join(parts[2:])
        payload = context.chat_data.get('pending_task_payload')
        
        context.chat_data.pop('conversation_state', None)
        context.chat_data.pop('pending_task_payload', None)

        if not payload:
            await query.edit_message_text("❌ خطا: اطلاعات تسک یافت نشد. لطفاً دوباره تلاش کنید.")
            return

        payload[correction_type] = selected_value
        task_name = payload.get('task_name', 'بدون نام')
        
        await query.edit_message_text(f"در حال ساخت تسک '{task_name}' با {correction_type} اصلاح شده '{selected_value}'...")

        try:
            result = await tools._create_task_tool(update=update, context=context, **payload)
            if result:
                 final_message = result.get('message', 'عملیات با موفقیت انجام شد.')
                 if url := result.get('url'):
                     final_message += f"\nلینک تسک: {url}"
                 await query.edit_message_text(final_message)
        except Exception as e:
            logger.error(f"خطا در ساخت تسک پس از اصلاح: {e}", exc_info=True)
            await query.edit_message_text(f"❌ خطای غیرمنتظره: {e}")
        return

    keyboard = []
    text = "لطفاً انتخاب کنید:"
    back_button = None
    if action == "browse" and parts[1] == "spaces":
        text = "لیست فضاها:"
        spaces = await asyncio.to_thread(database.get_documents, config.SPACES_COLLECTION_ID)
        keyboard = [[InlineKeyboardButton(s['name'], callback_data=f"view_space_{s['clickup_space_id']}")] for s in spaces]
    elif action == "view":
        entity, entity_id = parts[1], '_'.join(parts[2:])
        if entity == "space":
            text = "لیست پوشه‌ها:"
            folders = await asyncio.to_thread(database.get_documents, config.FOLDERS_COLLECTION_ID, [database.Query.equal("space_id", [entity_id])])
            keyboard = [[InlineKeyboardButton(f['name'], callback_data=f"view_folder_{f['clickup_folder_id']}")] for f in folders]
            back_button = InlineKeyboardButton("↩️ بازگشت به فضاها", callback_data="browse_spaces")
        elif entity == "folder":
            text = "لیست لیست‌ها:"
            folder = await asyncio.to_thread(database.get_single_document, config.FOLDERS_COLLECTION_ID, 'clickup_folder_id', entity_id)
            lists = await asyncio.to_thread(database.get_documents, config.LISTS_COLLECTION_ID, [database.Query.equal("folder_id", [entity_id])])
            keyboard = [[InlineKeyboardButton(l['name'], callback_data=f"view_list_{l['clickup_list_id']}")] for l in lists]
            if folder and folder.get('space_id'):
                back_button = InlineKeyboardButton("↩️ بازگشت به پوشه‌ها", callback_data=f"view_space_{folder['space_id']}")
        elif entity == "list":
            text = "لیست تسک‌ها:"
            lst = await asyncio.to_thread(database.get_single_document, config.LISTS_COLLECTION_ID, 'clickup_list_id', entity_id)
            tasks = await asyncio.to_thread(database.get_documents, config.TASKS_COLLECTION_ID, [database.Query.equal("list_id", [entity_id])])
            keyboard = [[InlineKeyboardButton(t['title'], callback_data=f"view_task_{t['clickup_task_id']}")] for t in tasks]
            keyboard.append([InlineKeyboardButton("➕ ساخت تسک جدید", callback_data=f"newtask_in_list_{entity_id}")])
            if lst and lst.get('folder_id'):
                back_button = InlineKeyboardButton("↩️ بازگشت به لیست‌ها", callback_data=f"view_folder_{lst['folder_id']}")
        elif entity == "task":
            await render_task_view(query, entity_id)
            return
    elif action == "delete" and parts[1] == "task":
        task_id = '_'.join(parts[2:])
        text = "آیا از حذف این تسک مطمئن هستید؟"
        keyboard = [[InlineKeyboardButton("✅ بله", callback_data=f"confirm_delete_{task_id}")], [InlineKeyboardButton("❌ خیر", callback_data=f"view_task_{task_id}")]]
    elif action == "confirm" and parts[1] == "delete":
        task_id = '_'.join(parts[2:])
        await query.edit_message_text("در حال حذف تسک...")
        task = await asyncio.to_thread(database.get_single_document, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id)
        success = await asyncio.to_thread(clickup_api.delete_task_in_clickup, task_id)
        if success:
            await asyncio.to_thread(database.delete_document_by_clickup_id, config.TASKS_COLLECTION_ID, task_id)
            text = "✅ تسک با موفقیت حذف شد."
            if task and task.get('list_id'):
                back_button = InlineKeyboardButton("↩️ بازگشت به لیست تسک‌ها", callback_data=f"view_list_{task['list_id']}")
        else:
            text = "❌ حذف تسک ناموفق بود."
            back_button = InlineKeyboardButton("↩️ بازگشت به تسک", callback_data=f"view_task_{task_id}")
    if not keyboard and not back_button:
        text = "موردی برای نمایش پیدا نشد."
    if back_button:
        keyboard.append([back_button])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def new_task_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lists = await asyncio.to_thread(database.get_documents, config.LISTS_COLLECTION_ID)
    keyboard = [[InlineKeyboardButton(lst['name'], callback_data=f"select_list_{lst['clickup_list_id']}")] for lst in lists]
    keyboard.append([InlineKeyboardButton("لغو ❌", callback_data="cancel_conv")])
    await update.message.reply_text("لطفاً لیستی که تسک باید در آن ساخته شود را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_LIST

async def new_task_in_list_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    list_id = query.data.split('_')[-1]
    context.user_data['list_id'] = list_id
    lst = await asyncio.to_thread(database.get_single_document, config.LISTS_COLLECTION_ID, 'clickup_list_id', list_id)
    list_name = lst['name'] if lst else "انتخاب شده"
    await query.edit_message_text(text=f"ساخت تسک جدید در لیست: *{list_name}*\n\nلطفاً عنوان تسک را وارد کنید (می‌توانید با ارسال /cancel لغو کنید):", parse_mode='Markdown')
    return CREATE_TYPING_TITLE

async def list_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['list_id'] = query.data.split('_')[-1]
    await query.edit_message_text(text="عالی! حالا لطفاً عنوان تسک را وارد کنید (می‌توانید با ارسال /cancel لغو کنید):")
    return CREATE_TYPING_TITLE

async def title_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['title'] = update.message.text
    await update.message.reply_text("عنوان ذخیره شد. حالا توضیحات تسک را وارد کنید (می‌توانید با ارسال /skip از این مرحله عبور کنید):")
    return CREATE_TYPING_DESCRIPTION

async def description_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['description'] = update.message.text
    return await ask_for_status(update, context)

async def skip_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['description'] = ""
    return await ask_for_status(update, context)

async def ask_for_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    list_id = context.user_data['list_id']
    statuses = await asyncio.to_thread(clickup_api.get_list_statuses, list_id)
    keyboard = [[InlineKeyboardButton(status['status'], callback_data=f"select_status_{status['status']}")] for status in statuses]
    keyboard.append([InlineKeyboardButton("عبور ➡️", callback_data="select_status_skip")])
    await update.message.reply_text("وضعیت تسک را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_STATUS

async def status_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    status = query.data.split('_')[-1]
    context.user_data['status'] = status if status != 'skip' else None
    keyboard = [
        [InlineKeyboardButton("فوری", callback_data="priority_1"), InlineKeyboardButton("بالا", callback_data="priority_2")],
        [InlineKeyboardButton("متوسط", callback_data="priority_3"), InlineKeyboardButton("پایین", callback_data="priority_4")],
        [InlineKeyboardButton("عبور ➡️", callback_data="priority_skip")]
    ]
    await query.edit_message_text("اولویت تسک را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_PRIORITY

async def priority_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    priority = query.data.split('_')[1]
    context.user_data['priority'] = int(priority) if priority != 'skip' else None
    await query.edit_message_text("تاریخ پایان تسک را وارد کنید (مثال: 2025-12-31) یا با ارسال /skip عبور کنید:")
    return CREATE_TYPING_DUE_DATE

async def due_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        date_obj = datetime.strptime(update.message.text, "%Y-%m-%d")
        context.user_data['due_date'] = int(date_obj.timestamp() * 1000)
    except ValueError:
        await update.message.reply_text("فرمت تاریخ اشتباه است. لطفاً دوباره تلاش کنید (YYYY-MM-DD) یا /skip را بزنید.")
        return CREATE_TYPING_DUE_DATE
    return await ask_for_assignee(update, context)

async def skip_due_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['due_date'] = None
    return await ask_for_assignee(update, context)

async def ask_for_assignee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = await asyncio.to_thread(database.get_documents, config.USERS_COLLECTION_ID)
    keyboard = [[InlineKeyboardButton(user['username'], callback_data=f"select_user_{user['clickup_user_id']}")] for user in users]
    keyboard.append([InlineKeyboardButton("عبور ➡️", callback_data="select_user_skip")])
    message_text = "عالی! حالا مسئول انجام تسک را انتخاب کنید:"
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard))
    return CREATE_SELECTING_ASSIGNEE

async def assignee_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = context.user_data
    user_id = query.data.split('_')[-1]
    user_data['assignee_id'] = user_id if user_id != 'skip' else None

    await query.edit_message_text(text="در حال ساخت تسک با جزئیات کامل در کلیک‌آپ...")

    payload = {"name": user_data['title'], "description": user_data.get('description', '')}
    if user_data.get('assignee_id'): payload["assignees"] = [int(user_data['assignee_id'])]
    if user_data.get('status'): payload["status"] = user_data['status']
    if user_data.get('priority'): payload["priority"] = user_data['priority']
    if user_data.get('due_date'): payload["due_date"] = user_data['due_date']

    success, task_data = await asyncio.to_thread(clickup_api.create_task_in_clickup_api, user_data['list_id'], payload)

    if success:
        await asyncio.to_thread(clickup_api.sync_single_task_from_clickup, task_data['id'])
        await query.edit_message_text(text=f"✅ تسک با موفقیت ساخته شد! لینک: {task_data.get('url')}")
    else:
        await query.edit_message_text(text=f"❌ ساخت تسک ناموفق بود. خطا: {task_data.get('err', 'نامشخص')}")

    user_data.clear()
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.message.delete()
    if update.message:
        await start_command(update, context)
    context.user_data.clear()
    return ConversationHandler.END

async def unexpected_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles unexpected messages inside a conversation."""
    await update.message.reply_text(
        "در حال حاضر منتظر پاسخ دیگری از شما هستم. "
        "لطفاً از دکمه‌ها استفاده کنید یا اطلاعات خواسته‌شده را وارد کنید.\n"
        "برای خروج کامل می‌توانید از دستور /cancel استفاده کنید."
    )

def get_create_task_conv_handler():
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex('^➕ ساخت تسک جدید$'), new_task_entry),
            CallbackQueryHandler(new_task_in_list_start, pattern='^newtask_in_list_')
        ],
        states={
            CREATE_SELECTING_LIST: [CallbackQueryHandler(list_selected, pattern='^select_list_')],
            CREATE_TYPING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, title_received)],
            CREATE_TYPING_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, description_received), CommandHandler("skip", skip_description)],
            CREATE_SELECTING_STATUS: [CallbackQueryHandler(status_selected, pattern='^select_status_')],
            CREATE_SELECTING_PRIORITY: [CallbackQueryHandler(priority_selected, pattern='^priority_')],
            CREATE_TYPING_DUE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, due_date_received), CommandHandler("skip", skip_due_date)],
            CREATE_SELECTING_ASSIGNEE: [CallbackQueryHandler(assignee_selected, pattern='^select_user_')],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation), 
            CallbackQueryHandler(cancel_conversation, pattern='^cancel_conv$'),
            MessageHandler(filters.TEXT & ~filters.COMMAND, unexpected_message)
        ],
    )

# --- توابع مکالمه ویرایش تسک ---

async def show_edit_menu(update_or_message, context: ContextTypes.DEFAULT_TYPE, message_text: str):
    keyboard = [
        [InlineKeyboardButton("ویرایش عنوان", callback_data="edit_field_name"), InlineKeyboardButton("ویرایش توضیحات", callback_data="edit_field_description")],
        [InlineKeyboardButton("تغییر وضعیت", callback_data="edit_field_status"), InlineKeyboardButton("تغییر اولویت", callback_data="edit_field_priority")],
        [InlineKeyboardButton("تغییر تاریخ تحویل", callback_data="edit_field_due_date"), InlineKeyboardButton("تغییر مسئول تسک", callback_data="edit_field_assignees")],
        [InlineKeyboardButton("↩️ بازگشت به تسک", callback_data="back_to_task")]
    ]
    target_message = update_or_message.message if isinstance(update_or_message, CallbackQuery) else update_or_message.effective_message
    await target_message.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return EDIT_SELECTING_FIELD

async def edit_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task_id = '_'.join(query.data.split('_')[2:])
    context.user_data['edit_task_id'] = task_id
    task = await asyncio.to_thread(database.get_single_document, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id)
    if not task:
        await query.edit_message_text("خطا: تسک مورد نظر یافت نشد.")
        context.user_data.clear()
        return ConversationHandler.END
    context.user_data['task'] = task
    return await show_edit_menu(query, context, "کدام بخش را می‌خواهید ویرایش کنید؟")

async def edit_field_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    field_to_edit = '_'.join(query.data.split('_')[2:])
    context.user_data['field_to_edit'] = field_to_edit
    task = context.user_data.get('task')
    if not task:
        await query.edit_message_text("خطا: اطلاعات تسک در حافظه یافت نشد. لطفاً دوباره تلاش کنید.")
        context.user_data.clear()
        return ConversationHandler.END
    prompt_text = ""
    keyboard = []
    next_state = EDIT_SELECTING_VALUE
    if field_to_edit in ['name', 'description', 'due_date']:
        next_state = EDIT_TYPING_VALUE
        if field_to_edit == 'name':
            current_value = task.get('title', 'خالی')
            prompt_text = f"عنوان فعلی: *{current_value}*\n\nلطفاً عنوان جدید را وارد کنید (یا برای لغو /cancel را بفرستید):"
        elif field_to_edit == 'description':
            current_value = task.get('content', 'خالی')
            prompt_text = f"توضیحات فعلی: *{current_value}*\n\nلطفاً توضیحات جدید را وارد کنید (یا برای لغو /cancel را بفرستید):"
        elif field_to_edit == 'due_date':
            current_value = "تعیین نشده"
            if task.get('due_date'):
                try:
                    dt_object = datetime.fromtimestamp(int(task['due_date']) / 1000)
                    current_value = dt_object.strftime('%Y-%m-%d')
                except (ValueError, TypeError):
                    current_value = "نامشخص"
            prompt_text = f"تاریخ تحویل فعلی: *{current_value}*\n\nتاریخ جدید را وارد کنید (YYYY-MM-DD) یا /cancel را بفرستید:"
        await query.edit_message_text(text=prompt_text, reply_markup=None, parse_mode='Markdown')
    elif field_to_edit == 'status':
        list_id = task.get('list_id')
        statuses = await asyncio.to_thread(clickup_api.get_list_statuses, list_id)
        keyboard = [[InlineKeyboardButton(s['status'], callback_data=f"edit_value_{s['status']}")] for s in statuses]
        prompt_text = f"وضعیت فعلی: *{task.get('status', 'N/A')}*\n\nوضعیت جدید را انتخاب کنید:"
    elif field_to_edit == 'priority':
        keyboard = [
            [InlineKeyboardButton("فوری", callback_data="edit_value_1"), InlineKeyboardButton("بالا", callback_data="edit_value_2")],
            [InlineKeyboardButton("متوسط", callback_data="edit_value_3"), InlineKeyboardButton("پایین", callback_data="edit_value_4")],
            [InlineKeyboardButton("حذف اولویت", callback_data="edit_value_0")]
        ]
        prompt_text = f"اولویت فعلی: *{task.get('priority', 'N/A')}*\n\nاولویت جدید را انتخاب کنید:"
    elif field_to_edit == 'assignees':
        users = await asyncio.to_thread(database.get_documents, config.USERS_COLLECTION_ID)
        keyboard = [[InlineKeyboardButton(u['username'], callback_data=f"edit_value_{u['clickup_user_id']}")] for u in users]
        prompt_text = "مسئول جدید را انتخاب کنید:"
    if keyboard:
        keyboard.append([InlineKeyboardButton("❌ لغو", callback_data="cancel_edit_field")])
        await query.edit_message_text(prompt_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return next_state

async def process_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, new_value):
    query = update.callback_query
    editable_message = query.message if query else await update.message.reply_text("در حال پردازش...")
    await editable_message.edit_text("در حال به‌روزرسانی...")
    task_id = context.user_data['edit_task_id']
    field_to_edit = context.user_data['field_to_edit']
    payload = {}
    api_value = new_value
    if field_to_edit == 'priority':
        api_value = int(new_value)
    elif field_to_edit == 'assignees':
        api_value = {'add': [int(new_value)], 'rem': []}
    elif field_to_edit == 'due_date':
        parsed_timestamp = parse_due_date(new_value)
        if not parsed_timestamp:
            await editable_message.edit_text("❌ فرمت تاریخ نامعتبر است. لطفاً دوباره تلاش کنید.")
            return EDIT_TYPING_VALUE
        api_value = parsed_timestamp
    payload[field_to_edit] = api_value
    success, response_data = await asyncio.to_thread(clickup_api.update_task_in_clickup_api, task_id, payload)
    if success:
        asyncio.create_task(asyncio.to_thread(clickup_api.sync_single_task_from_clickup, task_id))
        task = context.user_data.get('task', {})
        if field_to_edit == 'name': task['title'] = new_value
        elif field_to_edit == 'description': task['content'] = new_value
        elif field_to_edit == 'status': task['status'] = new_value
        elif field_to_edit == 'due_date': task['due_date'] = api_value
        elif field_to_edit == 'priority':
            priority_val = int(new_value)
            priority_map_from_int = {1: "فوری", 2: "بالا", 3: "متوسط", 4: "پایین"}
            task['priority'] = priority_map_from_int.get(priority_val) if priority_val != 0 else None
        context.user_data['task'] = task
        return await show_edit_menu(editable_message, context, "✅ به‌روزرسانی موفق بود. مورد دیگری برای ویرایش هست؟")
    else:
        return await show_edit_menu(editable_message, context, f"❌ به‌روزرسانی ناموفق بود: {response_data.get('err', 'خطای نامشخص')}")

async def edit_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_edit(update, context, update.message.text)

async def edit_value_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await process_edit(update, context, query.data.split('_')[-1])

async def back_to_task_from_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query: await query.answer()
    task_id = context.user_data.get('edit_task_id')
    context.user_data.clear()
    if task_id:
        await render_task_view(query or update, task_id)
    return ConversationHandler.END

async def cancel_edit_and_return_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    message_text = "عملیات لغو شد. لطفاً بخش دیگری را برای ویرایش انتخاب کنید:"
    if query:
        await query.answer()
        return await show_edit_menu(query, context, message_text)
    elif update.message:
        keyboard = [
            [InlineKeyboardButton("ویرایش عنوان", callback_data="edit_field_name"), InlineKeyboardButton("ویرایش توضیحات", callback_data="edit_field_description")],
            [InlineKeyboardButton("تغییر وضعیت", callback_data="edit_field_status"), InlineKeyboardButton("تغییر اولویت", callback_data="edit_field_priority")],
            [InlineKeyboardButton("تغییر تاریخ تحویل", callback_data="edit_field_due_date"), InlineKeyboardButton("تغییر مسئول تسک", callback_data="edit_field_assignees")],
            [InlineKeyboardButton("↩️ بازگشت به تسک", callback_data="back_to_task")]
        ]
        await update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return EDIT_SELECTING_FIELD
    return ConversationHandler.END

def get_edit_task_conv_handler():
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
                CallbackQueryHandler(cancel_edit_and_return_to_menu, pattern='^cancel_edit_field$')
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            MessageHandler(filters.TEXT & ~filters.COMMAND, unexpected_message)
        ],
        per_user=True,
        per_chat=True,
    )

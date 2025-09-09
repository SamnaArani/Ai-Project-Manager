# -*- coding: utf-8 -*-
import requests
import logging
import config
import database

logger = logging.getLogger(__name__)

# --- توابع API پایه ---

def _make_request(url: str, token: str, method: str = 'GET', **kwargs) -> dict | None:
    """یک تابع کمکی برای ارسال درخواست به API کلیک‌اپ و مدیریت خطاها."""
    headers = {'Authorization': token, 'Content-Type': 'application/json'}
    try:
        response = requests.request(method, url, headers=headers, timeout=15, **kwargs)
        response.raise_for_status()
        if response.status_code == 204: # No Content
            return {}
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"خطا در درخواست API کلیک‌اپ به {url}: {e}")
        return None

def validate_token(token: str) -> dict | None:
    """
    توکن API کلیک‌اپ را اعتبارسنجی می‌کند.
    در صورت موفقیت، اطلاعات کاربر را برمی‌گرداند، در غیر این صورت None.
    """
    return _make_request("https://api.clickup.com/api/v2/user", token)

def delete_task_in_clickup(task_id: str, token: str) -> bool:
    response = _make_request(f"https://api.clickup.com/api/v2/task/{task_id}", token, 'DELETE')
    return response is not None

def create_task_in_clickup_api(list_id: str, payload: dict, token: str) -> tuple[bool, dict]:
    response = _make_request(f"https://api.clickup.com/api/v2/list/{list_id}/task", token, 'POST', json=payload)
    return response is not None, response or {}

def update_task_in_clickup_api(task_id: str, payload: dict, token: str) -> tuple[bool, dict]:
    response = _make_request(f"https://api.clickup.com/api/v2/task/{task_id}", token, 'PUT', json=payload)
    return response is not None, response or {}

def get_list_statuses(list_id: str, token: str) -> list:
    response = _make_request(f"https://api.clickup.com/api/v2/list/{list_id}", token)
    return response.get('statuses', []) if response else []

def get_tasks_from_clickup_list(list_id: str, token: str) -> list:
    response = _make_request(f"https://api.clickup.com/api/v2/list/{list_id}/task?archived=false&include_closed=true", token)
    return response.get('tasks', []) if response else []

def get_teams(token: str) -> list:
    response = _make_request("https://api.clickup.com/api/v2/team", token)
    return response.get('teams', []) if response else []

def get_team_members(team_id: str, token: str) -> list:
    """اعضای یک تیم مشخص را از کلیک‌اپ دریافت می‌کند."""
    team_data = get_teams(token)
    for team in team_data:
        if str(team['id']) == team_id:
            return [member['user'] for member in team.get('members', [])]
    return []

def get_spaces(team_id: str, token: str) -> list:
    response = _make_request(f"https://api.clickup.com/api/v2/team/{team_id}/space?archived=false", token)
    return response.get('spaces', []) if response else []

def get_folders(space_id: str, token: str) -> list:
    response = _make_request(f"https://api.clickup.com/api/v2/space/{space_id}/folder?archived=false", token)
    return response.get('folders', []) if response else []

def get_lists(folder_id: str, token: str) -> list:
    response = _make_request(f"https://api.clickup.com/api/v2/folder/{folder_id}/list?archived=false", token)
    return response.get('lists', []) if response else []

def get_folderless_lists(space_id: str, token: str) -> list:
    """لیست‌های بدون پوشه را در یک فضا دریافت می‌کند."""
    response = _make_request(f"https://api.clickup.com/api/v2/space/{space_id}/list?archived=false", token)
    return response.get('lists', []) if response else []

# --- توابع قالب‌بندی دیتا ---

def _format_space_data(space: dict) -> dict:
    return {'clickup_space_id': str(space.get('id')), 'name': space.get('name')}

def _format_folder_data(folder: dict, space_id: str) -> dict:
    return {'clickup_folder_id': str(folder.get('id')), 'name': folder.get('name'), 'space_id': str(space_id)}

def _format_list_data(lst: dict, folder_id: str | None = None) -> dict:
    data = {'clickup_list_id': str(lst.get('id')), 'name': lst.get('name')}
    if folder_id:
        data['folder_id'] = str(folder_id)
    return data

def _format_task_data(task: dict) -> dict:
    priority_map_from_int = {1: "فوری", 2: "بالا", 3: "متوسط", 4: "پایین"}
    priority_string = "خالی"
    priority_data = task.get('priority')
    if priority_data:
        if isinstance(priority_data, dict):
            priority_string = priority_data.get('priority', 'خالی')
        elif isinstance(priority_data, (str, int)):
             try:
                priority_val = int(priority_data)
                priority_string = priority_map_from_int.get(priority_val, 'خالی')
             except (ValueError, TypeError):
                priority_string = "خالی"
    
    list_obj = task.get('list', {})
    
    # [FIX] Ensure date fields are integers or None
    def to_int_timestamp(date_val):
        if date_val is None:
            return None
        try:
            return int(date_val)
        except (ValueError, TypeError):
            return None # Return None if conversion fails
    
    data = {
        'clickup_task_id': str(task.get('id')), 
        'title': task.get('name'),
        'status': task.get('status', {}).get('status'), 
        'list_id': str(list_obj.get('id')) if list_obj and list_obj.get('id') else None,
        'priority': priority_string,
        'content': task.get('description') or task.get('text_content') or '',
        'start_date': to_int_timestamp(task.get('start_date')),
        'due_date': to_int_timestamp(task.get('due_date'))
    }
    if assignees := task.get('assignees', []):
        if assignees and assignees[0]:
            data['assignee_name'] = assignees[0].get('username')
    
    return {k: v for k, v in data.items()}

# --- توابع همگام‌سازی ---

def sync_single_task_from_clickup(task_id: str, token: str, telegram_id: str):
    response = _make_request(f"https://api.clickup.com/api/v2/task/{task_id}", token)
    if response:
        task_data = _format_task_data(response)
        task_data['telegram_id'] = telegram_id
        database.upsert_document(config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_data['clickup_task_id'], task_data)
        logger.info(f"تسک {task_id} برای کاربر {telegram_id} همگام‌سازی شد.")
        return database.get_single_document(config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id)
    return None

def sync_tasks_for_list(list_id: str, token: str, telegram_id: str) -> int:
    logger.info(f"شروع همگام‌سازی تسک‌ها برای لیست {list_id}...")
    clickup_tasks = get_tasks_from_clickup_list(list_id, token)
    if not clickup_tasks: 
        logger.info(f"هیچ تسکی در لیست {list_id} یافت نشد.")
        return 0
    
    count = 0
    for task in clickup_tasks:
        try:
            task_data = _format_task_data(task)
            task_data['telegram_id'] = telegram_id
            database.upsert_document(config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_data['clickup_task_id'], task_data)
            count += 1
        except Exception as e: 
            logger.error(f"خطا در همگام‌سازی تسک {task.get('id')}: {e}", exc_info=True)
            
    logger.info(f"{count} تسک از لیست {list_id} برای کاربر {telegram_id} همگام‌سازی شد.")
    return len(clickup_tasks)

def sync_all_user_data(token: str, telegram_id: str) -> bool:
    logger.info(f"شروع همگام‌سازی ساختار ClickUp برای کاربر {telegram_id}...")
    teams = get_teams(token)
    if not teams:
        logger.error(f"هیچ تیمی برای توکن کاربر {telegram_id} یافت نشد.")
        return False
    logger.info(f"تعداد {len(teams)} تیم یافت شد.")

    for team in teams:
        team_id = str(team['id'])
        members = get_team_members(team_id, token)
        for member in members:
            username = member.get('username')
            if not username:
                username = f"کاربر مهمان ({member.get('id')})"
                logger.warning(f"کاربر با شناسه {member.get('id')} نام کاربری ندارد. نام پیش‌فرض '{username}' اختصاص داده شد.")

            user_data = {
                'clickup_user_id': str(member['id']),
                'username': username,
                'email': member.get('email', ''),
                'telegram_id': telegram_id
            }
            database.upsert_document(config.APPWRITE_DATABASE_ID, config.CLICKUP_USERS_COLLECTION_ID, 'clickup_user_id', str(member['id']), user_data)
        
        spaces = get_spaces(team_id, token)
        for space in spaces:
            space_id = str(space['id'])
            space_data = _format_space_data(space)
            space_data['telegram_id'] = telegram_id
            database.upsert_document(config.APPWRITE_DATABASE_ID, config.SPACES_COLLECTION_ID, 'clickup_space_id', space_id, space_data)
            
            folders = get_folders(space_id, token)
            for folder in folders:
                folder_id = str(folder['id'])
                folder_data = _format_folder_data(folder, space_id)
                folder_data['telegram_id'] = telegram_id
                database.upsert_document(config.APPWRITE_DATABASE_ID, config.FOLDERS_COLLECTION_ID, 'clickup_folder_id', folder_id, folder_data)
                
                lists_in_folder = get_lists(folder_id, token)
                for lst in lists_in_folder:
                    list_id = str(lst['id'])
                    list_data = _format_list_data(lst, folder_id)
                    list_data['telegram_id'] = telegram_id
                    database.upsert_document(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, 'clickup_list_id', list_id, list_data)
                    sync_tasks_for_list(list_id, token, telegram_id)

            folderless_lists = get_folderless_lists(space_id, token)
            for lst in folderless_lists:
                 list_id = str(lst['id'])
                 list_data = _format_list_data(lst)
                 list_data['telegram_id'] = telegram_id
                 database.upsert_document(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, 'clickup_list_id', list_id, list_data)
                 sync_tasks_for_list(list_id, token, telegram_id)

    logger.info(f"همگام‌سازی ساختار ClickUp برای کاربر {telegram_id} با موفقیت به پایان رسید.")
    return True


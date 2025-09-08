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
    response = _make_request(f"https://api.clickup.com/api/v2/list/{list_id}/task", token)
    return response.get('tasks', []) if response else []

def get_teams(token: str) -> list:
    response = _make_request("https://api.clickup.com/api/v2/team", token)
    return response.get('teams', []) if response else []

def get_team_members(team_id: str, token: str) -> list:
    """اعضای یک تیم مشخص را از کلیک‌اپ دریافت می‌کند."""
    team_data = get_teams(token)
    for team in team_data:
        if team['id'] == team_id:
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
    return {'clickup_space_id': space.get('id'), 'name': space.get('name')}

def _format_folder_data(folder: dict, space_id: str) -> dict:
    return {'clickup_folder_id': folder.get('id'), 'name': folder.get('name'), 'space_id': space_id}

def _format_list_data(lst: dict, folder_id: str | None = None) -> dict:
    """داده‌های لیست را برای ذخیره‌سازی در دیتابیس قالب‌بندی می‌کند."""
    data = {'clickup_list_id': lst.get('id'), 'name': lst.get('name')}
    if folder_id:
        data['folder_id'] = folder_id
    
    # لایه حفاظتی: اطمینان از اینکه فیلد مشکل‌ساز هرگز وجود نخواهد داشت
    data.pop('space_id', None) 
    
    # لاگ تشخیصی برای بررسی داده‌ها قبل از ارسال
    logger.info(f"داده فرمت‌شده برای لیست '{data.get('name')}': Keys={list(data.keys())}")
    return data

def _format_task_data(task: dict) -> dict:
    priority_map = {1: "فوری", 2: "بالا", 3: "متوسط", 4: "پایین"}
    priority_str = "متوسط"
    if p := task.get('priority'):
        try: priority_str = priority_map.get(int(p.get('priority')), "متوسط")
        except (ValueError, TypeError, AttributeError): pass
    
    data = {'clickup_task_id': task.get('id'), 'title': task.get('name'), 'status': (task.get('status') or {}).get('status'), 'list_id': (task.get('list') or {}).get('id'), 'priority': priority_str, 'content': task.get('description') or task.get('text_content') or '', 'start_date': task.get('start_date'), 'due_date': task.get('due_date')}
    if assignees := task.get('assignees'): data['assignee_name'] = assignees[0].get('username')
    return {k: v for k, v in data.items() if v is not None}

# --- توابع همگام‌سازی ---

def sync_single_task_from_clickup(task_id: str, token: str):
    response = _make_request(f"https://api.clickup.com/api/v2/task/{task_id}", token)
    if response:
        task_data = _format_task_data(response)
        database.upsert_document(config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_data['clickup_task_id'], task_data)
        logger.info(f"تسک {task_id} همگام‌سازی شد.")
        return database.get_single_document(config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id)
    return None

def sync_tasks_for_list(list_id: str, token: str) -> int:
    clickup_tasks = get_tasks_from_clickup_list(list_id, token)
    if not clickup_tasks: return 0
    
    for task in clickup_tasks:
        try:
            task_data = _format_task_data(task)
            database.upsert_document(config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_data['clickup_task_id'], task_data)
        except Exception as e: logger.error(f"خطا در همگام‌سازی تسک {task.get('id')}: {e}")
            
    logger.info(f"همگام‌سازی برای لیست {list_id} کامل شد.")
    return len(clickup_tasks)

def sync_all_user_data(token: str) -> bool:
    """ساختار کلیک‌اپ (فضاها، پوشه‌ها، لیست‌ها) را همگام‌سازی می‌کند."""
    logger.info("شروع همگام‌سازی ساختار ClickUp...")
    teams = get_teams(token)
    if not teams:
        logger.error("هیچ تیمی برای این توکن یافت نشد.")
        return False
    logger.info(f"تعداد {len(teams)} تیم یافت شد.")

    for team in teams:
        team_id = team['id']
        logger.info(f"درحال پردازش تیم: {team.get('name')} ({team_id})")
        
        spaces = get_spaces(team_id, token)
        logger.info(f"تعداد {len(spaces)} فضا در این تیم یافت شد.")
        for space in spaces:
            space_id = space['id']
            logger.info(f"  درحال پردازش فضا: {space.get('name')} ({space_id})")
            database.upsert_document(config.APPWRITE_DATABASE_ID, config.SPACES_COLLECTION_ID, 'clickup_space_id', space_id, _format_space_data(space))
            
            folders = get_folders(space_id, token)
            logger.info(f"  تعداد {len(folders)} پوشه در این فضا یافت شد.")
            for folder in folders:
                folder_id = folder['id']
                logger.info(f"    درحال پردازش پوشه: {folder.get('name')} ({folder_id})")
                database.upsert_document(config.APPWRITE_DATABASE_ID, config.FOLDERS_COLLECTION_ID, 'clickup_folder_id', folder_id, _format_folder_data(folder, space_id))
                
                lists_in_folder = get_lists(folder_id, token)
                logger.info(f"    تعداد {len(lists_in_folder)} لیست در این پوشه یافت شد.")
                for lst in lists_in_folder:
                    logger.info(f"      درحال پردازش لیست: {lst.get('name')} ({lst.get('id')})")
                    database.upsert_document(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, 'clickup_list_id', lst['id'], _format_list_data(lst, folder_id))
            
            folderless_lists = get_folderless_lists(space_id, token)
            logger.info(f"  تعداد {len(folderless_lists)} لیست بدون پوشه در این فضا یافت شد.")
            for lst in folderless_lists:
                 logger.info(f"    درحال پردازش لیست بدون پوشه: {lst.get('name')} ({lst.get('id')})")
                 database.upsert_document(config.APPWRITE_DATABASE_ID, config.LISTS_COLLECTION_ID, 'clickup_list_id', lst['id'], _format_list_data(lst))

    logger.info("همگام‌سازی ساختار ClickUp با موفقیت به پایان رسید.")
    return True


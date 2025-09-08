# clickup_api.py

import requests
import logging
import config
import database

logger = logging.getLogger(__name__)

# --- توابع API پایه ---

def _get_user_clickup_token(user_id: str) -> str:
    """توکن کلیک‌آپ کاربر را از دیتابیس دریافت می‌کند."""
    user_doc = database.get_single_document(config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, 'telegram_id', user_id)
    if user_doc and user_doc.get('clickup_token'):
        return user_doc['clickup_token']
    raise ValueError(f"توکن کلیک‌آپ برای کاربر {user_id} یافت نشد.")

def delete_task_in_clickup(task_id: str, user_id: str) -> bool:
    """تسک را از ClickUp حذف می‌کند."""
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    try:
        headers = {'Authorization': _get_user_clickup_token(user_id)}
        response = requests.delete(url, headers=headers)
        return response.status_code == 204
    except ValueError as e:
        logger.error(f"خطا در حذف تسک: {e}")
        return False

def create_task_in_clickup_api(list_id: str, payload: dict, user_id: str) -> tuple[bool, dict]:
    """تسک جدید در ClickUp ایجاد می‌کند."""
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    try:
        headers = {'Authorization': _get_user_clickup_token(user_id), 'Content-Type': 'application/json'}
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            logger.info(f"تسک با موفقیت در لیست {list_id} برای کاربر {user_id} ساخته شد.")
            return True, response.json()
        else:
            logger.error(f"خطا در ساخت تسک در کلیک‌آپ. وضعیت: {response.status_code}, پاسخ: {response.text}")
            return False, response.json()
    except ValueError as e:
        logger.error(f"خطا در ساخت تسک: {e}")
        return False, {"err": str(e)}


def update_task_in_clickup_api(task_id: str, payload: dict, user_id: str) -> tuple[bool, dict]:
    """تسک موجود در ClickUp را به‌روزرسانی می‌کند."""
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    try:
        headers = {'Authorization': _get_user_clickup_token(user_id), 'Content-Type': 'application/json'}
        response = requests.put(url, headers=headers, json=payload)
        return response.status_code == 200, response.json()
    except ValueError as e:
        logger.error(f"خطا در به‌روزرسانی تسک: {e}")
        return False, {"err": str(e)}

def get_list_statuses(list_id: str, user_id: str) -> list:
    """وضعیت‌های یک لیست را از ClickUp دریافت می‌کند."""
    url = f"https://api.clickup.com/api/v2/list/{list_id}"
    try:
        headers = {'Authorization': _get_user_clickup_token(user_id)}
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json().get('statuses', [])
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"خطا در دریافت وضعیت‌های لیست {list_id} از ClickUp: {e}")
        return []

# --- توابع همگام‌سازی ---

def get_all_entities(user_id: str, endpoint: str, collection_id: str, entity_type: str):
    """تابع کمکی عمومی برای دریافت و ذخیره تمام موجودیت‌ها (spaces, folders, lists)"""
    try:
        headers = {'Authorization': _get_user_clickup_token(user_id)}
        url = f"https://api.clickup.com/api/v2/{endpoint}"
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        entities = response.json().get(entity_type, [])
        for entity in entities:
            data = {
                'clickup_id': entity['id'],
                'name': entity['name']
            }
            if entity_type == 'folders' and 'space' in entity:
                data['space_id'] = entity['space']['id']
            elif entity_type == 'lists' and 'folder' in entity:
                data['folder_id'] = entity['folder']['id']
            
            database.upsert_document(collection_id, 'clickup_id', entity['id'], data, user_id=user_id)
            logger.info(f"موجودیت {entity_type} با ID {entity['id']} برای کاربر {user_id} همگام‌سازی شد.")
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"خطا در همگام‌سازی موجودیت‌های {entity_type} برای کاربر {user_id}: {e}")

def get_all_users(user_id: str):
    """تمام کاربران تیم کلیک‌آپ را دریافت و ذخیره می‌کند."""
    try:
        headers = {'Authorization': _get_user_clickup_token(user_id)}
        url = "https://api.clickup.com/api/v2/team"
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        teams = response.json().get('teams', [])
        if not teams: return
        
        team_id = teams[0]['id']
        members_url = f"https://api.clickup.com/api/v2/team/{team_id}/user"
        members_response = requests.get(members_url, headers=headers)
        members_response.raise_for_status()
        
        users = members_response.json().get('members', [])
        for user in users:
            user_data = {
                'clickup_user_id': user['id'],
                'username': user['username']
            }
            database.upsert_document(config.CLICKUP_USERS_COLLECTION_ID, 'clickup_user_id', user['id'], user_data, user_id=user_id)
            logger.info(f"کاربر کلیک‌آپ با ID {user['id']} برای کاربر {user_id} همگام‌سازی شد.")
            
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"خطا در همگام‌سازی کاربران کلیک‌آپ برای کاربر {user_id}: {e}")


def _format_task_data(task: dict, user_id: str) -> dict:
    """دیتای تسک را از فرمت ClickUp به فرمت دیتابیس محلی تبدیل می‌کند."""
    priority_map_from_int = {1: "فوری", 2: "بالا", 3: "متوسط", 4: "پایین"}
    priority_string = "متوسط"
    if priority_data := task.get('priority'):
        try:
            priority_int = int(priority_data['priority'])
            priority_string = priority_map_from_int.get(priority_int, "متوسط")
        except (ValueError, TypeError, KeyError):
            logger.warning(f"اولویت نامعتبر ({priority_data}) برای تسک {task.get('id')} دریافت شد.")
    
    data = {
        'clickup_task_id': task.get('id'), 
        'title': task.get('name'),
        'status': task.get('status', {}).get('status'), 
        'list_id': task.get('list', {}).get('id'),
        'priority': priority_string,
        'content': task.get('description') or task.get('text_content') or '',
        'start_date': task.get('start_date'),
        'due_date': task.get('due_date'),
        'user_id': user_id # اضافه کردن user_id برای فیلتر
    }
    if assignees := task.get('assignees', []):
        data['assignee_name'] = assignees[0].get('username')
    
    return {k: v for k, v in data.items() if v is not None}

def sync_single_task_from_clickup(task_id: str, user_id: str):
    """یک تسک را از ClickUp دریافت و در دیتابیس محلی ذخیره یا به‌روزرسانی می‌کند."""
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    try:
        headers = {'Authorization': _get_user_clickup_token(user_id)}
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        task_data = _format_task_data(response.json(), user_id)
        
        database.upsert_document(config.TASKS_COLLECTION_ID, 'clickup_task_id', task_data['clickup_task_id'], task_data, user_id=user_id)
        logger.info(f"تسک {task_id} برای کاربر {user_id} با موفقیت همگام‌سازی شد.")
        return database.get_single_document_by_user(config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id, user_id)
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"خطا در همگام‌سازی تسک {task_id} از ClickUp برای کاربر {user_id}: {e}")
    except Exception as e:
        logger.error(f"خطای عمومی در همگام‌سازی تسک {task_id} برای کاربر {user_id}: {e}")
    return None

def sync_tasks_for_list(list_id: str, user_id: str) -> int:
    """تمام تسک‌های یک لیست را بین ClickUp و دیتابیس محلی همگام‌سازی می‌کند."""
    logger.info(f"شروع همگام‌سازی کامل برای لیست {list_id} و کاربر {user_id}...")
    try:
        headers = {'Authorization': _get_user_clickup_token(user_id)}
        url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        clickup_tasks = response.json().get('tasks', [])
        
        if not clickup_tasks:
            logger.warning(f"هیچ تسکی در لیست {list_id} برای همگام‌سازی یافت نشد.")
            return 0
        
        for task in clickup_tasks:
            try:
                task_data = _format_task_data(task, user_id)
                database.upsert_document(
                    config.TASKS_COLLECTION_ID,
                    'clickup_task_id',
                    task_data['clickup_task_id'],
                    task_data,
                    user_id=user_id
                )
            except Exception as e:
                logger.error(f"خطا در همگام‌سازی تسک {task.get('id')} در لیست {list_id} برای کاربر {user_id}: {e}")
                
        logger.info(f"همگام‌سازی برای لیست {list_id} کامل شد. {len(clickup_tasks)} تسک پردازش شد.")
        return len(clickup_tasks)
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"خطا در دریافت تسک‌ها از لیست {list_id} از ClickUp برای کاربر {user_id}: {e}")
        return 0

def get_all_lists(user_id: str) -> list:
    """تمام لیست‌های یک کاربر را از دیتابیس محلی دریافت می‌کند."""
    return database.get_documents_by_user(config.LISTS_COLLECTION_ID, user_id)

def sync_all_data_for_user(user_id: str):
    """
    تمام اطلاعات (فضاهای کاری، فولدرها، لیست‌ها، کاربران و تسک‌ها)
    را برای یک کاربر از ClickUp همگام‌سازی می‌کند.
    """
    try:
        logger.info(f"شروع همگام‌سازی کامل اطلاعات کلیک‌آپ برای کاربر {user_id}...")
        
        # گام 1: همگام‌سازی کاربران
        get_all_users(user_id)

        # گام 2: همگام‌سازی فضاها (Spaces)
        get_all_entities(user_id, "space", config.SPACES_COLLECTION_ID, "spaces")

        # گام 3: همگام‌سازی فولدرها (Folders)
        get_all_entities(user_id, "folder", config.FOLDERS_COLLECTION_ID, "folders")

        # گام 4: همگام‌سازی لیست‌ها (Lists)
        get_all_entities(user_id, "list", config.LISTS_COLLECTION_ID, "lists")
        
        # گام 5: همگام‌سازی تسک‌ها (Tasks)
        all_lists = get_all_lists(user_id)
        for lst in all_lists:
            sync_tasks_for_list(lst['clickup_id'], user_id)
            
        logger.info(f"همگام‌سازی کامل برای کاربر {user_id} با موفقیت به پایان رسید.")
        return True
    except Exception as e:
        logger.error(f"خطا در همگام‌سازی کامل اطلاعات برای کاربر {user_id}: {e}", exc_info=True)
        return False

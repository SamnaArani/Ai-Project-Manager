# clickup_api.py

import requests
import logging
import config
import database

logger = logging.getLogger(__name__)

# --- توابع API پایه ---

def delete_task_in_clickup(task_id: str) -> bool:
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    headers = {'Authorization': config.CLICKUP_API_TOKEN}
    response = requests.delete(url, headers=headers)
    return response.status_code == 204

def create_task_in_clickup_api(list_id: str, payload: dict) -> tuple[bool, dict]:
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    headers = {'Authorization': config.CLICKUP_API_TOKEN, 'Content-Type': 'application/json'}
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        logger.info(f"تسک با موفقیت در لیست {list_id} ساخته شد.")
        return True, response.json()
    else:
        logger.error(f"خطا در ساخت تسک در کلیک‌آپ. وضعیت: {response.status_code}, پاسخ: {response.text}")
        return False, response.json()

def update_task_in_clickup_api(task_id: str, payload: dict) -> tuple[bool, dict]:
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    headers = {'Authorization': config.CLICKUP_API_TOKEN, 'Content-Type': 'application/json'}
    response = requests.put(url, headers=headers, json=payload)
    return response.status_code == 200, response.json()

def get_list_statuses(list_id: str) -> list:
    url = f"https://api.clickup.com/api/v2/list/{list_id}"
    headers = {'Authorization': config.CLICKUP_API_TOKEN}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json().get('statuses', [])
    except requests.exceptions.RequestException as e:
        logger.error(f"خطا در دریافت وضعیت‌های لیست {list_id} از ClickUp: {e}")
        return []

def get_tasks_from_clickup_list(list_id: str) -> list:
    """تمام تسک‌های یک لیست مشخص را از ClickUp دریافت می‌کند."""
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    headers = {'Authorization': config.CLICKUP_API_TOKEN}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json().get('tasks', [])
    except requests.exceptions.RequestException as e:
        logger.error(f"خطا در دریافت تسک‌ها از لیست {list_id} در ClickUp: {e}")
        return []

# --- توابع همگام‌سازی ---

def _format_task_data(task: dict) -> dict:
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
        'due_date': task.get('due_date')
    }
    if assignees := task.get('assignees', []):
        data['assignee_name'] = assignees[0].get('username')
    
    return {k: v for k, v in data.items() if v is not None}

def sync_single_task_from_clickup(task_id: str):
    """یک تسک را از ClickUp دریافت و در دیتابیس محلی ذخیره یا به‌روزرسانی می‌کند."""
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    headers = {'Authorization': config.CLICKUP_API_TOKEN}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        task_data = _format_task_data(response.json())
        
        database.upsert_document(
            database_id=config.APPWRITE_DATABASE_ID, 
            collection_id=config.TASKS_COLLECTION_ID, 
            query_key='clickup_task_id', 
            query_value=task_data['clickup_task_id'], 
            data=task_data
        )
        logger.info(f"تسک {task_id} با موفقیت همگام‌سازی شد.")
        return database.get_single_document(config.APPWRITE_DATABASE_ID, config.TASKS_COLLECTION_ID, 'clickup_task_id', task_id)
    except requests.exceptions.RequestException as e:
        logger.error(f"خطا در همگام‌سازی تسک {task_id} از ClickUp: {e}")
    except Exception as e:
        logger.error(f"خطای عمومی در همگام‌سازی تسک {task_id}: {e}")
    return None

def sync_tasks_for_list(list_id: str) -> int:
    """تمام تسک‌های یک لیست را بین ClickUp و دیتابیس محلی همگام‌سازی می‌کند."""
    logger.info(f"شروع همگام‌سازی کامل برای لیست {list_id}...")
    clickup_tasks = get_tasks_from_clickup_list(list_id)
    if not clickup_tasks:
        logger.warning(f"هیچ تسکی در لیست {list_id} برای همگام‌سازی یافت نشد.")
        return 0
    
    for task in clickup_tasks:
        try:
            task_data = _format_task_data(task)
            database.upsert_document(
                database_id=config.APPWRITE_DATABASE_ID,
                collection_id=config.TASKS_COLLECTION_ID,
                query_key='clickup_task_id',
                query_value=task_data['clickup_task_id'],
                data=task_data
            )
        except Exception as e:
            logger.error(f"خطا در همگام‌سازی تسک {task.get('id')} در لیست {list_id}: {e}")
            
    logger.info(f"همگام‌سازی برای لیست {list_id} کامل شد. {len(clickup_tasks)} تسک پردازش شد.")
    return len(clickup_tasks)


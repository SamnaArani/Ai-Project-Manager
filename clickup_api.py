# clickup_api.py

import requests
import logging
import config
import database

logger = logging.getLogger(__name__)

# سایر توابع (delete_task, create_task, etc.) را بدون تغییر نگه دارید...
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


def sync_single_task_from_clickup(task_id: str):
    """
    یک تسک را از ClickUp دریافت و در دیتابیس محلی (Appwrite) ذخیره یا به‌روزرسانی می‌کند.
    این تابع اصلاح شده است تا database_id را به درستی ارسال کند.
    """
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    headers = {'Authorization': config.CLICKUP_API_TOKEN}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        task = response.json()
        
        priority_map_from_int = {1: "فوری", 2: "بالا", 3: "متوسط", 4: "پایین"}
        priority_string = "متوسط"

        priority_data = task.get('priority')
        if priority_data and 'priority' in priority_data:
            try:
                priority_int = int(priority_data['priority'])
                priority_string = priority_map_from_int.get(priority_int, "متوسط")
            except (ValueError, TypeError, KeyError):
                logger.warning(f"اولویت نامعتبر از کلیک‌آپ دریافت شد. مقدار پیش‌فرض 'متوسط' استفاده می‌شود.")

        data = {
            'clickup_task_id': task.get('id'), 
            'title': task.get('name'),
            'status': task.get('status', {}).get('status'), 
            'list_id': task.get('list', {}).get('id'),
            'priority': priority_string,
            'content': task.get('description') or task.get('text_content') or '',
            'due_date': task.get('due_date')
        }
        assignees = task.get('assignees', [])
        if assignees:
            data['assignee_name'] = assignees[0].get('username')
        
        data_to_save = {k: v for k, v in data.items() if v is not None}
        
        if 'priority' not in data_to_save:
            data_to_save['priority'] = "متوسط"

        # --- اصلاح اصلی ---
        # database_id به فراخوانی اضافه شد تا مشخص شود در کدام دیتابیس ذخیره شود.
        database.upsert_document(
            database_id=config.APPWRITE_DATABASE_ID, 
            collection_id=config.TASKS_COLLECTION_ID, 
            key_field='clickup_task_id', 
            key_value=task.get('id'), 
            data=data_to_save
        )
        
        logger.info(f"تسک {task_id} با موفقیت همگام‌سازی شد.")
        
        # --- اصلاح ثانویه ---
        # database_id اینجا هم اضافه شد.
        return database.get_single_document(
            collection_id=config.TASKS_COLLECTION_ID, 
            key_field='clickup_task_id', 
            key_value=task_id,
            database_id=config.APPWRITE_DATABASE_ID
        )
    except requests.exceptions.RequestException as e:
        logger.error(f"خطا در همگام‌سازی تسک {task_id} از ClickUp: {e}")
        return None
    except Exception as e:
        logger.error(f"خطای عمومی در همگام‌سازی تسک {task_id}: {e}")
        return None

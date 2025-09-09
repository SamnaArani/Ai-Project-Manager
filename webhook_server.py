import logging
import asyncio
from aiohttp import web
import config
import clickup_api
import database

logger = logging.getLogger(__name__)

async def clickup_webhook_handler(request: web.Request):
    if not request.body_exists:
        return web.Response(status=400, text="Bad Request: No data received.")
    
    data = await request.json()
    event = data.get('event')
    task_id = data.get('task_id')
    history_items = data.get('history_items', [])
    
    # برای وب‌هوک‌های کلیک‌اپ، توکن ثابتی وجود ندارد، پس از توکن ادمین استفاده می‌کنیم
    # شما باید یک مکانیزم برای یافتن کاربر مربوطه پیاده‌سازی کنید اگر نیاز است
    admin_token = None # Needs a way to get a valid token
    # For now, we might have to skip operations requiring a specific user token if one isn't available
    
    logger.info(f"دریافت وب‌هوک از کلیک‌آپ: event={event}, task_id={task_id}")

    if event in ['taskCreated', 'taskUpdated'] and task_id:
        # Note: This will fail if a token is required and not provided.
        # A robust solution needs a way to map webhook to a user/token.
        # For now, let's assume a global token might be used for generic syncs if available
        # But the new clickup_api requires it. This part needs re-thinking in a real app.
        logger.warning("Webhook received for task creation/update, but no user token is available. Sync might fail.")
        # Example placeholder: find a default user/token to perform the sync
        # user_doc = database.get_single_document(...)
        # if user_doc and user_doc.get('clickup_token'):
        #     await asyncio.to_thread(clickup_api.sync_single_task_from_clickup, task_id, user_doc['clickup_token'])

    elif event == 'taskDeleted' and task_id:
        try:
            # Deleting from local DB doesn't require a token
            await asyncio.to_thread(
                database.delete_document_by_clickup_id, 
                config.APPWRITE_DATABASE_ID, 
                config.TASKS_COLLECTION_ID, 
                'clickup_task_id', 
                task_id
            )
        except Exception as e:
            logger.error(f"خطا در حذف تسک {task_id} از طریق وب‌هوک: {e}", exc_info=True)
    
    return web.Response(status=200)

async def run_webhook_server():
    app = web.Application()
    app.add_routes([web.post('/clickup-webhook', clickup_webhook_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', 8080)
    await site.start()
    logger.info("وب‌سرور برای وب‌هوک‌ها در http://localhost:8080 اجرا شد.")
    await asyncio.Event().wait()

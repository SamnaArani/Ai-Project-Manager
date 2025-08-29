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
    
    logger.info(f"دریافت وب‌هوک از کلیک‌آپ: event={event}, task_id={task_id}")
    logger.debug(f"اطلاعات کامل وب‌هوک: {data}")

    if event in ['taskCreated', 'taskUpdated'] and task_id:
        try:
            # اجرای همگام‌سازی در یک ترد جداگانه برای جلوگیری از بلاک شدن
            await asyncio.to_thread(clickup_api.sync_single_task_from_clickup, task_id)
        except Exception as e:
            logger.error(f"خطا در همگام‌سازی تسک {task_id} از طریق وب‌هوک: {e}", exc_info=True)

    elif event == 'taskDeleted' and task_id:
        try:
            await asyncio.to_thread(database.delete_document_by_clickup_id, config.TASKS_COLLECTION_ID, task_id)
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
    # برای همیشه در حال اجرا باقی می‌ماند
    await asyncio.Event().wait()

import logging
from appwrite.client import Client
from appwrite.services.databases import Databases
from appwrite.id import ID
from appwrite.query import Query
from appwrite.exception import AppwriteException
import config

logger = logging.getLogger(__name__)

_client = None

def get_db_client():
    """یک نمونه Singleton از کلاینت Appwrite ایجاد و بازمی‌گرداند."""
    global _client
    if _client is None:
        _client = (
            Client()
            .set_endpoint(config.APPWRITE_ENDPOINT)
            .set_project(config.APPWRITE_PROJECT_ID)
            .set_key(config.APPWRITE_API_KEY)
            .set_self_signed()
        )
    return _client

def create_document(database_id, collection_id, data):
    """یک سند جدید در کالکشن مشخص شده ایجاد می‌کند."""
    try:
        db = Databases(get_db_client())
        return db.create_document(database_id, collection_id, ID.unique(), data)
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در ایجاد سند در کالکشن {collection_id}: {e}")
        raise

def get_documents(database_id, collection_id, queries=None):
    """
    اسناد را از یک کالکشن مشخص دریافت می‌کند.
    به‌روزرسانی: به طور پیش‌فرض اسناد را بر اساس تاریخ ایجاد (قدیمی‌ترین) مرتب می‌کند.
    """
    try:
        db = Databases(get_db_client())
        if queries is None:
            queries = []
        
        # اضافه کردن مرتب‌سازی پیش‌فرض برای نمایش قدیمی‌ترین آیتم‌ها در ابتدا
        queries.append(Query.order_asc("$createdAt"))

        return db.list_documents(database_id, collection_id, queries=queries).get('documents', [])
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در دریافت اسناد از کالکشن {collection_id}: {e}")
        return []

def get_single_document(database_id, collection_id, key, value):
    """یک سند را بر اساس یک کلید و مقدار مشخص پیدا می‌کند."""
    try:
        db = Databases(get_db_client())
        response = db.list_documents(
            database_id, 
            collection_id, 
            queries=[Query.equal(key, [value])]
        )
        return response['documents'][0] if response['total'] > 0 else None
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در دریافت سند با {key}={value}: {e}")
        return None

def upsert_document(database_id, collection_id, query_key, query_value, data):
    """یک سند را به‌روزرسانی می‌کند یا در صورت عدم وجود، آن را ایجاد می‌کند."""
    try:
        db = Databases(get_db_client())
        existing_doc = get_single_document(database_id, collection_id, query_key, query_value)
        
        if existing_doc:
            document_id = existing_doc['$id']
            db.update_document(database_id, collection_id, document_id, data)
        else:
            db.create_document(database_id, collection_id, ID.unique(), data)
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در ذخیره سند در کالکشن {collection_id}: {e}")

def delete_document_by_clickup_id(collection_id, clickup_id):
    """
    یک سند را بر اساس شناسه ClickUp از دیتابیس اصلی (ClickUp) حذف می‌کند.
    """
    try:
        doc = get_single_document(config.APPWRITE_DATABASE_ID, collection_id, 'clickup_task_id', clickup_id)
        if doc:
            db = Databases(get_db_client())
            db.delete_document(config.APPWRITE_DATABASE_ID, collection_id, doc['$id'])
            logger.info(f"سند با ClickUp ID {clickup_id} از کالکشن {collection_id} حذف شد.")
            return True
        return False
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در حذف سند با ClickUp ID {clickup_id}: {e}")
        return False


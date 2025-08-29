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

def get_documents(collection_id, queries=None):
    try:
        db = Databases(get_db_client())
        if queries is None:
            queries = []
        return db.list_documents(config.APPWRITE_DATABASE_ID, collection_id, queries=queries).get('documents', [])
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در دریافت اسناد از کالکشن {collection_id}: {e}")
        return []

def get_single_document(collection_id, key, value):
    try:
        db = Databases(get_db_client())
        response = db.list_documents(
            config.APPWRITE_DATABASE_ID, 
            collection_id, 
            queries=[Query.equal(key, [value])]
        )
        if response['total'] > 0:
            return response['documents'][0]
        return None
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در دریافت سند با {key}={value}: {e}")
        return None

def upsert_document(collection_id, query_key, query_value, data):
    try:
        db = Databases(get_db_client())
        response = db.list_documents(
            config.APPWRITE_DATABASE_ID, 
            collection_id, 
            [Query.equal(query_key, [str(query_value)])]
        )
        if response['total'] > 0:
            document_id = response['documents'][0]['$id']
            db.update_document(config.APPWRITE_DATABASE_ID, collection_id, document_id, data)
        else:
            db.create_document(config.APPWRITE_DATABASE_ID, collection_id, ID.unique(), data)
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در ذخیره سند در کالکشن {collection_id}: {e}")

def delete_document_by_clickup_id(collection_id, clickup_id):
    doc = get_single_document(collection_id, 'clickup_task_id', clickup_id)
    if doc:
        try:
            db = Databases(get_db_client())
            db.delete_document(config.APPWRITE_DATABASE_ID, collection_id, doc['$id'])
            logger.info(f"سند با ClickUp ID {clickup_id} از کالکشن {collection_id} حذف شد.")
        except AppwriteException as e:
            logger.error(f"خطای Appwrite در حذف سند با ClickUp ID {clickup_id}: {e}")

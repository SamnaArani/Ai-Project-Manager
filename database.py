# -*- coding: utf-8 -*-
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
        return None

def get_documents(database_id, collection_id, queries=None):
    try:
        db = Databases(get_db_client())
        if queries is None:
            queries = []
        return db.list_documents(database_id, collection_id, queries=queries).get('documents', [])
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در دریافت اسناد از کالکشن {collection_id}: {e}")
        return []

def get_single_document(database_id, collection_id, key, value):
    try:
        db = Databases(get_db_client())
        response = db.list_documents(
            database_id, 
            collection_id, 
            queries=[Query.equal(key, [value])]
        )
        if response['total'] > 0:
            return response['documents'][0]
        return None
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در دریافت سند با {key}={value}: {e}")
        return None

def upsert_document(database_id, collection_id, query_key, query_value, data):
    try:
        db = Databases(get_db_client())
        existing_doc = get_single_document(database_id, collection_id, query_key, str(query_value))
        
        if existing_doc:
            document_id = existing_doc['$id']
            return db.update_document(database_id, collection_id, document_id, data)
        else:
            # When creating, ensure the query key is also in the data
            if query_key not in data:
                data[query_key] = query_value
            return db.create_document(database_id, collection_id, ID.unique(), data)
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در ذخیره سند در کالکشن {collection_id}: {e}")
        return None

def delete_document_by_clickup_id(database_id, collection_id, clickup_id_key, clickup_id):
    doc = get_single_document(database_id, collection_id, clickup_id_key, clickup_id)
    if doc:
        try:
            db = Databases(get_db_client())
            db.delete_document(database_id, collection_id, doc['$id'])
            logger.info(f"سند با ClickUp ID {clickup_id} از کالکشن {collection_id} حذف شد.")
            return True
        except AppwriteException as e:
            logger.error(f"خطای Appwrite در حذف سند با ClickUp ID {clickup_id}: {e}")
    return False

async def create_bot_users_collection_if_not_exists():
    """کالکشن bot_users را در صورت عدم وجود ایجاد می‌کند."""
    db = Databases(get_db_client())
    try:
        db.get_collection(config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID)
        logger.info("کالکشن bot_users از قبل وجود دارد.")
    except AppwriteException as e:
        if e.code == 404:
            logger.info("کالکشن bot_users یافت نشد. در حال ایجاد...")
            try:
                db.create_collection(
                    database_id=config.APPWRITE_DATABASE_ID,
                    collection_id=config.BOT_USERS_COLLECTION_ID,
                    name="Bot Users",
                    permissions=['read("any")', 'create("any")', 'update("any")']
                )
                logger.info("کالکشن bot_users با موفقیت ایجاد شد.")
                db.create_string_attribute(config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, "telegram_id", 128, True)
                db.create_string_attribute(config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, "clickup_token", 2048, False)
                db.create_boolean_attribute(config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, "is_admin", True, False)
                db.create_boolean_attribute(config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, "is_active", True, False)
                logger.info("اتریبیوت‌های کالکشن bot_users با موفقیت ایجاد شدند.")
            except AppwriteException as create_e:
                logger.error(f"خطا در ایجاد کالکشن bot_users یا اتریبیوت‌های آن: {create_e}")
        else:
            logger.error(f"خطای Appwrite در بررسی کالکشن bot_users: {e}")


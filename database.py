# -*- coding: utf-8 -*-
import logging
import asyncio
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

async def _ensure_attribute(db, db_id, coll_id, existing_keys, attr_key, attr_type, size=None, required=False, default=None, array=False):
    """تابع کمکی برای ساخت اتریبیوت در صورت عدم وجود."""
    if attr_key in existing_keys:
        return
    try:
        logger.info(f"Attribute '{attr_key}' not found in '{coll_id}'. Creating it...")
        if attr_type == 'string':
            db.create_string_attribute(db_id, coll_id, key=attr_key, size=size, required=required, default=default, array=array)
        elif attr_type == 'integer':
            db.create_integer_attribute(db_id, coll_id, key=attr_key, required=required, default=default, array=array)
        elif attr_type == 'boolean':
            db.create_boolean_attribute(db_id, coll_id, key=attr_key, required=required, default=default, array=array)
        elif attr_type == 'datetime':
            db.create_datetime_attribute(db_id, coll_id, key=attr_key, required=required, default=default, array=array)
        
        logger.info(f"Attribute '{attr_key}' created. Waiting for it to become available...")
        await asyncio.sleep(2)
    except AppwriteException as e:
        if e.code == 409:
            logger.warning(f"Attribute '{attr_key}' already exists. Skipping.")
        else:
            logger.error(f"Failed to create attribute '{attr_key}': {e.message}")
            raise

async def setup_database_schemas():
    """ساختار دیتابیس را بررسی و در صورت نیاز، کالکشن‌ها و اتریبیوت‌ها را ایجاد می‌کند."""
    db = Databases(get_db_client())
    db_id = config.APPWRITE_DATABASE_ID
    
    collections = {
        config.BOT_USERS_COLLECTION_ID: {
            "name": "Bot Users",
            "attributes": [
                ("telegram_id", 'string', 128, True), ("clickup_token", 'string', 2048, False),
                ("is_active", 'boolean', None, True, False), ("is_admin", 'boolean', None, True, False),
                ("created_at", 'datetime', None, False), ("package_id", 'string', 128, False),
                ("usage_limit", 'integer', None, False, 0), ("used_count", 'integer', None, False, 0),
                ("expiry_date", 'datetime', None, False), ("clickup_user_id", 'string', 128, False),
                ("clickup_username", 'string', 255, False), ("clickup_email", 'string', 255, False),
                ("package_activation_date", 'datetime', None, False), # New field for activation date
            ]
        },
        config.CLICKUP_USERS_COLLECTION_ID: {
            "name": "ClickUp Users",
            "attributes": [
                ("telegram_id", 'string', 128, True), ("clickup_user_id", 'string', 128, True),
                ("username", 'string', 255, True), ("email", 'string', 255, True),
            ]
        },
        config.SPACES_COLLECTION_ID: {"name": "Spaces", "attributes": [("telegram_id", 'string', 128, True), ("clickup_space_id", 'string', 128, True), ("name", 'string', 255, True)]},
        config.FOLDERS_COLLECTION_ID: {"name": "Folders", "attributes": [("telegram_id", 'string', 128, True), ("clickup_folder_id", 'string', 128, True), ("name", 'string', 255, True), ("space_id", 'string', 128, True)]},
        config.LISTS_COLLECTION_ID: {"name": "Lists", "attributes": [("telegram_id", 'string', 128, True), ("clickup_list_id", 'string', 128, True), ("name", 'string', 255, True), ("folder_id", 'string', 128, False)]},
        config.TASKS_COLLECTION_ID: {
            "name": "Tasks",
            "attributes": [
                ("telegram_id", 'string', 128, True), ("clickup_task_id", 'string', 128, True),
                ("title", 'string', 512, True), ("status", 'string', 128, False),
                ("list_id", 'string', 128, True), ("priority", 'string', 128, False),
                ("content", 'string', 10000, False, ""), ("start_date", 'integer', None, False),
                ("due_date", 'integer', None, False), ("assignee_name", 'string', 255, False),
            ]
        },
        config.PACKAGES_COLLECTION_ID: {
            "name": "Packages",
            "attributes": [
                ("package_name", 'string', 255, True), ("package_description", 'string', 1024, False),
                ("ai_call_limit", 'integer', None, True, 0), ("monthly_price", 'integer', None, True, 0),
                ("is_active", 'boolean', None, True, True),
            ]
        },
        config.PAYMENT_REQUESTS_COLLECTION_ID: {
            "name": "Payment Requests",
            "attributes": [
                ("telegram_id", 'string', 128, True), ("package_id", 'string', 128, True),
                ("receipt_details", 'string', 2048, True),
                ("status", 'string', 50, False, "pending"),
                ("request_date", 'datetime', None, False), ("review_date", 'datetime', None, False),
                ("admin_notes", 'string', 1024, False),
            ]
        }
    }
    
    for coll_id, coll_info in collections.items():
        try:
            collection = db.get_collection(db_id, coll_id)
            keys = {attr['key'] for attr in collection['attributes']}
        except AppwriteException as e:
            if e.code == 404:
                logger.info(f"کالکشن '{coll_info['name']}' یافت نشد. در حال ایجاد...")
                db.create_collection(db_id, coll_id, coll_info['name'], permissions=['read("any")', 'create("any")', 'update("any")'])
                keys = set()
                await asyncio.sleep(2)
            else:
                raise
        
        for attr in coll_info['attributes']:
            await _ensure_attribute(db, db_id, coll_id, keys, *attr)

def create_document(database_id, collection_id, data):
    try:
        db = Databases(get_db_client())
        return db.create_document(database_id, collection_id, ID.unique(), data)
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در ایجاد سند در کالکشن {collection_id}: {e.message}")
        raise

def get_documents(database_id, collection_id, queries=None):
    try:
        db = Databases(get_db_client())
        queries = queries or []
        queries.append(Query.limit(500)) 
        return db.list_documents(database_id, collection_id, queries=queries).get('documents', [])
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در دریافت اسناد از کالکشن {collection_id}: {e.message}")
        return []

def get_single_document(database_id, collection_id, key, value):
    try:
        db = Databases(get_db_client())
        response = db.list_documents(database_id, collection_id, queries=[Query.equal(key, [value])])
        return response['documents'][0] if response['total'] > 0 else None
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در دریافت سند با {key}={value}: {e.message}")
        return None

def get_single_document_by_id(database_id, collection_id, document_id):
    """یک سند را با شناسه منحصر به فرد Appwrite ($id) آن دریافت می‌کند."""
    try:
        db = Databases(get_db_client())
        return db.get_document(database_id, collection_id, document_id)
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در دریافت سند با ID={document_id}: {e.message}")
        return None

def upsert_document(database_id, collection_id, query_key, query_value, data):
    try:
        db = Databases(get_db_client())
        str_query_value = str(query_value)
        existing_doc = get_single_document(database_id, collection_id, query_key, str_query_value)
        
        if existing_doc:
            return db.update_document(database_id, collection_id, existing_doc['$id'], data)
        else:
            if query_key not in data:
                data[query_key] = query_value
            return db.create_document(database_id, collection_id, ID.unique(), data)
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در ذخیره سند در کالکشن {collection_id}: {e.message}")
        raise

def delete_document(database_id, collection_id, document_id):
    """یک سند را با شناسه آن حذف می‌کند."""
    try:
        db = Databases(get_db_client())
        db.delete_document(database_id, collection_id, document_id)
        return True
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در حذف سند {document_id} از کالکشن {collection_id}: {e.message}")
        return False

def delete_document_by_clickup_id(database_id, collection_id, clickup_id_key, clickup_id):
    """یک سند را با شناسه کلیک‌اپ آن پیدا کرده و حذف می‌کند."""
    doc = get_single_document(database_id, collection_id, clickup_id_key, clickup_id)
    if doc:
        return delete_document(database_id, collection_id, doc['$id'])
    return False


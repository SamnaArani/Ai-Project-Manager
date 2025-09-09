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

async def _ensure_attribute(db, db_id, coll_id, existing_keys, attr_key, attr_type, size=None, required=False, default=None):
    """تابع کمکی برای ساخت اتریبیوت در صورت عدم وجود."""
    if attr_key in existing_keys:
        return
    try:
        logger.info(f"Attribute '{attr_key}' not found in '{coll_id}'. Creating it...")
        if attr_type == 'string':
            db.create_string_attribute(db_id, coll_id, key=attr_key, size=size, required=required, default=default)
        elif attr_type == 'integer':
            db.create_integer_attribute(db_id, coll_id, key=attr_key, required=required, default=default)
        elif attr_type == 'boolean':
            db.create_boolean_attribute(db_id, coll_id, key=attr_key, required=required, default=default)
        elif attr_type == 'datetime':
            db.create_datetime_attribute(db_id, coll_id, key=attr_key, required=required, default=default)
        
        logger.info(f"Attribute '{attr_key}' created. Waiting for it to become available...")
        await asyncio.sleep(2)
    except AppwriteException as e:
        if e.code == 409:
            logger.warning(f"Attribute '{attr_key}' already exists. Skipping.")
        else:
            logger.error(f"Failed to create attribute '{attr_key}': {e}")
            raise

async def setup_database_schemas():
    """ساختار دیتابیس را بررسی و در صورت نیاز، کالکشن‌ها و اتریبیوت‌ها را ایجاد می‌کند."""
    db = Databases(get_db_client())
    db_id = config.APPWRITE_DATABASE_ID
    
    collections_to_check = {
        config.SPACES_COLLECTION_ID: "Spaces",
        config.FOLDERS_COLLECTION_ID: "Folders",
        config.LISTS_COLLECTION_ID: "Lists",
        config.TASKS_COLLECTION_ID: "Tasks",
        config.CLICKUP_USERS_COLLECTION_ID: "ClickUp Users",
        config.BOT_USERS_COLLECTION_ID: "Bot Users"
    }

    # اطمینان از وجود اتریبیوت telegram_id در کالکشن‌های کلیک‌اپ
    for coll_id, coll_name in collections_to_check.items():
        if coll_id == config.BOT_USERS_COLLECTION_ID: continue # این کالکشن ساختار متفاوتی دارد
        try:
            collection = db.get_collection(db_id, coll_id)
            existing_attrs = {attr['key'] for attr in collection['attributes']}
            await _ensure_attribute(db, db_id, coll_id, existing_attrs, "telegram_id", 'string', 128, required=True)
        except AppwriteException as e:
            if e.code == 404: logger.warning(f"کالکشن '{coll_name}' یافت نشد. لطفاً از وجود آن مطمئن شوید.")
            else: logger.error(f"خطا در بررسی کالکشن '{coll_name}': {e}")

    # بررسی و ساخت کالکشن کاربران ربات
    try:
        collection = db.get_collection(db_id, config.BOT_USERS_COLLECTION_ID)
        existing_attrs = {attr['key'] for attr in collection['attributes']}
        await _ensure_attribute(db, db_id, config.BOT_USERS_COLLECTION_ID, existing_attrs, "created_at", 'datetime', required=False)
    except AppwriteException as e:
        if e.code == 404:
            logger.info("کالکشن 'bot_users' یافت نشد. در حال ایجاد...")
            db.create_collection(db_id, config.BOT_USERS_COLLECTION_ID, "Bot Users", permissions=['read("any")', 'create("any")', 'update("any")'])
            await _ensure_attribute(db, db_id, config.BOT_USERS_COLLECTION_ID, {}, "telegram_id", 'string', 128, required=True)
            await _ensure_attribute(db, db_id, config.BOT_USERS_COLLECTION_ID, {}, "clickup_token", 'string', 2048, required=False)
            await _ensure_attribute(db, db_id, config.BOT_USERS_COLLECTION_ID, {}, "is_active", 'boolean', required=True, default=False)
            await _ensure_attribute(db, db_id, config.BOT_USERS_COLLECTION_ID, {}, "is_admin", 'boolean', required=True, default=False)
            await _ensure_attribute(db, db_id, config.BOT_USERS_COLLECTION_ID, {}, "created_at", 'datetime', required=False)
            logger.info("کالکشن 'bot_users' و اتریبیوت‌های آن با موفقیت ایجاد شد.")

    # [FIX] اطمینان از اینکه folder_id در کالکشن لیست‌ها اختیاری است
    try:
        collection = db.get_collection(db_id, config.LISTS_COLLECTION_ID)
        existing_attrs = {attr['key'] for attr in collection['attributes']}
        # اطمینان حاصل کنید که این اتریبیوت به عنوان اختیاری ساخته می‌شود
        await _ensure_attribute(db, db_id, config.LISTS_COLLECTION_ID, existing_attrs, "folder_id", 'string', 255, required=False)
    except AppwriteException as e:
         logger.error(f"خطا در بررسی کالکشن لیست‌ها: {e}")


def create_document(database_id, collection_id, data):
    try:
        db = Databases(get_db_client())
        return db.create_document(database_id, collection_id, ID.unique(), data)
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در ایجاد سند در کالکشن {collection_id}: {e}")
        raise

def get_documents(database_id, collection_id, queries=None):
    try:
        db = Databases(get_db_client())
        queries = queries or []
        queries.append(Query.limit(500)) # افزایش محدودیت برای دریافت آیتم‌های بیشتر
        return db.list_documents(database_id, collection_id, queries=queries).get('documents', [])
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در دریافت اسناد از کالکشن {collection_id}: {e}")
        return []

def get_single_document(database_id, collection_id, key, value):
    try:
        db = Databases(get_db_client())
        response = db.list_documents(database_id, collection_id, queries=[Query.equal(key, [value])])
        return response['documents'][0] if response['total'] > 0 else None
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در دریافت سند با {key}={value}: {e}")
        return None

def upsert_document(database_id, collection_id, query_key, query_value, data):
    try:
        db = Databases(get_db_client())
        # برای upsert کردن بر اساس یک کلید مشخص، باید ابتدا سند موجود را پیدا کنیم
        existing_doc = get_single_document(database_id, collection_id, query_key, str(query_value))
        
        if existing_doc:
            # اگر سند موجود بود، آن را آپدیت می‌کنیم
            return db.update_document(database_id, collection_id, existing_doc['$id'], data)
        else:
            # در غیر این صورت، سند جدیدی می‌سازیم
            # اطمینان حاصل می‌کنیم که کلید کوئری در دیتای ورودی وجود دارد
            if query_key not in data:
                data[query_key] = query_value
            return db.create_document(database_id, collection_id, ID.unique(), data)
    except AppwriteException as e:
        logger.error(f"خطای Appwrite در ذخیره سند در کالکشن {collection_id}: {e.message}")
        raise

def delete_document_by_clickup_id(database_id, collection_id, clickup_id_key, clickup_id):
    doc = get_single_document(database_id, collection_id, clickup_id_key, clickup_id)
    if doc:
        try:
            db = Databases(get_db_client())
            db.delete_document(database_id, collection_id, doc['$id'])
            return True
        except AppwriteException as e:
            logger.error(f"خطای Appwrite در حذف سند با ClickUp ID {clickup_id}: {e}")
    return False


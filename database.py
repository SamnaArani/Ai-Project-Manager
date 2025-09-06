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

async def get_databases_service():
    """یک نمونه سرویس Databases ایجاد و بازمی‌گرداند."""
    return Databases(get_db_client())

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

async def create_bot_users_collection_if_not_exists():
    """کالکشن bot_users را در صورت عدم وجود ایجاد می‌کند."""
    db = Databases(get_db_client())
    try:
        # Check if collection exists
        db.get_collection(config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID)
        logger.info("کالکشن bot_users از قبل وجود دارد.")
    except AppwriteException as e:
        if e.code == 404:
            try:
                # Create collection
                db.create_collection(
                    database_id=config.APPWRITE_DATABASE_ID,
                    collection_id=config.BOT_USERS_COLLECTION_ID,
                    name="Bot Users",
                    permissions=['read("any")', 'create("any")', 'update("any")']
                )
                logger.info("کالکشن bot_users با موفقیت ایجاد شد.")
            except AppwriteException as e:
                logger.error(f"خطا در ایجاد کالکشن bot_users: {e}")
                return

            try:
                # Create attributes for the collection
                db.create_string_attribute(config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, "telegram_id", 128, required=True, array=False)
                db.create_string_attribute(config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, "clickup_token", 2048, required=False, array=False)
                db.create_boolean_attribute(config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, "is_admin", required=True, default=False)
                db.create_boolean_attribute(config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, "is_active", required=True, default=False)
                db.create_string_attribute(config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, "package_id", 128, required=False, array=False)
                db.create_integer_attribute(config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, "usage_limit", required=False)
                db.create_integer_attribute(config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, "used_count", required=True, default=0)
                db.create_datetime_attribute(config.APPWRITE_DATABASE_ID, config.BOT_USERS_COLLECTION_ID, "expiry_date", required=False)
                logger.info("اتریبیوت‌های کالکشن bot_users با موفقیت ایجاد شدند.")
            except AppwriteException as e:
                logger.error(f"خطا در ایجاد اتریبیوت‌های کالکشن bot_users: {e}")
        else:
            logger.error(f"خطای Appwrite در بررسی کالکشن bot_users: {e}")
            
async def create_packages_collection_if_not_exists():
    """کالکشن packages را در صورت عدم وجود ایجاد می‌کند."""
    db = Databases(get_db_client())
    try:
        db.get_collection(config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID)
        logger.info("کالکشن packages از قبل وجود دارد.")
    except AppwriteException as e:
        if e.code == 404:
            try:
                db.create_collection(
                    database_id=config.APPWRITE_DATABASE_ID,
                    collection_id=config.PACKAGES_COLLECTION_ID,
                    name="Packages",
                    permissions=['read("any")']
                )
                logger.info("کالکشن packages با موفقیت ایجاد شد.")
            except AppwriteException as e:
                logger.error(f"خطا در ایجاد کالکشن packages: {e}")
                return

            try:
                db.create_string_attribute(config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, "package_name", 128, required=True, array=False)
                db.create_integer_attribute(config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, "ai_call_limit", required=False)
                db.create_integer_attribute(config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, "monthly_price", required=True, default=0)
                db.create_boolean_attribute(config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, "is_active", required=True, default=True)
                db.create_string_attribute(config.APPWRITE_DATABASE_ID, config.PACKAGES_COLLECTION_ID, "package_description", 2048, required=False, array=False)
                logger.info("اتریبیوت‌های کالکشن packages با موفقیت ایجاد شدند.")
            except AppwriteException as e:
                logger.error(f"خطا در ایجاد اتریبیوت‌های کالکشن packages: {e}")
        else:
            logger.error(f"خطای Appwrite در بررسی کالکشن packages: {e}")

"""Repositorio MongoDB: consecutivos atómicos y edición con control de versión."""
import os
import re
import threading
from datetime import datetime, timezone
from functools import wraps
from uuid import uuid4

from bson.decimal128 import Decimal128
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from .domain import AppError, ROOT, numeric_priority, read_json


def database_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DuplicateKeyError as exc:
            raise AppError("El código o registro ya existe en MongoDB. Actualiza antes de reintentar.", 409) from exc
        except PyMongoError as exc:
            # No devolver detalles del driver que puedan contener URI o credenciales.
            raise AppError("No se pudo completar la operación en MongoDB. Comprueba el servicio y la configuración. Si estabas guardando, actualiza antes de reintentar.", 503) from exc
    return wrapped


class MongoStore:
    def __init__(self, database=None, config_path=None, categories_path=None):
        self.config_path = config_path or ROOT / "config" / "mongodb.json"
        self.categories_path = categories_path or ROOT / "config" / "categories.json"
        self._database = database
        self.client = None
        self._ready = False
        self._init_lock = threading.Lock()

    def categories(self):
        categories = read_json(self.categories_path).get("categories")
        if not isinstance(categories, list) or not categories:
            raise AppError("Configura las categorías en config/categories.json.", 503)
        for item in categories:
            if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k].strip() for k in ("id", "name", "code_prefix")):
                raise AppError("Cada categoría necesita id, name y code_prefix.", 503)
            if not re.fullmatch(r"[a-z0-9_-]+", item["id"]) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,15}", item["code_prefix"]):
                raise AppError("Identificador o prefijo de categoría inválido.", 503)
            if type(item.get("code_digits")) is not int or not 1 <= item["code_digits"] <= 10:
                raise AppError("code_digits debe estar entre 1 y 10.", 503)
        for key in ("id", "code_prefix"):
            if len({item[key] for item in categories}) != len(categories):
                raise AppError(f"Las categorías deben tener {key} únicos.", 503)
        return categories

    def category(self, category_id):
        for item in self.categories():
            if item["id"] == category_id:
                return item
        raise AppError("La categoría seleccionada no existe.", 404)

    @property
    def db(self):
        if self._database is None:
            from pathlib import Path
            if not Path(self.config_path).exists() and not os.environ.get("ANTIJIRA_MONGODB_URI"):
                raise AppError("Falta la conexión MongoDB. Crea config/mongodb.json desde el ejemplo y configura la URI de Atlas, o define ANTIJIRA_MONGODB_URI.", 503)
            config = read_json(self.config_path) if Path(self.config_path).exists() else read_json(ROOT / "config" / "mongodb.example.json")
            uri = os.environ.get("ANTIJIRA_MONGODB_URI", config.get("uri", ""))
            name = os.environ.get("ANTIJIRA_MONGODB_DATABASE", config.get("database", "antijira"))
            timeout = config.get("timeout_ms", 5000)
            if not isinstance(uri, str) or not uri.startswith(("mongodb://", "mongodb+srv://")):
                raise AppError("Configura una URI MongoDB válida en el archivo privado o en ANTIJIRA_MONGODB_URI.", 503)
            if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,63}", name) or type(timeout) is not int or not 100 <= timeout <= 60000:
                raise AppError("Nombre de base de datos o timeout de MongoDB inválido.", 503)
            self.client = MongoClient(uri, serverSelectionTimeoutMS=timeout, connectTimeoutMS=timeout, socketTimeoutMS=timeout, appname="Antijira", tz_aware=True)
            self._database = self.client[name]
        return self._database

    @database_operation
    def initialize(self):
        with self._init_lock:
            if self._ready:
                return
            db = self.db
            db.command("ping")
            db.tasks.create_index("code", unique=True, name="unique_task_code")
            db.tasks.create_index("migration_key", unique=True, partialFilterExpression={"migration_key": {"$type": "string"}}, name="unique_migration_source")
            db.tasks.create_index([("category", 1), ("status", 1), ("priority", 1)])
            db.tasks.create_index("assignee")
            db.users.create_index("username_key", unique=True, name="unique_username")
            db.sessions.create_index("expires_at", expireAfterSeconds=0, name="expire_sessions")
            db.audit.create_index([("created_at", -1)])
            # Recupera el máximo real si se restauraron documentos sin contadores.
            maximum = db.tasks.find_one(sort=[("_id", -1)], projection={"_id": 1})
            db.counters.update_one({"_id": "task_id"}, {"$max": {"value": maximum["_id"] if maximum else 1}}, upsert=True)
            for category in self.categories():
                maximum = 0
                pattern = re.compile(re.escape(category["code_prefix"]) + r"(\d+)$")
                for task in db.tasks.find({"category": category["id"]}, {"code": 1}):
                    match = pattern.fullmatch(task.get("code", ""))
                    if match:
                        maximum = max(maximum, int(match[1]))
                db.counters.update_one({"_id": "code:" + category["id"]}, {"$max": {"value": maximum}}, upsert=True)
            self._ready = True

    def _next(self, counter):
        value = self.db.counters.find_one_and_update({"_id": counter}, {"$inc": {"value": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
        return value["value"]

    def _code(self, category):
        return f"{category['code_prefix']}{self._next('code:' + category['id']):0{category['code_digits']}d}"

    @staticmethod
    def encode(task):
        data = dict(task)
        try:
            data["priority"] = Decimal128(numeric_priority(data.get("priority", "")))
        except AppError:
            # Datos históricos inválidos permanecen visibles y corregibles.
            data["priority"] = str(data.get("priority", ""))
        return data

    @staticmethod
    def public(document):
        if document is None:
            return None
        data = {k: v for k, v in document.items() if not k.startswith("_") and k not in ("migration_key", "legacy", "history")}
        data["id"] = document["_id"]
        data["row"] = document["_id"]  # Compatibilidad con el cliente: ahora es ID estable, no fila.
        if isinstance(data.get("priority"), Decimal128):
            data["priority"] = str(data["priority"].to_decimal())
        for key in ("created_at", "updated_at"):
            if isinstance(data.get(key), datetime):
                data[key] = data[key].isoformat()
        return data

    @database_operation
    def list_tasks(self):
        self.initialize()
        names = {item["id"]: item["name"] for item in self.categories()}
        tasks = []
        for document in self.db.tasks.find():
            task = self.public(document)
            task["category_name"] = names.get(task["category"], task.get("category_name", task["category"]))
            tasks.append(task)
        return tasks

    @database_operation
    def get(self, category_id, task_id):
        self.initialize()
        return self.public(self.db.tasks.find_one({"_id": task_id, "category": category_id}))

    @database_operation
    def create(self, task, category, actor=None):
        self.initialize()
        document = self.encode(task)
        now = datetime.now(timezone.utc)
        username = actor.get("username") if actor else "system"
        document.update(_id=self._next("task_id"), code=self._code(category), revision=uuid4().hex,
                        category=category["id"], category_name=category["name"], created_at=now, updated_at=now,
                        created_by=username, updated_by=username,
                        history=[{"action":"task.create","actor":username,"at":now,"changes":{"code":None,"category":category["id"]}}])
        self.db.tasks.insert_one(document)
        self.record_audit("task.create", actor, {"task_id": document["_id"], "code": document["code"]})
        return self.public(document)

    @database_operation
    def update(self, old, task, destination, actor=None):
        self.initialize()
        changes = self.encode(task)
        now = datetime.now(timezone.utc)
        username = actor.get("username") if actor else "system"
        changes.update(category=destination["id"], category_name=destination["name"], revision=uuid4().hex, updated_at=now, updated_by=username)
        if old["category"] != destination["id"]:
            changes["code"] = self._code(destination)
        history_changes = {key: {"from": old.get(key), "to": value}
                           for key, value in changes.items()
                           if key not in ("revision", "updated_at", "updated_by") and old.get(key) != value}
        document = self.db.tasks.find_one_and_update(
            {"_id": old["id"], "category": old["category"], "revision": old["revision"]},
            {"$set": changes, "$push": {"history": {"action": "task.update", "actor": username,
                                                       "at": now, "changes": history_changes}}},
            return_document=ReturnDocument.AFTER)
        if document is None:
            raise AppError("La tarea cambió desde que la abriste. Actualiza y vuelve a abrirla para conservar los cambios del otro usuario.", 409)
        audit_changes = {key: {"from": old.get(key), "to": self.public(document).get(key)}
                         for key in changes if key not in ("revision", "updated_at", "updated_by")
                         and old.get(key) != self.public(document).get(key)}
        self.record_audit("task.update", actor, {"task_id": document["_id"], "code": document["code"], "changes": audit_changes})
        return self.public(document)

    def record_audit(self, action, actor, details=None):
        try:
            self.db.audit.insert_one({"action":action, "actor": actor.get("username") if actor else "system",
                                      "actor_id": actor.get("id") if actor else None,
                                      "details": details or {}, "created_at": datetime.now(timezone.utc)})
        except PyMongoError:
            # La historia de tareas se almacena atómicamente en el propio documento.
            pass

    def close(self):
        if self.client is not None:
            self.client.close()

    @database_operation
    def import_records(self, records, apply=False):
        """Importación explícita e idempotente; no sobrescribe documentos existentes."""
        self.initialize()
        seen = set()
        pending = []
        skipped = 0
        maxima = {}
        for record in records:
            category = self.category(record["task"]["category"])
            if self.db.tasks.find_one({"migration_key": record["migration_key"]}):
                skipped += 1
                continue
            code = record["task"].get("code", "").strip()
            if code:
                pattern = re.fullmatch(re.escape(category["code_prefix"]) + r"(\d+)", code)
                if not pattern:
                    raise AppError(f"Código incompatible con la categoría: {code}. La importación se canceló antes de insertar tareas.", 409)
                if code in seen or self.db.tasks.find_one({"code": code}):
                    raise AppError(f"El código {code} está repetido o ya existe en MongoDB. No se sobrescribirá.", 409)
                seen.add(code)
                maxima[category["id"]] = max(maxima.get(category["id"], 0), int(pattern[1]))
            pending.append(record)
        if not apply:
            return {"pending": len(pending), "already_imported": skipped, "inserted": 0}
        for category_id, maximum in maxima.items():
            self.db.counters.update_one({"_id": "code:" + category_id}, {"$max": {"value": maximum}}, upsert=True)
        inserted = 0
        for record in pending:
            task = record["task"]
            category = self.category(task["category"])
            document = self.encode(task)
            document.update(_id=self._next("task_id"), code=task.get("code") or self._code(category),
                            category_name=category["name"], migration_key=record["migration_key"],
                            legacy=record["legacy"], revision=uuid4().hex,
                            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
            try:
                self.db.tasks.insert_one(document)
                inserted += 1
            except DuplicateKeyError:
                if self.db.tasks.find_one({"migration_key": record["migration_key"]}):
                    skipped += 1
                else:
                    raise
        return {"pending": 0, "already_imported": skipped, "inserted": inserted}

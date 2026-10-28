import copy
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

import mongomock
from bson.decimal128 import Decimal128
from antijira.domain import AppError, parameters
from antijira.mongodb import MongoStore
from antijira.mongo_service import MongoTaskService
from app import make_handler


def payload(category="desarrollo"):
    config = parameters()
    return {"category": category, "name": "Prueba MongoDB", "module": "CARTERA",
            "description": "Descripción", "requirements": "Requerimiento",
            "priority": "5,5", "complexity": config["complexities"][0],
            "status": config["statuses"][0], "assignee": config["people"][0],
            "estimated_days": 2, "planned_start": "2026-10-30", "end_mode": "auto"}


class MongoTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().antijira_test
        self.store = MongoStore(database=self.db)
        self.service = MongoTaskService(self.store)

    def test_create_persists_and_reloads(self):
        created = self.service.create_task(payload())
        other = MongoTaskService(MongoStore(database=self.db))
        loaded = other.list_tasks()["tasks"][0]
        self.assertEqual(loaded["code"], "DS-001")
        self.assertEqual(created["row"], loaded["id"])
        self.assertEqual(loaded["planned_end"], "2026-11-03")
        self.assertIsInstance(self.db.tasks.find_one()["priority"], Decimal128)

    def test_category_sequences_are_independent(self):
        def create(i):
            return self.service.create_task(payload("errores" if i % 2 else "desarrollo"))
        # Inicialización separada; la prueba paralela ejercita incrementos y guardados.
        self.store.initialize()
        with ThreadPoolExecutor(max_workers=12) as executor:
            tasks = list(executor.map(create, range(80)))
        self.assertEqual(len({t["code"] for t in tasks}), 80)
        self.assertEqual(len({t["id"] for t in tasks}), 80)
        self.assertEqual(self.db.tasks.count_documents({}), 80)

    def test_stale_write_rejected_by_database(self):
        old = self.service.create_task(payload())
        changes = {"name": "Primera edición"}
        self.store.update(old, changes, self.store.category("desarrollo"))
        with self.assertRaises(AppError) as error:
            self.store.update(old, {"name": "Edición antigua"}, self.store.category("desarrollo"))
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.store.get("desarrollo", old["id"])["name"], "Primera edición")

    def test_move_keeps_id_and_legacy_fields(self):
        old = self.service.create_task(payload())
        self.db.tasks.update_one({"_id": old["id"]}, {"$set": {"legacy": {"extra": "conservar"}}})
        new = self.service.update_task("desarrollo", old["id"], {"category": "errores", "revision": old["revision"]})
        self.assertEqual(new["id"], old["id"])
        self.assertEqual(new["code"], "ER-001")
        self.assertEqual(self.db.tasks.count_documents({}), 1)
        self.assertEqual(self.db.tasks.find_one()["legacy"], {"extra": "conservar"})

    def test_numeric_sort_and_status_dates(self):
        for priority in ("6.2", "2", "5,5"):
            self.service.create_task({**payload(), "priority": priority})
        self.assertEqual([t["priority"] for t in self.service.list_tasks()["tasks"]], ["6.2", "5.5", "2"])
        old = self.service.list_tasks()["tasks"][0]
        with patch("antijira.mongo_service.today_co", return_value="2026-10-28"):
            new = self.service.update_task(old["category"], old["id"], {"revision": old["revision"], "status": "EN CURSO"})
        self.assertEqual(new["actual_start"], "2026-10-28")

    def test_only_ten_most_recent_completed_are_returned(self):
        for number in range(12):
            task = self.service.create_task({**payload(), "name": f"Terminada {number}", "status": "TERMINADO"})
            self.db.tasks.update_one({"_id": task["id"]}, {"$set": {"actual_end": f"2026-10-{number + 1:02d}"}})
        tasks = self.service.list_tasks()["tasks"]
        self.assertEqual(len(tasks), 10)
        self.assertNotIn("Terminada 0", {task["name"] for task in tasks})
        self.assertNotIn("Terminada 1", {task["name"] for task in tasks})

    def test_import_is_idempotent_and_reserves_codes(self):
        records = [{"migration_key": "source1", "task": {**payload(), "code": "DS-124"}, "legacy": {"hours": 24}}]
        preview = self.store.import_records(records)
        self.assertEqual(preview["pending"], 1)
        self.assertEqual(self.db.tasks.count_documents({}), 0)
        self.assertEqual(self.store.import_records(records, apply=True)["inserted"], 1)
        self.assertEqual(self.store.import_records(records, apply=True)["already_imported"], 1)
        self.assertEqual(self.service.create_task(payload())["code"], "DS-125")

    def test_import_conflicts_do_not_overwrite(self):
        old = self.service.create_task(payload())
        records = [{"migration_key": "source2", "task": {**payload(), "code": old["code"]}, "legacy": {}}]
        with self.assertRaises(AppError):
            self.store.import_records(records, apply=True)
        self.assertEqual(self.db.tasks.count_documents({}), 1)

    def test_private_configuration_not_returned(self):
        config = self.service.public_config()
        self.assertNotIn("uri", config)
        self.assertNotIn("password", str(config))
        self.assertEqual(config["storage"], "mongodb")

    def test_database_errors_are_redacted(self):
        from pymongo.errors import ServerSelectionTimeoutError
        with patch.object(self.store, "initialize", side_effect=ServerSelectionTimeoutError("private-password")):
            with self.assertRaises(AppError) as error:
                self.store.list_tasks()
        self.assertNotIn("private-password", str(error.exception))

    def test_missing_connection_has_clear_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            store = MongoStore(config_path=Path(directory) / "missing.json")
            with self.assertRaises(AppError) as error:
                store.initialize()
        self.assertIn("config/mongodb.json", str(error.exception))

    def test_http_uses_mongo_service_end_to_end(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(url + "/api/config") as response:
                config = json.load(response)
            self.assertEqual(config["parameters"]["storage"], "mongodb")
            headers = {"Content-Type": "application/json", "X-CSRF-Token": config["csrf_token"]}
            with urlopen(Request(url + "/api/tasks", json.dumps(payload()).encode(), headers)) as response:
                task = json.load(response)["task"]
            changes = {"revision": task["revision"], "category": "errores", "priority": "6,2"}
            with urlopen(Request(url + f"/api/tasks/desarrollo/{task['row']}", json.dumps(changes).encode(), headers, method="PUT")) as response:
                updated = json.load(response)["task"]
            self.assertEqual(updated["id"], task["id"])
            self.assertEqual(updated["code"], "ER-001")
            with urlopen(url + "/api/tasks") as response:
                tasks = json.load(response)["tasks"]
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["priority"], "6.2")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()

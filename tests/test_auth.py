import unittest

import mongomock

from antijira.auth import AuthService
from antijira.domain import AppError, parameters
from antijira.mongodb import MongoStore
from antijira.mongo_service import MongoTaskService


def task_payload():
    config = parameters()
    return {"category":"desarrollo", "name":"Tarea auditada", "module":"CARTERA",
            "description":"", "requirements":"", "priority":"6,2",
            "complexity":config["complexities"][0], "status":config["statuses"][0],
            "assignee":config["people"][0], "estimated_days":1,
            "planned_start":"2026-10-30", "end_mode":"auto", "dead_time":True}


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().antijira_auth
        self.store = MongoStore(database=self.db)
        self.auth = AuthService(self.store)
        self.admin = self.auth.ensure_admin("Una-clave-segura-2026")

    def test_permanent_admin_password_is_hashed_and_unique(self):
        document = self.db.users.find_one({"username_key":"admin"})
        self.assertTrue(document["permanent"])
        self.assertNotIn("Una-clave-segura-2026", str(document["password"]))
        with self.assertRaises(AppError) as error:
            self.auth.ensure_admin("Otra-clave-segura-2026")
        self.assertEqual(error.exception.status, 409)

    def test_login_session_and_logout(self):
        token, user = self.auth.login("ADMIN", "Una-clave-segura-2026", "127.0.0.1")
        self.assertEqual(user["username"], "admin")
        self.assertEqual(self.auth.authenticate(token)["id"], self.admin["id"])
        self.auth.logout(token, user)
        self.assertIsNone(self.auth.authenticate(token))

    def test_admin_creates_users_but_regular_user_cannot(self):
        user = self.auth.create_user({"username":"maria", "password":"Clave-temporal-2026", "role":"user"}, self.admin)
        self.assertEqual(user["role"], "user")
        with self.assertRaises(AppError) as error:
            self.auth.create_user({"username":"pedro", "password":"Clave-temporal-2026"}, user)
        self.assertEqual(error.exception.status, 403)

    def test_task_actions_keep_actor_and_dead_time(self):
        service = MongoTaskService(self.store)
        created = service.create_task(task_payload(), self.admin)
        self.assertTrue(created["dead_time"])
        updated = service.update_task(created["category"], created["id"], {"revision":created["revision"], "priority":"7.5"}, self.admin)
        document = self.db.tasks.find_one({"_id":created["id"]})
        self.assertEqual(updated["updated_by"], "admin")
        self.assertEqual([entry["actor"] for entry in document["history"]], ["admin", "admin"])
        self.assertEqual(self.db.audit.count_documents({"actor":"admin", "action":{"$in":["task.create","task.update"]}}), 2)

    def test_only_admin_can_correct_real_dates_but_status_stamps_them(self):
        service = MongoTaskService(self.store)
        user = self.auth.create_user({"username":"operador", "password":"Clave-temporal-2026", "role":"user"}, self.admin)
        created = service.create_task(task_payload(), user)
        with self.assertRaises(AppError) as error:
            service.update_task(created["category"], created["id"], {"revision":created["revision"], "actual_start":"2026-10-20"}, user)
        self.assertEqual(error.exception.status, 403)
        started = service.update_task(created["category"], created["id"], {"revision":created["revision"], "status":"EN CURSO"}, user)
        self.assertTrue(started["actual_start"])
        corrected = service.update_task(started["category"], started["id"], {"revision":started["revision"], "actual_start":"2026-10-20"}, self.admin)
        self.assertEqual(corrected["actual_start"], "2026-10-20")

    def test_five_failed_logins_lock_account(self):
        for _ in range(5):
            with self.assertRaises(AppError):
                self.auth.login("admin", "contraseña-equivocada", "127.0.0.1")
        with self.assertRaises(AppError) as error:
            self.auth.login("admin", "Una-clave-segura-2026", "127.0.0.1")
        self.assertEqual(error.exception.status, 429)


if __name__ == "__main__":
    unittest.main()

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from .domain import AppError, COLUMNS, parameters, validate_task, next_code, numeric_priority


def public_task(task):
    return {key: value for key, value in task.items() if not key.startswith("_")}


def today_co():
    return datetime.now(timezone(timedelta(hours=-5))).date().isoformat()


class TaskService:
    def __init__(self, store, config_loader=parameters):
        self.store = store
        self.config_loader = config_loader

    def public_config(self):
        return {**self.config_loader(), "categories": self.store.categories()}

    def list_tasks(self):
        tasks, warnings = [], []
        with self.store.lock:
            for category in self.store.categories():
                rows, _, _, messages = self.store.read(category)
                tasks.extend(rows)
                warnings.extend(messages)
        def order(task):
            try:
                return Decimal(numeric_priority(task["priority"]))
            except AppError:
                return Decimal("Infinity")
        tasks.sort(key=order)
        return {"tasks": [public_task(t) for t in tasks], "warnings": warnings}

    def create_task(self, payload):
        config = self.config_loader()
        task = validate_task(payload, config)
        category = self.store.category(payload.get("category"))
        with self.store.lock:
            self.store.prepare(category)
            tasks, headers, next_row, _ = self.store.read(category)
            task["code"] = next_code(tasks, {**config, **category})
            task.update(category=category["id"], category_name=category["name"])
            if task["status"] in config["started_statuses"]:
                task["actual_start"] = today_co()
            if task["status"] in config["completed_statuses"]:
                task["actual_end"] = today_co()
            self.store.append(task, headers, next_row, category)
        return public_task(task)

    def update_task(self, category_id, row_number, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("revision"), str):
            raise AppError("Recarga la tarea antes de editarla: falta la versión de origen.", 409)
        config = self.config_loader()
        category = self.store.category(category_id)
        destination = self.store.category(payload.get("category", category_id))
        with self.store.lock:
            tasks, headers, _, _ = self.store.read(category)
            old = next((task for task in tasks if task["row"] == row_number), None)
            if not old or old["revision"] != payload["revision"]:
                raise AppError("Esta tarea cambió o se movió desde que la abriste. Actualiza el tablero y vuelve a abrirla para no sobrescribir cambios.", 409)
            task = validate_task(payload, config, existing=old)
            if task["status"] != old["status"]:
                if task["status"] in config["started_statuses"] and not task["actual_start"] and "actual_start" not in payload:
                    task["actual_start"] = today_co()
                if task["status"] in config["completed_statuses"] and not task["actual_end"] and "actual_end" not in payload:
                    task["actual_end"] = today_co()
            self.store.prepare(destination)
            target_tasks, target_headers, target_row, _ = self.store.read(destination)
            moving = category_id != destination["id"]
            fresh_tasks = self.store.read(category)[0] if moving else target_tasks
            fresh = next((t for t in fresh_tasks if t["row"] == row_number), None)
            if not fresh or fresh["revision"] != old["revision"]:
                raise AppError("La tarea cambió mientras se preparaba la operación. Actualiza antes de guardar.", 409)
            if moving or not old["code"]:
                task["code"] = next_code(target_tasks, {**config, **destination})
            task.update(category=destination["id"], category_name=destination["name"])
            if moving:
                self.store.move(task, old, headers, category, destination, target_headers, target_row)
            else:
                fields = [key for key in COLUMNS if task[key] != old.get(key, "")]
                self.store.update(task, fields, target_headers, row_number, category)
        return public_task(task)

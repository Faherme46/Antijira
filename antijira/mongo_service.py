"""Casos de uso de tareas sobre MongoDB; usa las validaciones actuales."""
from decimal import Decimal
from .domain import AppError, parameters, validate_task, numeric_priority
from .service import today_co


class MongoTaskService:
    def __init__(self, store, config_loader=parameters):
        self.store = store
        self.config_loader = config_loader

    def public_config(self):
        return {**self.config_loader(), "categories": self.store.categories(), "storage": "mongodb"}

    def list_tasks(self):
        tasks = self.store.list_tasks()
        def order(task):
            try:
                return Decimal(numeric_priority(task.get("priority", "")))
            except AppError:
                return Decimal("-Infinity")
        # completed = set(self.config_loader()["completed_statuses"])
        # active = [task for task in tasks if task.get("status") not in completed]
        # finished = [task for task in tasks if task.get("status") in completed]
        # finished.sort(key=lambda task: task.get("actual_end") or task.get("updated_at") or task.get("created_at") or "", reverse=True)
        # tasks = active + finished
        tasks.sort(key=order, reverse=True)
        return {"tasks": tasks, "warnings": []}

    def create_task(self, payload, actor=None):
        config = self.config_loader()
        task = validate_task(payload, config)
        category = self.store.category(payload.get("category"))
        if task["status"] in config["started_statuses"]:
            task["actual_start"] = today_co()
        if task["status"] in config["completed_statuses"]:
            task["actual_end"] = today_co()
        return self.store.create(task, category, actor)

    def update_task(self, category_id, task_id, payload, actor=None):
        if not isinstance(payload, dict) or not isinstance(payload.get("revision"), str):
            raise AppError("Falta la versión de la tarea. Actualiza el tablero.", 409)
        old = self.store.get(category_id, task_id)
        if not old or old["revision"] != payload["revision"]:
            raise AppError("La tarea cambió o se movió. Actualiza el tablero y vuelve a abrirla.", 409)
        config = self.config_loader()
        destination = self.store.category(payload.get("category", category_id))
        task = validate_task(payload, config, old)
        if task["status"] != old["status"]:
            if task["status"] in config["started_statuses"] and not task["actual_start"] and "actual_start" not in payload:
                task["actual_start"] = today_co()
            if task["status"] in config["completed_statuses"] and not task["actual_end"] and "actual_end" not in payload:
                task["actual_end"] = today_co()
        return self.store.update(old, task, destination, actor)

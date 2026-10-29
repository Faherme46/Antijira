"""Casos de uso de tareas sobre MongoDB; usa las validaciones actuales."""
from decimal import Decimal
import threading
from .domain import AppError, parameters, validate_task, numeric_priority
from .service import today_co
from .calendar_co import business_end, holidays, next_business_day


class MongoTaskService:
    def __init__(self, store, config_loader=parameters):
        self.store = store
        self.config_loader = config_loader
        self._schedule_lock = threading.RLock()

    def public_config(self):
        calendar = [day.isoformat() for year in range(1900, 2091) for day in sorted(holidays(year))]
        return {**self.config_loader(), "categories": self.store.categories(), "storage": "mongodb", "holidays": calendar}

    def list_tasks(self):
        tasks = self.store.list_tasks()
        def order(task):
            try:
                return Decimal(numeric_priority(task.get("priority", "")))
            except AppError:
                return Decimal("-Infinity")
        completed = set(self.config_loader()["completed_statuses"])
        active = [task for task in tasks if task.get("status") not in completed]
        finished = [task for task in tasks if task.get("status") in completed]
        finished.sort(key=lambda task: task.get("actual_end") or task.get("updated_at") or task.get("created_at") or "", reverse=True)
        tasks = active + finished[:10]
        tasks.sort(key=order, reverse=True)
        return {"tasks": tasks, "warnings": []}

    def create_task(self, payload, actor=None):
        with self._schedule_lock:
            config = self.config_loader()
            task = validate_task(payload, config)
            category = self.store.category(payload.get("category"))
            if task["status"] in config["started_statuses"]:
                task["actual_start"] = today_co()
            if task["status"] in config["completed_statuses"]:
                task["actual_end"] = today_co()
            created = self.store.create(task, category, actor)
            self._cascade_after(created, actor)
            return created

    def update_task(self, category_id, task_id, payload, actor=None):
        with self._schedule_lock:
            return self._update_task(category_id, task_id, payload, actor)

    def _update_task(self, category_id, task_id, payload, actor=None):
        if not isinstance(payload, dict) or not isinstance(payload.get("revision"), str):
            raise AppError("Falta la versión de la tarea. Actualiza el tablero.", 409)
        if actor is not None and actor.get("role") != "admin" and any(field in payload for field in ("actual_start", "actual_end")):
            raise AppError("Solo un administrador puede cambiar manualmente las fechas reales.", 403)
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
        updated = self.store.update(old, task, destination, actor)
        scheduling_fields = {"priority", "planned_start", "planned_end", "estimated_days", "end_mode", "assignee", "dead_time"}
        if scheduling_fields.intersection(payload):
            self._cascade_after(updated, actor, reflow_dead="dead_time" in payload and bool(updated.get("dead_time")))
        return updated

    def _cascade_after(self, anchor, actor=None, reflow_dead=False):
        """Encadena las tareas activas del responsable después de la tarea modificada."""
        config = self.config_loader()
        if anchor.get("status") in config["completed_statuses"]:
            return
        tasks = [task for task in self.store.list_tasks()
                 if task.get("assignee") == anchor.get("assignee")
                 and task.get("status") not in config["completed_statuses"]
                 and not task.get("dead_time")]
        def key(task):
            try:
                priority = Decimal(numeric_priority(task.get("priority", "")))
            except AppError:
                priority = Decimal("-Infinity")
            return (-priority, task.get("id", 0))
        tasks.sort(key=key)
        if anchor.get("dead_time"):
            if not reflow_dead:
                return
            anchor_key = key(anchor)
            previous = [task for task in tasks if key(task) < anchor_key]
            following = [task for task in tasks if key(task) > anchor_key]
            if not following:
                return
            if previous:
                previous_end = previous[-1]["planned_end"]
                first_start = next_business_day(previous_end)
            else:
                first_start = anchor["planned_start"]
            for index, task in enumerate(following):
                start = first_start if index == 0 else next_business_day(previous_end)
                end = business_end(start, int(task["estimated_days"]))
                if task.get("planned_start") != start or task.get("planned_end") != end:
                    self.store.reschedule(task, start, end, actor, anchor.get("code"))
                previous_end = end
            return
        position = next((index for index, task in enumerate(tasks) if task.get("id") == anchor.get("id")), None)
        if position is None:
            return
        previous_end = anchor["planned_end"]
        for task in tasks[position + 1:]:
            start = next_business_day(previous_end)
            end = business_end(start, int(task["estimated_days"]))
            if task.get("planned_start") != start or task.get("planned_end") != end:
                self.store.reschedule(task, start, end, actor, anchor.get("code"))
            previous_end = end

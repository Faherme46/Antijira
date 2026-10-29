"""Validaciones y contrato de columnas, independientes del transporte."""
import json
import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from .calendar_co import business_end

ROOT = Path(__file__).resolve().parent.parent
COLUMNS = {
    "code": "CODIGO", 
    "name": "SOPORTE", 
    "module": "MODULO",
    "priority": "PRIORIDAD", 
    "assignee": "responsable",
    "planned_start": "fecha inicio",
    "estimated_days": "TIEMPO ESTIMADO (DIAS)", 
    "planned_end": "Fecha entrega", 
    "actual_start": "Inicio real",
    "actual_end": "ENTREGA REAL", 
    "status": "Estado",
    "complexity": "COMPLEJIDAD",
    "description": "DESCRIPCION",
    "requirements": "REQUERIMIENTO",
    "end_mode": "MODO ENTREGA",
    "dead_time": "TIEMPO MUERTO",
}
DATE_FIELDS = ("planned_start", "planned_end", "actual_start", "actual_end")
ALIASES = {"name": ["ERRORES", "Nombre"], "module": ["RELACIONADO"],
           "code": ["Código"], "assignee": ["Responsable"],
           "planned_start": ["Inicio programado"], "planned_end": ["Fin programado"],
           "actual_end": ["Fin real"], "requirements": ["REQUERIMIENTOS"]}


def normalized_header(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFD", str(value)) if not unicodedata.combining(c)).upper().split())


def column_positions(headers):
    result = {}
    for field, label in COLUMNS.items():
        names = {normalized_header(v) for v in [label, *ALIASES.get(field, [])]}
        matches = [i for i, header in enumerate(headers) if normalized_header(header) in names]
        if len(matches) > 1:
            raise AppError(f"Hay encabezados ambiguos para {label}. Revisa la hoja.", 409)
        if matches:
            result[field] = matches[0]
    return result


class AppError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError) as exc:
        raise AppError(f"No se pudo leer {Path(path).name}. Revisa el archivo JSON local.", 503) from exc


def parameters():
    data = read_json(ROOT / "config" / "parameters.json")
    for key in ("people", "complexities", "statuses"):
        values = data.get(key)
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values) or len(set(values)) != len(values):
            raise AppError(f"La configuración '{key}' debe ser una lista de textos únicos.", 503)
    completed = data.get("completed_statuses", [])
    if not isinstance(completed, list) or any(v not in data["statuses"] for v in completed):
        raise AppError("completed_statuses debe contener estados configurados.", 503)
    data.setdefault("started_statuses", [s for s in data["statuses"] if s.upper() in ("EN CURSO", "EN PROCESO")])
    data.setdefault("saturday_workers", [])
    if not isinstance(data["started_statuses"], list) or any(s not in data["statuses"] for s in data["started_statuses"]):
        raise AppError("started_statuses debe contener estados configurados.", 503)
    if not isinstance(data["saturday_workers"], list) or any(p not in data["people"] for p in data["saturday_workers"]):
        raise AppError("saturday_workers debe contener personas configuradas.", 503)
    if not isinstance(data.get("code_prefix"), str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,15}", data["code_prefix"]):
        raise AppError("code_prefix debe ser un prefijo alfanumérico válido.", 503)
    for key, low, high in (("code_digits", 1, 10), ("upcoming_days", 0, 365)):
        if type(data.get(key)) is not int or not low <= data[key] <= high:
            raise AppError(f"Configuración inválida: {key}.", 503)
    return data


def normalize_date(value):
    if value in (None, ""):
        return ""
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        try:
            return (date(1899, 12, 30) + timedelta(days=int(value))).isoformat()
        except (ValueError, OverflowError):
            raise ValueError("Fecha serial fuera de rango")
    text = str(value).strip()
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", text):
        d, m, y = map(int, text.split("/"))
        return date(y, m, d).isoformat()
    return date.fromisoformat(text).isoformat()


def numeric_priority(value):
    try:
        if isinstance(value, bool) or not re.fullmatch(r"\d+(?:[.,]\d+)?", str(value).strip()):
            raise ValueError()
        number = Decimal(str(value).strip().replace(",", "."))
        if not number.is_finite() or number <= 0 or number > 1000000:
            raise ValueError()
        return format(number.normalize(), "f")
    except (ValueError, InvalidOperation):
        raise AppError("La prioridad debe ser un número positivo, por ejemplo 1, 5,5 o 6,2.")


def duration_days(value):
    try:
        if isinstance(value, bool) or not re.fullmatch(r"\d+(?:\.0+)?", str(value).strip()):
            raise ValueError()
        days = int(Decimal(str(value)))
        if not 1 <= days <= 3660:
            raise ValueError()
        return days
    except (ValueError, InvalidOperation):
        raise AppError("El tiempo estimado debe ser un entero entre 1 y 3660 días hábiles.")


def valid_date(value, field, optional=False):
    if optional and value == "":
        return ""
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError()
        result = normalize_date(value)
        if not 1900 <= date.fromisoformat(result).year <= 2090:
            raise ValueError()
        return result
    except (TypeError, ValueError):
        raise AppError(f"{COLUMNS[field]} debe ser una fecha válida entre 1900 y 2090.")


def valid_attachments(value):
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > 25:
        raise AppError("Los adjuntos deben ser una lista de máximo 25 enlaces.")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise AppError("Cada adjunto necesita nombre y enlace.")
        name = item.get("name", "")
        link = item.get("link", "")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 180:
            raise AppError("El nombre de cada adjunto debe tener entre 1 y 180 caracteres.")
        if not isinstance(link, str) or len(link.strip()) > 2048:
            raise AppError("El enlace del adjunto no es válido.")
        parsed = urlsplit(link.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise AppError("Los adjuntos deben usar enlaces completos http:// o https://.")
        result.append({"name": name.strip(), "link": link.strip()})
    return result


def validate_task(payload, config, existing=None):
    if not isinstance(payload, dict):
        raise AppError("Envía un objeto JSON con los datos de la tarea.")
    task = {key: existing.get(key, "") if existing else "" for key in COLUMNS}
    changed = lambda key: existing is None or key in payload
    for key, limit in (("name", 1000), ("module", 180), ("description", 15000), ("requirements", 15000)):
        if not changed(key):
            continue
        value = payload.get(key, "")
        if not isinstance(value, str) or len(value.strip()) > limit or (key == "name" and len(value.strip()) < 3):
            raise AppError(f"{COLUMNS[key]} no es válido (máximo {limit} caracteres).")
        task[key] = value.strip()
    for field, options in (("assignee", "people"), ("complexity", "complexities"), ("status", "statuses")):
        if not changed(field):
            continue
        if payload.get(field) not in config[options]:
            raise AppError(f"Selecciona un valor válido para {COLUMNS[field].lower()}.")
        task[field] = payload[field]
    if changed("priority"):
        task["priority"] = numeric_priority(payload.get("priority", ""))
    if changed("dead_time"):
        value = payload.get("dead_time", False)
        if type(value) is not bool:
            raise AppError("Tiempo muerto debe ser un valor sí/no.")
        task["dead_time"] = value
    if existing is None or "attachments" in payload:
        task["attachments"] = valid_attachments(payload.get("attachments", []))
    else:
        task["attachments"] = valid_attachments(existing.get("attachments", []))
    if changed("estimated_days"):
        task["estimated_days"] = duration_days(payload.get("estimated_days", ""))
    if changed("planned_start"):
        task["planned_start"] = valid_date(payload.get("planned_start"), "planned_start")
   
    if changed("end_mode"):
        task["end_mode"] = payload.get("end_mode", "auto")
        if task["end_mode"] not in ("auto", "manual"):
            raise AppError("El modo de entrega debe ser auto o manual.")
    schedule_changed = existing is None or any(k in payload for k in ("planned_start", "estimated_days",  "end_mode", "planned_end"))
    if schedule_changed:
        if task["end_mode"] == "auto":
            start = valid_date(task["planned_start"], "planned_start")
            task["planned_end"] = business_end(start, duration_days(task["estimated_days"] ))
        else:
            task["planned_end"] = valid_date(payload.get("planned_end", task["planned_end"]), "planned_end")
    if schedule_changed and task["planned_end"] < task["planned_start"]:
        raise AppError("El fin programado no puede ser anterior al inicio programado.")
    if existing:
        for field in ("actual_start", "actual_end"):
            if field in payload:
                task[field] = valid_date(payload[field], field, optional=True)
        if any(k in payload for k in ("actual_start", "actual_end")) and task["actual_start"] and task["actual_end"] and task["actual_end"] < task["actual_start"]:
            raise AppError("La finalización real no puede ser anterior al inicio real.")
    return task


def next_code(tasks, config):
    pattern = re.compile(re.escape(config["code_prefix"]) + r"(\d+)$")
    numbers, seen = [], set()
    for task in tasks:
        code = str(task.get("code", "")).strip()
        if not code:
            continue  # Filas históricas sin código: se asigna al editar, no se borran.
        if code in seen:
            raise AppError("Hay códigos duplicados en Google Sheets. Corrige la hoja antes de registrar tareas.", 409)
        seen.add(code)
        match = pattern.fullmatch(code)
        if not match:
            raise AppError(f"El código {code} no coincide con el prefijo configurado.", 409)
        numbers.append(int(match.group(1)))
    return f"{config['code_prefix']}{max(numbers, default=0) + 1:0{config['code_digits']}d}"

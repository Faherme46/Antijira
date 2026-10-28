"""Operaciones de Google Sheets, preservando encabezados y columnas del usuario."""
import hashlib
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import quote
from .domain import AppError, COLUMNS, DATE_FIELDS, ROOT, normalize_date, read_json, column_positions, normalized_header, numeric_priority


def column_letter(index):
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def revision(row):
    values = list(row)
    while values and values[-1] in (None, ""):
        values.pop()
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class SheetsStore:
    def __init__(self, config_path=None):
        self.config_path = Path(config_path or ROOT / "config" / "google_sheets.json")
        self.lock = threading.RLock()

    def categories(self):
        config = read_json(self.config_path)
        categories = config.get("categories") or [{"id": "desarrollo", "name": "Desarrollo", "worksheet": config.get("worksheet", "PRIORIDADES DESARROLLO"), "code_prefix": "DS-", "code_digits": 3}]
        if not isinstance(categories, list) or not categories:
            raise AppError("Configura una lista de categorías en google_sheets.json.", 503)
        for category in categories:
            if not isinstance(category, dict) or any(not isinstance(category.get(k), str) or not category[k].strip() for k in ("id", "name", "worksheet", "code_prefix")):
                raise AppError("Cada categoría necesita id, name, worksheet y code_prefix.", 503)
            if not re.fullmatch(r"[a-z0-9_-]+", category["id"]) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,15}", category["code_prefix"]):
                raise AppError("Identificador o prefijo de categoría inválido.", 503)
            if type(category.get("code_digits", 3)) is not int or not 1 <= category.get("code_digits", 3) <= 10:
                raise AppError("code_digits de categoría debe estar entre 1 y 10.", 503)
        for key in ("id", "worksheet", "code_prefix"):
            if len({c[key] for c in categories}) != len(categories):
                raise AppError(f"Las categorías deben tener {key} únicos.", 503)
        return categories

    def category(self, category_id):
        for category in self.categories():
            if category["id"] == category_id:
                return category
        raise AppError("La categoría seleccionada no existe.", 404)

    def _session(self):
        config = read_json(self.config_path)
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", str(config.get("spreadsheet_id", ""))) or config["spreadsheet_id"].startswith("REEMPLAZAR"):
            raise AppError("Configura spreadsheet_id en config/google_sheets.json.", 503)
        raw = config.get("credentials_file")
        if not isinstance(raw, str) or not raw.strip():
            raise AppError("Configura credentials_file con la ruta de la clave de servicio.", 503)
        path = Path(os.path.expandvars(raw)).expanduser()
        if not path.is_absolute():
            path = self.config_path.parent / path
        path = path.resolve()
        if not path.is_file():
            raise AppError("No se encontró la clave de servicio. Revisa credentials_file.", 503)
        timeout = config.get("timeout_seconds", 20)
        if type(timeout) not in (int, float) or not 1 <= timeout <= 60:
            raise AppError("timeout_seconds debe estar entre 1 y 60.", 503)
        try:
            from google.oauth2 import service_account
            from google.auth.transport.requests import AuthorizedSession
        except ImportError as exc:
            raise AppError("Instala las dependencias: pip install -r requirements.txt.", 503) from exc
        try:
            credentials = service_account.Credentials.from_service_account_file(
                str(path), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        except Exception as exc:
            raise AppError("No se pudo cargar la clave de servicio. Comprueba su formato y permisos.", 503) from exc
        return config, AuthorizedSession(credentials, refresh_timeout=timeout)

    def _api(self, method, suffix, body=None, params=None):
        config, session = self._session()
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{config['spreadsheet_id']}{suffix}"
        try:
            response = session.request(method, url, json=body, params=params, timeout=config.get("timeout_seconds", 20))
        except Exception as exc:
            message = "No se pudo conectar con Google Sheets. Comprueba la conexión y las credenciales."
            if method != "GET":
                message += " El resultado del envío es incierto: actualiza el tablero antes de reintentar."
            raise AppError(message, 502) from exc
        finally:
            session.close()
        if response.status_code in (401, 403):
            raise AppError("Google rechazó el acceso. Habilita Sheets API y comparte la hoja con la cuenta de servicio como Editor.", 502)
        if response.status_code in (400, 404):
            raise AppError("No se encontró la pestaña o el rango, o la estructura no permite la operación. Revisa las categorías y ejecuta --init-sheet si falta una pestaña.", 502)
        if response.status_code == 429:
            raise AppError("Se alcanzó el límite de Google Sheets. Espera unos segundos y vuelve a intentar.", 503)
        if not response.ok:
            raise AppError("Google Sheets no pudo completar la operación. Actualiza antes de reintentar una escritura.", 502)
        try:
            return response.json()
        except ValueError as exc:
            raise AppError("Google Sheets devolvió una respuesta inválida. Actualiza antes de reintentar.", 502) from exc

    def _request(self, method, cell_range, body=None, params=None, category=None):
        category = category or self.categories()[0]
        name = "'" + category["worksheet"].replace("'", "''") + "'"
        if cell_range:
            name += "!" + cell_range
        suffix = "/values/" + quote(name, safe="") + (":append" if method == "POST" else "")
        return self._api(method, suffix, body, params)

    def metadata(self):
        result = self._api("GET", "", params={"fields": "sheets.properties"})
        return {s["properties"]["title"]: s["properties"] for s in result.get("sheets", [])}

    def read(self, category=None):
        category = category or self.categories()[0]
        values = self._request("GET", "", params={"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER"}, category=category).get("values", [])
        if not values:
            return [], [], 2, [f"{category['name']}: pestaña vacía; se prepararán encabezados al guardar."]
        headers = [str(v).strip() for v in values[0]]
        positions = column_positions(headers)
        if "name" not in positions or "code" not in positions:
            raise AppError(f"{category['name']}: faltan encabezados de código y nombre. No se modificó la hoja.", 409)
        tasks, warnings = [], []
        for row_number, row in enumerate(values[1:], 2):
            if not any(value not in (None, "") for value in row):
                continue
            if not any(row[positions[k]] if positions[k] < len(row) else "" for k in ("code", "name")):
                continue
            task = {key: row[positions[key]] if key in positions and positions[key] < len(row) else "" for key in COLUMNS}
            for key in COLUMNS:
                if key not in DATE_FIELDS:
                    task[key] = str(task[key]) if task[key] is not None else ""
            for key in DATE_FIELDS:
                try:
                    task[key] = normalize_date(task[key])
                except (ValueError, TypeError):
                    warnings.append(f"{category['name']}, fila {row_number}: {COLUMNS[key]} no es una fecha válida.")
                    task[key] = ""
           
            task["end_mode"] = task["end_mode"] if task["end_mode"] in ("auto", "manual") else "manual"
            try:
                task["priority"] = numeric_priority(task["priority"])
            except AppError:
                pass
            task.update(category=category["id"], category_name=category["name"], row=row_number,
                        revision=revision(row), _raw=row)
            if not task["code"]:
                task["uncoded"] = True
            tasks.append(task)
        if any(not t["code"] for t in tasks):
            warnings.append(f"{category['name']}: hay tareas históricas sin código; se asignará al guardarlas.")
        if any(not t["estimated_days"] for t in tasks):
            warnings.append(f"{category['name']}: hay tareas sin duración en días; se conserva su entrega histórica hasta que las edites.")
        return tasks, headers, len(values) + 1, warnings

    def append(self, task, headers, next_row, category=None):
        row = [""] * len(headers)
        for key, index in column_positions(headers).items():
            row[index] = task.get(key, "")
        return self._request("POST", f"A{next_row}:{column_letter(len(headers))}{next_row}",
                             {"majorDimension": "ROWS", "values": [row]},
                             {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}, category=category)

    def prepare(self, category):
        """Solo añade encabezados ausentes al final, sin ocupar columnas con datos."""
        meta = self.metadata()
        if category["worksheet"] not in meta:
            result = self._api("POST", ":batchUpdate", {"requests": [{"addSheet": {"properties": {"title": category["worksheet"]}}}]})
            properties = result["replies"][0]["addSheet"]["properties"]
            headers = []
        else:
            properties = meta[category["worksheet"]]
            _, headers, _, _ = self.read(category)
        positions = column_positions(headers)
        missing = [COLUMNS[k] for k in COLUMNS if k not in positions]
        if not missing:
            return
        raw = self._request("GET", "", category=category).get("values", [])
        start = max([len(headers), *[len(row) for row in raw]])
        end = start + len(missing)
        requests = []
        capacity = properties["gridProperties"]["columnCount"]
        if end > capacity:
            requests.append({"appendDimension": {"sheetId": properties["sheetId"], "dimension": "COLUMNS", "length": end - capacity}})
        requests.append({"updateCells": {"start": {"sheetId": properties["sheetId"], "rowIndex": 0, "columnIndex": start},
                       "rows": [{"values": [{"userEnteredValue": {"stringValue": h}} for h in missing]}], "fields": "userEnteredValue"}})
        self._api("POST", ":batchUpdate", {"requests": requests})

    def update(self, task, fields, headers, row_number, category):
        positions = column_positions(headers)
        name = "'" + category["worksheet"].replace("'", "''") + "'!"
        data = [{"range": name + column_letter(positions[key]+1) + str(row_number), "values": [[task[key]]]} for key in fields if key in positions]
        if data:
            self._api("POST", "/values:batchUpdate", {"valueInputOption": "RAW", "data": data})

    def move(self, task, old, source_headers, category, destination, dest_headers, dest_row):
        """Traslado atómico: conserva extras compatibles y elimina solo la fila origen."""
        meta = self.metadata()
        src, dst = meta[category["worksheet"]], meta[destination["worksheet"]]
        source_positions = column_positions(source_headers)
        target_positions = column_positions(dest_headers)
        extras = [i for i in range(len(old["_raw"])) if i not in source_positions.values() and old["_raw"][i] not in (None, "")]
        target_names = {normalized_header(h): i for i, h in enumerate(dest_headers) if h}
        if any(i >= len(source_headers) or not source_headers[i] or normalized_header(source_headers[i]) not in target_names for i in extras):
            raise AppError("La tarea tiene columnas adicionales que no existen en destino. Agrega esos encabezados antes de cambiar la categoría para conservar sus datos.", 409)
        requests = []
        if dest_row > dst["gridProperties"]["rowCount"]:
            requests.append({"appendDimension": {"sheetId": dst["sheetId"], "dimension": "ROWS", "length": dest_row-dst["gridProperties"]["rowCount"]}})
        for i in extras:
            j = target_names[normalized_header(source_headers[i])]
            requests.append({"copyPaste": {"source": {"sheetId": src["sheetId"], "startRowIndex": old["row"]-1, "endRowIndex": old["row"], "startColumnIndex": i, "endColumnIndex": i+1},
                                          "destination": {"sheetId": dst["sheetId"], "startRowIndex": dest_row-1, "endRowIndex": dest_row, "startColumnIndex": j, "endColumnIndex": j+1}, "pasteType": "PASTE_NORMAL"}})
        for key, index in target_positions.items():
            if key in source_positions and task.get(key) == old.get(key):
                i = source_positions[key]
                requests.append({"copyPaste": {"source": {"sheetId": src["sheetId"], "startRowIndex": old["row"]-1, "endRowIndex": old["row"], "startColumnIndex": i, "endColumnIndex": i+1},
                                              "destination": {"sheetId": dst["sheetId"], "startRowIndex": dest_row-1, "endRowIndex": dest_row, "startColumnIndex": index, "endColumnIndex": index+1}, "pasteType": "PASTE_NORMAL"}})
                continue
            value = task.get(key, "")
            cell = {"boolValue": value} if type(value) is bool else {"numberValue": value} if isinstance(value, (int, float)) else {"stringValue": str(value)}
            requests.append({"updateCells": {"start": {"sheetId": dst["sheetId"], "rowIndex": dest_row-1, "columnIndex": index}, "rows": [{"values": [{"userEnteredValue": cell}]}], "fields": "userEnteredValue"}})
        requests.append({"deleteDimension": {"range": {"sheetId": src["sheetId"], "dimension": "ROWS", "startIndex": old["row"]-1, "endIndex": old["row"]}}})
        self._api("POST", ":batchUpdate", {"requests": requests})

    def initialize(self, category=None):
        with self.lock:
            for item in ([category] if category else self.categories()):
                self.prepare(item)

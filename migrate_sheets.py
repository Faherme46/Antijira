"""Importación opcional. Sin --apply solo comprueba; nunca escribe en Sheets."""
import argparse
import hashlib
import json
from antijira.domain import AppError, COLUMNS, read_json
from antijira.mongodb import MongoStore
from antijira.sheets import SheetsStore


def collect(source):
    config = read_json(source.config_path)
    records, warnings = [], []
    for category in source.categories():
        tasks, headers, _, messages = source.read(category)
        warnings.extend(messages)
        for task in tasks:
            identity = json.dumps([config["spreadsheet_id"], category["worksheet"], task["row"]], ensure_ascii=False)
            records.append({
                "migration_key": hashlib.sha256(identity.encode()).hexdigest(),
                "task": {**{key: task.get(key, "") for key in COLUMNS}, "category": category["id"]},
                "legacy": {"worksheet": category["worksheet"], "row": task["row"], "headers": headers, "values": task["_raw"]},
            })
    return records, warnings


def main():
    parser = argparse.ArgumentParser(description="Importar Google Sheets a MongoDB sin alterar la hoja original")
    parser.add_argument("--apply", action="store_true", help="Insertar tareas. Sin esta opción solo comprueba duplicados y cuenta registros.")
    args = parser.parse_args()
    target = MongoStore()
    try:
        records, warnings = collect(SheetsStore())
        result = target.import_records(records, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        for warning in warnings:
            print(warning)
        if not args.apply:
            print("Comprobación terminada. Usa --apply para importar. No se insertaron tareas.")
    except AppError as exc:
        parser.exit(1, str(exc) + "\n")
    finally:
        target.close()


if __name__ == "__main__":
    main()

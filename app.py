"""Servidor HTTP nativo. Ejecutar: python app.py"""
import argparse
import json
import logging
import secrets
import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlsplit, unquote
from antijira.domain import AppError, ROOT, parameters, duration_days, valid_date
from antijira.calendar_co import business_end
from antijira.locking import InstanceLock
from antijira.mongo_service import MongoTaskService
from antijira.mongodb import MongoStore
from antijira.auth import AuthService

ASSETS = {"/": ("templates/index.html", "text/html; charset=utf-8"),
          "/static/app.js": ("static/app.js", "text/javascript; charset=utf-8"),
          "/static/auth.js": ("static/auth.js", "text/javascript; charset=utf-8"),
          "/static/table.js": ("static/table.js", "text/javascript; charset=utf-8"),
          "/static/auth.css": ("static/auth.css", "text/css; charset=utf-8"),
          "/static/table.css": ("static/table.css", "text/css; charset=utf-8"),
          "/static/styles-v2.css": ("static/styles-v2.css", "text/css; charset=utf-8"),
          "/static/styles.css": ("static/styles.css", "text/css; charset=utf-8")}


def make_handler(service, auth_service=None):
    csrf_token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Antijira/1.0"

        def reply(self, status, body, content_type="application/json; charset=utf-8", cookies=None):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8") if isinstance(body, (dict, list)) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for cookie in cookies or []:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(data)

        def session_token(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                return cookie["antijira_session"].value if "antijira_session" in cookie else ""
            except Exception:
                return ""

        def current_user(self):
            if auth_service is None:
                return {"id": 0, "username": "test", "role": "admin", "active": True, "permanent": True}
            return auth_service.authenticate(self.session_token())

        def require_user(self, admin=False):
            user = self.current_user()
            if not user:
                raise AppError("Debes iniciar sesión.", 401)
            if admin and user.get("role") != "admin":
                raise AppError("Se requieren permisos de administrador.", 403)
            return user

        def read_json_body(self):
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise AppError("El contenido debe enviarse como application/json.", 415)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise AppError("Tamaño del mensaje inválido.")
            if not 0 < length <= 131072:
                raise AppError("La solicitud está vacía o excede el tamaño permitido.", 413)
            try:
                payload = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                raise AppError("El cuerpo JSON no es válido.")
            if not isinstance(payload, dict):
                raise AppError("Envía un objeto JSON.")
            return payload

        def do_GET(self):
            try:
                path = urlsplit(self.path).path
                if path in ASSETS:
                    name, mime = ASSETS[path]
                    return self.reply(200, (ROOT / name).read_bytes(), mime)
                if path == "/api/session":
                    user = self.current_user()
                    return self.reply(200, {"authenticated": bool(user), "user": user})
                if path == "/api/config":
                    self.require_user()
                    return self.reply(200, {"parameters": service.public_config(), "csrf_token": csrf_token})
                if path == "/api/tasks":
                    self.require_user()
                    return self.reply(200, service.list_tasks())
                if path == "/api/users":
                    user = self.require_user(admin=True)
                    return self.reply(200, {"users": auth_service.list_users(user)})
                if path == "/api/audit":
                    user = self.require_user(admin=True)
                    return self.reply(200, {"audit": auth_service.list_audit(user)})
                self.reply(404, {"error": "Recurso no encontrado."})
            except AppError as exc:
                self.reply(exc.status, {"error": str(exc)})
            except Exception:
                logging.exception("Error al consultar")
                self.reply(500, {"error": "Error interno del servidor. Consulta el registro de Python."})

        def do_POST(self):
            try:
                path = unquote(urlsplit(self.path).path)
                parts = path.strip("/").split("/")
                if path == "/api/login":
                    if auth_service is None:
                        return self.reply(200, {"user": self.current_user()})
                    payload = self.read_json_body()
                    token, user = auth_service.login(payload.get("username", ""), payload.get("password", ""), self.client_address[0])
                    cookie = f"antijira_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200"
                    return self.reply(200, {"user": user}, cookies=[cookie])
                if path == "/api/logout":
                    user = self.require_user()
                    if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf_token):
                        raise AppError("Sesión de página vencida. Recarga la página.", 403)
                    if auth_service:
                        auth_service.logout(self.session_token(), user)
                    return self.reply(200, {"ok": True}, cookies=["antijira_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"])
                if path == "/api/users":
                    user = self.require_user(admin=True)
                    if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf_token):
                        raise AppError("Sesión de página vencida. Recarga la página.", 403)
                    payload = self.read_json_body()
                    return self.reply(201, {"user": auth_service.create_user(payload, user)})
                editing = self.command == "PUT" and len(parts) == 4 and parts[:2] == ["api", "tasks"]
                if not editing and (self.command != "POST" or path not in ("/api/tasks", "/api/schedule")):
                    return self.reply(404, {"error": "Recurso no encontrado."})
                user = self.require_user()
                if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf_token):
                    raise AppError("Sesión de página vencida. Recarga la página.", 403)
                payload = self.read_json_body()
                if path == "/api/schedule":
                    start = valid_date(payload.get("planned_start"), "planned_start")
                    days = duration_days(payload.get("estimated_days"))
                    saturdays = False
                    if type(saturdays) is not bool:
                        raise AppError("La opción de sábados debe ser sí/no.")
                    return self.reply(200, {"planned_end": business_end(start, days, saturdays)})
                if editing:
                    if not parts[3].isdigit() or int(parts[3]) < 2:
                        raise AppError("Referencia de tarea inválida.")
                    task = service.update_task(parts[2], int(parts[3]), payload, user) if auth_service else service.update_task(parts[2], int(parts[3]), payload)
                    return self.reply(200, {"task": task})
                task = service.create_task(payload, user) if auth_service else service.create_task(payload)
                self.reply(201, {"task": task})
            except AppError as exc:
                self.reply(exc.status, {"error": str(exc)})
            except Exception as e:
                logging.exception("Error al registrar")
                self.reply(500, {"error": "No se pudo confirmar el registro. Actualiza el tablero antes de reintentar." + str(e)})

        def do_PUT(self):
            self.do_POST()

        def setup(self):
            super().setup()
            self.connection.settimeout(60)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Antijira · gestión de tareas")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=10000)
    parser.add_argument("--init-db", action="store_true", help="Comprobar MongoDB y crear índices y contadores")
    parser.add_argument("--init-admin", action="store_true", help="Crear una sola vez el administrador permanente")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        with InstanceLock():
            store = MongoStore()
            if args.init_db:
                store.initialize()
                store.close()
                print("MongoDB preparado. Ya puedes ejecutar python app.py.")
                return
            auth = AuthService(store)
            if args.init_admin:
                password = getpass.getpass("Contraseña inicial del administrador (mínimo  caracteres): ")
                confirm = getpass.getpass("Repite la contraseña: ")
                if password != confirm:
                    raise AppError("Las contraseñas no coinciden.")
                user = auth.ensure_admin(password)
                store.close()
                print(f"Administrador permanente creado: {user['username']}")
                return
            try:
                with ThreadingHTTPServer((args.host, args.port), make_handler(MongoTaskService(store), auth)) as server:
                    print(f"Antijira disponible en http://{args.host}:{args.port}", flush=True)
                    server.serve_forever()
            finally:
                store.close()
    except (AppError, OSError) as exc:
        parser.exit(1, f"No se pudo iniciar Antijira: {exc}\n")
    except KeyboardInterrupt:
        print("\nServidor detenido.")


if __name__ == "__main__":
    main()

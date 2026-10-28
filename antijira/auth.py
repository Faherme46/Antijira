"""Autenticación local sobre MongoDB: usuarios, sesiones y auditoría."""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from .domain import AppError


def username_key(value):
    return str(value).strip().casefold()


def validate_username(value):
    value = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,40}", value):
        raise AppError("El usuario debe tener entre 3 y 40 caracteres: letras, números, punto, guion o guion bajo.")
    return value


def validate_password(value):
    if not isinstance(value, str) or len(value) < 10 or len(value) > 256:
        raise AppError("La contraseña debe tener entre 10 y 256 caracteres.")
    return value


def hash_password(password, salt=None):
    validate_password(password)
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return {"algorithm": "scrypt-16384-8-1", "salt": salt.hex(), "digest": digest.hex()}


def verify_password(password, stored):
    try:
        salt = bytes.fromhex(stored["salt"])
        expected = bytes.fromhex(stored["digest"])
        actual = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(actual, expected)
    except (KeyError, TypeError, ValueError):
        return False


class AuthService:
    def __init__(self, store, session_hours=12):
        self.store = store
        self.session_hours = session_hours

    def _ready(self):
        self.store.initialize()

    def ensure_admin(self, password, username="admin"):
        self._ready()
        username = validate_username(username)
        validate_password(password)
        existing = self.store.db.users.find_one({"role": "admin", "permanent": True})
        if existing:
            raise AppError("El administrador permanente ya existe.", 409)
        now = datetime.now(timezone.utc)
        user = {"_id": self.store._next("user_id"), "username": username, "username_key": username_key(username),
                "password": hash_password(password), "role": "admin", "active": True, "permanent": True,
                "created_at": now, "created_by": "setup", "failed_logins": 0}
        self.store.db.users.insert_one(user)
        self.store.record_audit("user.bootstrap_admin", {"username": "setup"}, {"username": username})
        return self.public_user(user)

    @staticmethod
    def public_user(user):
        return {"id": user["_id"], "username": user["username"], "role": user["role"],
                "active": bool(user.get("active", True)), "permanent": bool(user.get("permanent", False)),
                "created_at": user.get("created_at").isoformat() if isinstance(user.get("created_at"), datetime) else user.get("created_at", "")}

    def create_user(self, payload, actor):
        if actor.get("role") != "admin":
            raise AppError("Solo el administrador puede crear usuarios.", 403)
        username = validate_username(payload.get("username", ""))
        password = validate_password(payload.get("password", ""))
        role = payload.get("role", "user")
        if role not in ("user", "admin"):
            raise AppError("El rol debe ser user o admin.")
        now = datetime.now(timezone.utc)
        user = {"_id": self.store._next("user_id"), "username": username, "username_key": username_key(username),
                "password": hash_password(password), "role": role, "active": True, "permanent": False,
                "created_at": now, "created_by": actor["username"], "failed_logins": 0}
        try:
            self.store.db.users.insert_one(user)
        except Exception as exc:
            from pymongo.errors import DuplicateKeyError
            if isinstance(exc, DuplicateKeyError):
                raise AppError("Ese nombre de usuario ya existe.", 409) from exc
            raise
        self.store.record_audit("user.create", actor, {"user_id": user["_id"], "username": username, "role": role})
        return self.public_user(user)

    def list_users(self, actor):
        if actor.get("role") != "admin":
            raise AppError("Solo el administrador puede consultar usuarios.", 403)
        return [self.public_user(user) for user in self.store.db.users.find().sort("username_key", 1)]

    def login(self, username, password, remote_address=""):
        self._ready()
        key = username_key(username)
        user = self.store.db.users.find_one({"username_key": key})
        now = datetime.now(timezone.utc)
        locked_until = user.get("locked_until") if user else None
        if locked_until and locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if locked_until and locked_until > now:
            raise AppError("Demasiados intentos. Espera 15 minutos antes de volver a iniciar sesión.", 429)
        valid = bool(user and user.get("active", True) and isinstance(password, str) and verify_password(password, user.get("password", {})))
        if not valid:
            if user:
                attempts = int(user.get("failed_logins", 0)) + 1
                update = {"failed_logins": attempts}
                if attempts >= 5:
                    update.update(failed_logins=0, locked_until=now + timedelta(minutes=15))
                self.store.db.users.update_one({"_id": user["_id"]}, {"$set": update})
            self.store.record_audit("auth.login_failed", {"username": str(username)[:40]}, {"remote_address": remote_address})
            raise AppError("Usuario o contraseña incorrectos.", 401)
        self.store.db.users.update_one({"_id": user["_id"]}, {"$set": {"failed_logins": 0, "last_login_at": now}, "$unset": {"locked_until": ""}})
        token = secrets.token_urlsafe(32)
        session = {"_id": hashlib.sha256(token.encode()).hexdigest(), "user_id": user["_id"],
                   "created_at": now, "expires_at": now + timedelta(hours=self.session_hours), "remote_address": remote_address}
        self.store.db.sessions.insert_one(session)
        actor = self.public_user(user)
        self.store.record_audit("auth.login", actor, {"remote_address": remote_address})
        return token, actor

    def authenticate(self, token):
        if not token:
            return None
        self._ready()
        session_id = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        session = self.store.db.sessions.find_one({"_id": session_id, "expires_at": {"$gt": now}})
        if not session:
            return None
        user = self.store.db.users.find_one({"_id": session["user_id"], "active": True})
        return self.public_user(user) if user else None

    def logout(self, token, actor):
        if token:
            self.store.db.sessions.delete_one({"_id": hashlib.sha256(token.encode()).hexdigest()})
        self.store.record_audit("auth.logout", actor)

    def list_audit(self, actor, limit=100):
        if actor.get("role") != "admin":
            raise AppError("Solo el administrador puede consultar la auditoría.", 403)
        rows = []
        for item in self.store.db.audit.find().sort("created_at", -1).limit(min(max(limit, 1), 200)):
            rows.append({"action": item["action"], "actor": item.get("actor", "system"),
                         "details": item.get("details", {}), "created_at": item["created_at"].isoformat()})
        return rows

"""Single-owner authentication with server-side sessions and schema migrations."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import secrets
import sqlite3
import time
from werkzeug.security import check_password_hash, generate_password_hash
from shared import AppError


class AuthRepository:
    def __init__(self, state):
        state.mkdir(parents=True, exist_ok=True)
        self.db = state / "web.sqlite3"
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                db.executescript("""
                    CREATE TABLE owner(id INTEGER PRIMARY KEY CHECK(id=1),password_hash TEXT NOT NULL);
                    CREATE TABLE sessions(hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires REAL NOT NULL);
                    CREATE TABLE attempts(ip TEXT NOT NULL,at REAL NOT NULL);
                    CREATE INDEX attempts_lookup ON attempts(ip,at);
                    PRAGMA user_version=1;
                """)
            elif version != 1:
                raise RuntimeError("Unsupported web database schema")
        self.db.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def password_hash(self):
        with self.connect() as db:
            row = db.execute("SELECT password_hash FROM owner WHERE id=1").fetchone()
        return row[0] if row else None

    def set_password_hash(self, value):
        with self.connect() as db:
            db.execute("INSERT INTO owner VALUES(1,?) ON CONFLICT(id) DO UPDATE SET password_hash=excluded.password_hash", (value,))
            db.execute("DELETE FROM sessions")

    def fail_count(self, ip):
        with self.connect() as db:
            db.execute("DELETE FROM attempts WHERE at<?", (time.time() - 600,))
            return db.execute("SELECT count(*) FROM attempts WHERE ip=?", (ip,)).fetchone()[0]

    def fail(self, ip):
        with self.connect() as db:
            db.execute("INSERT INTO attempts VALUES(?,?)", (ip, time.time()))

    def session(self, token):
        if not token or len(token) > 256:
            return None
        with self.connect() as db:
            row = db.execute("SELECT hash,csrf,expires FROM sessions WHERE hash=? AND expires>?",
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return dict(row) if row else None

    def create_session(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES(?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), csrf, time.time() + 86400))
        return token, csrf

    def delete_session(self, hashed):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE hash=?", (hashed,))


class AuthService:
    def __init__(self, repository):
        self.repository = repository

    def login(self, password, ip):
        if not isinstance(password, str) or len(password) > 512:
            raise AppError("密码格式不正确。")
        if self.repository.fail_count(ip) >= 8:
            raise AppError("尝试次数过多，请在 10 分钟后再试。", "rate_limited", 429)
        saved = self.repository.password_hash()
        if not saved:
            raise AppError("请先在 NAS 控制台设置网页管理密码。", "setup_required", 503)
        if not check_password_hash(saved, password):
            self.repository.fail(ip)
            raise AppError("管理密码不正确。", "invalid_credentials", 401)
        return self.repository.create_session()

    def set_password(self, password):
        if not isinstance(password, str) or not 12 <= len(password) <= 512:
            raise AppError("网页管理密码需要 12–512 个字符。")
        self.repository.set_password_hash(generate_password_hash(password, method="scrypt"))

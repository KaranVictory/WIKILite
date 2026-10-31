from __future__ import annotations

import base64
import difflib
import hashlib
import ipaddress
import io
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from flask import (
    Flask, abort, flash, g, redirect, render_template, request, session,
    url_for, send_file, send_from_directory, jsonify, Response,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

from namumark import render_namumark, extract_categories, extract_redirect
from feature_rules import MAX_ACCOUNTS_PER_IP, is_personal_subpage_title, reserve_registration_ip_slot

BASE_DIR = Path(__file__).resolve().parent
IS_VERCEL = bool(os.environ.get("VERCEL"))
STATIC_DIR = BASE_DIR / "static"
INSTANCE_DIR = Path(os.environ.get("INSTANCE_DIR") or ("/tmp/namulite-instance" if IS_VERCEL else BASE_DIR / "instance"))
UPLOAD_DIR = STATIC_DIR / "uploads"
INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
# Vercel's deployment filesystem is read-only; uploads are stored in PostgreSQL there.
if not IS_VERCEL:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
# Vercel Functions cap each non-streaming request body at 4.5 MB. Keep image
# uploads below that gateway ceiling; large backup ZIPs use chunked uploads below.
MAX_IMAGE_SIZE = 3 * 1024 * 1024 if IS_VERCEL else 8 * 1024 * 1024
MAX_RESTORE_SIZE = 190 * 1024 * 1024
RESTORE_CHUNK_SIZE = 3 * 1024 * 1024
PRIMARY_TABLES = (
    "users", "pages", "deleted_page_tombstones", "deleted_page_revisions", "page_deletion_events", "revisions", "discussions",
    "site_settings", "page_acls", "user_bans", "user_warnings", "ip_bans", "login_logs",
    "registration_logs", "registration_ip_limits", "namulite_uploads",
)

def load_or_create_secret_key() -> str:
    """Use a stable local key for sessions and encrypted owner settings."""
    configured = os.environ.get("SECRET_KEY")
    if configured:
        return configured
    if IS_VERCEL:
        raise RuntimeError("Vercel 배포에서는 세션을 안정적으로 유지하기 위해 SECRET_KEY 환경 변수를 설정해야 합니다.")
    key_path = INSTANCE_DIR / "secret_key"
    try:
        if key_path.exists():
            value = key_path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(48)
        key_path.write_text(value, encoding="utf-8")
        try:
            key_path.chmod(0o600)
        except OSError:
            pass
        return value
    except OSError:
        # An unwritable instance folder is not ideal, but the server can still start.
        return secrets.token_urlsafe(48)


app = Flask(__name__, static_folder=None)
app.config.update(
    TRUST_PROXY=os.environ.get("TRUST_PROXY", "0") == "1",
    SECRET_KEY=load_or_create_secret_key(),
    DATABASE_URL=os.environ.get("DATABASE_URL", "").strip(),
    DATABASE=os.environ.get("DATABASE_PATH") or str(INSTANCE_DIR / "wiki.db"),
    MAX_CONTENT_LENGTH=(4 * 1024 * 1024 if IS_VERCEL else 200 * 1024 * 1024),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
)
if app.config["TRUST_PROXY"]:
    # Only enable this when running behind a trusted reverse proxy.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0, x_prefix=0)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db():
    if "db" not in g:
        database_url = app.config.get("DATABASE_URL", "")
        if database_url:
            from db_compat import PostgresCompatConnection
            g.db = PostgresCompatConnection(database_url)
        else:
            if IS_VERCEL:
                raise RuntimeError(
                    "Vercel 배포에는 영구 데이터 저장을 위해 DATABASE_URL 환경 변수가 필요합니다. "
                    "Neon/PostgreSQL 연결 문자열을 설정한 뒤 다시 배포하세요."
                )
            db_path = app.config["DATABASE"]
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            g.db = sqlite3.connect(db_path, timeout=15)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
            g.db.execute("PRAGMA busy_timeout = 15000")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def save_uploaded_asset(filename: str, data: bytes) -> None:
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if app.config.get("DATABASE_URL"):
        db = get_db()
        db.execute(
            "INSERT INTO namulite_uploads(filename, content_type, file_data, size_bytes, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(filename) DO UPDATE SET "
            "content_type = excluded.content_type, file_data = excluded.file_data, "
            "size_bytes = excluded.size_bytes, created_at = excluded.created_at",
            (filename, content_type, data, len(data), now_iso()),
        )
        db.commit()
    else:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        (UPLOAD_DIR / filename).write_bytes(data)


def delete_uploaded_asset(filename: str) -> None:
    if not filename or Path(filename).name != filename:
        return
    if app.config.get("DATABASE_URL"):
        db = get_db()
        db.execute("DELETE FROM namulite_uploads WHERE filename = ?", (filename,))
        db.commit()
    else:
        path = UPLOAD_DIR / filename
        if path.parent.resolve() == UPLOAD_DIR.resolve() and path.is_file():
            path.unlink(missing_ok=True)


@app.route("/static/<path:filename>", endpoint="static")
def serve_static_asset(filename: str):
    """Serve versioned static files and durable user-uploaded files."""
    if filename.startswith("uploads/"):
        stored_name = filename[len("uploads/"):]
        if not stored_name or Path(stored_name).name != stored_name:
            abort(404)
        if app.config.get("DATABASE_URL"):
            row = get_db().execute(
                "SELECT content_type, file_data FROM namulite_uploads WHERE filename = ?",
                (stored_name,),
            ).fetchone()
            if not row:
                abort(404)
            return send_file(
                io.BytesIO(bytes(row["file_data"])), mimetype=row["content_type"],
                download_name=stored_name, max_age=86400,
            )
        if not (UPLOAD_DIR / stored_name).is_file():
            abort(404)
    return send_from_directory(STATIC_DIR, filename, max_age=86400)


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            is_owner INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            registered_ip TEXT,
            last_ip TEXT,
            terms_accepted_at TEXT,
            terms_version TEXT,
            privacy_accepted_at TEXT,
            privacy_version TEXT
        );
        CREATE TABLE IF NOT EXISTS pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deleted_page_tombstones (
            title TEXT PRIMARY KEY,
            deleted_at TEXT NOT NULL,
            deleted_by INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS deleted_page_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            original_revision_id INTEGER NOT NULL,
            editor_name TEXT NOT NULL,
            content TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            deleted_at TEXT NOT NULL,
            deleted_by_name TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS deleted_page_revisions_title_idx ON deleted_page_revisions(title, created_at DESC);
        CREATE TABLE IF NOT EXISTS page_deletion_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            deleted_at TEXT NOT NULL,
            deleted_by_name TEXT NOT NULL,
            UNIQUE(title, deleted_at)
        );
        CREATE INDEX IF NOT EXISTS page_deletion_events_title_idx ON page_deletion_events(title, deleted_at DESC);
        CREATE TABLE IF NOT EXISTS revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            editor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            editor_name TEXT NOT NULL,
            content TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            license_consent_at TEXT,
            license_version TEXT,
            ai_policy_version TEXT
        );
        CREATE INDEX IF NOT EXISTS revisions_page_id_idx ON revisions(page_id, id DESC);
        CREATE INDEX IF NOT EXISTS revisions_created_idx ON revisions(id DESC);
        CREATE TABLE IF NOT EXISTS discussions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            username TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS discussions_page_idx ON discussions(page_id, id DESC);
        CREATE TABLE IF NOT EXISTS site_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS page_acls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            action TEXT NOT NULL CHECK(action IN ('read', 'edit', 'discuss')),
            policy TEXT NOT NULL CHECK(policy IN ('everyone', 'member', 'age', 'contributor', 'admin')),
            min_account_days INTEGER NOT NULL DEFAULT 15,
            reason TEXT NOT NULL DEFAULT '',
            updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(page_id, action)
        );
        CREATE TABLE IF NOT EXISTS user_bans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            revoked_at TEXT,
            revoked_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            source_ip TEXT
        );
        CREATE INDEX IF NOT EXISTS user_bans_user_idx ON user_bans(user_id, id DESC);
        CREATE TABLE IF NOT EXISTS user_warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            issued_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            acknowledged_at TEXT
        );
        CREATE INDEX IF NOT EXISTS user_warnings_user_idx ON user_warnings(user_id, id DESC);
        CREATE TABLE IF NOT EXISTS ip_bans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT NOT NULL,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            revoked_at TEXT,
            revoked_by INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS ip_bans_ip_idx ON ip_bans(ip_address, id DESC);
        CREATE INDEX IF NOT EXISTS ip_bans_active_idx ON ip_bans(revoked_at, expires_at);
        CREATE TABLE IF NOT EXISTS login_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            username TEXT NOT NULL,
            ip_address TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS login_logs_created_idx ON login_logs(created_at DESC);
        CREATE INDEX IF NOT EXISTS login_logs_user_idx ON login_logs(user_id, id DESC);
        CREATE TABLE IF NOT EXISTS registration_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            username TEXT NOT NULL,
            ip_address TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS registration_logs_created_idx ON registration_logs(created_at DESC);
        CREATE INDEX IF NOT EXISTS registration_logs_user_idx ON registration_logs(user_id, id DESC);
        CREATE TABLE IF NOT EXISTS registration_ip_limits (
            ip_address TEXT PRIMARY KEY,
            account_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS namulite_uploads (
            filename TEXT PRIMARY KEY,
            content_type TEXT NOT NULL,
            file_data BLOB NOT NULL,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS namulite_restore_uploads (
            upload_id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            total_chunks INTEGER NOT NULL,
            total_bytes BIGINT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS namulite_restore_chunks (
            upload_id TEXT NOT NULL REFERENCES namulite_restore_uploads(upload_id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            chunk_data BLOB NOT NULL,
            PRIMARY KEY(upload_id, chunk_index)
        );
        """
    )
    # Migrate databases created by earlier NamuLite versions without overwriting data.
    # Older releases retained only a tombstone for deleted pages; recover its
    # deletion event (the cascaded revision contents cannot be reconstructed).
    db.execute(
        "INSERT OR IGNORE INTO page_deletion_events(title, deleted_at, deleted_by_name) "
        "SELECT t.title, t.deleted_at, COALESCE(u.username, '운영진') "
        "FROM deleted_page_tombstones t LEFT JOIN users u ON u.id = t.deleted_by"
    )
    user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
    if "registered_ip" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN registered_ip TEXT")
    if "last_ip" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN last_ip TEXT")
    if "is_owner" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN is_owner INTEGER NOT NULL DEFAULT 0")
    # Backward-compatible bootstrap: designate the earliest administrator as owner.
    if not db.execute("SELECT 1 FROM users WHERE is_owner = 1 LIMIT 1").fetchone():
        first_admin = db.execute("SELECT id FROM users WHERE is_admin = 1 ORDER BY id ASC LIMIT 1").fetchone()
        first_user = first_admin or db.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1").fetchone()
        if first_user:
            db.execute("UPDATE users SET is_owner = 1, is_admin = 1 WHERE id = ?", (first_user["id"],))
    for column, sql_type in (
        ("terms_accepted_at", "TEXT"),
        ("terms_version", "TEXT"),
        ("privacy_accepted_at", "TEXT"),
        ("privacy_version", "TEXT"),
    ):
        if column not in user_columns:
            db.execute(f"ALTER TABLE users ADD COLUMN {column} {sql_type}")
    revision_columns = {row["name"] for row in db.execute("PRAGMA table_info(revisions)").fetchall()}
    for column in ("license_consent_at", "license_version", "ai_policy_version"):
        if column not in revision_columns:
            db.execute(f"ALTER TABLE revisions ADD COLUMN {column} TEXT")
    # A public user document (with owner-only editing) is created for every account.
    db.execute(
        "INSERT OR IGNORE INTO pages(title, created_at, updated_at) "
        "SELECT '사용자:' || username, created_at, created_at FROM users u "
        "WHERE NOT EXISTS (SELECT 1 FROM deleted_page_tombstones t WHERE t.title = '사용자:' || u.username)"
    )
    # Backfill the recent registration audit view from legacy account fields where the
    # original registration IP was retained. Records outside the retention window are
    # then removed by the normal cleanup handler.
    db.execute(
        "INSERT INTO registration_logs(user_id, username, ip_address, created_at) "
        "SELECT u.id, u.username, u.registered_ip, u.created_at FROM users u "
        "WHERE u.registered_ip IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM registration_logs l WHERE l.user_id = u.id)"
    )
    # Keep the account-per-IP quota independent from the 180-day audit-log retention.
    ip_counts = db.execute(
        "SELECT registered_ip, COUNT(*) AS account_count FROM users "
        "WHERE registered_ip IS NOT NULL GROUP BY registered_ip"
    ).fetchall()
    for ip_row in ip_counts:
        db.execute(
            "INSERT INTO registration_ip_limits(ip_address, account_count) VALUES (?, ?) "
            "ON CONFLICT(ip_address) DO NOTHING",
            (ip_row["registered_ip"], ip_row["account_count"]),
        )
    db.commit()


with app.app_context():
    init_db()


TERMS_VERSION = "wiki-terms-2026-10-27-v1"
PRIVACY_VERSION = "privacy-notice-2026-10-30-v3"
LICENSE_VERSION = "CC-BY-NC-SA-2.0-KR-2026-10-27-v1"
AI_POLICY_VERSION = "generative-ai-policy-2026-10-27-v1"
LOGIN_LOG_RETENTION_DAYS = 180
REGISTRATION_LOG_RETENTION_DAYS = 180


SITE_DEFAULTS = {
    "site_name": os.environ.get("WIKI_NAME", "나무라이트 위키"),
    "site_tagline": "지식을 함께 모으는 위키입니다.",
    "site_description": "문서를 작성하고 서로의 지식을 나누는 작은 위키입니다.",
    "theme_primary": "#30394a",
    "theme_accent": "#315a9b",
    "site_style": "classic",
    "site_logo": "",
    # Global ACL defaults. Individual documents may override these rules.
    "acl_read_mode": "everyone",
    "acl_read_days": "15",
    "acl_edit_mode": "member",
    "acl_edit_days": "15",
    "acl_discuss_mode": "member",
    "acl_discuss_days": "15",
    # PostgreSQL settings are encrypted/stored for owner-only connection testing.
    # This version continues to use SQLite as the primary wiki datastore.
    "postgres_host": "",
    "postgres_port": "5432",
    "postgres_database": "",
    "postgres_username": "",
    "postgres_password_encrypted": "",
    "postgres_sslmode": "prefer",
    "backup_retention_days": "30",
}


def get_site_settings() -> dict[str, str]:
    settings = dict(SITE_DEFAULTS)
    try:
        rows = get_db().execute("SELECT setting_key, setting_value FROM site_settings").fetchall()
        for row in rows:
            if row["setting_key"] in settings:
                settings[row["setting_key"]] = row["setting_value"]
    except sqlite3.OperationalError:
        # Allows templates to fail gracefully only if a very old database is opened
        # before the schema migration has run; init_db normally creates the table.
        pass
    return settings


def save_site_settings(values: dict[str, str]) -> None:
    db = get_db()
    db.executemany(
        "INSERT INTO site_settings(setting_key, setting_value) VALUES (?, ?) "
        "ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value",
        [(key, value) for key, value in values.items() if key in SITE_DEFAULTS],
    )
    db.commit()


def _password_fernet():
    """Derive an encryption key from the persistent Flask secret key."""
    from cryptography.fernet import Fernet
    material = hashlib.sha256((str(app.config["SECRET_KEY"]) + "|namulite-postgres-password-v1").encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(material))


def encrypt_setting_secret(value: str) -> str:
    return _password_fernet().encrypt(value.encode("utf-8")).decode("ascii") if value else ""


def decrypt_setting_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _password_fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def backup_directory() -> Path:
    # Local archives are temporary on Vercel; the durable copy is stored in PostgreSQL.
    path = (Path("/tmp/namulite-backups") if IS_VERCEL else INSTANCE_DIR / "backups")
    path.mkdir(parents=True, exist_ok=True)
    return path


def configured_backup_retention_days() -> int:
    try:
        return max(1, min(3650, int(get_site_settings().get("backup_retention_days", "30"))))
    except (TypeError, ValueError):
        return 30


def _postgres_connect():
    """Connect to the configured backup store, or DATABASE_URL on Vercel."""
    settings = get_site_settings()
    host = settings.get("postgres_host", "").strip()
    database = settings.get("postgres_database", "").strip()
    username = settings.get("postgres_username", "").strip()
    password = decrypt_setting_secret(settings.get("postgres_password_encrypted", ""))
    import psycopg
    if host and database and username and password:
        return psycopg.connect(
            host=host, port=int(settings.get("postgres_port", "5432")), dbname=database,
            user=username, password=password, sslmode=settings.get("postgres_sslmode", "require"),
            connect_timeout=8, prepare_threshold=None,
        )
    database_url = app.config.get("DATABASE_URL", "")
    if database_url:
        return psycopg.connect(database_url, connect_timeout=8, prepare_threshold=None)
    raise RuntimeError("소유자 설정에서 PostgreSQL 연결 정보를 저장하거나 DATABASE_URL 환경 변수를 설정해 주세요.")


def _ensure_pg_backup_table(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS namulite_backups (
                id BIGSERIAL PRIMARY KEY,
                filename TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                archive BYTEA NOT NULL,
                size_bytes BIGINT NOT NULL
            )
        """)
    connection.commit()


def store_backup_in_postgres(archive_path: Path) -> None:
    """Store a complete ZIP backup in PostgreSQL as BYTEA."""
    data = archive_path.read_bytes()
    with _postgres_connect() as connection:
        _ensure_pg_backup_table(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO namulite_backups(filename, archive, size_bytes) VALUES (%s, %s, %s)",
                (archive_path.name, data, len(data)),
            )
            cursor.execute(
                "DELETE FROM namulite_backups WHERE created_at < NOW() - (%s * INTERVAL '1 day')",
                (configured_backup_retention_days(),),
            )
        connection.commit()


def purge_expired_backups() -> int:
    """Delete expired local archives and, when configured, PostgreSQL backup rows."""
    cutoff = datetime.now(timezone.utc).timestamp() - configured_backup_retention_days() * 86400
    removed = 0
    for path in backup_directory().glob("wiki-backup-*.zip"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    settings = get_site_settings()
    if app.config.get("DATABASE_URL") or (settings.get("postgres_host") and settings.get("postgres_database") and settings.get("postgres_username") and settings.get("postgres_password_encrypted")):
        try:
            with _postgres_connect() as connection:
                _ensure_pg_backup_table(connection)
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM namulite_backups WHERE created_at < NOW() - (%s * INTERVAL '1 day')", (configured_backup_retention_days(),))
                    removed += cursor.rowcount
                connection.commit()
        except Exception as exc:
            app.logger.warning("PostgreSQL backup retention cleanup failed: %s", exc.__class__.__name__)
    return removed


def list_postgres_backups() -> list[dict]:
    settings = get_site_settings()
    if not app.config.get("DATABASE_URL") and not (settings.get("postgres_host") and settings.get("postgres_database") and settings.get("postgres_username") and settings.get("postgres_password_encrypted")):
        return []
    try:
        with _postgres_connect() as connection:
            _ensure_pg_backup_table(connection)
            with connection.cursor() as cursor:
                cursor.execute("SELECT id, filename, created_at, size_bytes FROM namulite_backups ORDER BY created_at DESC LIMIT 100")
                rows = cursor.fetchall()
        return [{"id": row[0], "name": row[1], "created_at": row[2].isoformat(timespec="seconds"), "size": row[3]} for row in rows]
    except Exception as exc:
        app.logger.warning("Listing PostgreSQL backups failed: %s", exc.__class__.__name__)
        return []


def _json_default(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__namulite_bytes_b64__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (datetime,)):
        return value.isoformat()
    raise TypeError(f"Unsupported backup value type: {type(value).__name__}")


def _create_postgres_snapshot() -> dict:
    db = get_db()
    snapshot = {"format": "namulite-postgres-backup-v1", "tables": {}}
    for table in PRIMARY_TABLES:
        rows = db.execute(f'SELECT * FROM "{table}"').fetchall()
        snapshot["tables"][table] = [dict(row) for row in rows]
    return snapshot


def create_manual_backup() -> Path:
    """Create a ZIP snapshot compatible with either local SQLite or Vercel/PostgreSQL."""
    backups = backup_directory()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-UTC")
    archive_path = backups / f"wiki-backup-{stamp}-{secrets.token_hex(3)}.zip"
    if app.config.get("DATABASE_URL"):
        snapshot = _create_postgres_snapshot()
        snapshot_json = json.dumps(snapshot, ensure_ascii=False, default=_json_default, separators=(",", ":"))
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("wiki-postgres.json", snapshot_json)
            archive.writestr(
                "BACKUP-INFO.txt",
                "NamuLite Wiki PostgreSQL backup\n"
                f"Created UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                "Contents: wiki-postgres.json (table snapshot including uploaded images).\n"
                "Restore target: NamuLite running with DATABASE_URL configured for PostgreSQL.\n",
            )
    else:
        with tempfile.TemporaryDirectory(prefix="namulite-backup-") as temp_dir:
            snapshot_path = Path(temp_dir) / "wiki.db"
            source = sqlite3.connect(app.config["DATABASE"], timeout=30)
            target = sqlite3.connect(str(snapshot_path), timeout=30)
            try:
                source.backup(target)
                target.commit()
            finally:
                target.close()
                source.close()
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.write(snapshot_path, "wiki.db")
                if UPLOAD_DIR.exists():
                    for item in UPLOAD_DIR.rglob("*"):
                        if item.is_file():
                            archive.write(item, "uploads/" + item.relative_to(UPLOAD_DIR).as_posix())
                archive.writestr(
                    "BACKUP-INFO.txt",
                    "NamuLite Wiki backup\n"
                    f"Created UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                    "Contents: wiki.db (consistent SQLite snapshot), uploads/.\n"
                    "Primary storage: SQLite.\n",
                )
    purge_expired_backups()
    if archive_path.stat().st_size > MAX_RESTORE_SIZE:
        archive_path.unlink(missing_ok=True)
        raise ValueError("생성된 백업 ZIP이 190MB를 초과하여 이 버전의 복원 한도를 넘습니다. 이미지 또는 오래된 기록을 줄인 뒤 다시 시도해 주세요.")
    return archive_path


def _decode_backup_value(value):
    if isinstance(value, dict) and set(value) == {"__namulite_bytes_b64__"}:
        try:
            return base64.b64decode(value["__namulite_bytes_b64__"], validate=True)
        except Exception as exc:
            raise ValueError("백업의 바이너리 데이터가 손상되었습니다.") from exc
    if isinstance(value, dict):
        return {key: _decode_backup_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_backup_value(item) for item in value]
    return value


def _restore_postgres_snapshot(snapshot: dict) -> None:
    if snapshot.get("format") != "namulite-postgres-backup-v1" or not isinstance(snapshot.get("tables"), dict):
        raise ValueError("지원하지 않는 PostgreSQL 백업 형식입니다.")
    tables = snapshot["tables"]
    if not isinstance(tables.get("users"), list) or not isinstance(tables.get("pages"), list):
        raise ValueError("백업에 사용자 또는 문서 데이터가 없습니다.")
    for table in PRIMARY_TABLES:
        if table in tables and not isinstance(tables[table], list):
            raise ValueError(f"백업의 {table} 데이터 형식이 올바르지 않습니다.")

    # Close the request's connection before replacing records to avoid holding a
    # stale transaction open while the restore transaction runs.
    current_db = g.pop("db", None)
    if current_db is not None:
        current_db.close()

    import psycopg
    from psycopg import sql
    database_url = app.config.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL이 설정되어 있지 않습니다.")

    with psycopg.connect(database_url, connect_timeout=10, prepare_threshold=None) as connection:
        with connection.cursor() as cursor:
            # Only known NamuLite tables are restored; the backup archive table is retained.
            cursor.execute(
                sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(sql.Identifier(table) for table in PRIMARY_TABLES)
                )
            )
            for table in PRIMARY_TABLES:
                rows = tables.get(table, [])
                if not rows:
                    continue
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position",
                    (table,),
                )
                allowed_columns = [record[0] for record in cursor.fetchall()]
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError(f"백업의 {table} 행 형식이 올바르지 않습니다.")
                    columns = [column for column in allowed_columns if column in row]
                    if not columns:
                        continue
                    values = [_decode_backup_value(row[column]) for column in columns]
                    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                        sql.Identifier(table),
                        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
                        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                    )
                    cursor.execute(statement, values)
            # Rebuild per-IP counts after restoring backups from versions without that table.
            cursor.execute("DELETE FROM registration_ip_limits")
            cursor.execute(
                "INSERT INTO registration_ip_limits(ip_address, account_count) "
                "SELECT registered_ip, COUNT(*) FROM users WHERE registered_ip IS NOT NULL "
                "GROUP BY registered_ip ON CONFLICT(ip_address) DO UPDATE "
                "SET account_count = EXCLUDED.account_count"
            )
            # Keep generated IDs higher than restored IDs, so the next insert cannot collide.
            for table in PRIMARY_TABLES:
                if table in {"deleted_page_tombstones", "site_settings", "namulite_uploads"}:
                    continue
                if not any("id" in row for row in tables.get(table, []) if isinstance(row, dict)):
                    continue
                cursor.execute(
                    sql.SQL(
                        "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                        "GREATEST(COALESCE(MAX(id), 1), 1), MAX(id) IS NOT NULL) FROM {}"
                    ).format(sql.Identifier(table)),
                    (table,),
                )
        connection.commit()


def _sqlite_zip_to_postgres_snapshot(archive: zipfile.ZipFile, temp_root: Path) -> dict:
    """Convert a legacy/local SQLite ZIP backup to the PostgreSQL snapshot format."""
    if "wiki.db" not in archive.namelist():
        raise ValueError("SQLite 백업 ZIP 안에 wiki.db가 없습니다.")
    db_path = temp_root / "source-wiki.db"
    with archive.open("wiki.db") as source, db_path.open("wb") as target:
        import shutil
        shutil.copyfileobj(source, target)
    source_db = sqlite3.connect(str(db_path))
    source_db.row_factory = sqlite3.Row
    try:
        check = source_db.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise ValueError("SQLite 백업 데이터베이스 무결성 검사에 실패했습니다.")
        found_tables = {row[0] for row in source_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"users", "pages"}.issubset(found_tables):
            raise ValueError("NamuLite 위키 데이터베이스가 아닙니다.")
        snapshot = {"format": "namulite-postgres-backup-v1", "tables": {}}
        for table in PRIMARY_TABLES:
            if table not in found_tables:
                snapshot["tables"][table] = []
                continue
            rows = source_db.execute(f'SELECT * FROM "{table}"').fetchall()
            snapshot["tables"][table] = [dict(row) for row in rows]
    finally:
        source_db.close()

    # Previous versions kept uploaded image bytes under uploads/ in the ZIP.
    asset_rows = {row.get("filename"): row for row in snapshot["tables"].get("namulite_uploads", []) if row.get("filename")}
    for name in archive.namelist():
        if not name.startswith("uploads/") or name.endswith("/"):
            continue
        relative = Path(name).relative_to("uploads")
        if len(relative.parts) != 1:
            continue
        filename = relative.name
        if not filename or filename.startswith("."):
            continue
        data = archive.read(name)
        asset_rows[filename] = {
            "filename": filename,
            "content_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
            "file_data": data,
            "size_bytes": len(data),
            "created_at": now_iso(),
        }
    snapshot["tables"]["namulite_uploads"] = list(asset_rows.values())
    return snapshot


def restore_backup_upload(uploaded_file) -> None:
    """Safely restore a NamuLite ZIP backup for the active database backend."""
    with tempfile.TemporaryDirectory(prefix="namulite-restore-") as temp_dir:
        temp_root = Path(temp_dir)
        archive_path = temp_root / "upload.zip"
        uploaded_file.save(archive_path)
        if archive_path.stat().st_size > MAX_RESTORE_SIZE:
            raise ValueError("백업 파일은 190MB 이하여야 합니다.")
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(infos) > 5000:
                raise ValueError("ZIP 안의 파일 수가 너무 많습니다.")
            expanded_total = 0
            for info in infos:
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise ValueError("안전하지 않은 파일 경로가 포함된 ZIP입니다.")
                if info.file_size > MAX_RESTORE_SIZE:
                    raise ValueError("압축 해제 후 개별 파일이 190MB를 초과합니다.")
                expanded_total += info.file_size
                if expanded_total > MAX_RESTORE_SIZE:
                    raise ValueError("압축 해제 후 전체 파일 합계가 190MB를 초과합니다.")

            if app.config.get("DATABASE_URL"):
                if "wiki-postgres.json" in names:
                    try:
                        snapshot = json.loads(archive.read("wiki-postgres.json"))
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise ValueError("PostgreSQL 백업 JSON을 읽을 수 없습니다.") from exc
                elif "wiki.db" in names:
                    # Migration path from earlier local SQLite-based NamuLite releases.
                    snapshot = _sqlite_zip_to_postgres_snapshot(archive, temp_root)
                else:
                    raise ValueError("NamuLite 백업에 wiki-postgres.json 또는 wiki.db가 없습니다.")
                _restore_postgres_snapshot(snapshot)
                return

            if "wiki.db" not in names:
                raise ValueError("백업 ZIP 안에 wiki.db가 없습니다.")
            db_temp = temp_root / "wiki.db"
            with archive.open("wiki.db") as source, db_temp.open("wb") as target:
                import shutil
                shutil.copyfileobj(source, target)
            check = sqlite3.connect(str(db_temp))
            try:
                result = check.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise ValueError("백업 데이터베이스의 무결성 검사에 실패했습니다.")
                tables_in_backup = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "users" not in tables_in_backup or "pages" not in tables_in_backup:
                    raise ValueError("NamuLite 위키 데이터베이스가 아닙니다.")
            finally:
                check.close()
            current_db = g.pop("db", None)
            if current_db is not None:
                current_db.close()
            destination = Path(app.config["DATABASE"]).resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            replacement = destination.with_name(destination.name + ".restore.tmp")
            shutil.copy2(db_temp, replacement)
            os.replace(replacement, destination)
            restored_uploads = temp_root / "restored_uploads"
            restored_uploads.mkdir()
            for name in names:
                if name.startswith("uploads/") and not name.endswith("/"):
                    relative = Path(name).relative_to("uploads")
                    target = (restored_uploads / relative).resolve()
                    if restored_uploads.resolve() not in target.parents:
                        raise ValueError("안전하지 않은 업로드 경로입니다.")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as source, target.open("wb") as out:
                        shutil.copyfileobj(source, out)
            if UPLOAD_DIR.exists():
                old_uploads = UPLOAD_DIR.with_name("uploads.restore-old")
                if old_uploads.exists():
                    shutil.rmtree(old_uploads)
                os.replace(UPLOAD_DIR, old_uploads)
                os.replace(restored_uploads, UPLOAD_DIR)
                shutil.rmtree(old_uploads, ignore_errors=True)
            else:
                os.replace(restored_uploads, UPLOAD_DIR)


def list_backups() -> list[dict]:
    if IS_VERCEL:
        return []  # Vercel's /tmp is scratch space, never a durable backup list.
    items = []
    for path in sorted(backup_directory().glob("wiki-backup-*.zip"), key=lambda item: item.stat().st_mtime, reverse=True)[:100]:
        try:
            stat = path.stat()
            items.append({"name": path.name, "size": stat.st_size, "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")})
        except OSError:
            continue
    return items


def client_ip() -> str | None:
    """Return the peer IP. Trust forwarded headers only when TRUST_PROXY is enabled."""
    raw = (request.remote_addr or "").strip()
    try:
        return str(ipaddress.ip_address(raw)) if raw else None
    except ValueError:
        return None


def expiry_from_days(days_value: str) -> str | None:
    if days_value in {"permanent", "0"}:
        return None
    try:
        days = int(days_value)
    except (TypeError, ValueError):
        raise ValueError("차단 기간은 1~3650일 또는 무기한이어야 합니다.")
    if not 1 <= days <= 3650:
        raise ValueError("차단 기간은 1~3650일 또는 무기한이어야 합니다.")
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


def _active_expiry_clause() -> str:
    return "(expires_at IS NULL OR expires_at > ?) AND revoked_at IS NULL"


def active_user_ban(user_id: int):
    return get_db().execute(
        f"SELECT * FROM user_bans WHERE user_id = ? AND {_active_expiry_clause()} ORDER BY id DESC LIMIT 1",
        (user_id, now_iso()),
    ).fetchone()


def active_ip_ban(ip: str | None):
    if not ip:
        return None
    return get_db().execute(
        f"SELECT * FROM ip_bans WHERE ip_address = ? AND {_active_expiry_clause()} ORDER BY id DESC LIMIT 1",
        (ip, now_iso()),
    ).fetchone()


def contribution_block_reason(action: str) -> str | None:
    """Bans block contributions, while reading remains available unless read ACL denies it."""
    if action not in {"edit", "discuss", "register"}:
        return None
    if getattr(g, "user", None) is not None and g.user["is_admin"]:
        return None
    if action != "register" and getattr(g, "user", None) is not None:
        ban = active_user_ban(g.user["id"])
        if ban:
            return f"이 계정은 차단되었습니다. 사유: {ban['reason']}"
    ipban = active_ip_ban(client_ip())
    if ipban:
        return f"현재 IP는 기여가 차단되었습니다. 사유: {ipban['reason']}"
    return None


@app.before_request
def load_logged_in_user():
    user_id = session.get("user_id")
    g.user = None
    g.unread_warnings = []
    g.unread_warning_count = 0
    g.show_login_warning_modal = False
    if user_id is not None:
        g.user = get_db().execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if g.user is None:
            session.clear()
        else:
            # Consume the one-shot flag set only by a successful login.
            # If unread warnings still exist, they are shown in a modal on that landing page.
            g.show_login_warning_modal = bool(session.pop("show_warning_modal_after_login", False))
            ip = client_ip()
            if ip and ip != g.user["last_ip"]:
                get_db().execute("UPDATE users SET last_ip = ? WHERE id = ?", (ip, g.user["id"]))
                get_db().commit()
            g.unread_warning_count = get_db().execute(
                "SELECT COUNT(*) FROM user_warnings WHERE user_id = ? AND acknowledged_at IS NULL",
                (g.user["id"],),
            ).fetchone()[0]
            g.unread_warnings = get_db().execute(
                "SELECT w.id, w.reason, w.created_at, a.username AS issuer_name "
                "FROM user_warnings w LEFT JOIN users a ON a.id = w.issued_by "
                "WHERE w.user_id = ? AND w.acknowledged_at IS NULL "
                "ORDER BY w.id DESC LIMIT 100",
                (g.user["id"],),
            ).fetchall()
    # Purge expired IP audit events at most once per UTC day per app process.
    today = datetime.now(timezone.utc).date().isoformat()
    if app.config.get("_ip_log_cleanup_date") != today:
        db = get_db()
        login_cutoff = (datetime.now(timezone.utc) - timedelta(days=LOGIN_LOG_RETENTION_DAYS)).isoformat(timespec="seconds")
        registration_cutoff = (datetime.now(timezone.utc) - timedelta(days=REGISTRATION_LOG_RETENTION_DAYS)).isoformat(timespec="seconds")
        db.execute("DELETE FROM login_logs WHERE created_at < ?", (login_cutoff,))
        db.execute("DELETE FROM registration_logs WHERE created_at < ?", (registration_cutoff,))
        db.commit()
        app.config["_ip_log_cleanup_date"] = today
    # Honor the configured backup retention window at most once per UTC day.
    if app.config.get("_backup_cleanup_date") != today:
        try:
            purge_expired_backups()
        except Exception:
            app.logger.exception("Expired backup cleanup failed")
        app.config["_backup_cleanup_date"] = today


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            flash("이 기능을 사용하려면 로그인해야 합니다.", "warning")
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return view(**kwargs)
    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            flash("관리자 페이지는 로그인해야 이용할 수 있습니다.", "warning")
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        if not (g.user["is_admin"] or g.user["is_owner"]):
            abort(403)
        return view(**kwargs)
    return wrapped_view


def owner_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            flash("소유자 설정은 로그인해야 이용할 수 있습니다.", "warning")
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        if not g.user["is_owner"]:
            abort(403, description="이 기능은 위키 소유자만 사용할 수 있습니다.")
        return view(**kwargs)
    return wrapped_view


ACL_ACTIONS = {
    "read": "읽기",
    "edit": "편집",
    "discuss": "토론",
}
ACL_POLICIES = {"everyone", "member", "age", "contributor", "admin"}
GLOBAL_ACL_POLICIES = {"everyone", "member", "age", "admin"}
ACL_POLICY_LABELS = {
    "everyone": "누구나",
    "member": "로그인한 사용자",
    "age": "가입 후 일정 기간 경과",
    "contributor": "해당 문서 기여자",
    "admin": "관리자만",
}


def global_policy(action: str) -> dict:
    settings = get_site_settings()
    mode = settings.get(f"acl_{action}_mode", "everyone" if action == "read" else "member")
    if mode not in GLOBAL_ACL_POLICIES:
        mode = "everyone" if action == "read" else "member"
    try:
        days = max(0, min(3650, int(settings.get(f"acl_{action}_days", "15"))))
    except (TypeError, ValueError):
        days = 15
    return {"policy": mode, "min_account_days": days, "inherited": True, "reason": ""}


def effective_policy(page, action: str) -> dict:
    if action not in ACL_ACTIONS:
        raise ValueError("Unknown ACL action")
    if page is not None:
        override = get_db().execute(
            "SELECT policy, min_account_days, reason FROM page_acls WHERE page_id = ? AND action = ?",
            (page["id"], action),
        ).fetchone()
        if override:
            return {"policy": override["policy"], "min_account_days": override["min_account_days"],
                    "inherited": False, "reason": override["reason"]}
    return global_policy(action)


def account_age_days(user) -> int:
    try:
        created = datetime.fromisoformat(user["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - created.astimezone(timezone.utc)
        return max(0, delta.days)
    except (TypeError, ValueError, KeyError):
        return 0


def can_access_page(page, action: str, user=None) -> bool:
    user = user if user is not None else getattr(g, "user", None)
    if user is not None and user["is_admin"]:
        return True
    policy = effective_policy(page, action)
    mode = policy["policy"]
    if mode == "everyone":
        return True
    if mode == "member":
        return user is not None
    if mode == "age":
        return user is not None and account_age_days(user) >= int(policy["min_account_days"])
    if mode == "contributor":
        if user is None or page is None:
            return False
        return bool(get_db().execute(
            "SELECT 1 FROM revisions WHERE page_id = ? AND editor_id = ? LIMIT 1",
            (page["id"], user["id"]),
        ).fetchone())
    if mode == "admin":
        return user is not None and bool(user["is_admin"])
    return False


def require_page_access(page, action: str):
    if can_access_page(page, action):
        return None
    if getattr(g, "user", None) is None:
        flash(f"이 문서의 {ACL_ACTIONS[action]} 권한은 로그인한 사용자에게만 허용됩니다.", "warning")
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    policy = effective_policy(page, action)
    if policy["policy"] == "age":
        message = f"이 작업은 계정 생성 후 {policy['min_account_days']}일이 지난 사용자만 할 수 있습니다. 현재 가입 경과: {account_age_days(g.user)}일."
    elif policy["policy"] == "contributor":
        message = "이 작업은 해당 문서의 기여자만 할 수 있습니다."
    elif policy["policy"] == "admin":
        message = "이 작업은 관리자만 할 수 있습니다."
    else:
        message = f"이 문서의 {ACL_ACTIONS[action]} 권한이 없습니다."
    abort(403, description=message)


def require_contribution_access(action: str):
    reason = contribution_block_reason(action)
    if reason:
        abort(403, description=reason)


def is_safe_next(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return not parsed.netloc and not parsed.scheme and value.startswith("/") and not value.startswith("//")


def personal_document_owner_name(title: str) -> str | None:
    """Return the account owning a `사용자:account[/subpage]` document."""
    if not title.startswith("사용자:"):
        return None
    suffix = title[len("사용자:"):]
    owner_name = suffix.split("/", 1)[0].strip()
    return owner_name or None


def personal_document_owner(title: str):
    owner_name = personal_document_owner_name(title)
    if owner_name is None:
        return None
    return get_db().execute(
        "SELECT id, username, created_at FROM users WHERE username = ?", (owner_name,)
    ).fetchone()


def enforce_personal_document_edit(title: str):
    """User namespace pages are editable by their owner and admins only."""
    if not title.startswith("사용자:"):
        return
    owner_name = personal_document_owner_name(title)
    owner = personal_document_owner(title)
    if owner_name is None or owner is None:
        abort(404, description="사용자 문서의 계정을 찾을 수 없습니다.")
    if g.user is None:
        flash("사용자 문서를 편집하려면 로그인해야 합니다.", "warning")
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    if not g.user["is_admin"] and g.user["id"] != owner["id"]:
        abort(403, description="다른 사용자의 사용자 문서는 편집할 수 없습니다.")
    return None


def fetch_page(title: str):
    return get_db().execute("SELECT * FROM pages WHERE title = ?", (title,)).fetchone()


def resolve_document_title(route_title: str):
    """Decode URL-escaped document titles and repair legacy escaped page names.

    Some server/proxy combinations or manually created links can leave a title
    such as ``%EB%8C%80%EB%AC%B8`` stored literally in the database. Prefer the
    decoded title and rename that legacy page in place so revision/ACL relations
    that use its page id remain intact.
    """
    raw_title = (route_title or "").strip()
    title = unquote(raw_title).strip()
    if not title:
        return title, None

    page = fetch_page(title)
    if page is not None:
        return title, page

    # Find legacy rows when the database contains the encoded title, whether
    # the route parameter itself was encoded or the WSGI layer already decoded it.
    legacy_candidates = []
    if raw_title and raw_title != title:
        legacy_candidates.append(raw_title)
    encoded_title = quote(title, safe="")
    if encoded_title != title and encoded_title not in legacy_candidates:
        legacy_candidates.append(encoded_title)

    db = get_db()
    for legacy_title in legacy_candidates:
        legacy_page = fetch_page(legacy_title)
        if legacy_page is None:
            continue
        # A normal title may have been created in the meantime; never overwrite it.
        if fetch_page(title) is not None:
            return title, fetch_page(title)
        db.execute("UPDATE pages SET title = ? WHERE id = ?", (title, legacy_page["id"]))
        # Keep a deletion tombstone in sync when it can be renamed without a key collision.
        db.execute(
            "UPDATE deleted_page_tombstones SET title = ? WHERE title = ? "
            "AND NOT EXISTS (SELECT 1 FROM deleted_page_tombstones WHERE title = ?)",
            (title, legacy_title, title),
        )
        db.commit()
        return title, fetch_page(title)

    return title, None


def fetch_latest_revision(page_id: int):
    return get_db().execute(
        "SELECT * FROM revisions WHERE page_id = ? ORDER BY id DESC LIMIT 1", (page_id,)
    ).fetchone()


def page_url(title: str) -> str:
    return "/w/" + quote(title, safe="")


def save_revision(
    title: str, content: str, summary: str, editor,
    license_consent_at: str, license_version: str, ai_policy_version: str,
):
    db = get_db()
    now = now_iso()
    page = fetch_page(title)
    if page is None:
        db.execute("DELETE FROM deleted_page_tombstones WHERE title = ?", (title,))
        cur = db.execute(
            "INSERT INTO pages(title, created_at, updated_at) VALUES (?, ?, ?)",
            (title, now, now),
        )
        page_id = cur.lastrowid
    else:
        page_id = page["id"]
        db.execute("UPDATE pages SET updated_at = ? WHERE id = ?", (now, page_id))
    editor_id = editor["id"] if editor is not None else None
    editor_name = editor["username"] if editor is not None else "익명"
    db.execute(
        "INSERT INTO revisions(page_id, editor_id, editor_name, content, summary, created_at, "
        "license_consent_at, license_version, ai_policy_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (page_id, editor_id, editor_name, content, summary[:200], now,
         license_consent_at, license_version, ai_policy_version),
    )
    db.commit()


@app.context_processor
def inject_globals():
    settings = get_site_settings()
    private_setting_keys = {
        "postgres_host", "postgres_port", "postgres_database", "postgres_username",
        "postgres_password_encrypted", "postgres_sslmode", "backup_retention_days",
    }
    public_settings = {key: value for key, value in settings.items() if key not in private_setting_keys}
    return {
        **public_settings,
        "site_name": settings["site_name"],
        "page_url": page_url,
        "current_user": getattr(g, "user", None),
        "unread_warnings": getattr(g, "unread_warnings", []),
        "unread_warning_count": getattr(g, "unread_warning_count", 0),
        "show_login_warning_modal": getattr(g, "show_login_warning_modal", False),
        "database_mode": "PostgreSQL/Neon (영구 저장)" if app.config.get("DATABASE_URL") else "SQLite (로컬)",
        "is_vercel": IS_VERCEL,
        "csrf_token": csrf_token,
    }


def csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(24)
    return session["csrf_token"]


@app.before_request
def protect_forms():
    if request.method == "POST":
        supplied = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not supplied or not expected or not secrets.compare_digest(supplied, expected):
            abort(400, description="요청 검증에 실패했습니다. 페이지를 새로고침한 뒤 다시 시도해 주세요.")


@app.route("/")
def index():
    page = fetch_page("대문")
    if page:
        denial = require_page_access(page, "read")
        if denial is not None:
            return denial
        latest = fetch_latest_revision(page["id"])
        content = latest["content"] if latest else ""
        rendered = render_namumark(content)
    else:
        rendered = (
            '<p>나무라이트 위키에 오신 것을 환영합니다.</p>'
            '<p><a href="/edit/%EB%8C%80%EB%AC%B8">대문 문서를 만들어 보세요.</a></p>'
            '<p>이 프로젝트는 나무마크 호환성을 단계적으로 확장하는 초기 버전입니다.</p>'
        )
    recent_rows = get_db().execute(
        "SELECT r.id, r.editor_name, r.summary, r.created_at, p.title, p.id AS page_id "
        "FROM revisions r JOIN pages p ON p.id = r.page_id ORDER BY r.id DESC LIMIT 80"
    ).fetchall()
    recent = [row for row in recent_rows if can_access_page({"id": row["page_id"]}, "read")][:8]
    return render_template("index.html", rendered=rendered, recent=recent)


@app.route("/w/<path:title>")
def view_page(title: str):
    title, page = resolve_document_title(title)
    if not title or len(title) > 200:
        abort(404)
    if page is None:
        return render_template("missing.html", title=title), 404
    denial = require_page_access(page, "read")
    if denial is not None:
        return denial

    selected_rev = request.args.get("rev", type=int)
    if selected_rev:
        revision = get_db().execute(
            "SELECT * FROM revisions WHERE id = ? AND page_id = ?", (selected_rev, page["id"])
        ).fetchone()
        if revision is None:
            abort(404)
    else:
        revision = fetch_latest_revision(page["id"])
    content = revision["content"] if revision else ""
    redirect_target = extract_redirect(content)
    if redirect_target and not selected_rev:
        return redirect(page_url(redirect_target), code=302)
    rendered = render_namumark(content)
    categories = extract_categories(content)
    is_user_document = title.startswith("사용자:")
    user_doc_owner = personal_document_owner(title) if is_user_document else None
    can_edit_user_document = bool(
        is_user_document and user_doc_owner and g.user is not None
        and (g.user["is_admin"] or g.user["id"] == user_doc_owner["id"])
    )
    can_edit_page = (not is_user_document) or can_edit_user_document

    # Hierarchical pages use a slash-separated title, e.g. "DDS/DDS의 장비".
    # Show existing readable ancestors and every readable descendant.
    ancestors = []
    title_parts = title.split("/")
    for index in range(1, len(title_parts)):
        ancestor_title = "/".join(title_parts[:index])
        ancestor_page = fetch_page(ancestor_title)
        if ancestor_page is not None and can_access_page(ancestor_page, "read"):
            ancestors.append(ancestor_page)

    child_prefix = title + "/"
    child_rows = get_db().execute(
        "SELECT p.id, p.title, p.updated_at FROM pages p "
        "WHERE substr(p.title, 1, length(?)) = ? "
        "ORDER BY p.title COLLATE NOCASE",
        (child_prefix, child_prefix),
    ).fetchall()
    subpages = []
    for child in child_rows:
        if can_access_page(child, "read"):
            item = dict(child)
            item["depth"] = max(1, item["title"].count("/") - title.count("/"))
            subpages.append(item)
    can_create_subpage = bool(
        g.user is not None and not is_user_document and can_edit_page
        and can_access_page(page, "edit")
    )

    return render_template(
        "page.html", page=page, revision=revision, rendered=rendered,
        categories=categories, is_old=bool(selected_rev and revision and revision["id"] != (fetch_latest_revision(page["id"]) or {"id": 0})["id"]),
        is_user_document=is_user_document, user_doc_owner=user_doc_owner,
        user_doc_owner_name=personal_document_owner_name(title) if is_user_document else None,
        is_user_home_document=bool(is_user_document and user_doc_owner and title == f"사용자:{user_doc_owner['username']}"),
        can_edit_user_document=can_edit_user_document, can_edit_page=can_edit_page,
        is_empty_user_document=bool(is_user_document and not content.strip()),
        ancestors=ancestors, subpages=subpages, can_create_subpage=can_create_subpage,
        can_delete_page=bool(g.user is not None and (g.user["is_admin"] or g.user["is_owner"])),
    )


@app.route("/edit/<path:title>", methods=["GET", "POST"])
def edit_page(title: str):
    title, page = resolve_document_title(title)
    if is_personal_subpage_title(title):
        abort(403, description="개인 문서는 하위 문서를 지원하지 않습니다.")
    if not title or len(title) > 200:
        abort(400, description="문서 제목은 1~200자여야 합니다.")
    personal_denial = enforce_personal_document_edit(title)
    if personal_denial is not None:
        return personal_denial
    denial = require_page_access(page, "edit")
    if denial is not None:
        return denial
    require_contribution_access("edit")
    latest = fetch_latest_revision(page["id"]) if page else None
    content = latest["content"] if latest else ""
    summary = ""
    preview_html = None
    uploaded_files: list[str] = []
    license_agreed = False
    if request.method == "POST":
        content = request.form.get("content", "")
        summary = request.form.get("summary", "").strip()
        action = request.form.get("action", "save")
        license_agreed = request.form.get("license_agree") == "on"
        if len(content) > 500_000:
            flash("문서 본문은 500,000자 이하로 작성해 주세요.", "error")
        elif action == "upload":
            allowed_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
            signatures = {
                ".png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
                ".jpg": lambda data: data.startswith(b"\xff\xd8\xff"),
                ".jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
                ".gif": lambda data: data.startswith((b"GIF87a", b"GIF89a")),
                ".webp": lambda data: len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP",
            }
            for upload in request.files.getlist("upload_files"):
                if not upload or not upload.filename:
                    continue
                raw_name = Path(upload.filename.replace("\\", "/")).name
                extension = Path(raw_name).suffix.lower()
                original_name = secure_filename(raw_name) or raw_name
                if extension not in allowed_extensions:
                    flash(f"지원하지 않는 이미지 형식입니다: {original_name or '파일'}", "error")
                    continue
                data = upload.read(MAX_IMAGE_SIZE + 1)
                if len(data) > MAX_IMAGE_SIZE:
                    flash(f"이미지는 파일당 {MAX_IMAGE_SIZE // (1024 * 1024)}MB 이하만 업로드할 수 있습니다: {original_name}", "error")
                    continue
                if not signatures[extension](data):
                    flash(f"이미지 파일 내용이 확장자와 일치하지 않습니다: {original_name}", "error")
                    continue
                stem = secure_filename(Path(raw_name).stem)[:100] or "image"
                stored_name = f"{uuid.uuid4().hex[:10]}_{stem}{extension}"
                save_uploaded_asset(stored_name, data)
                uploaded_files.append(stored_name)
            if uploaded_files:
                flash("이미지를 업로드했습니다. 아래에 표시된 나무마크를 문서에 넣으세요.", "success")
            else:
                flash("업로드할 수 있는 이미지가 없습니다. PNG, JPG, GIF, WEBP 파일을 확인하세요.", "warning")
        elif action == "preview":
            preview_html = render_namumark(content)
        elif action == "save" and not license_agreed:
            flash("문서를 저장하려면 기여 라이선스 및 생성형 AI 정책 동의가 필요합니다.", "error")
        elif action == "save" and latest and content == latest["content"]:
            flash("내용이 바뀌지 않아 저장하지 않았습니다.", "info")
            return redirect(page_url(title))
        elif action == "save":
            save_revision(
                title, content, summary, g.user, now_iso(), LICENSE_VERSION, AI_POLICY_VERSION
            )
            flash("문서를 저장했습니다. 기여 라이선스 및 생성형 AI 정책 동의가 기록되었습니다.", "success")
            return redirect(page_url(title))
        else:
            flash("알 수 없는 편집 작업입니다. 다시 시도해 주세요.", "error")
    return render_template(
        "edit.html", title=title, content=content, summary=summary,
        preview_html=preview_html, uploaded_files=uploaded_files,
        license_agreed=license_agreed,
    )


@app.route("/admin", methods=["GET", "POST"])
@admin_required
def admin_dashboard():
    tab = request.args.get("tab", "overview")
    if tab == "warnings":
        tab = "users"  # Backward-compatible redirect for the former separate warning tab.
    if tab not in {"overview", "appearance", "users", "pages", "acl", "bans", "logins", "registrations"}:
        tab = "overview"

    if request.method == "POST" and tab == "acl":
        values = {}
        errors = []
        for action in ACL_ACTIONS:
            mode = request.form.get(f"acl_{action}_mode", "").strip()
            days_text = request.form.get(f"acl_{action}_days", "15").strip()
            if mode not in GLOBAL_ACL_POLICIES:
                errors.append(f"{ACL_ACTIONS[action]} 기본 권한 설정이 올바르지 않습니다.")
            try:
                days = int(days_text)
                if not 0 <= days <= 3650:
                    raise ValueError
            except ValueError:
                errors.append(f"{ACL_ACTIONS[action]} 가입 기간은 0~3650일이어야 합니다.")
                days = 15
            values[f"acl_{action}_mode"] = mode if mode in GLOBAL_ACL_POLICIES else ("everyone" if action == "read" else "member")
            values[f"acl_{action}_days"] = str(days)
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            save_site_settings(values)
            flash("기본 ACL 설정을 저장했습니다. 문서별 ACL 설정은 해당 문서의 ACL 탭에서 별도로 변경할 수 있습니다.", "success")
            return redirect(url_for("admin_dashboard", tab="acl"))
    elif request.method == "POST":
        name = request.form.get("site_name", "").strip()
        tagline = request.form.get("site_tagline", "").strip()
        description = request.form.get("site_description", "").strip()
        primary = request.form.get("theme_primary", "").strip()
        accent = request.form.get("theme_accent", "").strip()
        style = request.form.get("site_style", "classic").strip()
        errors = []
        if not (2 <= len(name) <= 60):
            errors.append("위키 이름은 2~60자로 입력해 주세요.")
        if len(tagline) > 120:
            errors.append("한 줄 소개는 120자 이하로 입력해 주세요.")
        if len(description) > 300:
            errors.append("사이트 설명은 300자 이하로 입력해 주세요.")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", primary):
            errors.append("기본 색상은 #30394a 형식의 16진수 색상이어야 합니다.")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
            errors.append("링크 색상은 #315a9b 형식의 16진수 색상이어야 합니다.")
        if style not in {"classic", "soft", "warm"}:
            errors.append("지원하지 않는 화면 스타일입니다.")

        settings = get_site_settings()
        new_logo = settings.get("site_logo", "")
        uploaded_logo = request.files.get("logo_file")
        remove_logo = request.form.get("remove_logo") == "1"
        if uploaded_logo and uploaded_logo.filename:
            raw_name = Path(uploaded_logo.filename.replace("\\", "/")).name
            ext = Path(raw_name).suffix.lower()
            signature = {
                ".png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
                ".jpg": lambda data: data.startswith(b"\xff\xd8\xff"),
                ".jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
                ".gif": lambda data: data.startswith((b"GIF87a", b"GIF89a")),
                ".webp": lambda data: len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP",
            }
            if ext not in signature:
                errors.append("로고는 PNG, JPG, GIF, WEBP만 업로드할 수 있습니다.")
            else:
                data = uploaded_logo.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    errors.append("로고 파일은 2MB 이하로 업로드해 주세요.")
                elif not signature[ext](data):
                    errors.append("이미지 파일의 내용이 확장자와 일치하지 않습니다.")
                else:
                    new_logo = f"site-logo-{uuid.uuid4().hex[:12]}{ext}"
                    save_uploaded_asset(new_logo, data)
                    remove_logo = False

        if errors:
            for message in errors:
                flash(message, "error")
        else:
            if remove_logo:
                if new_logo:
                    delete_uploaded_asset(Path(new_logo).name)
                new_logo = ""
            save_site_settings({
                "site_name": name,
                "site_tagline": tagline,
                "site_description": description,
                "theme_primary": primary.lower(),
                "theme_accent": accent.lower(),
                "site_style": style,
                "site_logo": new_logo,
            })
            flash("위키 꾸미기 설정을 저장했습니다.", "success")
            return redirect(url_for("admin_dashboard", tab="appearance"))
        tab = "appearance"

    settings = get_site_settings()
    db = get_db()
    stats = {
        "users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "pages": db.execute("SELECT COUNT(*) FROM pages").fetchone()[0],
        "revisions": db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0],
        "discussions": db.execute("SELECT COUNT(*) FROM discussions").fetchone()[0],
        "admins": db.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0],
        "login_events": db.execute(
            "SELECT COUNT(*) FROM login_logs WHERE created_at >= ?",
            ((datetime.now(timezone.utc) - timedelta(days=LOGIN_LOG_RETENTION_DAYS)).isoformat(timespec="seconds"),),
        ).fetchone()[0],
        "registration_events": db.execute(
            "SELECT COUNT(*) FROM registration_logs WHERE created_at >= ?",
            ((datetime.now(timezone.utc) - timedelta(days=REGISTRATION_LOG_RETENTION_DAYS)).isoformat(timespec="seconds"),),
        ).fetchone()[0],
    }
    stats["active_user_bans"] = db.execute(
        f"SELECT COUNT(*) FROM user_bans WHERE {_active_expiry_clause()}", (now_iso(),)
    ).fetchone()[0]
    stats["active_ip_bans"] = db.execute(
        f"SELECT COUNT(*) FROM ip_bans WHERE {_active_expiry_clause()}", (now_iso(),)
    ).fetchone()[0]
    users = []
    if tab == "users":
        warning_counts = {
            row["user_id"]: row["warning_count"]
            for row in db.execute(
                "SELECT user_id, COUNT(*) AS warning_count FROM user_warnings GROUP BY user_id"
            ).fetchall()
        }
        warning_history_by_user = {}
        warning_history_rows = db.execute(
            "SELECT w.id, w.user_id, w.reason, w.created_at, w.acknowledged_at, "
            "a.username AS issuer_name FROM user_warnings w "
            "LEFT JOIN users a ON a.id = w.issued_by ORDER BY w.id DESC LIMIT 1000"
        ).fetchall()
        for warning_row in warning_history_rows:
            history = warning_history_by_user.setdefault(warning_row["user_id"], [])
            if len(history) < 20:
                history.append(warning_row)
        for row in db.execute(
            "SELECT id, username, is_admin, is_owner, created_at, registered_ip, last_ip FROM users ORDER BY id ASC LIMIT 500"
        ).fetchall():
            item = dict(row)
            item["active_ban"] = active_user_ban(item["id"])
            quota_row = db.execute(
                "SELECT account_count FROM registration_ip_limits WHERE ip_address = ?",
                (item["registered_ip"],),
            ).fetchone() if item["registered_ip"] else None
            item["ip_account_count"] = quota_row["account_count"] if quota_row else 0
            item["warning_count"] = warning_counts.get(item["id"], 0)
            item["warning_history"] = warning_history_by_user.get(item["id"], [])
            users.append(item)
    pages = db.execute(
        "SELECT p.id, p.title, p.updated_at, r.editor_name FROM pages p "
        "LEFT JOIN revisions r ON r.id = (SELECT MAX(r2.id) FROM revisions r2 WHERE r2.page_id = p.id) "
        "ORDER BY p.updated_at DESC LIMIT 100"
    ).fetchall() if tab == "pages" else []
    user_bans = db.execute(
        "SELECT b.*, u.username, c.username AS creator_name FROM user_bans b "
        "LEFT JOIN users u ON u.id = b.user_id LEFT JOIN users c ON c.id = b.created_by "
        "ORDER BY b.id DESC LIMIT 300"
    ).fetchall() if tab == "bans" else []
    ip_bans = db.execute(
        "SELECT b.*, u.username, c.username AS creator_name FROM ip_bans b "
        "LEFT JOIN users u ON u.id = b.user_id LEFT JOIN users c ON c.id = b.created_by "
        "ORDER BY b.id DESC LIMIT 300"
    ).fetchall() if tab == "bans" else []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOGIN_LOG_RETENTION_DAYS)).isoformat(timespec="seconds")
    login_logs = db.execute(
        "SELECT l.id, l.username, l.ip_address, l.created_at, u.is_admin "
        "FROM login_logs l LEFT JOIN users u ON u.id = l.user_id "
        "WHERE l.created_at >= ? ORDER BY l.id DESC LIMIT 500", (cutoff,)
    ).fetchall() if tab == "logins" else []
    registration_cutoff = (datetime.now(timezone.utc) - timedelta(days=REGISTRATION_LOG_RETENTION_DAYS)).isoformat(timespec="seconds")
    registration_logs = db.execute(
        "SELECT l.id, l.username, l.ip_address, l.created_at, u.is_admin "
        "FROM registration_logs l LEFT JOIN users u ON u.id = l.user_id "
        "WHERE l.created_at >= ? ORDER BY l.id DESC LIMIT 500", (registration_cutoff,)
    ).fetchall() if tab == "registrations" else []
    acl_defaults = {action: global_policy(action) for action in ACL_ACTIONS}
    return render_template(
        "admin.html", tab=tab, settings=settings, stats=stats, users=users, pages=pages,
        user_bans=user_bans, ip_bans=ip_bans, login_logs=login_logs,
        registration_logs=registration_logs, max_accounts_per_ip=MAX_ACCOUNTS_PER_IP,
        acl_defaults=acl_defaults, policy_labels=ACL_POLICY_LABELS, current_time=now_iso(),
        login_log_retention_days=LOGIN_LOG_RETENTION_DAYS,
        registration_log_retention_days=REGISTRATION_LOG_RETENTION_DAYS,
    )


@app.route("/admin/warnings", methods=["POST"])
@admin_required
def admin_issue_warning():
    target_id = request.form.get("user_id", type=int)
    reason = request.form.get("reason", "").strip()
    target = get_db().execute(
        "SELECT id, username FROM users WHERE id = ?", (target_id,)
    ).fetchone() if target_id else None
    if target is None:
        flash("경고를 보낼 계정을 찾을 수 없습니다.", "error")
    elif not (2 <= len(reason) <= 1000):
        flash("경고 사유는 2~1000자로 입력해 주세요.", "error")
    elif target["id"] == g.user["id"]:
        flash("현재 로그인한 계정에는 경고를 보낼 수 없습니다.", "error")
    else:
        db = get_db()
        db.execute(
            "INSERT INTO user_warnings(user_id, issued_by, reason, created_at) VALUES (?, ?, ?, ?)",
            (target["id"], g.user["id"], reason, now_iso()),
        )
        db.commit()
        flash(f"{target['username']} 계정에 경고를 전달했습니다.", "success")
    return redirect(url_for("admin_dashboard", tab="users"))


@app.route("/warnings")
@login_required
def warning_inbox():
    warnings = get_db().execute(
        "SELECT w.*, a.username AS issuer_name FROM user_warnings w "
        "LEFT JOIN users a ON a.id = w.issued_by "
        "WHERE w.user_id = ? ORDER BY w.id DESC LIMIT 200",
        (g.user["id"],),
    ).fetchall()
    return render_template("warnings.html", warnings=warnings)


@app.route("/warnings/<int:warning_id>/acknowledge", methods=["POST"])
@login_required
def acknowledge_warning(warning_id: int):
    db = get_db()
    result = db.execute(
        "UPDATE user_warnings SET acknowledged_at = ? "
        "WHERE id = ? AND user_id = ? AND acknowledged_at IS NULL",
        (now_iso(), warning_id, g.user["id"]),
    )
    db.commit()
    if result.rowcount:
        flash("경고 내용을 확인 처리했습니다.", "success")
    else:
        flash("해당 경고를 찾을 수 없거나 이미 확인했습니다.", "info")
    return redirect(url_for("warning_inbox"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_user_role(user_id: int):
    target = get_db().execute(
        "SELECT id, username, is_admin, is_owner FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if target is None:
        abort(404)
    desired_admin = request.form.get("is_admin") == "1"
    if target["is_owner"]:
        flash("소유자의 관리자 권한은 이 메뉴에서 변경할 수 없습니다.", "error")
    elif target["id"] == g.user["id"]:
        flash("현재 로그인한 자신의 관리자 권한은 이 화면에서 변경할 수 없습니다.", "warning")
    elif bool(target["is_admin"]) == desired_admin:
        flash("변경할 내용이 없습니다.", "info")
    elif target["is_admin"] and not desired_admin:
        admin_count = get_db().execute(
            "SELECT COUNT(*) FROM users WHERE is_admin = 1"
        ).fetchone()[0]
        if admin_count <= 1:
            flash("마지막 관리자의 권한은 해제할 수 없습니다.", "error")
        else:
            get_db().execute("UPDATE users SET is_admin = 0 WHERE id = ?", (user_id,))
            get_db().commit()
            flash(f"{target['username']} 계정을 일반 사용자로 변경했습니다.", "success")
    else:
        get_db().execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user_id,))
        get_db().commit()
        flash(f"{target['username']} 계정에 관리자 권한을 부여했습니다.", "success")
    return redirect(url_for("admin_dashboard", tab="users"))


@app.route("/owner/users/<int:user_id>/role", methods=["POST"])
@owner_required
def owner_user_role(user_id: int):
    db = get_db()
    target = db.execute("SELECT id, username, is_admin, is_owner FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        abort(404)
    if target["id"] == g.user["id"]:
        flash("현재 소유자의 권한은 이 화면에서 변경할 수 없습니다.", "warning")
        return redirect(url_for("admin_dashboard", tab="users"))
    desired_owner = request.form.get("is_owner") == "1"
    if bool(target["is_owner"]) == desired_owner:
        flash("변경할 내용이 없습니다.", "info")
    elif not desired_owner:
        owner_count = db.execute("SELECT COUNT(*) FROM users WHERE is_owner = 1").fetchone()[0]
        if owner_count <= 1:
            flash("마지막 소유자의 권한은 해제할 수 없습니다.", "error")
        else:
            db.execute("UPDATE users SET is_owner = 0 WHERE id = ?", (user_id,))
            db.commit()
            flash(f"{target['username']} 계정의 소유자 권한을 해제했습니다. 기존 관리자 권한은 유지됩니다.", "success")
    else:
        db.execute("UPDATE users SET is_owner = 1, is_admin = 1 WHERE id = ?", (user_id,))
        db.commit()
        flash(f"{target['username']} 계정에 소유자 권한을 부여했습니다.", "success")
    return redirect(url_for("admin_dashboard", tab="users"))


@app.route("/admin/users/<int:user_id>/ban", methods=["POST"])
@admin_required
def admin_ban_user(user_id: int):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        abort(404)
    if target["is_admin"] or target["id"] == g.user["id"]:
        flash("관리자 계정은 이 화면에서 차단할 수 없습니다.", "error")
        return redirect(url_for("admin_dashboard", tab="users"))
    reason = request.form.get("reason", "").strip()[:500]
    if not reason:
        flash("차단 사유를 입력해 주세요.", "error")
        return redirect(url_for("admin_dashboard", tab="users"))
    try:
        expires_at = expiry_from_days(request.form.get("days", "7"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin_dashboard", tab="users"))
    if active_user_ban(user_id):
        flash("이미 차단된 계정입니다. 차단 관리 탭에서 상태를 확인해 주세요.", "warning")
        return redirect(url_for("admin_dashboard", tab="users"))
    db.execute(
        "INSERT INTO user_bans(user_id, created_by, reason, created_at, expires_at, source_ip) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, g.user["id"], reason, now_iso(), expires_at, client_ip()),
    )
    ip_banned = []
    for raw_ip in {target["registered_ip"], target["last_ip"]}:
        if not raw_ip:
            continue
        try:
            ip = str(ipaddress.ip_address(raw_ip))
        except ValueError:
            continue
        db.execute(
            "INSERT INTO ip_bans(ip_address, created_by, user_id, source, reason, created_at, expires_at) "
            "VALUES (?, ?, ?, 'account', ?, ?, ?)",
            (ip, g.user["id"], user_id, f"계정 차단 연동: {reason}", now_iso(), expires_at),
        )
        ip_banned.append(ip)
    db.commit()
    if ip_banned:
        flash(f"{target['username']} 계정을 차단했고, 확인된 IP {len(ip_banned)}개에도 같은 기간 차단을 적용했습니다.", "success")
    else:
        flash(f"{target['username']} 계정을 차단했습니다. 저장된 IP가 없어 IP 차단은 추가되지 않았습니다.", "warning")
    return redirect(url_for("admin_dashboard", tab="users"))


@app.route("/admin/user-bans/<int:ban_id>/revoke", methods=["POST"])
@admin_required
def admin_revoke_user_ban(ban_id: int):
    db = get_db()
    ban = db.execute("SELECT * FROM user_bans WHERE id = ?", (ban_id,)).fetchone()
    if ban is None:
        abort(404)
    if ban["revoked_at"] is None:
        db.execute(
            "UPDATE user_bans SET revoked_at = ?, revoked_by = ? WHERE id = ?",
            (now_iso(), g.user["id"], ban_id),
        )
        # Remove only IP blocks created by this account-ban workflow. Other manual or
        # overlapping blocks remain active and continue to protect the wiki.
        db.execute(
            "UPDATE ip_bans SET revoked_at = ?, revoked_by = ? WHERE user_id = ? AND source = 'account' AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?)",
            (now_iso(), g.user["id"], ban["user_id"], now_iso()),
        )
        db.commit()
        flash("계정 차단과 해당 계정 차단에서 생성된 IP 차단을 해제했습니다.", "success")
    else:
        flash("이미 해제된 차단입니다.", "info")
    return redirect(url_for("admin_dashboard", tab="bans"))


@app.route("/admin/ip-bans", methods=["POST"])
@admin_required
def admin_add_ip_ban():
    raw_ip = request.form.get("ip_address", "").strip()
    reason = request.form.get("reason", "").strip()[:500]
    try:
        ip = str(ipaddress.ip_address(raw_ip))
    except ValueError:
        flash("올바른 IPv4 또는 IPv6 주소를 입력해 주세요. CIDR 대역은 아직 지원하지 않습니다.", "error")
        return redirect(url_for("admin_dashboard", tab="bans"))
    if not reason:
        flash("IP 차단 사유를 입력해 주세요.", "error")
        return redirect(url_for("admin_dashboard", tab="bans"))
    try:
        expires_at = expiry_from_days(request.form.get("days", "7"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin_dashboard", tab="bans"))
    get_db().execute(
        "INSERT INTO ip_bans(ip_address, created_by, source, reason, created_at, expires_at) "
        "VALUES (?, ?, 'manual', ?, ?, ?)",
        (ip, g.user["id"], reason, now_iso(), expires_at),
    )
    get_db().commit()
    flash(f"IP {ip}에 차단을 적용했습니다. 같은 공인 IP를 공유하는 다른 사용자도 글쓰기와 토론이 제한될 수 있습니다.", "success")
    return redirect(url_for("admin_dashboard", tab="bans"))


@app.route("/admin/ip-bans/<int:ban_id>/revoke", methods=["POST"])
@admin_required
def admin_revoke_ip_ban(ban_id: int):
    db = get_db()
    ban = db.execute("SELECT * FROM ip_bans WHERE id = ?", (ban_id,)).fetchone()
    if ban is None:
        abort(404)
    if ban["revoked_at"] is None:
        db.execute(
            "UPDATE ip_bans SET revoked_at = ?, revoked_by = ? WHERE id = ?",
            (now_iso(), g.user["id"], ban_id),
        )
        db.commit()
        flash(f"IP {ban['ip_address']} 차단 기록을 해제했습니다. 같은 IP에 다른 활성 차단이 있다면 그 차단은 유지됩니다.", "success")
    else:
        flash("이미 해제된 차단입니다.", "info")
    return redirect(url_for("admin_dashboard", tab="bans"))


@app.route("/delete/<path:title>", methods=["POST"])
@admin_required
def delete_page(title: str):
    """Delete a page only after an administrator opens it and submits its form."""
    title, page = resolve_document_title(title)
    if page is None:
        abort(404)
    db = get_db()
    deleted_at = now_iso()
    deleted_by_name = g.user["username"]
    db.execute(
        "INSERT INTO deleted_page_tombstones(title, deleted_at, deleted_by) VALUES (?, ?, ?) "
        "ON CONFLICT(title) DO UPDATE SET deleted_at = excluded.deleted_at, deleted_by = excluded.deleted_by",
        (title, deleted_at, g.user["id"]),
    )
    db.execute(
        "INSERT INTO deleted_page_revisions(title, original_revision_id, editor_name, content, summary, created_at, deleted_at, deleted_by_name) "
        "SELECT ?, id, editor_name, content, summary, created_at, ?, ? FROM revisions WHERE page_id = ?",
        (title, deleted_at, deleted_by_name, page["id"]),
    )
    db.execute(
        "INSERT INTO page_deletion_events(title, deleted_at, deleted_by_name) VALUES (?, ?, ?)",
        (title, deleted_at, deleted_by_name),
    )
    db.execute("DELETE FROM pages WHERE id = ?", (page["id"],))
    db.commit()
    flash(f"문서 '{title}'을(를) 삭제했습니다. 편집 기록은 역사에 보존됩니다.", "success")
    return redirect(url_for("index"))


@app.route("/acl/<path:title>", methods=["GET", "POST"])
@admin_required
def page_acl(title: str):
    title, page = resolve_document_title(title)
    if page is None:
        abort(404)
    db = get_db()
    if request.method == "POST":
        errors = []
        submitted_rules = {}
        for action in ACL_ACTIONS:
            mode = request.form.get(f"policy_{action}", "inherit").strip()
            reason = request.form.get(f"reason_{action}", "").strip()[:500]
            days_text = request.form.get(f"days_{action}", "15").strip()
            if mode != "inherit" and mode not in ACL_POLICIES:
                errors.append(f"{ACL_ACTIONS[action]} 권한 정책이 올바르지 않습니다.")
                continue
            try:
                days = int(days_text)
                if not 0 <= days <= 3650:
                    raise ValueError
            except ValueError:
                errors.append(f"{ACL_ACTIONS[action]} 가입 기간은 0~3650일이어야 합니다.")
                continue
            submitted_rules[action] = {"mode": mode, "reason": reason, "days": days}
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            for action, rule in submitted_rules.items():
                if rule["mode"] == "inherit":
                    db.execute("DELETE FROM page_acls WHERE page_id = ? AND action = ?", (page["id"], action))
                    continue
                db.execute(
                    "INSERT INTO page_acls(page_id, action, policy, min_account_days, reason, updated_by, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(page_id, action) DO UPDATE SET "
                    "policy=excluded.policy, min_account_days=excluded.min_account_days, reason=excluded.reason, "
                    "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (page["id"], action, rule["mode"], rule["days"], rule["reason"], g.user["id"], now_iso()),
                )
            db.commit()
            flash("문서별 ACL 설정을 저장했습니다.", "success")
            return redirect(url_for("page_acl", title=title))
    settings = {action: effective_policy(page, action) for action in ACL_ACTIONS}
    return render_template(
        "page_acl.html", page=page, acl_settings=settings,
        policy_labels=ACL_POLICY_LABELS, actions=ACL_ACTIONS,
    )


@app.route("/history/<path:title>")
def history(title: str):
    title, page = resolve_document_title(title)
    db = get_db()
    if page is None:
        tombstone = db.execute(
            "SELECT title FROM deleted_page_tombstones WHERE title = ?", (title,)
        ).fetchone()
        if tombstone is None:
            abort(404)
    else:
        denial = require_page_access(page, "read")
        if denial is not None:
            return denial

    # Keep the history of deleted documents, including earlier versions, even
    # though the live page row and its FK-linked revisions are removed.
    history_rows = []
    if page is not None:
        active_rows = db.execute(
            "SELECT id, editor_name, summary, created_at FROM revisions "
            "WHERE page_id = ? ORDER BY id DESC LIMIT 200", (page["id"],)
        ).fetchall()
        history_rows.extend({**dict(row), "event_type": "edit", "revision_id": row["id"], "archived": False}
                            for row in active_rows)
    archived_rows = db.execute(
        "SELECT original_revision_id, editor_name, summary, created_at, deleted_at "
        "FROM deleted_page_revisions WHERE title = ? ORDER BY created_at DESC LIMIT 200", (title,)
    ).fetchall()
    history_rows.extend({
        "id": row["original_revision_id"], "editor_name": row["editor_name"],
        "summary": row["summary"], "created_at": row["created_at"],
        "event_type": "edit", "revision_id": None, "archived": True,
    } for row in archived_rows)
    deletion_rows = db.execute(
        "SELECT id, deleted_by_name, deleted_at FROM page_deletion_events "
        "WHERE title = ? ORDER BY deleted_at DESC LIMIT 100", (title,)
    ).fetchall()
    history_rows.extend({
        "id": -row["id"], "editor_name": row["deleted_by_name"],
        "summary": "문서 삭제", "created_at": row["deleted_at"],
        "event_type": "delete", "revision_id": None, "archived": False,
    } for row in deletion_rows)
    history_rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
    history_rows = history_rows[:300]
    return render_template("history.html", page=page, title=title, revisions=history_rows)


@app.route("/diff/<int:revision_id>")
def diff_revision(revision_id: int):
    db = get_db()
    current = db.execute("SELECT * FROM revisions WHERE id = ?", (revision_id,)).fetchone()
    if current is None:
        abort(404)
    page = db.execute("SELECT * FROM pages WHERE id = ?", (current["page_id"],)).fetchone()
    denial = require_page_access(page, "read")
    if denial is not None:
        return denial
    previous = db.execute(
        "SELECT * FROM revisions WHERE page_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
        (current["page_id"], revision_id),
    ).fetchone()
    old_text = previous["content"] if previous else ""
    new_text = current["content"]
    table = difflib.HtmlDiff(wrapcolumn=100).make_table(
        old_text.splitlines(), new_text.splitlines(),
        fromdesc="이전 버전" if previous else "문서 생성 전",
        todesc="변경 버전", context=True, numlines=3,
    )
    return render_template("diff.html", page_title=db.execute(
        "SELECT title FROM pages WHERE id = ?", (current["page_id"],)
    ).fetchone()["title"], revision=current, diff_table=table)


@app.route("/deleted-diff/<int:revision_id>/<path:title>")
def deleted_diff_revision(revision_id: int, title: str):
    """Show a diff for a revision archived when its document was deleted."""
    title, page = resolve_document_title(title)
    db = get_db()
    current = db.execute(
        "SELECT * FROM deleted_page_revisions WHERE title = ? AND original_revision_id = ? "
        "ORDER BY deleted_at DESC LIMIT 1",
        (title, revision_id),
    ).fetchone()
    if current is None:
        abort(404)

    # If the title has since been recreated, its current revisions belong to
    # the same document history and may be the immediately preceding version.
    candidates = []
    archived_previous = db.execute(
        "SELECT content, created_at, original_revision_id FROM deleted_page_revisions "
        "WHERE title = ? AND created_at < ? ORDER BY created_at DESC, original_revision_id DESC LIMIT 1",
        (title, current["created_at"]),
    ).fetchone()
    if archived_previous is not None:
        candidates.append(archived_previous)
    if page is not None:
        denial = require_page_access(page, "read")
        if denial is not None:
            return denial
        live_previous = db.execute(
            "SELECT r.content, r.created_at, r.id AS original_revision_id FROM revisions r "
            "WHERE r.page_id = ? AND r.created_at < ? ORDER BY r.created_at DESC, r.id DESC LIMIT 1",
            (page["id"], current["created_at"]),
        ).fetchone()
        if live_previous is not None:
            candidates.append(live_previous)
    previous = max(candidates, key=lambda row: (row["created_at"], row["original_revision_id"])) if candidates else None

    table = difflib.HtmlDiff(wrapcolumn=100).make_table(
        (previous["content"] if previous else "").splitlines(),
        current["content"].splitlines(),
        fromdesc="이전 버전" if previous else "문서 생성 전",
        todesc="삭제 전 보존 버전",
        context=True, numlines=3,
    )
    revision_view = {
        "created_at": current["created_at"],
        "editor_name": current["editor_name"],
    }
    return render_template(
        "diff.html", page_title=title, revision=revision_view,
        diff_table=table, is_deleted_history=(page is None),
    )


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()[:100]
    results = []
    if query:
        # SQLite LIKE treats % and _ as wildcards; escape them for literal search.
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        matching_rows = get_db().execute(
            "SELECT p.id, p.title, p.updated_at, r.content "
            "FROM pages p JOIN revisions r ON r.id = (SELECT MAX(r2.id) FROM revisions r2 WHERE r2.page_id = p.id) "
            "WHERE p.title LIKE ? ESCAPE '\\' OR r.content LIKE ? ESCAPE '\\' "
            "ORDER BY CASE WHEN p.title = ? THEN 0 WHEN p.title LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END, p.title LIMIT 100",
            (f"%{escaped}%", f"%{escaped}%", query, f"{escaped}%"),
        ).fetchall()
        # Do not suggest creating a page when matching pages exist but are hidden by read ACL.
        results = [row for row in matching_rows if can_access_page(row, "read")]
        can_create_document = not matching_rows and fetch_page(query) is None
    else:
        can_create_document = False
    return render_template("search.html", query=query, results=results, can_create_document=can_create_document)


@app.route("/recent")
def recent_changes():
    db = get_db()
    edit_rows = db.execute(
        "SELECT r.id, r.editor_name, r.summary, r.created_at, p.title, p.id AS page_id "
        "FROM revisions r JOIN pages p ON p.id = r.page_id ORDER BY r.id DESC LIMIT 200"
    ).fetchall()
    rows = [dict(row, event_type="edit") for row in edit_rows
            if can_access_page({"id": row["page_id"]}, "read")]
    delete_rows = db.execute(
        "SELECT id, deleted_by_name AS editor_name, '문서 삭제' AS summary, "
        "deleted_at AS created_at, title, NULL AS page_id "
        "FROM page_deletion_events ORDER BY id DESC LIMIT 200"
    ).fetchall()
    rows.extend(dict(row, event_type="delete") for row in delete_rows)
    rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
    return render_template("recent.html", rows=rows[:100])


@app.route("/category/<path:name>")
def category(name: str):
    pages = get_db().execute(
        "SELECT p.id, p.title, p.updated_at, r.content FROM pages p "
        "JOIN revisions r ON r.id = (SELECT MAX(r2.id) FROM revisions r2 WHERE r2.page_id = p.id) "
        "WHERE r.content LIKE ? ORDER BY p.title LIMIT 500",
        (f"%[[분류:{name}%",),
    ).fetchall()
    pages = [p for p in pages if name in extract_categories(p["content"]) and can_access_page(p, "read")]
    return render_template("category.html", name=name, pages=pages)


@app.route("/random")
def random_page():
    rows = get_db().execute(
        "SELECT p.id, p.title FROM pages p "
        "WHERE EXISTS (SELECT 1 FROM revisions r WHERE r.page_id = p.id) "
        "ORDER BY RANDOM()"
    ).fetchall()
    page = next((row for row in rows if can_access_page(row, "read")), None)
    if page is None:
        flash("랜덤 문서로 이동할 수 있는 문서가 아직 없습니다.", "info")
        return redirect(url_for("index"))
    return redirect(page_url(page["title"]))


@app.route("/subpage/new", methods=["GET", "POST"])
@login_required
def new_subpage():
    """Create a page nested under an existing ordinary wiki page."""
    parent_title = request.values.get("parent", "").strip().strip("/")
    if not parent_title or len(parent_title) > 200:
        abort(400, description="상위 문서 제목이 올바르지 않습니다.")
    if parent_title.startswith("사용자:"):
        abort(403, description="개인 문서는 하위 문서를 지원하지 않습니다.")
    parent = fetch_page(parent_title)
    if parent is None:
        abort(404, description="상위 문서가 존재하지 않습니다.")

    personal_denial = enforce_personal_document_edit(parent_title)
    if personal_denial is not None:
        return personal_denial
    denial = require_page_access(parent, "read")
    if denial is not None:
        return denial
    denial = require_page_access(parent, "edit")
    if denial is not None:
        return denial
    require_contribution_access("edit")

    subpage = ""
    if request.method == "POST":
        subpage = request.form.get("subpage", "").strip().strip("/")
        invalid = (
            not subpage
            or len(subpage) > 200
            or any(ord(ch) < 32 for ch in subpage)
            or "\\" in subpage
            or "//" in subpage
            or any(not part.strip() or part in {".", ".."} for part in subpage.split("/"))
        )
        title = f"{parent_title}/{subpage}" if subpage else ""
        if invalid:
            flash("하위 문서 이름을 확인해 주세요. 빈 이름, 역슬래시, 연속된 슬래시 및 상대 경로는 사용할 수 없습니다.", "error")
        elif len(title) > 200:
            flash("전체 문서 제목은 200자 이하여야 합니다.", "error")
        elif fetch_page(title) is not None:
            flash("같은 이름의 문서가 이미 있습니다. 해당 문서로 이동합니다.", "info")
            return redirect(page_url(title))
        else:
            return redirect(url_for("edit_page", title=title))

    return render_template("new_subpage.html", parent_title=parent_title, subpage=subpage)


@app.route("/user/<username>/new", methods=["GET", "POST"])
@login_required
def new_user_subpage(username: str):
    abort(403, description="개인 문서는 하위 문서를 지원하지 않습니다.")


@app.route("/owner/restore-upload/start", methods=["POST"])
@owner_required
def owner_restore_upload_start():
    if not IS_VERCEL or not app.config.get("DATABASE_URL"):
        return jsonify({"error": "분할 업로드는 Vercel PostgreSQL 모드에서만 지원됩니다."}), 400
    try:
        filename = secure_filename(request.form.get("filename", "backup.zip")) or "backup.zip"
        size_bytes = int(request.form.get("size_bytes", "0"))
        total_chunks = int(request.form.get("total_chunks", "0"))
        expected_chunks = max(1, (size_bytes + RESTORE_CHUNK_SIZE - 1) // RESTORE_CHUNK_SIZE)
        if not filename.lower().endswith(".zip") or not 0 < size_bytes <= MAX_RESTORE_SIZE:
            raise ValueError("ZIP 백업은 190MB 이하이어야 합니다.")
        if total_chunks != expected_chunks:
            raise ValueError("업로드 분할 수가 올바르지 않습니다.")
        upload_id = uuid.uuid4().hex
        db = get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(timespec="seconds")
        db.execute("DELETE FROM namulite_restore_uploads WHERE created_at < ?", (cutoff,))
        db.execute(
            "INSERT INTO namulite_restore_uploads(upload_id, filename, total_chunks, total_bytes, created_at) VALUES (?, ?, ?, ?, ?)",
            (upload_id, filename, total_chunks, size_bytes, now_iso()),
        )
        db.commit()
        return jsonify({"upload_id": upload_id, "total_chunks": total_chunks, "chunk_size": RESTORE_CHUNK_SIZE})
    except (ValueError, sqlite3.Error) as exc:
        return jsonify({"error": str(exc) or "업로드를 시작하지 못했습니다."}), 400
    except Exception as exc:
        app.logger.exception("Could not start chunked backup upload")
        return jsonify({"error": f"업로드 시작 실패: {exc.__class__.__name__}"}), 500


@app.route("/owner/restore-upload/chunk", methods=["POST"])
@owner_required
def owner_restore_upload_chunk():
    if not IS_VERCEL or not app.config.get("DATABASE_URL"):
        return jsonify({"error": "분할 업로드는 Vercel PostgreSQL 모드에서만 지원됩니다."}), 400
    upload_id = request.form.get("upload_id", "")
    try:
        chunk_index = int(request.form.get("chunk_index", "-1"))
    except ValueError:
        return jsonify({"error": "청크 번호가 올바르지 않습니다."}), 400
    uploaded = request.files.get("chunk")
    if not re.fullmatch(r"[a-f0-9]{32}", upload_id) or uploaded is None:
        return jsonify({"error": "업로드 식별자 또는 데이터가 없습니다."}), 400
    try:
        db = get_db()
        meta = db.execute("SELECT total_chunks, created_at FROM namulite_restore_uploads WHERE upload_id = ?", (upload_id,)).fetchone()
        if not meta or datetime.fromisoformat(meta["created_at"]) < datetime.now(timezone.utc) - timedelta(hours=6):
            return jsonify({"error": "업로드가 만료되었습니다. 다시 시작해 주세요."}), 404
        if not 0 <= chunk_index < meta["total_chunks"]:
            return jsonify({"error": "청크 번호가 범위를 벗어났습니다."}), 400
        data = uploaded.read(RESTORE_CHUNK_SIZE + 1)
        if not data or len(data) > RESTORE_CHUNK_SIZE:
            return jsonify({"error": "청크 크기가 올바르지 않습니다."}), 400
        db.execute(
            "INSERT INTO namulite_restore_chunks(upload_id, chunk_index, chunk_data) VALUES (?, ?, ?) "
            "ON CONFLICT(upload_id, chunk_index) DO UPDATE SET chunk_data = excluded.chunk_data",
            (upload_id, chunk_index, data),
        )
        db.commit()
        return jsonify({"ok": True, "chunk_index": chunk_index})
    except Exception as exc:
        app.logger.exception("Could not save backup upload chunk")
        return jsonify({"error": f"청크 저장 실패: {exc.__class__.__name__}"}), 500


@app.route("/owner/restore-upload/finish", methods=["POST"])
@owner_required
def owner_restore_upload_finish():
    if not IS_VERCEL or not app.config.get("DATABASE_URL"):
        return jsonify({"error": "분할 업로드는 Vercel PostgreSQL 모드에서만 지원됩니다."}), 400
    upload_id = request.form.get("upload_id", "")
    if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
        return jsonify({"error": "업로드 식별자가 올바르지 않습니다."}), 400
    try:
        db = get_db()
        meta = db.execute(
            "SELECT filename, total_chunks, total_bytes, created_at FROM namulite_restore_uploads WHERE upload_id = ?",
            (upload_id,),
        ).fetchone()
        if not meta:
            return jsonify({"error": "업로드를 찾을 수 없습니다. 다시 시작해 주세요."}), 404
        total = 0
        seen = 0
        with tempfile.TemporaryDirectory(prefix="namulite-chunked-restore-") as temp_dir:
            archive_path = Path(temp_dir) / "backup.zip"
            cursor = db.execute(
                "SELECT chunk_index, chunk_data FROM namulite_restore_chunks WHERE upload_id = ? ORDER BY chunk_index ASC",
                (upload_id,),
            )
            with archive_path.open("wb") as out:
                expected = 0
                for row in cursor:
                    if row["chunk_index"] != expected:
                        raise ValueError("백업 업로드 청크가 누락되었습니다.")
                    blob = bytes(row["chunk_data"])
                    out.write(blob)
                    total += len(blob)
                    seen += 1
                    expected += 1
            if seen != meta["total_chunks"] or total != meta["total_bytes"]:
                raise ValueError("업로드된 백업 크기가 원본과 일치하지 않습니다.")
            with archive_path.open("rb") as stream:
                from werkzeug.datastructures import FileStorage
                restore_backup_upload(FileStorage(stream=stream, filename=meta["filename"]))
        # restore_backup_upload replaces primary rows but leaves staging tables intact.
        cleanup = get_db()
        cleanup.execute("DELETE FROM namulite_restore_uploads WHERE upload_id = ?", (upload_id,))
        cleanup.commit()
        session.clear()
        flash("백업을 복원했습니다. 보안을 위해 다시 로그인해 주세요.", "success")
        return jsonify({"ok": True, "redirect": url_for("login")})
    except Exception as exc:
        app.logger.exception("Chunked backup restore failed")
        try:
            cleanup = get_db()
            cleanup.execute("DELETE FROM namulite_restore_uploads WHERE upload_id = ?", (upload_id,))
            cleanup.commit()
        except Exception:
            pass
        return jsonify({"error": f"복원 실패: {exc}"}), 400


@app.route("/owner", methods=["GET"])
@owner_required
def owner_settings():
    settings = get_site_settings()
    safe_pg_settings = {
        "host": settings.get("postgres_host", ""),
        "port": settings.get("postgres_port", "5432"),
        "database": settings.get("postgres_database", ""),
        "username": settings.get("postgres_username", ""),
        "sslmode": settings.get("postgres_sslmode", "prefer"),
        "password_saved": bool(settings.get("postgres_password_encrypted")),
    }
    backups = list_backups()
    pg_backups = list_postgres_backups()
    total_bytes = sum(item["size"] for item in backups) + sum(item["size"] for item in pg_backups)
    return render_template(
        "owner.html", pg_settings=safe_pg_settings,
        backup_retention_days=configured_backup_retention_days(), backups=backups,
        pg_backups=pg_backups, backup_total_bytes=total_bytes,
    )


@app.route("/owner", methods=["POST"])
@owner_required
def owner_settings_post():
    action = request.form.get("action", "save")
    if action == "backup":
        try:
            archive_path = create_manual_backup()
            store_backup_in_postgres(archive_path)
        except Exception as exc:
            app.logger.exception("Manual wiki backup to PostgreSQL failed")
            flash(f"백업에 실패했습니다: {exc.__class__.__name__}. PostgreSQL 연결 설정과 서버 로그를 확인해 주세요.", "error")
        else:
            flash(f"백업을 만들고 PostgreSQL에 저장했습니다: {archive_path.name}", "success")
        return redirect(url_for("owner_settings"))

    if action == "restore_upload":
        uploaded = request.files.get("backup_file")
        if not uploaded or not uploaded.filename or not uploaded.filename.lower().endswith(".zip"):
            flash("복원할 ZIP 백업 파일을 선택해 주세요.", "error")
            return redirect(url_for("owner_settings"))
        try:
            restore_backup_upload(uploaded)
        except Exception as exc:
            app.logger.exception("Backup restore failed")
            flash(f"복원에 실패했습니다: {exc}", "error")
        else:
            session.clear()
            flash("백업 파일에서 위키 데이터와 업로드 이미지를 복원했습니다. 다시 로그인해 주세요.", "success")
            return redirect(url_for("login"))
        return redirect(url_for("owner_settings"))

    if action not in {"save", "test"}:
        abort(400)
    host = request.form.get("postgres_host", "").strip()[:255]
    port_text = request.form.get("postgres_port", "5432").strip()
    database = request.form.get("postgres_database", "").strip()[:255]
    username = request.form.get("postgres_username", "").strip()[:255]
    sslmode = request.form.get("postgres_sslmode", "prefer").strip()
    password_input = request.form.get("postgres_password", "")
    clear_password = request.form.get("clear_postgres_password") == "1"
    retention_text = request.form.get("backup_retention_days", "30").strip()
    errors = []
    try:
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        port = 5432
        errors.append("PostgreSQL 포트는 1~65535 사이의 숫자여야 합니다.")
    if sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        errors.append("지원하지 않는 SSL 모드입니다.")
    try:
        retention = int(retention_text)
        if not 1 <= retention <= 3650:
            raise ValueError
    except ValueError:
        retention = 30
        errors.append("백업 보관 기간은 1~3650일로 설정해 주세요.")

    settings = get_site_settings()
    password = "" if clear_password else (password_input if password_input else decrypt_setting_secret(settings.get("postgres_password_encrypted", "")))
    if action == "test" and not errors:
        if not host or not database or not username:
            errors.append("연결 테스트를 하려면 호스트, 데이터베이스, 사용자 이름을 입력해야 합니다.")
        elif not password:
            errors.append("연결 테스트를 하려면 PostgreSQL 비밀번호를 입력하거나 저장된 비밀번호가 있어야 합니다.")
        else:
            try:
                import psycopg
                with psycopg.connect(
                    host=host, port=port, dbname=database, user=username, password=password,
                    sslmode=sslmode, connect_timeout=5,
                ) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT 1")
                        result = cursor.fetchone()
                if result and result[0] == 1:
                    flash("PostgreSQL 연결 테스트에 성공했습니다." + (" 현재 주 데이터 저장소도 PostgreSQL/Neon입니다." if app.config.get("DATABASE_URL") else " 현재 주 데이터 저장소는 로컬 SQLite입니다."), "success")
            except ImportError:
                errors.append("PostgreSQL 연결 테스트 모듈이 없습니다. requirements.txt의 패키지를 설치한 뒤 서버를 다시 실행해 주세요.")
            except Exception as exc:
                app.logger.warning("PostgreSQL connection test failed: %s", exc.__class__.__name__)
                errors.append(f"PostgreSQL 연결에 실패했습니다 ({exc.__class__.__name__}). 호스트, 포트, 자격 증명, SSL 설정을 확인해 주세요.")
    if errors:
        for message in errors:
            flash(message, "error")
        return redirect(url_for("owner_settings"))

    if action == "save":
        encrypted_password = settings.get("postgres_password_encrypted", "")
        try:
            if clear_password:
                encrypted_password = ""
            elif password_input:
                encrypted_password = encrypt_setting_secret(password_input)
        except ImportError:
            flash("비밀번호 암호화 모듈이 없어 설정을 저장하지 않았습니다. requirements.txt를 설치해 주세요.", "error")
            return redirect(url_for("owner_settings"))
        save_site_settings({
            "postgres_host": host, "postgres_port": str(port),
            "postgres_database": database, "postgres_username": username,
            "postgres_password_encrypted": encrypted_password,
            "postgres_sslmode": sslmode, "backup_retention_days": str(retention),
        })
        purge_expired_backups()
        flash("소유자 설정을 저장했습니다." + (" 주 데이터베이스는 DATABASE_URL 환경 변수에서 관리됩니다." if app.config.get("DATABASE_URL") else " PostgreSQL 연결 정보는 백업 저장소로 사용됩니다."), "success")
        return redirect(url_for("owner_settings"))
    return redirect(url_for("owner_settings"))


@app.route("/owner/backups/<path:filename>", methods=["GET"])
@owner_required
def owner_download_backup(filename: str):
    if IS_VERCEL:
        abort(404)
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.startswith("wiki-backup-") or not safe_name.endswith(".zip"):
        abort(404)
    path = (backup_directory() / safe_name).resolve()
    if path.parent != backup_directory().resolve() or not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=safe_name, mimetype="application/zip")


@app.route("/owner/postgres-backups/<int:backup_id>/download", methods=["GET"])
@owner_required
def owner_download_postgres_backup(backup_id: int):
    try:
        with _postgres_connect() as connection:
            _ensure_pg_backup_table(connection)
            with connection.cursor() as cursor:
                cursor.execute("SELECT filename, archive FROM namulite_backups WHERE id = %s", (backup_id,))
                row = cursor.fetchone()
        if not row:
            abort(404)
        data = bytes(row[1])
        filename = Path(row[0]).name
        # Stream large archives: Vercel Function responses have a 4.5 MB
        # non-streaming payload limit.
        response = Response((data[offset:offset + 64 * 1024] for offset in range(0, len(data), 64 * 1024)), mimetype="application/zip")
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as exc:
        if isinstance(exc, Exception) and exc.__class__.__name__ == "NotFound":
            raise
        app.logger.warning("PostgreSQL backup download failed: %s", exc.__class__.__name__)
        flash("PostgreSQL 백업을 다운로드하지 못했습니다. 연결 설정을 확인해 주세요.", "error")
        return redirect(url_for("owner_settings"))


@app.route("/owner/postgres-backups/<int:backup_id>/restore", methods=["POST"])
@owner_required
def owner_restore_postgres_backup(backup_id: int):
    try:
        with _postgres_connect() as connection:
            _ensure_pg_backup_table(connection)
            with connection.cursor() as cursor:
                cursor.execute("SELECT filename, archive FROM namulite_backups WHERE id = %s", (backup_id,))
                row = cursor.fetchone()
        if not row:
            abort(404)
        from io import BytesIO
        from werkzeug.datastructures import FileStorage
        restore_backup_upload(FileStorage(stream=BytesIO(bytes(row[1])), filename=Path(row[0]).name))
    except Exception as exc:
        app.logger.exception("Restore from PostgreSQL backup failed")
        flash(f"PostgreSQL 백업 복원에 실패했습니다: {exc.__class__.__name__}.", "error")
        return redirect(url_for("owner_settings"))
    else:
        session.clear()
        flash(f"PostgreSQL 백업 {Path(row[0]).name}에서 복원했습니다. 다시 로그인해 주세요.", "success")
        return redirect(url_for("login"))


@app.route("/terms")
def terms_page():
    return render_template("terms.html", terms_version=TERMS_VERSION, license_version=LICENSE_VERSION, ai_policy_version=AI_POLICY_VERSION)


@app.route("/privacy")
def privacy_page():
    return render_template("privacy.html", privacy_version=PRIVACY_VERSION, login_log_retention_days=LOGIN_LOG_RETENTION_DAYS, registration_log_retention_days=REGISTRATION_LOG_RETENTION_DAYS)


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        block = contribution_block_reason("register")
        if block:
            abort(403, description=block)
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        terms_agreed = request.form.get("terms_agree") == "on"
        privacy_agreed = request.form.get("privacy_agree") == "on"
        if not terms_agreed:
            flash("회원가입을 하려면 이용약관에 동의해야 합니다.", "error")
        elif not privacy_agreed:
            flash("회원가입을 하려면 개인정보 처리 안내를 확인하고 동의해야 합니다.", "error")
        elif not re.fullmatch(r"[가-힣A-Za-z0-9_]{2,20}", username):
            flash("아이디는 한글·영문·숫자·밑줄로 2~20자여야 합니다.", "error")
        elif len(password) < 8:
            flash("비밀번호는 8자 이상이어야 합니다.", "error")
        elif password != confirm:
            flash("비밀번호가 일치하지 않습니다.", "error")
        else:
            db = get_db()
            signup_ip = client_ip()
            if not signup_ip:
                flash("접속 IP를 확인할 수 없어 가입을 진행할 수 없습니다. 운영자에게 문의해 주세요.", "error")
            else:
                try:
                    slot_reserved = reserve_registration_ip_slot(db, signup_ip, MAX_ACCOUNTS_PER_IP)
                    if not slot_reserved:
                        db.rollback()
                        flash(
                            f"한 IP 주소에서는 최대 {MAX_ACCOUNTS_PER_IP}개 계정까지만 가입할 수 있습니다. "
                            "같은 공인 IP를 공유하는 다른 이용자의 계정도 합산됩니다.",
                            "error",
                        )
                    else:
                        has_owner = db.execute("SELECT 1 FROM users WHERE is_owner = 1 LIMIT 1").fetchone()
                        is_first_owner = not has_owner
                        accepted_at = now_iso()
                        cur = db.execute(
                            "INSERT INTO users(username, password_hash, is_admin, is_owner, created_at, registered_ip, last_ip, "
                            "terms_accepted_at, terms_version, privacy_accepted_at, privacy_version) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (username, generate_password_hash(password), 1 if is_first_owner else 0, 1 if is_first_owner else 0,
                             accepted_at, signup_ip, signup_ip, accepted_at, TERMS_VERSION,
                             accepted_at, PRIVACY_VERSION),
                        )
                        db.execute(
                            "INSERT INTO registration_logs(user_id, username, ip_address, created_at) VALUES (?, ?, ?, ?)",
                            (cur.lastrowid, username, signup_ip, accepted_at),
                        )
                        db.execute(
                            "INSERT OR IGNORE INTO pages(title, created_at, updated_at) VALUES (?, ?, ?)",
                            (f"사용자:{username}", accepted_at, accepted_at),
                        )
                        signup_cutoff = (datetime.now(timezone.utc) - timedelta(days=REGISTRATION_LOG_RETENTION_DAYS)).isoformat(timespec="seconds")
                        db.execute("DELETE FROM registration_logs WHERE created_at < ?", (signup_cutoff,))
                        db.commit()
                        session.clear()
                        session["user_id"] = cur.lastrowid
                        flash("회원가입이 완료되었습니다." + (" 첫 가입 계정은 위키 소유자이자 관리자입니다." if is_first_owner else ""), "success")
                        return redirect(url_for("index"))
                except sqlite3.IntegrityError:
                    db.rollback()
                    flash("이미 사용 중인 아이디입니다.", "error")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("아이디 또는 비밀번호를 확인해 주세요.", "error")
        else:
            db = get_db()
            ip = client_ip()
            logged_at = now_iso()
            db.execute(
                "INSERT INTO login_logs(user_id, username, ip_address, created_at) VALUES (?, ?, ?, ?)",
                (user["id"], user["username"], ip, logged_at),
            )
            if ip:
                db.execute("UPDATE users SET last_ip = ? WHERE id = ?", (ip, user["id"]))
            cutoff = (datetime.now(timezone.utc) - timedelta(days=LOGIN_LOG_RETENTION_DAYS)).isoformat(timespec="seconds")
            db.execute("DELETE FROM login_logs WHERE created_at < ?", (cutoff,))
            db.commit()
            session.clear()
            session["user_id"] = user["id"]
            pending_warning_count = db.execute(
                "SELECT COUNT(*) FROM user_warnings WHERE user_id = ? AND acknowledged_at IS NULL",
                (user["id"],),
            ).fetchone()[0]
            if pending_warning_count:
                session["show_warning_modal_after_login"] = True
            flash("로그인했습니다. 보안 목적으로 로그인 IP가 기록됩니다.", "success")
            next_url = request.args.get("next")
            return redirect(next_url if is_safe_next(next_url) else url_for("index"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("로그아웃했습니다.", "info")
    return redirect(url_for("index"))


@app.route("/discuss/<path:title>", methods=["GET", "POST"])
def discuss(title: str):
    title, page = resolve_document_title(title)
    if page is None:
        abort(404)
    denial = require_page_access(page, "read")
    if denial is not None:
        return denial
    if request.method == "POST":
        denial = require_page_access(page, "discuss")
        if denial is not None:
            return denial
        require_contribution_access("discuss")
        body = request.form.get("content", "").strip()
        if not body:
            flash("토론 내용을 입력해 주세요.", "error")
        elif len(body) > 10_000:
            flash("토론 글은 10,000자 이하로 작성해 주세요.", "error")
        else:
            username = g.user["username"] if g.user is not None else "익명"
            user_id = g.user["id"] if g.user is not None else None
            get_db().execute(
                "INSERT INTO discussions(page_id, user_id, username, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (page["id"], user_id, username, body, now_iso()),
            )
            get_db().commit()
            flash("토론 글을 등록했습니다.", "success")
            return redirect(url_for("discuss", title=title))
    posts = get_db().execute(
        "SELECT * FROM discussions WHERE page_id = ? ORDER BY id ASC LIMIT 500", (page["id"],)
    ).fetchall()
    can_discuss = can_access_page(page, "discuss") and contribution_block_reason("discuss") is None
    return render_template("discuss.html", page=page, posts=posts, can_discuss=can_discuss)


@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", code=400, message=getattr(error, "description", "잘못된 요청입니다.")), 400


@app.errorhandler(403)
def forbidden(error):
    message = getattr(error, "description", None) or "이 페이지를 이용할 권한이 없습니다."
    return render_template("error.html", code=403, message=message), 403


@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", code=404, message="요청한 문서를 찾을 수 없습니다."), 404


@app.errorhandler(413)
def too_large(_error):
    return render_template("error.html", code=413, message="전송한 데이터가 너무 큽니다."), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)

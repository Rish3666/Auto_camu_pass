"""Encrypted SQLite store for self-service Telegram bot users."""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet


def _get_fernet(secret_key: str | None = None) -> Fernet:
    key = (secret_key or os.environ.get("CAMU_SECRET_KEY", "")).strip()
    if not key:
        raise RuntimeError("CAMU_SECRET_KEY environment variable is required.")
    return Fernet(key.encode("utf-8"))


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path = Path("data/users.db")) -> None:
    """Create the users table if it does not exist, migrating columns if needed."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                camu_username TEXT NOT NULL,
                camu_password_enc BLOB NOT NULL,
                pass_type TEXT NOT NULL DEFAULT 'Week End Gate Pass',
                reason TEXT NOT NULL DEFAULT 'Weekend outing',
                out_time TEXT NOT NULL DEFAULT '06:00',
                in_time TEXT NOT NULL DEFAULT '20:00',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_submitted_date TEXT,
                last_status TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "last_submitted_date" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_submitted_date TEXT")
        if "last_status" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_status TEXT")


def upsert_user(
    *,
    telegram_user_id: int,
    chat_id: int,
    name: str,
    camu_username: str,
    camu_password: str,
    pass_type: str = "Week End Gate Pass",
    reason: str = "Weekend outing",
    out_time: str = "06:00",
    in_time: str = "20:00",
    db_path: Path = Path("data/users.db"),
    secret_key: str | None = None,
) -> None:
    """Insert or update a friend's encrypted Camu settings."""
    init_db(db_path)
    fernet = _get_fernet(secret_key)
    enc_pw = fernet.encrypt(camu_password.encode("utf-8"))
    now = datetime.now(timezone.utc).isoformat()

    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (
                telegram_user_id, chat_id, name, camu_username, camu_password_enc,
                pass_type, reason, out_time, in_time, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                name = excluded.name,
                camu_username = excluded.camu_username,
                camu_password_enc = excluded.camu_password_enc,
                pass_type = excluded.pass_type,
                reason = excluded.reason,
                out_time = excluded.out_time,
                in_time = excluded.in_time,
                enabled = 1,
                updated_at = excluded.updated_at
            """,
            (
                telegram_user_id,
                chat_id,
                name,
                camu_username,
                enc_pw,
                pass_type,
                reason,
                out_time,
                in_time,
                now,
                now,
            ),
        )


def _row_to_user(row: sqlite3.Row, fernet: Fernet, *, include_password: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "telegram_user_id": row["telegram_user_id"],
        "chat_id": row["chat_id"],
        "name": row["name"],
        "username": row["camu_username"],
        "pass_type": row["pass_type"],
        "reason": row["reason"],
        "out_time": row["out_time"],
        "in_time": row["in_time"],
        "enabled": bool(row["enabled"]),
        "last_submitted_date": row["last_submitted_date"],
        "last_status": row["last_status"],
    }
    if include_password:
        data["password"] = fernet.decrypt(row["camu_password_enc"]).decode("utf-8")
    return data


def get_user(
    telegram_user_id: int,
    *,
    include_password: bool = False,
    db_path: Path = Path("data/users.db"),
    secret_key: str | None = None,
) -> dict[str, Any] | None:
    """Fetch a single registered user by Telegram user ID."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
        ).fetchone()
    if not row:
        return None
    fernet = _get_fernet(secret_key) if include_password else None
    return _row_to_user(row, fernet, include_password=include_password)  # type: ignore[arg-type]


def update_user_fields(
    telegram_user_id: int,
    *,
    name: str | None = None,
    camu_username: str | None = None,
    camu_password: str | None = None,
    pass_type: str | None = None,
    reason: str | None = None,
    out_time: str | None = None,
    in_time: str | None = None,
    db_path: Path = Path("data/users.db"),
    secret_key: str | None = None,
) -> bool:
    """Update one or more fields for an existing registered user. Returns True if user existed."""
    init_db(db_path)
    updates: list[str] = []
    params: list[Any] = []
    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if camu_username is not None:
        updates.append("camu_username = ?")
        params.append(camu_username)
    if camu_password is not None:
        fernet = _get_fernet(secret_key)
        updates.append("camu_password_enc = ?")
        params.append(fernet.encrypt(camu_password.encode("utf-8")))
    if pass_type is not None:
        updates.append("pass_type = ?")
        params.append(pass_type)
    if reason is not None:
        updates.append("reason = ?")
        params.append(reason)
    if out_time is not None:
        updates.append("out_time = ?")
        params.append(out_time)
    if in_time is not None:
        updates.append("in_time = ?")
        params.append(in_time)

    if not updates:
        return False

    updates.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(telegram_user_id)

    with _connect(db_path) as conn:
        cur = conn.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE telegram_user_id = ?",
            tuple(params),
        )
        return cur.rowcount > 0


def set_enabled(
    telegram_user_id: int,
    enabled: bool,
    *,
    db_path: Path = Path("data/users.db"),
) -> bool:
    """Pause or resume weekly submissions for a user. Returns True if user existed."""
    init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE users SET enabled = ?, updated_at = ? WHERE telegram_user_id = ?",
            (1 if enabled else 0, now, telegram_user_id),
        )
        return cur.rowcount > 0


def delete_user(telegram_user_id: int, *, db_path: Path = Path("data/users.db")) -> bool:
    """Delete a user's stored credentials and settings. Returns True if deleted."""
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM users WHERE telegram_user_id = ?", (telegram_user_id,))
        return cur.rowcount > 0


def record_submission_status(
    telegram_user_id: int,
    *,
    status: str,
    submitted_date: str | None = None,
    db_path: Path = Path("data/users.db"),
) -> bool:
    """Record the latest submission status and optionally last_submitted_date."""
    init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        if submitted_date:
            cur = conn.execute(
                "UPDATE users SET last_status = ?, last_submitted_date = ?, updated_at = ? WHERE telegram_user_id = ?",
                (status, submitted_date, now, telegram_user_id),
            )
        else:
            cur = conn.execute(
                "UPDATE users SET last_status = ?, updated_at = ? WHERE telegram_user_id = ?",
                (status, now, telegram_user_id),
            )
        return cur.rowcount > 0


def list_active_users(
    *,
    db_path: Path = Path("data/users.db"),
    secret_key: str | None = None,
) -> list[dict[str, Any]]:
    """Return all enabled users with decrypted passwords for scheduled submission."""
    init_db(db_path)
    fernet = _get_fernet(secret_key)
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM users WHERE enabled = 1").fetchall()
    return [_row_to_user(row, fernet, include_password=True) for row in rows]

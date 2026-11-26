"""Unit tests for the encrypted SQLite user store."""

import sqlite3

from crypto_helper import generate_key
from db import delete_user, get_user, list_active_users, set_enabled, upsert_user


def test_db_encrypts_password_and_crud(tmp_path):
    db_file = tmp_path / "users.db"
    key = generate_key()

    upsert_user(
        telegram_user_id=101,
        chat_id=101,
        name="Rish",
        camu_username="rish@college.edu",
        camu_password="super-secret-password",
        db_path=db_file,
        secret_key=key,
    )

    # Confirm plaintext password is NOT stored in SQLite
    raw_row = sqlite3.connect(str(db_file)).execute(
        "SELECT camu_password_enc FROM users WHERE telegram_user_id = 101"
    ).fetchone()
    assert b"super-secret-password" not in raw_row[0]

    user = get_user(101, include_password=True, db_path=db_file, secret_key=key)
    assert user is not None
    assert user["password"] == "super-secret-password"
    assert user["enabled"] is True

    assert set_enabled(101, False, db_path=db_file) is True
    assert list_active_users(db_path=db_file, secret_key=key) == []

    assert set_enabled(101, True, db_path=db_file) is True
    assert len(list_active_users(db_path=db_file, secret_key=key)) == 1

    assert delete_user(101, db_path=db_file) is True
    assert get_user(101, db_path=db_file) is None

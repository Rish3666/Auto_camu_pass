"""Unit tests for encryption, roster loading, fault isolation, and notifications."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from camu_client import SubmissionResult
from crypto_helper import decrypt_users, encrypt_users, generate_key
from notifier import format_batch_summary
from runner import load_users, run_batch


def test_encrypt_decrypt_roundtrip():
    key = generate_key()
    sample = [{"name": "Alice", "username": "a@b.com", "password": "pw", "enabled": True}]
    token = encrypt_users(sample, key)
    assert b"pw" not in token
    assert decrypt_users(token, key) == sample


def test_load_users_from_env(monkeypatch):
    payload = [{"name": "Bob", "username": "b@c.com", "password": "123", "enabled": True}]
    monkeypatch.setenv("USERS_CONFIG", json.dumps(payload))
    assert load_users() == payload


def test_load_users_from_encrypted_file(tmp_path, monkeypatch):
    monkeypatch.delenv("USERS_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    key = generate_key()
    sample = [{"name": "Cara", "username": "c@d.com", "password": "xyz", "enabled": True}]
    (tmp_path / "users.enc").write_bytes(encrypt_users(sample, key))
    monkeypatch.setenv("CAMU_SECRET_KEY", key)

    assert load_users() == sample


def test_run_batch_isolates_failures_and_skips_disabled(monkeypatch):
    users = [
        {"name": "DisabledUser", "username": "d@e.com", "password": "1", "enabled": False},
        {"name": "FailUser", "username": "f@e.com", "password": "2", "enabled": True},
        {"name": "PassUser", "username": "p@e.com", "password": "3", "enabled": True},
    ]
    monkeypatch.setenv("USERS_CONFIG", json.dumps(users))

    def fake_submit(browser, user, *, dry_run=False, **kwargs):
        if user["name"] == "FailUser":
            return SubmissionResult(name="FailUser", status="failed", message="Bad login")
        return SubmissionResult(name=user["name"], status="submitted", message="OK")

    fake_playwright = MagicMock()
    fake_browser = MagicMock()
    fake_playwright.chromium.launch.return_value = fake_browser

    with (
        patch("runner.sync_playwright") as mock_pw,
        patch("runner.submit_gatepass_for_user", side_effect=fake_submit),
        patch("runner.notify_all") as mock_notify,
    ):
        mock_pw.return_value.__enter__.return_value = fake_playwright
        results = run_batch(dry_run=False, min_jitter_sec=0, max_jitter_sec=0)

    assert [r.status for r in results] == ["skipped", "failed", "submitted"]
    mock_notify.assert_called_once()


def test_format_batch_summary():
    results = [
        SubmissionResult(name="Alice", status="submitted", message="Done"),
        SubmissionResult(name="Bob", status="failed", message="Wrong password"),
    ]
    text = format_batch_summary(results, dry_run=False)
    assert "Alice" in text
    assert "Bob" in text
    assert "Submitted: 1 | Failed: 1" in text

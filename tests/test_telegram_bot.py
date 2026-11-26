"""Unit tests for the self-service Telegram bot handlers and weekly scheduler."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import telegram_bot
from camu_client import SubmissionResult
from crypto_helper import generate_key
from db import get_user, record_submission_status, upsert_user


def _make_update(user_id: int = 42, chat_id: int = 42, text: str = ""):
    message = SimpleNamespace(
        text=text,
        reply_text=AsyncMock(),
        reply_photo=AsyncMock(),
        delete=AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        message=message,
    )


def _make_context():
    return SimpleNamespace(
        user_data={},
        bot=SimpleNamespace(
            send_message=AsyncMock(),
            send_photo=AsyncMock(),
        ),
    )


def test_validate_time_pair():
    assert telegram_bot.validate_time_pair("06:00", "20:00")[0] is True
    assert telegram_bot.validate_time_pair("20:00", "06:00")[0] is False
    assert telegram_bot.validate_time_pair("10:00", "10:00")[0] is False
    assert telegram_bot.validate_time_pair("25:00", "23:00")[0] is False
    assert telegram_bot.validate_time_pair("bad", "time")[0] is False


@pytest.mark.asyncio
async def test_register_conversation_and_status(tmp_path, monkeypatch):
    db_file = tmp_path / "bot.db"
    key = generate_key()
    monkeypatch.setattr(telegram_bot, "DB_PATH", db_file)
    monkeypatch.setenv("CAMU_SECRET_KEY", key)

    ctx = _make_context()

    assert await telegram_bot.register_start(_make_update(), ctx) == telegram_bot.ASK_NAME
    assert await telegram_bot.register_name(_make_update(text="Rish_Test"), ctx) == telegram_bot.ASK_USERNAME
    assert await telegram_bot.register_username(_make_update(text="rish_1@college.edu"), ctx) == telegram_bot.ASK_PASSWORD

    pw_update = _make_update(text="secret_pw_123")
    assert await telegram_bot.register_password(pw_update, ctx) == telegram_bot.ASK_PASS_TYPE
    pw_update.message.delete.assert_awaited_once()

    assert await telegram_bot.register_pass_type(_make_update(text="skip"), ctx) == telegram_bot.ASK_REASON
    assert await telegram_bot.register_reason(_make_update(text="skip"), ctx) == telegram_bot.ASK_TIMES

    bad_time_update = _make_update(text="22:00 08:00")
    assert await telegram_bot.register_times(bad_time_update, ctx) == telegram_bot.ASK_TIMES

    done_update = _make_update(text="skip")
    assert await telegram_bot.register_times(done_update, ctx) == telegram_bot.ConversationHandler.END

    saved = get_user(42, include_password=True, db_path=db_file, secret_key=key)
    assert saved is not None
    assert saved["name"] == "Rish_Test"
    assert saved["password"] == "secret_pw_123"
    assert saved["pass_type"] == telegram_bot.DEFAULT_PASS_TYPE
    assert saved["reason"] == telegram_bot.DEFAULT_REASON
    assert saved["out_time"] == telegram_bot.DEFAULT_OUT_TIME
    assert saved["in_time"] == telegram_bot.DEFAULT_IN_TIME

    status_update = _make_update()
    await telegram_bot.status_cmd(status_update, ctx)
    reply_text = status_update.message.reply_text.call_args.args[0]
    assert "Rish_Test" in reply_text
    assert "Week End Gate Pass" in reply_text


@pytest.mark.asyncio
async def test_manual_test_escapes_html_and_prevents_concurrent_runs(tmp_path, monkeypatch):
    db_file = tmp_path / "bot.db"
    key = generate_key()
    monkeypatch.setattr(telegram_bot, "DB_PATH", db_file)
    monkeypatch.setenv("CAMU_SECRET_KEY", key)

    upsert_user(
        telegram_user_id=7,
        chat_id=7,
        name="User <One>",
        camu_username="u@c.edu",
        camu_password="pw",
        db_path=db_file,
        secret_key=key,
    )

    shot = tmp_path / "dry.png"
    shot.write_bytes(b"fake-png")

    fake_res = SubmissionResult(
        name="User <One>",
        status="dry_run",
        message="Filled <form> with_underscores",
        screenshot_path=str(shot),
    )

    update = _make_update(user_id=7, chat_id=7)
    with patch("telegram_bot._run_single_user_sync", return_value=fake_res):
        await telegram_bot.test_cmd(update, _make_context())

    final_text = update.message.reply_text.call_args_list[-1].args[0]
    assert "<b>dry_run</b>" in final_text
    assert "&lt;form&gt;" in final_text
    update.message.reply_photo.assert_awaited_once()
    assert not shot.exists()

    telegram_bot._IN_FLIGHT_USERS.add(7)
    try:
        blocked = _make_update(user_id=7, chat_id=7)
        await telegram_bot.submit_cmd(blocked, _make_context())
        assert "already in progress" in blocked.message.reply_text.call_args.args[0]
    finally:
        telegram_bot._IN_FLIGHT_USERS.discard(7)


@pytest.mark.asyncio
async def test_weekly_job_dedups_and_inline_pause_callback(tmp_path, monkeypatch):
    db_file = tmp_path / "bot.db"
    key = generate_key()
    monkeypatch.setattr(telegram_bot, "DB_PATH", db_file)
    monkeypatch.setenv("CAMU_SECRET_KEY", key)

    friday_str, _ = telegram_bot.get_weekend_dates()

    upsert_user(telegram_user_id=1, chat_id=1, name="AlreadyDone", camu_username="a@c.edu", camu_password="1", db_path=db_file, secret_key=key)
    record_submission_status(1, status="submitted: ok", submitted_date=friday_str, db_path=db_file)

    upsert_user(telegram_user_id=2, chat_id=2, name="WillSucceed", camu_username="b@c.edu", camu_password="2", db_path=db_file, secret_key=key)

    def fake_run(user, *, dry_run):
        return SubmissionResult(name=user["name"], status="submitted", message="Done")

    ctx = _make_context()
    with (
        patch("telegram_bot._run_single_user_sync", side_effect=fake_run) as mock_run,
        patch("telegram_bot.asyncio.sleep", new=AsyncMock()),
    ):
        await telegram_bot.weekly_tuesday_job(ctx)

    assert mock_run.call_count == 1
    assert mock_run.call_args.args[0]["name"] == "WillSucceed"
    ctx.bot.send_message.assert_awaited_once()

    updated = get_user(2, db_path=db_file)
    assert updated is not None
    assert updated["last_submitted_date"] == friday_str

    cb_query = SimpleNamespace(data="action:pause", answer=AsyncMock(), edit_message_text=AsyncMock())
    cb_upd = SimpleNamespace(effective_user=SimpleNamespace(id=2), callback_query=cb_query)
    await telegram_bot.callback_action(cb_upd, _make_context())
    assert get_user(2, db_path=db_file)["enabled"] is False
    cb_query.answer.assert_awaited_once()
    cb_query.edit_message_text.assert_awaited_once()


def test_build_application_registers_thu_fri_job(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_bot, "DB_PATH", tmp_path / "boot.db")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TESTTOKEN")
    monkeypatch.setenv("CAMU_SECRET_KEY", generate_key())

    app = telegram_bot.build_application()
    assert app.job_queue is not None
    job_names = [job.name for job in app.job_queue.jobs()]
    assert "thu_fri_gatepass_batch" in job_names


def test_camu_client_json_api_payload_and_pass_resolution():
    from camu_client import build_crtlv_payload, build_utc_iso_from_ist, resolve_pass_type

    types_list = [
        {"passCd": "DAY_PASS", "code": "Day Outing"},
        {"passCd": "WEEK_END_PASS", "code": "Week End Gate Pass"},
    ]
    resolved = resolve_pass_type(types_list, "Week End Gate Pass")
    assert resolved["id"] == "WEEK_END_PASS"
    assert resolved["label"] == "Week End Gate Pass"

    # 06:00 IST = 00:30 UTC; 20:00 IST = 14:30 UTC
    assert build_utc_iso_from_ist("2026-11-27", "06:00") == "2026-11-27T00:30:00.000Z"
    assert build_utc_iso_from_ist("2026-11-29", "20:00") == "2026-11-29T14:30:00.000Z"

    payload = build_crtlv_payload(
        profile={"studentId": "stu123", "institutionId": "in456", "loggedInId": "log789", "name": "Test Student"},
        resolved_type=resolved,
        friday_date_str="2026-11-27",
        sunday_date_str="2026-11-29",
        out_time="06:00",
        in_time="20:00",
        reason="Weekend outing",
    )
    assert payload["passTy"] == "WEEK_END_PASS"
    assert payload["FrDt"] == "2026-11-27T00:00:00.000Z"
    assert payload["ToDt"] == "2026-11-29T00:00:00.000Z"
    assert payload["GateOutPass"] == "true"
    assert payload["HstleReq"] == "true"
    assert payload["timezoneOffSet"] == 330
    assert payload["PerId"] == "stu123"


@pytest.mark.asyncio
async def test_register_inline_buttons_and_update_command(tmp_path, monkeypatch):
    db_file = tmp_path / "bot.db"
    key = generate_key()
    monkeypatch.setattr(telegram_bot, "DB_PATH", db_file)
    monkeypatch.setenv("CAMU_SECRET_KEY", key)

    ctx = _make_context()
    await telegram_bot.register_start(_make_update(), ctx)
    await telegram_bot.register_name(_make_update(text="Alice"), ctx)
    await telegram_bot.register_username(_make_update(text="alice@c.edu"), ctx)
    await telegram_bot.register_password(_make_update(text="pw1"), ctx)

    # Tap inline buttons for Q4, Q5, Q6
    def _cb_update(data: str):
        msg = SimpleNamespace(reply_text=AsyncMock())
        q = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(), message=msg)
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=42),
            callback_query=q,
        )

    assert await telegram_bot.register_pass_type_cb(_cb_update("reg:pass:Week End Gate Pass"), ctx) == telegram_bot.ASK_REASON
    assert await telegram_bot.register_reason_cb(_cb_update("reg:reason:Weekend outing"), ctx) == telegram_bot.ASK_TIMES
    assert await telegram_bot.register_times_cb(_cb_update("reg:times:06:00 20:00"), ctx) == telegram_bot.ConversationHandler.END

    saved = get_user(42, db_path=db_file)
    assert saved["pass_type"] == "Week End Gate Pass"
    assert saved["reason"] == "Weekend outing"
    assert saved["out_time"] == "06:00"
    assert saved["in_time"] == "20:00"

    # Test /update command updating just the reason
    upd_ctx = _make_context()
    assert await telegram_bot.update_start(_make_update(), upd_ctx) == telegram_bot.UPD_CHOOSE
    assert await telegram_bot.update_choose_cb(_cb_update("upd:reason"), upd_ctx) == telegram_bot.UPD_VALUE
    assert await telegram_bot.update_value_text(_make_update(text="Going home"), upd_ctx) == telegram_bot.ConversationHandler.END
    assert get_user(42, db_path=db_file)["reason"] == "Going home"



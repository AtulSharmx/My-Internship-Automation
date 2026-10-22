import asyncio
from contextlib import suppress

import telegram_bot
from telegram_bot import (
    ApolloBot,
    coverage_text,
    elapsed_text,
    final_text,
    is_running_phase,
    limit_markup,
    reply_menu,
    safe_upload_name,
    status_text,
)


def test_upload_name_is_sanitized():
    assert safe_upload_name("../../company list.xlsx") == "company list.xlsx"
    assert safe_upload_name("bad<>|name.csv") == "bad___name.csv"
    assert safe_upload_name("") == "input.xlsx"


def test_status_text_escapes_company_and_shows_ledger():
    text = status_text(
        {
            "running": True,
            "phase": "browser",
            "planned": 5,
            "handled": 2,
            "current_company": "A & B <Co>",
            "verified": 1,
            "guessed": 3,
            "credits": 1,
            "ledger": {"companies": 7, "credits_used": 4},
        }
    )
    assert "A &amp; B &lt;Co&gt;" in text
    assert "2/5" in text
    assert "7 companies" in text


def test_coverage_text_counts_filled_people_and_blanks():
    assert coverage_text({"people_found": 118, "people_covered": 116}) == (
        "116/118 (98%) · 2 blank"
    )
    assert coverage_text({"people_found": 4, "people_covered": 4}) == "4/4 (100%)"
    assert coverage_text({"people_found": 0, "people_covered": 0}) == ""
    assert coverage_text({}) == ""


def test_status_and_final_text_show_email_coverage():
    data = {
        "phase": "done",
        "handled": 1,
        "people_found": 5,
        "people_covered": 4,
        "credits": 1,
    }
    assert "Email coverage:</b> 4/5 (80%) · 1 blank" in status_text(data)
    assert "Email coverage:</b> 4/5 (80%) · 1 blank" in final_text(data)


def test_final_text_reports_challenge_and_retry_hint():
    text = final_text(
        {
            "phase": "stopped",
            "status_text": "Stopped: Apollo verification/challenge screen",
            "challenge": "Security challenge did not complete",
            "handled": 2,
            "credits": 1,
        }
    )
    assert "Challenge" in text
    assert "Retry" in text
    assert "2" in text


def test_phase_detection_and_limit_buttons():
    assert is_running_phase({"running": True})
    assert is_running_phase({"phase": "browser"})
    assert not is_running_phase({"phase": "done", "running": False})
    callbacks = [
        button["callback_data"]
        for row in limit_markup()["inline_keyboard"]
        for button in row
    ]
    assert {"run:1", "run:5", "run:10", "run:25"} <= set(callbacks)


def test_elapsed_text_handles_iso_timestamps():
    assert (
        elapsed_text(
            {
                "started_at": "2026-10-21T13:00:00+00:00",
                "finished_at": "2026-10-21T13:01:05+00:00",
            }
        )
        == "1:05"
    )


def test_reply_menu_has_laptop_button():
    buttons = {
        button for row in reply_menu()["keyboard"] for button in row
    }
    assert "🖥️ Laptop" in buttons


class _FakeTelegram:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))
        return {"message_id": len(self.messages)}


class _FakeApi:
    def __init__(self, status=None):
        self._status = status or {"running": False, "phase": "idle"}
        self.reset_calls = 0
        self.close_calls = 0

    async def status(self):
        return self._status

    async def reset_desktop(self):
        self.reset_calls += 1

    async def close_desktop(self):
        self.close_calls += 1


def _bot_with_fakes(status=None):
    bot = object.__new__(ApolloBot)
    bot.tg = _FakeTelegram()
    bot.api = _FakeApi(status)
    bot.staged = {1: object()}
    bot.last_started = {1: ("old.xlsx", 5)}
    return bot


def test_start_opens_a_fresh_window_without_stale_upload_state(monkeypatch):
    bot = _bot_with_fakes()
    launched = []
    monkeypatch.setattr(
        telegram_bot, "show_desktop_window", lambda fresh=True: launched.append(fresh) or True
    )

    asyncio.run(bot.handle_start(1))

    assert bot.api.reset_calls == 1
    assert launched == [True]
    assert bot.staged == {}
    assert bot.last_started == {}
    assert bot.tg.messages == []


def test_start_while_running_does_not_reset_or_open(monkeypatch):
    bot = _bot_with_fakes({"running": True, "phase": "browser"})
    launched = []
    monkeypatch.setattr(
        telegram_bot, "show_desktop_window", lambda fresh=True: launched.append(fresh) or True
    )

    asyncio.run(bot.handle_start(1))

    assert bot.api.reset_calls == 0
    assert launched == []
    assert "Run already active" in bot.tg.messages[0][1]


def test_ready_upload_message_is_short():
    bot = _bot_with_fakes()

    asyncio.run(bot._announce_upload(1, {"status": "ready", "filename": "x.xlsx"}))

    assert bot.tg.messages == [(1, "📄 <b>File received</b>", None)]


def test_start_button_text_routes_to_start_handler():
    bot = object.__new__(ApolloBot)
    calls = []

    async def fake_start(chat_id):
        calls.append(chat_id)

    bot.handle_start = fake_start

    asyncio.run(bot._handle_text(7, "🚀 Start"))

    assert calls == [7]


def test_monitor_reports_run_started_and_closes_upload_window():
    bot = _bot_with_fakes(
        {"running": True, "phase": "browser", "run_id": "run-verify", "last_upload": None}
    )
    bot._saved_chat = lambda: "1"
    bot._upload_sig_initialized = False
    bot._upload_sig = ""
    bot.seen_runs = set()
    bot.finished_runs = set()
    bot.finalizing_runs = set()
    bot.watchers = {}
    bot.starting = set()
    bot._ensure_watcher = lambda *args, **kwargs: None

    async def exercise():
        task = asyncio.create_task(bot._monitor_activity())
        for _ in range(60):
            if bot.tg.messages:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert bot.tg.messages[0][1] == "🚀 <b>Run started</b>"
    assert bot.api.close_calls == 1
    assert "run-verify" in bot.seen_runs

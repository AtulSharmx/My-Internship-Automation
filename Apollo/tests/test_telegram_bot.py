from telegram_bot import (
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

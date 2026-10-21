"""Interactive configuration wizard for the startup discovery agent."""

from __future__ import annotations

import getpass
import re
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

from . import configure_console

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"


def read_env(path: Path = ENV_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    values = dotenv_values(path, encoding="utf-8")
    return {key: str(value) for key, value in values.items() if value is not None}


def _quote(value: str) -> str:
    if value == "":
        return '""'
    if re.search(r"[\s#\"'\\]", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def write_env(values: dict[str, str], path: Path = ENV_PATH) -> None:
    """Update keys without discarding comments or unrelated legacy keys."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    output: list[str] = []
    for line in lines:
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            output.append(f"{key}={_quote(remaining.pop(key))}")
        else:
            output.append(line)
    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.append("# Startup discovery agent")
        for key, value in remaining.items():
            output.append(f"{key}={_quote(value)}")
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _ask_bool(prompt: str, default: bool) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{prompt} [{default_text}]: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer y or n.")


def _ask_size(default: str = "1-10") -> str:
    while True:
        value = _ask("Target employee range (for example 1-10)", default)
        value = value.replace("–", "-").replace("—", "-")
        if re.fullmatch(r"\d+\s*-\s*\d+", value):
            low, high = (int(part.strip()) for part in value.split("-", 1))
            if 1 <= low <= high:
                return f"{low}-{high}"
        print("Enter an increasing range such as 1-10.")


def _ask_time(default: str = "") -> str:
    while True:
        value = _ask("Daily discovery time (24h HH:MM, blank = manual only)", default)
        if not value:
            return ""
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{hour:02d}:{minute:02d}"
        print("Use a valid 24-hour time such as 20:00 (displayed as 8:00 PM).")


def _is_blocked_source(value: str) -> bool:
    host = value.lower().strip().rstrip(".")
    return (
        host == "wellfound.com"
        or host.endswith(".wellfound.com")
        or host == "angel.co"
        or host.endswith(".angel.co")
    )


def _valid_domains(value: str) -> str:
    parts = [part.strip().lower() for part in value.replace(";", ",").split(",") if part.strip()]
    cleaned: list[str] = []
    for part in parts:
        part = part.replace("https://", "").replace("http://", "").split("/", 1)[0]
        if not re.fullmatch(r"[a-z0-9.-]+", part):
            print(f"Ignoring invalid domain: {part}")
            continue
        if _is_blocked_source(part):
            print("Wellfound/AngelList cannot be automated; use manual export/import.")
            continue
        cleaned.append(part)
    return ",".join(dict.fromkeys(cleaned))


def _valid_urls(value: str) -> str:
    parts = [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    cleaned: list[str] = []
    for part in parts:
        if not re.match(r"^https?://", part, flags=re.I):
            print(f"Ignoring URL without http(s): {part}")
            continue
        host = part.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
        if _is_blocked_source(host):
            print("Wellfound/AngelList URLs cannot be automated; use a manual export.")
            continue
        cleaned.append(part)
    return ",".join(dict.fromkeys(cleaned))


def detect_chat_id(token: str) -> str:
    """Read the latest chat id after the user sends the bot a message."""
    print("Open Telegram, send any message to your bot, then wait…")
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    deadline = time.time() + 60
    with httpx.Client(timeout=15.0) as client:
        while time.time() < deadline:
            try:
                response = client.get(url, params={"limit": 20, "timeout": 0})
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                time.sleep(2)
                continue
            if not payload.get("ok"):
                print("Telegram rejected the token. Check it and try again.")
                return ""
            for update in reversed(payload.get("result") or []):
                message = update.get("message") or update.get("edited_message") or {}
                chat = message.get("chat") or {}
                if chat.get("id") is not None:
                    return str(chat["id"])
                callback = update.get("callback_query") or {}
                callback_chat = (callback.get("message") or {}).get("chat") or {}
                if callback_chat.get("id") is not None:
                    return str(callback_chat["id"])
            time.sleep(2)
    return ""


def main() -> int:
    configure_console()
    existing = read_env()
    print("=" * 62)
    print("Wellfound Startup Discovery Agent — setup")
    print("=" * 62)
    print("This configures the local agent. It does not deploy anything or change AWS.")
    print()

    token = existing.get("TELEGRAM_BOT_TOKEN", "")
    if token and _ask_bool("A Telegram bot token is already configured. Keep it?", True):
        pass
    else:
        token = getpass.getpass("Telegram bot token (input hidden): ").strip()
        if not token:
            print("A bot token is required. Create one with @BotFather.")
            return 2

    chat_id = existing.get("TELEGRAM_CHAT_ID", "")
    if chat_id and _ask_bool("A Telegram chat id is already configured. Keep it?", True):
        pass
    elif _ask_bool("Detect the chat id automatically?", True):
        chat_id = detect_chat_id(token)
        if not chat_id:
            print("Could not detect a chat id automatically.")
    if not chat_id:
        chat_id = _ask("Telegram chat id (for example 123456789)")
    if not chat_id:
        print("A Telegram chat id is required.")
        return 2

    target = _ask_size(existing.get("STARTUP_TARGET_SIZE", "1-10"))
    enable_urls = _ask_bool(
        "Enable public URL discovery (only for sites whose terms permit it)?",
        existing.get("STARTUP_ENABLE_URL_DISCOVERY", "false").lower() in {"1", "true", "yes", "on"},
    )
    allowed = existing.get("STARTUP_ALLOWED_DOMAINS", "")
    seeds = existing.get("STARTUP_SEED_URLS", "")
    interval = existing.get("STARTUP_MIN_REQUEST_INTERVAL_SECONDS", "3")
    if enable_urls:
        while True:
            allowed = _valid_domains(
                _ask("Allow-listed domains (comma separated, no Wellfound)", allowed)
            )
            if allowed:
                break
            print("At least one domain is required when URL discovery is enabled.")
        seeds = _valid_urls(_ask("Seed URLs (comma separated, optional)", seeds))
        interval = _ask("Minimum seconds between requests to one host", interval or "3")
        try:
            if float(interval) < 1:
                interval = "1"
        except ValueError:
            interval = "3"
    daily = _ask_time(existing.get("STARTUP_DAILY_RUN_AT", ""))
    enable_browser = _ask_bool(
        "Collect 1-10 employee startups from Wellfound in your own visible "
        "browser (your sign-in, no CAPTCHA solving)?",
        existing.get("STARTUP_WELLFOUND_BROWSER", "false").lower()
        in {"1", "true", "yes", "on"},
    )
    browser_max = existing.get("STARTUP_WELLFOUND_MAX", "25")
    if enable_browser:
        browser_max = _ask("Maximum company profiles per run", browser_max or "25")

    values = {
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_CHAT_ID": chat_id,
        "STARTUP_TARGET_SIZE": target,
        "STARTUP_ENABLE_URL_DISCOVERY": "true" if enable_urls else "false",
        "STARTUP_ALLOWED_DOMAINS": allowed,
        "STARTUP_SEED_URLS": seeds,
        "STARTUP_MIN_REQUEST_INTERVAL_SECONDS": interval,
        "STARTUP_DAILY_RUN_AT": daily,
        "STARTUP_ENABLE_DATASETS": existing.get("STARTUP_ENABLE_DATASETS", "false"),
        "STARTUP_DATASET_SOURCES": existing.get("STARTUP_DATASET_SOURCES", "dbpedia"),
        "STARTUP_WELLFOUND_BROWSER": "true" if enable_browser else "false",
        "STARTUP_WELLFOUND_MAX": browser_max or "25",
        "STARTUP_BROWSER_PROFILE": existing.get("STARTUP_BROWSER_PROFILE", "browser_profile"),
        "STARTUP_DB_PATH": existing.get("STARTUP_DB_PATH", "data/startups.db"),
        "STARTUP_IMPORT_DIR": existing.get("STARTUP_IMPORT_DIR", "imports"),
        "STARTUP_EXPORT_DIR": existing.get("STARTUP_EXPORT_DIR", "exports"),
        "STARTUP_LOG_DIR": existing.get("STARTUP_LOG_DIR", "logs"),
        "STARTUP_MAX_URLS_PER_RUN": existing.get("STARTUP_MAX_URLS_PER_RUN", "20"),
    }
    write_env(values, ENV_PATH)
    for folder in ("imports", "exports", "data", "logs"):
        (ROOT / folder).mkdir(parents=True, exist_ok=True)

    print()
    print("Saved configuration to .env (secrets were not printed).")
    print("Next:")
    print("  1. Put a CSV/JSON/XLSX or saved HTML file in imports/, or send it to the bot.")
    print("     Template: templates/companies_template.csv")
    print("  2. Start the agent:  .\\.venv\\Scripts\\python.exe run.py")
    print("  3. In Telegram, tap Status or View Results.")
    if enable_browser:
        print(
            "  4. The browser collector is on: it opens a visible Chrome window "
            "with your own sign-in."
        )
    print("URL discovery remains off unless you explicitly enabled it for permitted sites.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

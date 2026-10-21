"""Telegram control panel for the local Apollo Email Finder.

The bot is intentionally a thin client over the existing local FastAPI app:
the web server owns the browser, ledger, and run state.  This process only
downloads Telegram documents, calls the local API, and sends results back.

Run it with ``pythonw.exe telegram_bot.py`` for a completely hidden launch.
The Windows startup shortcut created by ``--install-startup`` does the same
thing automatically after login.
"""

from __future__ import annotations

import asyncio
import ctypes
import html
import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import httpx

from apollo_tool.config import settings
from apollo_tool.excel_io import InputError, read_input
from desktop_window import show_desktop_window

ROOT = settings.root
API_URL = os.getenv("APOLLO_API_URL", "http://127.0.0.1:8765").strip().rstrip("/")
LOG_PATH = settings.logs_dir / "telegram_bot.log"
AUTH_FILE = settings.root / "data" / "telegram_chat_id.txt"
OFFSET_FILE = settings.root / "data" / "telegram_offset.txt"
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
ALLOWED_CHAT_ENV = "TELEGRAM_ALLOWED_CHAT_ID"
SUPPORTED_FILES = {".xlsx", ".xlsm", ".csv", ".json"}
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024

log = logging.getLogger("apollo.telegram")
_TOKEN = ""


# --------------------------------------------------------------------------- logging


def configure_logging() -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    if log.handlers:
        return
    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def redact(value: object) -> str:
    text = str(value)
    return text.replace(_TOKEN, "***") if _TOKEN else text


def safe_error(exc: BaseException) -> str:
    return redact(f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- helpers


class BotError(RuntimeError):
    pass


class TelegramError(BotError):
    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(redact(message))
        self.status_code = status_code


def escape(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def shorten(value: object, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def is_running_phase(data: dict[str, Any]) -> bool:
    return bool(data.get("running")) or data.get("phase") in {
        "starting",
        "browser",
        "writing",
        "stopping",
    }


def filename_of(raw: object) -> str:
    return Path(str(raw or "")).name


def format_number(value: object) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "0"


def _parse_time(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def elapsed_text(data: dict[str, Any]) -> str:
    started = _parse_time(data.get("started_at"))
    if started is None:
        return ""
    finished = _parse_time(data.get("finished_at"))
    if finished is None:
        finished = datetime.now(timezone.utc)
    seconds = max(0, int((finished - started).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def output_markup(path: Path | None = None) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = [
        [
            {"text": "📄 Workbook", "callback_data": "sendfile"},
            {"text": "🔁 New run", "callback_data": "start"},
        ]
    ]
    if path is None:
        rows[0][0] = {"text": "📊 Status", "callback_data": "status"}
    return {"inline_keyboard": rows}


def running_markup() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "⏹ Stop", "callback_data": "stop"},
                {"text": "🔄 Refresh", "callback_data": "status"},
            ]
        ]
    }


def retry_markup() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 Retry", "callback_data": "retry"},
                {"text": "📊 Status", "callback_data": "status"},
            ]
        ]
    }


def limit_markup() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "1 company", "callback_data": "run:1"},
                {"text": "5 companies", "callback_data": "run:5"},
                {"text": "10 companies", "callback_data": "run:10"},
            ],
            [
                {"text": "25 companies", "callback_data": "run:25"},
                {"text": "⬅️ Menu", "callback_data": "menu"},
            ],
        ]
    }


def reply_menu() -> dict[str, Any]:
    return {
        "keyboard": [
            ["🚀 Start", "📊 Status"],
            ["⏹ Stop", "📄 Latest file"],
            ["🖥️ Laptop"],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def safe_upload_name(raw: str) -> str:
    name = Path(str(raw or "input.xlsx")).name
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name or "input.xlsx"


def status_text(data: dict[str, Any]) -> str:
    running = is_running_phase(data)
    phase = shorten(data.get("phase") or "idle", 40).title()
    total = int(data.get("planned") or 0) or int(data.get("limit") or 0)
    handled = int(data.get("handled") or 0)
    current = shorten(data.get("current_company") or "", 80)
    lines: list[str] = []

    if running:
        lines.append("⏳ <b>Apollo run chal raha hai</b>")
        if total:
            lines.append(f"<b>Company:</b> {handled}/{total}")
        else:
            lines.append(f"<b>Handled:</b> {handled}")
        if current:
            lines.append(f"<b>Current:</b> {escape(current)}")
        live_elapsed = elapsed_text(data)
        if live_elapsed:
            lines.append(f"<b>Elapsed:</b> {live_elapsed}")
    else:
        lines.append("📊 <b>Apollo status</b>")
        lines.append(f"<b>Phase:</b> {escape(phase)}")

    lines.extend(
        [
            f"<b>Verified:</b> {format_number(data.get('verified'))}  "
            f"<b>Guessed:</b> {format_number(data.get('guessed'))}",
            f"<b>Skipped:</b> {format_number(data.get('skipped'))}  "
            f"<b>Errors:</b> {format_number(data.get('errors'))}",
            f"<b>Credits this run:</b> {format_number(data.get('credits'))}",
        ]
    )

    if data.get("challenge"):
        lines.append("\n⚠️ Apollo challenge detected — Chrome laptop par open hai.")
    elif data.get("login_required"):
        lines.append("\n🔐 Apollo login required — Chrome laptop par open hai.")

    if data.get("last_error"):
        lines.append(f"\n<b>Last error:</b> {escape(shorten(data.get('last_error'), 260))}")

    logs = data.get("logs") or []
    if logs:
        lines.append(f"<b>Update:</b> {escape(shorten(logs[-1], 170))}")

    output = data.get("output_path")
    if output and Path(str(output)).exists():
        lines.append(f"\n<b>Latest workbook:</b> {escape(filename_of(output))}")

    ledger = data.get("ledger") or {}
    if ledger:
        lines.append(
            f"\n<b>Ledger:</b> {format_number(ledger.get('companies'))} companies • "
            f"{format_number(ledger.get('credits_used'))} credits used"
        )
    return "\n".join(lines)


def final_text(data: dict[str, Any], had_challenge: bool = False) -> str:
    phase = str(data.get("phase") or "")
    status = str(data.get("status_text") or "")
    challenge = bool(data.get("challenge")) or had_challenge
    login = bool(data.get("login_required"))

    if challenge:
        title = "⚠️ <b>Run challenge par ruk gaya</b>"
        note = "Chrome laptop par open kar diya gaya hai. Challenge solve karke <b>🔄 Retry</b> dabao."
    elif login:
        title = "🔐 <b>Apollo login chahiye</b>"
        note = "Chrome laptop par open hai. Login karke <b>🔄 Retry</b> dabao."
    elif phase == "done":
        title = "✅ <b>Run complete</b>"
        note = "Workbook neeche bheji gayi hai."
    elif phase == "stopped":
        title = "⏹ <b>Run stopped</b>"
        note = "Partial workbook neeche bheji gayi hai."
    elif phase in {"error", "failed"}:
        title = "⚠️ <b>Run error</b>"
        note = "Jo companies complete hui hain, unki workbook neeche hai."
    else:
        title = "⏹ <b>Run khatam</b>"
        note = shorten(status, 220)

    lines = [
        title,
        note,
        "",
        f"<b>Companies handled:</b> {format_number(data.get('handled'))}",
        f"<b>Verified:</b> {format_number(data.get('verified'))}  "
        f"<b>Guessed:</b> {format_number(data.get('guessed'))}",
        f"<b>Skipped:</b> {format_number(data.get('skipped'))}  "
        f"<b>Errors:</b> {format_number(data.get('errors'))}",
        f"<b>Credits used:</b> {format_number(data.get('credits'))}",
    ]
    if data.get("last_error"):
        lines.append(f"<b>Error:</b> {escape(shorten(data.get('last_error'), 260))}")
    output = data.get("output_path")
    if output and Path(str(output)).exists():
        lines.append(f"<b>Workbook:</b> {escape(filename_of(output))}")
    return "\n".join(lines)


def _mime_for(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _write_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    tmp.replace(path)


def load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def save_offset(offset: int) -> None:
    try:
        _write_atomic(OFFSET_FILE, str(offset))
    except OSError as exc:
        log.warning("Could not save Telegram offset: %s", safe_error(exc))


# --------------------------------------------------------------------------- local API


class ServerProcess:
    """Find or start the hidden local FastAPI server."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._log_handle = None
        self._lock = asyncio.Lock()

    async def healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.5) as client:
                response = await client.get(f"{API_URL}/api/health")
                payload = response.json()
                return response.status_code == 200 and payload.get("ok") is True
        except (httpx.HTTPError, ValueError):
            return False

    def _python(self) -> Path:
        candidate = Path(sys.executable).with_name("python.exe")
        return candidate if candidate.exists() else Path(sys.executable)

    def _tail(self, limit: int = 900) -> str:
        try:
            text = (settings.logs_dir / "telegram_server.log").read_text(
                encoding="utf-8", errors="ignore"
            )
        except OSError:
            return ""
        return text[-limit:].strip()

    async def ensure(self) -> None:
        if await self.healthy():
            return
        async with self._lock:
            if await self.healthy():
                return
            if self._proc is None or self._proc.poll() is not None:
                settings.logs_dir.mkdir(parents=True, exist_ok=True)
                settings.logs_dir.joinpath("telegram_server.log").touch(exist_ok=True)
                if self._log_handle is None:
                    self._log_handle = open(  # noqa: SIM115
                        settings.logs_dir / "telegram_server.log",
                        "ab",
                        buffering=0,
                    )
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                cmd = [
                    str(self._python()),
                    str(ROOT / "run_ui.py"),
                    "--no-browser",
                    "--port",
                    "8765",
                ]
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=self._log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
                log.info("Started local Apollo server (pid=%s)", self._proc.pid)
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if await self.healthy():
                    return
                if self._proc is not None and self._proc.poll() is not None:
                    break
                await asyncio.sleep(0.5)
        detail = self._tail()
        message = "Local Apollo server start nahi hua."
        if detail:
            message += "\n\n" + detail
        raise BotError(message)


class ApiClient:
    def __init__(self, server: ServerProcess) -> None:
        self.server = server
        self.http = httpx.AsyncClient(base_url=API_URL, timeout=25.0)

    async def close(self) -> None:
        await self.http.aclose()

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
                detail = str(payload.get("detail") or "")
            except ValueError:
                detail = response.text[:400]
            raise BotError(detail or f"Local server error {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise BotError("Local server ne invalid response diya.") from exc

    async def request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        await self.server.ensure()
        try:
            response = await self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise BotError(
                f"Local server se connection nahi bana ({type(exc).__name__})."
            ) from exc
        return self._decode(response)

    async def upload(
        self, name: str, data: bytes, source: str = "telegram"
    ) -> dict[str, Any]:
        await self.server.ensure()
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            response = await self.http.post(
                "/api/upload",
                data={"source": source},
                files={"file": (name, data, mime)},
                timeout=120.0,
            )
        except httpx.HTTPError as exc:
            raise BotError(
                f"Local app tak upload nahi pahuncha ({type(exc).__name__})."
            ) from exc
        return self._decode(response)

    async def start(self, path: Path, limit: int) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/api/start",
            json={
                "path": str(path),
                "limit": limit,
                "dry_run": False,
                "hide_browser": True,
            },
        )

    async def status(self) -> dict[str, Any]:
        return await self.request("GET", "/api/status")

    async def stop(self) -> dict[str, Any]:
        return await self.request("POST", "/api/stop")


# --------------------------------------------------------------------------- Telegram API


class TelegramClient:
    def __init__(self, token: str) -> None:
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(65.0, connect=15.0))

    async def close(self) -> None:
        await self.http.aclose()

    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        last_error: Exception | None = None
        response: httpx.Response | None = None
        # A short network blip must not kill a live run watcher.  Do not retry
        # file uploads because their stream has already been consumed.
        for attempt in range(3):
            try:
                if files is None:
                    response = await self.http.post(
                        f"{self.base}/{method}",
                        data=payload or {},
                        timeout=timeout,
                    )
                else:
                    response = await self.http.post(
                        f"{self.base}/{method}",
                        data=payload or {},
                        files=files,
                        timeout=timeout,
                    )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                if files is not None or attempt >= 2:
                    break
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            if files is None and response.status_code >= 500 and attempt < 2:
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            break
        if last_error is not None or response is None:
            raise TelegramError(
                f"Telegram network error ({type(last_error).__name__})"
            ) from last_error

        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramError(
                f"Telegram invalid response ({response.status_code})",
                response.status_code,
            ) from exc
        if not data.get("ok"):
            raise TelegramError(
                str(data.get("description") or f"Telegram error {response.status_code}"),
                response.status_code,
            )
        return data.get("result")

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def set_my_commands(self) -> None:
        await self.call(
            "setMyCommands",
            {
                "commands": json.dumps(
                    [
                        {"command": "start", "description": "Welcome / main menu"},
                        {"command": "status", "description": "Run status"},
                        {"command": "stop", "description": "Stop after current company"},
                        {"command": "latest", "description": "Send latest workbook"},
                        {"command": "desktop", "description": "Open laptop popup"},
                        {"command": "help", "description": "Help"},
                    ]
                )
            },
        )

    async def get_updates(self, offset: int) -> list[dict[str, Any]]:
        result = await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": 30,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
            timeout=45.0,
        )
        return result if isinstance(result, list) else []

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        return await self.call("sendMessage", payload)

    async def edit_message(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        try:
            await self.call("editMessageText", payload)
        except TelegramError as exc:
            if "message is not modified" in str(exc).casefold():
                return
            raise

    async def answer_callback(
        self, callback_id: str, text: str = ""
    ) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        try:
            await self.call("answerCallbackQuery", payload)
        except TelegramError as exc:
            if "query is too old" not in str(exc).casefold():
                raise

    async def send_document(
        self, chat_id: int | str, path: Path, caption: str = ""
    ) -> None:
        if not path.exists() or not path.is_file():
            raise BotError("Workbook file nahi mili.")
        if path.stat().st_size > TELEGRAM_UPLOAD_LIMIT:
            raise BotError("Workbook 50 MB se badi hai; Telegram par send nahi ho sakti.")
        with path.open("rb") as handle:
            await self.call(
                "sendDocument",
                {
                    "chat_id": chat_id,
                    "caption": caption[:1024],
                    "parse_mode": "HTML",
                },
                files={"document": (path.name, handle, _mime_for(path))},
                timeout=120.0,
            )

    async def get_file(self, file_id: str) -> str:
        result = await self.call("getFile", {"file_id": file_id})
        file_path = str((result or {}).get("file_path") or "")
        if not file_path:
            raise TelegramError("Telegram ne file path nahi diya.")
        return file_path

    async def download(self, file_path: str) -> bytes:
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        try:
            response = await self.http.get(url, timeout=120.0)
        except httpx.HTTPError as exc:
            raise TelegramError(
                f"File download failed ({type(exc).__name__})"
            ) from exc
        if response.status_code != 200:
            raise TelegramError(
                f"File download failed ({response.status_code})",
                response.status_code,
            )
        return response.content


# --------------------------------------------------------------------------- bot


@dataclass
class StagedFile:
    path: Path
    display_name: str
    rows: int
    domains: int
    sheet: str
    columns: list[str]
    companies: list[str]

    @property
    def summary(self) -> str:
        return f"{self.rows} rows • {self.domains} unique companies"


class ApolloBot:
    def __init__(self, token: str) -> None:
        self.tg = TelegramClient(token)
        self.server = ServerProcess()
        self.api = ApiClient(self.server)
        self.staged: dict[int, StagedFile] = {}
        self.last_started: dict[int, tuple[Path, int]] = {}
        self.watchers: dict[int, asyncio.Task[None]] = {}
        self.denied: set[int] = set()
        self.starting: set[int] = set()
        self.offset = load_offset()
        self._monitor_task: asyncio.Task[None] | None = None
        self._desktop_task: asyncio.Task[None] | None = None
        self._desktop_last_attempt = 0.0
        self._desktop_runs: set[str] = set()
        self._upload_sig_initialized = False
        self._upload_sig = ""
        self.seen_runs: set[str] = set()
        self.finished_runs: set[str] = set()
        self.finalizing_runs: set[str] = set()

    async def close(self) -> None:
        for task in list(self.watchers.values()):
            task.cancel()
        if self._monitor_task is not None:
            self._monitor_task.cancel()
        await self.api.close()
        await self.tg.close()

    # ------------------------------------------------------------- authorization
    def _saved_chat(self) -> str:
        expected = os.getenv(ALLOWED_CHAT_ENV, "").strip()
        if expected:
            return expected
        try:
            return AUTH_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def authorized(self, chat_id: int) -> bool:
        expected = self._saved_chat()
        if expected:
            return str(chat_id) == expected
        try:
            _write_atomic(AUTH_FILE, str(chat_id))
            log.info("Paired Telegram chat (first user)")
            return True
        except OSError as exc:
            log.error("Could not save Telegram chat pairing: %s", safe_error(exc))
            return False

    async def deny(self, chat_id: int, callback_id: str = "") -> None:
        if callback_id:
            try:
                await self.tg.answer_callback(callback_id, "Not authorized")
            except TelegramError:
                pass
        if chat_id in self.denied:
            return
        self.denied.add(chat_id)
        try:
            await self.tg.send_message(
                chat_id,
                "🔒 Yeh bot private hai. Sirf is laptop ke owner ke Telegram chat se chalta hai.",
            )
        except TelegramError:
            pass

    def _check_auth(self, chat_id: int) -> bool:
        return self.authorized(chat_id)

    # ------------------------------------------------------------- Telegram output
    async def welcome(self, chat_id: int) -> None:
        text = (
            "👋 <b>Apollo Email Finder</b>\n\n"
            "Apollo web se company emails nikalne ka kaam ab <b>Telegram se</b> hoga. "
            "Laptop par CMD ya browser kholne ki zaroorat nahi.\n\n"
            "<b>Kaise chalta hai</b>\n"
            "1. 📎 se company file bhejo (XLSX / CSV / JSON)\n"
            "2. 🚀 <b>Start</b> dabao\n"
            "3. Limit choose karo\n"
            "4. Finished Excel file yahin milegi\n\n"
            "Laptop par ek chhota live popup bhi khulega — "
            "wahan file upload karo ya progress dekho. "
            "<b>🖥️ Laptop</b> se use dobara khol sakte ho.\n\n"
            "Har email reveal = <b>1 Apollo credit</b>. "
            "Pehle process ho chuki companies dobara nahi chalti."
        )
        await self.tg.send_message(chat_id, text, reply_markup=reply_menu())

    async def help(self, chat_id: int) -> None:
        text = (
            "❓ <b>Madad</b>\n\n"
            "• <b>🚀 Start</b> — latest upload ka preview aur credit limit dikhata hai.\n"
            "• <b>📊 Status</b> — current run, credits aur latest file.\n"
            "• <b>⏹ Stop</b> — current company ke baad run rokta hai.\n"
            "• <b>📄 Latest file</b> — last workbook dobara bhejta hai.\n"
            "• <b>🖥️ Laptop</b> — chhota upload/live popup open karta hai.\n\n"
            "Company file 📎 → File se bhejo. Photo nahi, document bhejo.\n"
            "Run ke waqt laptop on rehna chahiye. Challenge aaye to Chrome "
            "laptop par khud open hoga; solve karke Retry dabao.\n\n"
            "Credits sirf email reveal par lagte hain; free search 0 credits."
        )
        await self.tg.send_message(chat_id, text, reply_markup=reply_menu())

    async def send_status(self, chat_id: int) -> dict[str, Any] | None:
        try:
            data = await self.api.status()
        except BotError as exc:
            await self.tg.send_message(
                chat_id,
                f"⚠️ Status nahi mila.\n\n{escape(shorten(exc, 500))}",
            )
            return None
        markup = running_markup() if is_running_phase(data) else output_markup(
            Path(str(data["output_path"])) if data.get("output_path") else None
        )
        message = await self.tg.send_message(chat_id, status_text(data), reply_markup=markup)
        if is_running_phase(data):
            self._ensure_watcher(chat_id, int(message["message_id"]))
        return data

    # ------------------------------------------------------------- desktop + activity
    async def _show_desktop(self) -> None:
        try:
            opened = await asyncio.to_thread(show_desktop_window)
            if not opened:
                log.warning("Desktop window did not open (Edge/Chrome not found)")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not open desktop window: %s", safe_error(exc))

    def _show_desktop_soon(self, *, force: bool = False, run_id: str = "") -> None:
        """Open the popup at most once per action/run.

        The popup is deliberately not tied to startup, uploads or previews.
        Those events used to reopen it over and over.  It now opens only when
        a run actually starts, when activity from another window is detected,
        or when the user taps the Laptop button.
        """
        if run_id:
            if run_id in self._desktop_runs:
                return
            self._desktop_runs.add(run_id)
        elif not force:
            now = time.monotonic()
            if now - self._desktop_last_attempt < 12:
                return
        if self._desktop_task is not None and not self._desktop_task.done():
            return
        now = time.monotonic()
        if not force and now - self._desktop_last_attempt < 12:
            return
        self._desktop_last_attempt = now
        self._desktop_task = asyncio.create_task(self._show_desktop())

    @staticmethod
    def _upload_signature(upload: dict[str, Any] | None) -> str:
        if not upload:
            return ""
        return "|".join(
            str(upload.get(key) or "")
            for key in ("uploaded_at", "status", "filename", "size", "source")
        )

    async def _monitor_activity(self) -> None:
        """Mirror uploads and runs started outside Telegram into the chat."""
        while True:
            try:
                await asyncio.sleep(1.4)
                saved = self._saved_chat()
                if not saved:
                    continue
                try:
                    chat_id = int(saved)
                except ValueError:
                    continue
                data = await self.api.status()
                upload = data.get("last_upload")
                signature = self._upload_signature(upload)
                if not self._upload_sig_initialized:
                    self._upload_sig_initialized = True
                    self._upload_sig = signature
                elif signature and signature != self._upload_sig:
                    self._upload_sig = signature
                    if upload and upload.get("source") != "telegram":
                        await self._announce_upload(chat_id, upload)

                run_id = data.get("run_id")
                if is_running_phase(data):
                    if run_id:
                        self.seen_runs.add(run_id)
                    if chat_id not in self.watchers:
                        if chat_id in self.starting:
                            continue
                        message = await self.tg.send_message(
                            chat_id,
                            status_text(data),
                            reply_markup=running_markup(),
                        )
                        self._ensure_watcher(chat_id, int(message["message_id"]))
                        if run_id:
                            self._show_desktop_soon(run_id=run_id)
                elif (
                    run_id
                    and str(data.get("phase") or "") in {"done", "stopped", "error", "failed"}
                    and run_id in self.seen_runs
                    and run_id not in self.finished_runs
                    and run_id not in self.finalizing_runs
                    and chat_id not in self.watchers
                    and chat_id not in self.starting
                ):
                    await self._recover_finished_run(chat_id, run_id, data)
            except asyncio.CancelledError:
                raise
            except (TelegramError, BotError) as exc:
                log.warning("Activity monitor: %s", safe_error(exc))
            except Exception:  # noqa: BLE001
                log.exception("Activity monitor failed")

    async def _announce_upload(
        self, chat_id: int, upload: dict[str, Any]
    ) -> None:
        status = str(upload.get("status") or "")
        name = escape(upload.get("filename") or "file")
        if status == "uploading":
            await self.tg.send_message(
                chat_id,
                f"⏳ <b>Laptop se file upload ho rahi hai</b>\n\n📄 {name}\n"
                "Workbook read hone ke baad preview aa jayega.",
            )
        elif status == "ready":
            rows = format_number(upload.get("rows"))
            domains = format_number(upload.get("unique_domains"))
            await self.tg.send_message(
                chat_id,
                f"✅ <b>File laptop se upload ho gayi</b>\n\n"
                f"📄 {name}\n{rows} rows • {domains} unique companies\n\n"
                "Laptop popup me limit choose karke <b>Start</b> dabao.",
            )
        elif status == "error":
            await self.tg.send_message(
                chat_id,
                f"⚠️ <b>Laptop upload failed</b>\n\n📄 {name}\n"
                f"{escape(shorten(upload.get('error'), 300))}",
            )

    async def _recover_finished_run(
        self, chat_id: int, run_id: str, data: dict[str, Any]
    ) -> None:
        """Finish a run whose live watcher died (for example, network hiccup)."""
        self.finalizing_runs.add(run_id)
        try:
            await self.tg.send_message(
                chat_id,
                final_text(data, bool(data.get("challenge"))),
                reply_markup=self._final_markup(data),
            )
            self.finished_runs.add(run_id)
            await self._send_output_for_state(chat_id, data)
        except (TelegramError, BotError) as exc:
            log.warning("Could not recover finished run: %s", safe_error(exc))
        finally:
            self.finalizing_runs.discard(run_id)

    async def _startup_companion(self) -> None:
        """Bring up the hidden local server without opening the popup.

        The popup is intentionally user/run-driven.  Opening it on every bot
        restart made it appear again and again after login.
        """
        try:
            await self.server.ensure()
        except BotError as exc:
            log.warning("Local server was not ready: %s", safe_error(exc))

    # ------------------------------------------------------------- staging files
    async def _stage_path(self, path: Path, display_name: str) -> StagedFile:
        try:
            workbook = await asyncio.to_thread(read_input, path)
        except (InputError, OSError) as exc:
            raise BotError(str(exc)) from exc
        domains = {row.domain for row in workbook.rows if row.domain}
        if not domains:
            raise BotError(
                "File me koi valid website/domain nahi mila. "
                "Company Name aur Official Website columns check karo."
            )
        companies = []
        for row in workbook.rows:
            label = row.company_name or row.domain
            if label and label not in companies:
                companies.append(label)
        return StagedFile(
            path=path.resolve(),
            display_name=safe_upload_name(display_name or path.name),
            rows=len(workbook.rows),
            domains=len(domains),
            sheet=workbook.sheet_name,
            columns=list(workbook.headers),
            companies=companies[:8],
        )

    def _staged_from_upload(self, payload: dict[str, Any]) -> StagedFile:
        raw_path = str(payload.get("path") or "")
        path = Path(raw_path)
        if not raw_path or not path.exists() or not path.is_file():
            raise BotError("Local app ne uploaded file ka path nahi diya.")
        domains = int(payload.get("unique_domains") or 0)
        if domains <= 0:
            raise BotError(
                "File me koi valid website/domain nahi mila. "
                "Company Name aur Official Website columns check karo."
            )
        companies: list[str] = []
        for item in payload.get("preview") or []:
            label = str(item.get("company_name") or item.get("domain") or "").strip()
            if label and label not in companies:
                companies.append(label)
        return StagedFile(
            path=path.resolve(),
            display_name=safe_upload_name(
                str(payload.get("filename") or path.name)
            ),
            rows=int(payload.get("rows") or 0),
            domains=domains,
            sheet=str(payload.get("sheet") or "Sheet"),
            columns=[str(column) for column in payload.get("columns") or []],
            companies=companies[:8],
        )

    def _latest_upload_path(self) -> Path | None:
        try:
            candidates = [
                path
                for path in settings.uploads_dir.iterdir()
                if path.is_file() and path.suffix.lower() in SUPPORTED_FILES
            ]
        except OSError:
            return None
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    async def stage_latest(self, chat_id: int) -> StagedFile | None:
        path = self._latest_upload_path()
        if path is None:
            return None
        try:
            staged = await self._stage_path(path, path.name)
        except BotError as exc:
            log.warning("Latest upload could not be staged: %s", safe_error(exc))
            return None
        self.staged[chat_id] = staged
        return staged

    async def handle_document(
        self, chat_id: int, document: dict[str, Any]
    ) -> None:
        raw_name = str(document.get("file_name") or "input.xlsx")
        name = safe_upload_name(raw_name)
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_FILES:
            await self.tg.send_message(
                chat_id,
                "❌ Yeh file type support nahi hai.\n"
                "`.xlsx`, `.xlsm`, `.csv` ya `.json` bhejo.",
            )
            return
        size = int(document.get("file_size") or 0)
        if size and size > TELEGRAM_DOWNLOAD_LIMIT:
            await self.tg.send_message(
                chat_id,
                "❌ File 20 MB se badi hai; Telegram bot ise download nahi kar sakta.",
            )
            return

        await self.tg.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        file_id = str(document.get("file_id") or "")
        if not file_id:
            await self.tg.send_message(chat_id, "❌ Telegram ne file ID nahi di.")
            return

        notice = await self.tg.send_message(
            chat_id,
            f"📥 <b>File received</b>\n\n📄 {escape(name)}\n"
            f"{format_number(size)} bytes • downloading…",
        )
        notice_id = int(notice["message_id"])
        try:
            remote_path = await self.tg.get_file(file_id)
            data = await self.tg.download(remote_path)
            if not data:
                raise BotError("Downloaded file empty hai.")
            if len(data) > TELEGRAM_DOWNLOAD_LIMIT:
                raise BotError("File 20 MB se badi hai.")
        except (TelegramError, BotError) as exc:
            await self.tg.edit_message(
                chat_id,
                notice_id,
                f"❌ <b>File download nahi hui</b>\n\n📄 {escape(name)}\n"
                f"{escape(shorten(exc, 400))}",
            )
            return

        await self.tg.edit_message(
            chat_id,
            notice_id,
            f"⏳ <b>Uploading file</b>\n\n📄 {escape(name)}\n"
            f"{format_number(len(data))} bytes • reading workbook…",
        )
        try:
            payload = await self.api.upload(name, data, source="telegram")
            staged = self._staged_from_upload(payload)
        except BotError as exc:
            await self.tg.edit_message(
                chat_id,
                notice_id,
                f"❌ <b>File parse nahi hui</b>\n\n📄 {escape(name)}\n"
                f"{escape(shorten(exc, 500))}",
            )
            return

        self.staged[chat_id] = staged
        await self.send_preview(chat_id, staged, message_id=notice_id)

    async def send_preview(
        self,
        chat_id: int,
        staged: StagedFile,
        message_id: int | None = None,
    ) -> None:
        companies = ", ".join(escape(name) for name in staged.companies[:4])
        if len(staged.companies) > 4:
            companies += ", …"
        columns = ", ".join(escape(str(col)) for col in staged.columns[:7])
        if len(staged.columns) > 7:
            columns += ", …"
        prefix = "✅ <b>File uploaded</b>\n\n" if message_id is not None else ""
        text = (
            f"{prefix}📄 <b>{escape(staged.display_name)}</b>\n"
            f"{staged.summary}\n"
            f"<b>Sheet:</b> {escape(staged.sheet)}\n"
            f"<b>Columns:</b> {columns}\n"
        )
        if companies:
            text += f"<b>Starting with:</b> {companies}\n"
        text += (
            "\nKitne <b>new</b> companies process karun?\n"
            "Har email reveal = 1 credit. Pehle processed skip honge."
        )
        if message_id is not None:
            try:
                await self.tg.edit_message(
                    chat_id, message_id, text, reply_markup=limit_markup()
                )
                return
            except TelegramError as exc:
                log.warning("Could not edit upload preview: %s", safe_error(exc))
        await self.tg.send_message(chat_id, text, reply_markup=limit_markup())

    async def handle_start(self, chat_id: int) -> None:
        if is_running_phase(await self._safe_status()):
            await self.send_status(chat_id)
            return
        staged = self.staged.get(chat_id)
        if staged is None:
            staged = await self.stage_latest(chat_id)
        if staged is None:
            await self.tg.send_message(
                chat_id,
                "📂 Abhi koi company file nahi mili.\n\n"
                "📎 → <b>File</b> se `.xlsx`, `.xlsm`, `.csv` ya `.json` bhejo, "
                "phir <b>🚀 Start</b> dabao.",
                reply_markup=reply_menu(),
            )
            return
        await self.send_preview(chat_id, staged)

    async def _safe_status(self) -> dict[str, Any]:
        try:
            return await self.api.status()
        except BotError:
            return {}

    # ------------------------------------------------------------- runs
    async def handle_run(self, chat_id: int, limit: int) -> None:
        limit = max(1, min(int(limit), 100))
        staged = self.staged.get(chat_id)
        if staged is None:
            await self.handle_start(chat_id)
            return
        current = await self._safe_status()
        if is_running_phase(current):
            await self.send_status(chat_id)
            return
        self.starting.add(chat_id)
        try:
            try:
                started = await self.api.start(staged.path, limit)
            except BotError as exc:
                await self.tg.send_message(
                    chat_id,
                    f"⚠️ Run start nahi hua.\n\n{escape(shorten(exc, 600))}",
                )
                return
            run_id = str(started.get("run_id") or "")
            if run_id:
                self.seen_runs.add(run_id)
            # Open the laptop companion only after the run really started.
            self._show_desktop_soon(force=True, run_id=run_id)

            self.last_started[chat_id] = (staged.path, limit)
            text = (
                "🚀 <b>Run start ho gaya</b>\n\n"
                f"<b>Limit:</b> {limit} new companies\n"
                f"<b>File:</b> {escape(staged.display_name)}\n\n"
                "Chrome background me khulega. Telegram aur laptop popup "
                "dono par live status update hoga."
            )
            message = await self.tg.send_message(
                chat_id, text, reply_markup=running_markup()
            )
            self._ensure_watcher(chat_id, int(message["message_id"]))
        finally:
            self.starting.discard(chat_id)

    async def handle_retry(self, chat_id: int) -> None:
        item = self.last_started.get(chat_id)
        if item is None:
            await self.handle_start(chat_id)
            return
        path, limit = item
        if not path.exists():
            await self.tg.send_message(
                chat_id,
                "⚠️ Purani file nahi mili. Naya file bhejo, phir Start dabao.",
            )
            return
        try:
            self.staged[chat_id] = await self._stage_path(path, path.name)
        except BotError as exc:
            await self.tg.send_message(chat_id, f"⚠️ {escape(shorten(exc, 500))}")
            return
        await self.handle_run(chat_id, limit)

    def _ensure_watcher(self, chat_id: int, message_id: int) -> None:
        old = self.watchers.get(chat_id)
        if old is not None and not old.done():
            return
        task = asyncio.create_task(self._watch_run(chat_id, message_id))
        self.watchers[chat_id] = task
        task.add_done_callback(lambda _task: self.watchers.pop(chat_id, None))

    async def _watch_run(self, chat_id: int, message_id: int) -> None:
        last = ""
        final: dict[str, Any] = {}
        challenged = False
        try:
            await asyncio.sleep(1.2)
            while True:
                final = await self.api.status()
                if final.get("challenge"):
                    challenged = True
                if not is_running_phase(final):
                    break
                text = status_text(final)
                if text != last:
                    try:
                        await self.tg.edit_message(
                            chat_id, message_id, text, reply_markup=running_markup()
                        )
                        last = text
                    except TelegramError as exc:
                        if exc.status_code != 429:
                            raise
                        log.info("Telegram rate limit; slowing live edits")
                        await asyncio.sleep(4.0)
                        continue
                await asyncio.sleep(1.5)

            text = final_text(final, challenged)
            await self.tg.edit_message(
                chat_id, message_id, text, reply_markup=self._final_markup(final)
            )
            run_id = str(final.get("run_id") or "")
            if run_id:
                self.finished_runs.add(run_id)
            await self._send_output_for_state(chat_id, final)
        except asyncio.CancelledError:
            raise
        except TelegramError as exc:
            log.warning("Telegram watcher failed: %s", safe_error(exc))
        except BotError as exc:
            log.warning("Run watcher API failed: %s", safe_error(exc))
            try:
                await self.tg.send_message(
                    chat_id,
                    f"⚠️ Run status update ruk gaya.\n\n{escape(shorten(exc, 500))}",
                )
            except TelegramError:
                pass
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected watcher error")
            try:
                await self.tg.send_message(
                    chat_id,
                    f"⚠️ Unexpected error: {escape(shorten(safe_error(exc), 400))}",
                )
            except TelegramError:
                pass

    def _final_markup(self, data: dict[str, Any]) -> dict[str, Any]:
        if data.get("challenge") or data.get("login_required") or data.get("phase") in {
            "error",
            "failed",
        }:
            return retry_markup()
        output = data.get("output_path")
        return output_markup(Path(str(output)) if output and Path(str(output)).exists() else None)

    async def _send_output_for_state(
        self, chat_id: int, data: dict[str, Any]
    ) -> None:
        raw = str(data.get("output_path") or "")
        if not raw:
            return
        path = Path(raw)
        if not path.exists():
            return
        caption = (
            f"📊 <b>Apollo result</b>\n"
            f"Verified: {format_number(data.get('verified'))} • "
            f"Guessed: {format_number(data.get('guessed'))} • "
            f"Credits: {format_number(data.get('credits'))}"
        )
        try:
            await self.tg.send_document(chat_id, path, caption=caption)
        except (BotError, TelegramError) as exc:
            log.warning("Could not send output document: %s", safe_error(exc))
            await self.tg.send_message(
                chat_id,
                f"⚠️ Workbook send nahi hui: {escape(shorten(exc, 400))}",
            )

    async def stop(self, chat_id: int) -> None:
        data = await self._safe_status()
        if not is_running_phase(data):
            await self.tg.send_message(
                chat_id, "⏹ Koi run active nahi hai.", reply_markup=reply_menu()
            )
            return
        try:
            await self.api.stop()
        except BotError as exc:
            await self.tg.send_message(
                chat_id, f"⚠️ Stop nahi hua.\n\n{escape(shorten(exc, 500))}"
            )
            return
        await self.tg.send_message(
            chat_id,
            "⏹ <b>Stop request bheja</b>\nCurrent company complete hone ke baad rukega.",
            reply_markup=running_markup(),
        )

    # ------------------------------------------------------------- latest output
    def _latest_output_path(self, data: dict[str, Any]) -> Path | None:
        raw = str(data.get("output_path") or "")
        if raw:
            path = Path(raw)
            if path.exists() and path.is_file():
                return path
        try:
            files = [
                path
                for path in settings.exports_dir.iterdir()
                if path.is_file() and path.suffix.lower() == ".xlsx"
            ]
        except OSError:
            return None
        if not files:
            return None
        real = [path for path in files if "dry_run_sample" not in path.name]
        return max(real or files, key=lambda path: path.stat().st_mtime)

    async def send_latest(self, chat_id: int) -> None:
        data = await self._safe_status()
        path = self._latest_output_path(data)
        if path is None:
            await self.tg.send_message(
                chat_id,
                "📄 Abhi koi workbook export nahi hui. Pehle ek run complete karo.",
            )
            return
        try:
            await self.tg.send_document(
                chat_id,
                path,
                caption=f"📄 <b>{escape(path.name)}</b>",
            )
        except (BotError, TelegramError) as exc:
            await self.tg.send_message(
                chat_id,
                f"⚠️ Workbook send nahi hui.\n\n{escape(shorten(exc, 500))}",
            )

    # ------------------------------------------------------------- update dispatch
    async def _handle_text(self, chat_id: int, text: str) -> None:
        value = (text or "").strip()
        folded = value.casefold()
        if folded in {"start", "/run", "🚀 start"}:
            await self.handle_start(chat_id)
        elif folded in {"/start", "/menu", "menu"}:
            await self.welcome(chat_id)
        elif folded in {"/status", "status", "📊 status"} or folded.startswith("📊"):
            await self.send_status(chat_id)
        elif folded in {"/stop", "stop", "⏹ stop"} or folded.startswith("⏹"):
            await self.stop(chat_id)
        elif folded in {"/latest", "latest", "📄 latest file"} or folded.startswith("📄"):
            await self.send_latest(chat_id)
        elif (
            folded in {"/desktop", "desktop", "🖥️ laptop"}
            or "laptop" in folded
        ):
            await self.open_desktop(chat_id)
        elif folded in {"/help", "help", "❓ help"} or folded.startswith("❓"):
            await self.help(chat_id)
        else:
            await self.welcome(chat_id)

    async def open_desktop(self, chat_id: int) -> None:
        try:
            self._desktop_last_attempt = time.monotonic()
            opened = await asyncio.to_thread(show_desktop_window)
            if not opened:
                raise BotError("Edge/Chrome app window launch nahi hua.")
            await self.tg.send_message(
                chat_id,
                "🖥️ <b>Laptop popup open kar diya.</b>\n\n"
                "Wahin file upload karo ya live progress dekho; "
                "Telegram par bhi updates aate rahenge.",
            )
        except Exception as exc:  # noqa: BLE001
            await self.tg.send_message(
                chat_id,
                f"⚠️ Laptop popup open nahi hua.\n\n{escape(shorten(safe_error(exc), 400))}",
            )

    async def _handle_callback(
        self, chat_id: int, callback_id: str, data: str
    ) -> None:
        await self.tg.answer_callback(callback_id)
        if data in {"menu", "start"}:
            if data == "start":
                await self.handle_start(chat_id)
            else:
                await self.welcome(chat_id)
        elif data == "status":
            await self.send_status(chat_id)
        elif data == "stop":
            await self.stop(chat_id)
        elif data == "latest" or data == "sendfile":
            await self.send_latest(chat_id)
        elif data == "retry":
            await self.handle_retry(chat_id)
        elif data.startswith("run:"):
            try:
                limit = int(data.split(":", 1)[1])
            except ValueError:
                return
            await self.handle_run(chat_id, limit)
        elif data == "help":
            await self.help(chat_id)

    async def process_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            callback = update["callback_query"]
            message = callback.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is None:
                return
            callback_id = str(callback.get("id") or "")
            if not self._check_auth(int(chat_id)):
                await self.deny(int(chat_id), callback_id)
                return
            await self._handle_callback(
                int(chat_id), callback_id, str(callback.get("data") or "")
            )
            return

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        chat_id = int(chat_id)
        if not self._check_auth(chat_id):
            await self.deny(chat_id)
            return
        document = message.get("document")
        if isinstance(document, dict):
            await self.handle_document(chat_id, document)
            return
        text = str(message.get("text") or "")
        if text:
            await self._handle_text(chat_id, text)
            return
        await self.welcome(chat_id)

    # ------------------------------------------------------------- poll loop
    async def run(self) -> None:
        while True:
            try:
                me = await self.tg.get_me()
                break
            except TelegramError as exc:
                if exc.status_code in {401, 403}:
                    raise
                log.warning("Telegram connection failed; retrying: %s", safe_error(exc))
                await asyncio.sleep(10)
        username = str(me.get("username") or "bot")
        log.info("Connected to Telegram as @%s", username)
        try:
            await self.tg.set_my_commands()
        except TelegramError as exc:
            log.warning("setMyCommands failed: %s", safe_error(exc))

        saved = self._saved_chat()
        if saved:
            try:
                await self.tg.send_message(
                    int(saved),
                    "🟢 <b>Apollo bot online hai.</b>\n"
                    "Start dabao ya file bhejo — sab yahin se chalega.\n"
                    "Laptop par live popup bhi khul raha hai.",
                    reply_markup=reply_menu(),
                )
            except (ValueError, TelegramError) as exc:
                log.warning("Startup notice failed: %s", safe_error(exc))

        self._monitor_task = asyncio.create_task(self._monitor_activity())
        asyncio.create_task(self._startup_companion())

        while True:
            try:
                updates = await self.tg.get_updates(self.offset)
            except TelegramError as exc:
                if exc.status_code == 401:
                    raise
                log.warning("getUpdates failed: %s", safe_error(exc))
                await asyncio.sleep(5)
                continue
            for update in updates:
                # Advance the offset before acting.  A crash during a command
                # must not replay a Start click and spend more credits.
                self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
                save_offset(self.offset)
                try:
                    await self.process_update(update)
                except TelegramError as exc:
                    log.warning("Telegram update failed: %s", safe_error(exc))
                except BotError as exc:
                    log.warning("Update failed: %s", safe_error(exc))
                except Exception:  # noqa: BLE001
                    log.exception("Unexpected update failure")


# --------------------------------------------------------------------------- startup


def install_startup_shortcut() -> Path:
    """Create a hidden login shortcut; return its path."""
    appdata = os.getenv("APPDATA", "").strip()
    if not appdata:
        raise BotError("APPDATA not found; startup shortcut nahi ban saka.")
    startup = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup.mkdir(parents=True, exist_ok=True)
    shortcut = startup / "Apollo Telegram Bot.lnk"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    script = ROOT / "telegram_bot.py"

    def quote(value: Path) -> str:
        return str(value).replace("'", "''")

    ps = f"""
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut('{quote(shortcut)}')
$Shortcut.TargetPath = '{quote(pythonw)}'
$Shortcut.Arguments = '\"{quote(script)}\"'
$Shortcut.WorkingDirectory = '{quote(ROOT)}'
$Shortcut.WindowStyle = 7
$Shortcut.Description = 'Apollo Telegram bot - hidden local control panel'
$Shortcut.Save()
"""
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            ps,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 or not shortcut.exists():
        detail = (completed.stderr or completed.stdout or "").strip()
        raise BotError(f"Startup shortcut nahi bana. {shorten(detail, 400)}")
    return shortcut


def acquire_mutex() -> object | None:
    if os.name != "nt":
        return object()
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_bool,
        ctypes.c_wchar_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateMutexW(None, False, "Local\\ApolloTelegramBot")
    if not handle:
        return None
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return handle


def console_print(message: str) -> None:
    """pythonw has no stdout; never let a status print crash a hidden process."""
    if sys.stdout is not None:
        print(message, flush=True)


async def check_token(token: str) -> str:
    client = TelegramClient(token)
    try:
        me = await client.get_me()
        return str(me.get("username") or "unknown")
    finally:
        await client.close()


def main() -> int:
    global _TOKEN
    configure_logging()
    args = set(sys.argv[1:])

    if "--install-startup" in args:
        try:
            shortcut = install_startup_shortcut()
            console_print(f"Startup shortcut created: {shortcut}")
            return 0
        except Exception as exc:  # noqa: BLE001
            console_print(f"Startup shortcut failed: {safe_error(exc)}")
            return 1

    _TOKEN = os.getenv(TOKEN_ENV, "").strip()
    if not _TOKEN:
        log.error(
            "TELEGRAM_BOT_TOKEN is missing. Add it to .env, then start the bot again."
        )
        console_print("TELEGRAM_BOT_TOKEN missing. Add it to .env and retry.")
        return 2

    if "--check" in args:
        try:
            username = asyncio.run(check_token(_TOKEN))
            console_print(f"Telegram token OK: @{username}")
            return 0
        except Exception as exc:  # noqa: BLE001
            log.error("Telegram token check failed: %s", safe_error(exc))
            console_print(f"Telegram token check failed: {safe_error(exc)}")
            return 1

    mutex = acquire_mutex()
    if mutex is None:
        log.info("Another Apollo Telegram bot instance is already running; exiting.")
        return 0

    bot = ApolloBot(_TOKEN)
    try:
        asyncio.run(bot.run())
        return 0
    except KeyboardInterrupt:
        return 0
    except TelegramError as exc:
        log.error("Telegram stopped: %s", safe_error(exc))
        return 1
    except Exception:  # noqa: BLE001
        log.exception("Apollo Telegram bot crashed")
        return 1
    finally:
        try:
            asyncio.run(bot.close())
        except Exception:  # noqa: BLE001
            log.exception("Error while closing bot")


if __name__ == "__main__":
    raise SystemExit(main())

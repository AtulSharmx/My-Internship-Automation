"""Minimal Telegram control panel for the startup discovery agent.

Three actions only:

* **Start Scan** - starts the same background Wellfound scan.
* **Stop** - stops the active scan and keeps everything collected so far.
* **Download** - one file with all data so far, or every past export.

Activity is shown as a single word (plus an emoji), never as a wall of
counters: ``Running``, ``Stopping``, ``Stopped``, ``Done``, ``Failed``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .config import Config
from .discovery import DiscoveryService
from .export import EmptyReportError
from .jobs import (
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_INTERRUPTED,
    JOB_PAUSED,
    JOB_RUNNING,
    JOB_STOPPED,
    JOB_STOPPING,
    MODE_NEW,
    MODE_NEW_RETRY,
    JobBusy,
    JobError,
    ScanManager,
)
from .normalize import truncate

log = logging.getLogger("startup_agent.telegram")

# --- reply keyboard -------------------------------------------------------
START_SCAN = "🚀 Start Scan"
STOP = "⏹ Stop"
DOWNLOAD = "📥 Download"
MAIN_BUTTONS = {START_SCAN, STOP, DOWNLOAD}

# Labels from the older, larger menus. Old messages still carry them, so a
# tap must not fall through to an error.
LEGACY_START = {"🚀 Start New Scan", "🚀 Start new scan", "🔎 Discover Startups"}
LEGACY_STOP = {"⏹ Stop scan", "⏹ Stop scan (no active scan)"}
LEGACY_DOWNLOAD = {
    "📥 Download Reports",
    "📥 Download reports",
    "📤 Export Data",
}

UI_VERSION = "3"
TELEGRAM_FILE_LIMIT = 49 * 1024 * 1024

WELCOME_TEXT = (
    "👋 <b>Welcome to Wellfound Scanner</b>\n\n"
    "🚀 <b>Start Scan</b> — start scanning for new startups\n"
    "⏹ <b>Stop</b> — stop the scan and keep everything collected\n"
    "📥 <b>Download</b> — download this file or all past reports\n\n"
    "Tap a button below to begin."
)

STATUS_WORDS = {
    JOB_RUNNING: "🏃 Running",
    JOB_PAUSED: "🔐 Verify",
    JOB_STOPPING: "⏳ Stopping",
    JOB_STOPPED: "⏹ Stopped",
    JOB_COMPLETED: "✅ Done",
    JOB_FAILED: "❌ Failed",
    JOB_INTERRUPTED: "⚠️ Interrupted",
}


def status_word(status: str) -> str:
    """Return the one-word status shown in the chat."""
    return STATUS_WORDS.get(str(status or ""), "⏳ Starting")


def _fmt_hhmm(value: str) -> str:
    """Format a stored HH:MM value; kept for the config tests and notices."""
    if not value:
        return "manual only"
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        suffix = "AM" if hour < 12 else "PM"
        display = hour % 12 or 12
        return f"{display}:{minute:02d} {suffix}"
    except (ValueError, TypeError):
        return str(value)


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(
        self,
        cfg: Config,
        service: DiscoveryService,
        jobs: ScanManager | None = None,
    ) -> None:
        self.cfg = cfg
        self.service = service
        self.store = service.store
        self.jobs = jobs or ScanManager(cfg, self.store)
        self.jobs.progress_sink = self._on_progress
        self.jobs.event_sink = self._on_event
        self._client: httpx.AsyncClient | None = None
        self._offset = 0
        self._closed = False
        self._tasks: set[asyncio.Task] = set()
        self._started_at = datetime.now().astimezone()
        # One live message is edited in place while a scan runs.
        self._live_message_id: int | None = None
        self._live_job_id = ""
        self._live_word = ""

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"https://api.telegram.org/bot{self.cfg.telegram_token}",
                timeout=httpx.Timeout(
                    max(20.0, self.cfg.telegram_poll_timeout + 15.0), connect=10.0
                ),
            )
        return self._client

    async def _api(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        files: dict[str, Any] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if params:
            kwargs["data"] = params
        if files:
            kwargs["files"] = files
        try:
            response = await self._http().post(f"/{method}", **kwargs)
        except httpx.HTTPError as exc:
            # Do not include the request URL: it contains the bot token.
            raise TelegramError(f"{method} failed ({type(exc).__name__})") from None
        try:
            payload = response.json()
        except ValueError:
            raise TelegramError(f"{method} failed (invalid JSON response)") from None
        if not payload.get("ok"):
            description = str(payload.get("description", "unknown error"))[:300]
            raise TelegramError(f"{method} failed: {description}")
        return payload.get("result")

    async def _api_retry(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        files: dict[str, Any] | None = None,
        attempts: int = 3,
    ) -> Any:
        last: TelegramError | None = None
        for attempt in range(attempts):
            try:
                return await self._api(method, params, files=files)
            except TelegramError as exc:
                last = exc
                if attempt + 1 >= attempts:
                    break
                await asyncio.sleep(1.0 + attempt)
        raise last or TelegramError(f"{method} failed")

    async def send(
        self,
        text: str,
        keyboard: dict[str, Any] | None = None,
        *,
        preview: bool = False,
    ) -> bool:
        params: dict[str, Any] = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": truncate(text, 3900),
            "parse_mode": "HTML",
            "disable_web_page_preview": "false" if preview else "true",
        }
        if keyboard is not None:
            params["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
        try:
            await self._api_retry("sendMessage", params)
            return True
        except TelegramError as exc:
            log.warning("sendMessage failed: %s", exc)
            return False

    async def send_with_id(
        self, text: str, keyboard: dict[str, Any] | None = None
    ) -> int | None:
        """Send a message and return its id (used for live status edits)."""
        params: dict[str, Any] = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": truncate(text, 3900),
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if keyboard is not None:
            params["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
        try:
            result = await self._api_retry("sendMessage", params)
        except TelegramError as exc:
            log.warning("sendMessage failed: %s", exc)
            await self.send(text, keyboard)
            return None
        if isinstance(result, dict) and result.get("message_id"):
            return int(result["message_id"])
        return None

    async def send_document(self, path: Path, caption: str = "") -> bool:
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            log.warning("cannot read export %s: %s", path, exc)
            await self.send("❌ Failed")
            return False
        if len(data) > TELEGRAM_FILE_LIMIT:
            await self.send("⚠️ Large")
            return False
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        files = {"document": (path.name, data, mime)}
        params = {
            "chat_id": self.cfg.telegram_chat_id,
            "caption": truncate(caption, 900),
        }
        try:
            await self._api_retry("sendDocument", params, files=files, attempts=2)
            return True
        except TelegramError as exc:
            log.warning("sendDocument failed: %s", exc)
            await self.send("❌ Failed")
            return False

    async def edit_message(
        self,
        message_id: int,
        text: str,
        keyboard: dict[str, Any] | None = None,
    ) -> bool:
        params: dict[str, Any] = {
            "chat_id": self.cfg.telegram_chat_id,
            "message_id": message_id,
            "text": truncate(text, 3900),
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "reply_markup": json.dumps(
                keyboard if keyboard is not None else {"inline_keyboard": []},
                ensure_ascii=False,
            ),
        }
        try:
            await self._api_retry("editMessageText", params, attempts=2)
            return True
        except TelegramError as exc:
            if "message is not modified" in str(exc).lower():
                return True
            log.debug("editMessageText failed; sending a new message: %s", exc)
            return await self.send(text, keyboard)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = truncate(text, 180)
        try:
            await self._api_retry("answerCallbackQuery", params, attempts=2)
        except TelegramError as exc:
            log.debug("answerCallbackQuery failed: %s", exc)

    async def setup_commands(self) -> None:
        commands = [
            {"command": "start", "description": "Open the menu"},
            {"command": "scan", "description": "Start a scan"},
            {"command": "stop", "description": "Stop the scan"},
            {"command": "download", "description": "Download files"},
        ]
        try:
            await self._api(
                "setMyCommands",
                {"commands": json.dumps(commands, ensure_ascii=False)},
            )
        except TelegramError as exc:
            log.warning("setMyCommands failed: %s", exc)

    # ------------------------------------------------------------------
    # Keyboards
    # ------------------------------------------------------------------
    @staticmethod
    def main_keyboard() -> dict[str, Any]:
        return {
            "keyboard": [
                [{"text": START_SCAN}],
                [{"text": STOP}, {"text": DOWNLOAD}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
            "input_field_placeholder": "Choose an option...",
        }

    @staticmethod
    def menu_keyboard() -> dict[str, Any]:
        """Alias used by the scheduler and error notices."""
        return Telegram.main_keyboard()

    @staticmethod
    def back_keyboard() -> dict[str, Any]:
        return {"inline_keyboard": []}

    @staticmethod
    def download_keyboard() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "📄 Download this file",
                        "callback_data": "m:download:this",
                    }
                ],
                [
                    {
                        "text": "🗂 Download all past",
                        "callback_data": "m:download:past",
                    }
                ],
            ]
        }

    # ------------------------------------------------------------------
    # Panels
    # ------------------------------------------------------------------
    @staticmethod
    def _home_text() -> str:
        return "🤖 Wellfound Scanner"

    async def send_home(self) -> None:
        await self.send(self._home_text(), self.main_keyboard())

    async def send_welcome(self) -> None:
        await self.send(WELCOME_TEXT, self.main_keyboard())

    async def _show_download(self, message_id: int | None = None) -> None:
        text = "📥 Download"
        if message_id:
            await self.edit_message(message_id, text, self.download_keyboard())
        else:
            await self.send(text, self.download_keyboard())

    # ------------------------------------------------------------------
    # Status (one word, edited in place)
    # ------------------------------------------------------------------
    async def _show_status(self, snapshot: dict[str, Any]) -> None:
        job_id = str(snapshot.get("job_id") or "")
        word = status_word(str(snapshot.get("status") or ""))
        if job_id and job_id != self._live_job_id:
            self._live_job_id = job_id
            self._live_message_id = None
            self._live_word = ""
        if self._live_message_id is None:
            self._live_message_id = await self.send_with_id(word)
            self._live_word = word
            return
        if word != self._live_word:
            if await self.edit_message(self._live_message_id, word):
                self._live_word = word

    async def _show_job_status(self, job) -> None:
        snapshot = self.jobs.snapshot(job.job_id)
        if not snapshot:
            snapshot = {"job_id": job.job_id, "status": job.status}
        await self._show_status(snapshot)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    async def announce_started(self, job) -> None:
        """Show the one-word running status for a freshly started scan."""
        await self._show_job_status(job)

    async def start_scan(self) -> None:
        active = self.jobs.active_job()
        if active is not None:
            if active.status == JOB_PAUSED:
                # The only reason the agent pauses itself is a verification
                # page. Completing it in the browser and tapping Start again
                # continues the same run.
                try:
                    job = await self.jobs.resume()
                except (JobBusy, JobError) as exc:
                    log.warning("could not resume the scan: %s", exc)
                    await self.send("❌ Failed")
                    return
                if job is not None:
                    await self._show_job_status(job)
                    return
            await self._show_job_status(active)
            return

        settings = self.jobs.settings()
        mode = MODE_NEW_RETRY if settings.retry_incomplete else MODE_NEW
        try:
            job = await self.jobs.start(mode)
        except JobBusy as exc:
            log.info("scan already active: %s", exc)
            active = self.jobs.active_job()
            if active is not None:
                await self._show_job_status(active)
            else:
                await self.send("🏃 Running")
            return
        except (JobError, ValueError) as exc:
            log.warning("scan could not start: %s", exc)
            await self.send("❌ Failed")
            return
        await self.announce_started(job)

    async def stop_scan(self) -> None:
        job = await self.jobs.request_stop()
        if job is None:
            await self.send("💤 Idle")
            return
        await self._show_job_status(job)

    async def show_status(self) -> None:
        job = self.jobs.active_job()
        if job is None:
            await self.send("💤 Idle")
            return
        await self._show_job_status(job)

    # ------------------------------------------------------------------
    # Downloads
    # ------------------------------------------------------------------
    async def send_this_file(self) -> None:
        """One Excel file with every lead collected so far."""
        try:
            result = self.service.exporter.export_report("all", "xlsx")
        except EmptyReportError:
            await self.send("📭 Empty")
            return
        except Exception:  # noqa: BLE001 - report, never crash polling
            log.exception("all-data export failed")
            await self.send("❌ Failed")
            return
        await self.send_document(
            result.path,
            caption=f"📄 All data · {result.row_count} row(s)",
        )

    def _build_past_zip(self) -> tuple[Path | None, int]:
        """Zip every report already sitting in the export folder."""
        folder = self.cfg.export_dir
        if not folder.exists():
            return None, 0
        files = [
            path
            for path in sorted(folder.iterdir())
            if path.is_file()
            and path.suffix.lower() in {".csv", ".json", ".xlsx", ".txt"}
            and not path.name.startswith("all-past-")
        ]
        if not files:
            return None, 0
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = folder / f"all-past-{stamp}.zip"
        counter = 1
        while target.exists():
            target = folder / f"all-past-{stamp}-{counter}.zip"
            counter += 1
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, arcname=path.name)
        return target, len(files)

    async def send_all_past(self) -> None:
        try:
            path, count = await asyncio.to_thread(self._build_past_zip)
        except Exception:  # noqa: BLE001 - report, never crash polling
            log.exception("all-past archive failed")
            await self.send("❌ Failed")
            return
        if path is None:
            await self.send("📭 Empty")
            return
        await self.send_document(path, caption=f"🗂 All past · {count} file(s)")

    # ------------------------------------------------------------------
    # Progress and notifications
    # ------------------------------------------------------------------
    async def _on_progress(self, snapshot: dict[str, Any]) -> None:
        """Keep exactly one status message per run, showing one word."""
        if not snapshot:
            return
        try:
            await self._show_status(snapshot)
        except Exception:  # noqa: BLE001 - progress must never break a run
            log.debug("live status update failed", exc_info=True)

    async def _on_event(
        self, kind: str, job: dict[str, Any], detail: str
    ) -> None:
        if kind == "auth":
            await self._show_status(
                {
                    "job_id": str(job.get("job_id") or ""),
                    "status": JOB_PAUSED,
                }
            )
            return
        if kind not in {"completed", "stopped", "failed"}:
            return
        await self._show_status(
            {
                "job_id": str(job.get("job_id") or ""),
                "status": str(job.get("status") or ""),
            }
        )

    # ------------------------------------------------------------------
    # Incoming updates
    # ------------------------------------------------------------------
    def _authorized(self, chat: dict[str, Any] | None) -> bool:
        if not chat:
            return False
        target = str(self.cfg.telegram_chat_id or "").lstrip("@")
        if str(chat.get("id", "")) == target:
            return True
        username = str(chat.get("username") or "")
        return bool(target and username and username.lower() == target.lower())

    async def _handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        if not self._authorized(message.get("chat")):
            log.warning("ignored Telegram update from an unauthorized chat")
            return
        text = str(message.get("text") or "").strip()
        if not text:
            return
        if text.startswith("/"):
            await self._handle_command(text)
        elif text in MAIN_BUTTONS:
            await self._handle_main_button(text)
        elif text in LEGACY_START:
            await self.start_scan()
        elif text in LEGACY_STOP:
            await self.stop_scan()
        elif text in LEGACY_DOWNLOAD:
            await self._show_download()
        else:
            await self.send_home()

    async def _handle_command(self, text: str) -> None:
        command, _, _ = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        if command in {"/start", "/menu"}:
            await self.send_welcome()
        elif command in {"/scan", "/run"}:
            await self.start_scan()
        elif command == "/stop":
            await self.stop_scan()
        elif command in {"/download", "/reports"}:
            await self._show_download()
        elif command == "/status":
            await self.show_status()
        else:
            await self.send_home()

    async def _handle_main_button(self, text: str) -> None:
        if text == START_SCAN:
            await self.start_scan()
        elif text == STOP:
            await self.stop_scan()
        elif text == DOWNLOAD:
            await self._show_download()

    # ------------------------------------------------------------------
    # Callback routing
    # ------------------------------------------------------------------
    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        if not self._authorized(chat):
            await self.answer_callback(str(callback.get("id", "")), "Not authorized.")
            return
        data = str(callback.get("data") or "")
        callback_id = str(callback.get("id", ""))
        message_id = message.get("message_id")
        await self.answer_callback(callback_id)
        try:
            await self._route_callback(data, message_id)
        except Exception:  # noqa: BLE001 - one bad callback must not stop polling
            log.exception("callback failed: %s", data)
            await self.send("❌ Failed")

    async def _route_callback(self, data: str, message_id: int | None) -> None:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else data

        if action == "download":
            choice = parts[2] if len(parts) > 2 else ""
            if choice == "this":
                await self.send_this_file()
            elif choice == "past":
                await self.send_all_past()
            else:
                await self._show_download(message_id)
        elif action in {"scan", "resume"} or data in {"discover", "wellfound"}:
            await self.start_scan()
        elif action == "stop" or data == "stop":
            await self.stop_scan()
        elif action == "status" or data == "status":
            await self.show_status()
        elif action in {"reports", "rep", "export", "sheet"} or data in {
            "export",
            "sheet",
        }:
            await self._show_download(message_id)
        else:
            # Everything from the old menus (settings, runs, help, retry,
            # review, sources, ...) simply returns to the small menu.
            await self.send_home()

    # ------------------------------------------------------------------
    # Startup and polling
    # ------------------------------------------------------------------
    async def _prime_offset(self) -> None:
        saved = self.store.get_meta("telegram_offset", "")
        if saved.isdigit():
            self._offset = int(saved)
            return
        try:
            updates = await self._api(
                "getUpdates", {"offset": -1, "limit": 1, "timeout": 0}
            )
        except TelegramError as exc:
            log.warning("could not prime Telegram offset: %s", exc)
            return
        if updates:
            self._offset = int(updates[-1].get("update_id", 0)) + 1
            self.store.set_meta("telegram_offset", str(self._offset))

    async def startup_notice(self) -> None:
        """Send the new three-button keyboard once after this UI change."""
        if self.store.get_meta("telegram_ui_version") == UI_VERSION:
            return
        if await self.send(WELCOME_TEXT, self.main_keyboard()):
            self.store.set_meta("telegram_ui_version", UI_VERSION)

    async def announce_recovered(self, job) -> None:
        await self._show_job_status(job)

    async def announce_resumed(self, job) -> None:
        await self._show_job_status(job)

    @staticmethod
    def _run_text(summary) -> str:
        """Kept for the optional daily discovery notice."""
        return "✅ Done"

    async def run(self) -> None:
        await self._prime_offset()
        await self.setup_commands()
        await self.startup_notice()
        log.info("Telegram polling started")
        backoff = 1.0
        while not self._closed:
            try:
                updates = await self._api(
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": self.cfg.telegram_poll_timeout,
                        "limit": 100,
                        "allowed_updates": json.dumps(
                            ["message", "callback_query"]
                        ),
                    },
                )
                backoff = 1.0
            except TelegramError as exc:
                log.warning(
                    "Telegram polling failed: %s; retrying in %.0fs", exc, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)
                continue
            for update in updates or []:
                update_id = int(update.get("update_id", 0))
                try:
                    await self._handle_update(update)
                except Exception:  # noqa: BLE001 - keep the bot alive
                    log.exception("unhandled Telegram update error")
                finally:
                    self._offset = max(self._offset, update_id + 1)
                    self.store.set_meta("telegram_offset", str(self._offset))

    async def close(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

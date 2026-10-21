"""Telegram: outbound notifications + inbound commands.

A deliberately small implementation on top of ``httpx`` (long-polling
``getUpdates``) instead of a full bot framework - fewer dependencies, and every
retry/backoff decision stays visible in one file.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
from datetime import datetime

import httpx

from .config import Config
from .models import RuntimeState, Status, display_title
from .wellfound import format_age

log = logging.getLogger("wfbot.telegram")

#: Statuses that deserve their own Telegram message. Everything else is a
#: skip and only shows up as a count in the run card.
NOTIFY_STATUSES = frozenset(
    {
        Status.APPLIED,
        Status.UNVERIFIED,
        Status.NEEDS_REVIEW,
        Status.EXTERNAL,
        Status.FAILED,
    }
)

START_TEXT = """\
👋 <b>Wellfound Bot</b>
Choose an option below.
"""

HELP_TEXT = """\
❓ <b>Help</b>

Use the buttons below to run and control the bot.

<b>Pause / Resume</b>
Stops or restarts applications. The daily schedule keeps running.

<b>Form questions</b>
Reply here with the answer, or tap <b>Skip application</b>.
When there are several questions, send one line per answer.

<b>Typed commands</b>
/info - schedule and last run
/report - full activity report
/responses - employer replies
"""

BTN_STATUS = "\U0001f4ca Status"
BTN_APPS = "\U0001f4cb Applications"
BTN_RUN = "\u26a1 Run now"
BTN_PAUSE = "\u23ef\ufe0f Pause / Resume"
BTN_PAUSE_ACTION = "\u23f8\ufe0f Pause"
BTN_RESUME_ACTION = "\u25b6\ufe0f Resume"
BTN_SYNC = "\U0001f504 Sync"
BTN_MORE = "\u2699\ufe0f More"

MAIN_BUTTON_ACTIONS = {
    BTN_STATUS: "status",
    BTN_APPS: "apps",
    BTN_RUN: "run",
    BTN_PAUSE: "toggle",
    BTN_PAUSE_ACTION: "toggle",
    BTN_RESUME_ACTION: "toggle",
    BTN_SYNC: "sync",
    BTN_MORE: "more",
}


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


class Telegram:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state = RuntimeState()
        self._base_url = f"https://api.telegram.org/bot{cfg.telegram_token}"
        self._http: httpx.AsyncClient | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None
        self._offset = 0
        self._send_lock = asyncio.Semaphore(3)  # stay far below 30 msg/s
        self._commands: dict[str, object] = {}
        self._trigger_run: asyncio.Event | None = None
        self._store = None  # set by app wiring
        self._wf = None    # set by app wiring (used by /sync)
        #: Set while the bot is waiting for the operator to answer an
        #: application question. The next plain-text message resolves it.
        self._pending_question: asyncio.Future[str | None] | None = None
        self._pending_question_token = ""

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        """An httpx client bound to the *current* event loop.

        httpx clients are loop-bound, so a client is recreated if this object
        is ever used from a different loop (tests, restarts) instead of failing
        with 'Event loop is closed'.
        """
        loop = asyncio.get_running_loop()
        if self._http is None or self._http_loop is not loop:
            self._http = httpx.AsyncClient(
                base_url=self._base_url, timeout=httpx.Timeout(30.0, connect=10.0)
            )
            self._http_loop = loop
        return self._http
    def bind(self, *, store, trigger_run: asyncio.Event) -> None:
        self._store = store
        self._trigger_run = trigger_run

    def command(self, name: str):
        def decorator(func):
            self._commands[name] = func
            return func

        return decorator

    async def _api(self, method: str, **params):
        client = self._client()
        async with self._send_lock:
            response = await client.post(f"/{method}", data=params)
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {payload.get('description')}")
        return payload["result"]

    async def send(
        self, text: str, *, preview: bool = False, keyboard: dict | None = None
    ) -> None:
        for attempt in range(3):
            try:
                params = {
                    "chat_id": self.cfg.telegram_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": not preview,
                }
                if keyboard is not None:
                    params["reply_markup"] = json.dumps(keyboard)
                await self._api("sendMessage", **params)
                return
            except Exception as exc:  # noqa: BLE001 - network hiccups are expected
                if attempt == 2:
                    log.error("telegram send failed after retries: %s", exc)
                    return
                await asyncio.sleep(1.5 * (attempt + 1))

    def main_keyboard(self) -> dict:
        """The always-visible six-button panel.

        The fourth button reflects the current state instead of always saying
        "Pause / Resume", so the next tap is unambiguous.
        """
        paused = bool(self._store is not None and self._store.paused)
        toggle = BTN_RESUME_ACTION if paused else BTN_PAUSE_ACTION
        return {
            "keyboard": [
                [{"text": BTN_STATUS}, {"text": BTN_APPS}],
                [{"text": BTN_RUN}, {"text": toggle}],
                [{"text": BTN_SYNC}, {"text": BTN_MORE}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
            "input_field_placeholder": "Choose an option...",
        }

    async def _typing(self) -> None:
        try:
            await asyncio.wait_for(
                self._api(
                    "sendChatAction",
                    chat_id=self.cfg.telegram_chat_id,
                    action="typing",
                ),
                timeout=3,
            )
        except Exception as exc:  # noqa: BLE001 - cosmetic only
            log.debug("typing indicator failed: %s", exc)

    async def _typing_for(self, awaitable):
        """Keep the typing indicator alive while a slow action runs."""
        task = asyncio.ensure_future(awaitable)
        try:
            while not task.done():
                await self._typing()
                done, _ = await asyncio.wait({task}, timeout=4)
                if done:
                    break
            return await task
        finally:
            if not task.done():
                task.cancel()

    async def _edit_message(
        self, message_id: int, text: str, keyboard: dict | None = None
    ) -> bool:
        params = {
            "chat_id": self.cfg.telegram_chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            params["reply_markup"] = json.dumps(keyboard)
        try:
            await self._api("editMessageText", **params)
            return True
        except Exception as exc:  # noqa: BLE001 - fall back to a fresh message
            if "message is not modified" in str(exc).lower():
                return True
            log.debug("editMessageText failed: %s", exc)
            return False

    async def _answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            await self._api(
                "answerCallbackQuery",
                callback_query_id=callback_id,
                text=text[:200],
                show_alert=False,
            )
        except Exception as exc:  # noqa: BLE001 - the panel still works
            log.debug("answerCallbackQuery failed: %s", exc)

    async def setup_commands(self) -> None:
        """Publish a deliberately short command list in Telegram's menu."""
        commands = [
            {"command": "start", "description": "Open the button panel"},
            {"command": "status", "description": "Bot status and next run"},
            {"command": "applications", "description": "Recent applications"},
            {"command": "run", "description": "Run now"},
            {"command": "pause", "description": "Pause applying"},
            {"command": "resume", "description": "Resume applying"},
        ]
        try:
            await self._api("setMyCommands", commands=json.dumps(commands))
        except Exception as exc:  # noqa: BLE001 - not worth failing startup
            log.warning("setMyCommands failed: %s", exc)

    @staticmethod
    def _title(title: str, company: str) -> str:
        return display_title(title, company)

    @staticmethod
    def _link(url: str, label: str) -> str:
        if not url:
            return esc(label)
        safe_url = html.escape(str(url), quote=True)
        return f'<a href="{safe_url}">{esc(label)}</a>'

    @staticmethod
    def _clock(value: datetime, *, seconds: bool = False) -> str:
        """Format a local time as 8:00 PM instead of 20:00."""
        hour = value.hour % 12 or 12
        suffix = "AM" if value.hour < 12 else "PM"
        minute = f"{value.minute:02d}"
        if seconds:
            return f"{hour}:{minute}:{value.second:02d} {suffix}"
        return f"{hour}:{minute} {suffix}"

    @classmethod
    def _stamp(cls, value: datetime, *, seconds: bool = False) -> str:
        return f"{value:%d %b %Y}, {cls._clock(value, seconds=seconds)}"

    @classmethod
    def _clock_text(cls, value: object) -> str:
        """Convert a stored ``HH:MM`` setting to a 12-hour display string."""
        try:
            parsed = datetime.strptime(str(value).strip(), "%H:%M")
        except (TypeError, ValueError):
            return str(value)
        return cls._clock(parsed)

    def _when(self, value: object) -> str:
        if not value:
            return ""
        try:
            moment = datetime.fromisoformat(str(value)).astimezone()
        except (TypeError, ValueError):
            return ""
        now = datetime.now().astimezone()
        if moment.date() == now.date():
            return self._clock(moment)
        return f"{moment:%d %b} {self._clock(moment)}"

    def _report_data(self) -> dict:
        return self._store.report(
            goal=self.cfg.apply_goal,
            deadline=self.cfg.goal_deadline,
            daily_cap=self.cfg.max_apps_per_day,
        )

    def _panel_main(self, note: str = "") -> tuple[str, dict | None]:
        store = self._store
        state = self.state
        now = datetime.now().astimezone()
        report = self._report_data()
        today = report["today"]["applied"]
        total = report["all"]["applied"]
        history = int(report["all"]["counts"].get(Status.ALREADY_APPLIED, 0))

        lines = ["\U0001f4ca <b>Wellfound Bot</b>", "", "<b>Bot Status</b>"]
        if state.running:
            line = "\U0001f7e2 <b>Running</b> \u00b7 application pass in progress"
            if state.run_started_at:
                line += f" (started {self._clock(state.run_started_at)})"
            lines.append(line)
        elif store.paused:
            lines.append("\u23f8\ufe0f <b>Paused</b> \u00b7 applying is on hold")
        else:
            line = "\u26aa <b>Idle</b>"
            if state.next_run_at:
                delta = max(0, int((state.next_run_at - now).total_seconds()))
                if delta >= 86400:
                    timing = f"in {delta // 86400}d {delta % 86400 // 3600}h"
                elif delta >= 3600:
                    timing = f"in {delta // 3600}h {delta % 3600 // 60}m"
                elif delta >= 60:
                    timing = f"in {delta // 60}m"
                else:
                    timing = "in <1m"
                line += (
                    f" \u00b7 next run <b>{self._clock(state.next_run_at)}</b> "
                    f"({timing})"
                )
            else:
                line += " \u00b7 scheduling..."
            lines.append(line)
        if state.backoff_multiplier > 1:
            lines.append(f"\u26a0\ufe0f Backoff \u00d7{state.backoff_multiplier:g}")

        lines.extend(["", "<b>Today's Activity</b>"])
        if self.cfg.max_apps_per_day:
            lines.append(
                f"\u2705 Applied <b>{today}</b> of <b>{self.cfg.max_apps_per_day}</b> today"
            )
        else:
            lines.append(f"\u2705 Applied <b>{today}</b> today")

        lines.extend(["", "<b>Application History</b>"])
        if total or history:
            lines.append(f"Applied <b>{total}</b> total")
            if history:
                lines.append(f"Imported <b>{history}</b> from Wellfound")
            responses = report.get("responses") or {}
            if responses:
                order = {"pending": 0, "accepted": 1, "not accepted": 2}
                parts = [
                    f"{key.title()} <b>{count}</b>"
                    for key, count in sorted(
                        responses.items(), key=lambda item: order.get(item[0], 9)
                    )
                ]
                lines.append("\U0001f4ec " + " \u00b7 ".join(parts))
        else:
            lines.append("No applications recorded yet.")

        lines.extend(["", "<b>Last Application</b>"])
        rows = store.last_applied(1)
        if rows:
            row = rows[0]
            company = self._link(
                str(row["url"] or ""), str(row["company"] or "Unknown company")
            )
            title = self._title(str(row["title"] or ""), str(row["company"] or ""))
            lines.append(f"<b>{company}</b> \u00b7 {esc(title)}")
            icon = Status.ICONS.get(row["status"], "")
            label = Status.LABELS.get(row["status"], row["status"])
            meta = f"{icon} {esc(label)}"
            if row["response"]:
                meta += f" \u00b7 {esc(str(row['response']).title())}"
            when = self._when(row["applied_at"])
            if when:
                meta += f" \u00b7 {when}"
            lines.append(meta)
        else:
            lines.append("None yet.")

        if note:
            lines.extend(["", f"<i>{esc(note)}</i>"])
        return "\n".join(lines), None

    def _panel_apps(self, limit: int = 5) -> tuple[str, dict]:
        rows = self._store.last_applied(limit)
        lines = ["\U0001f4cb <b>Applications</b>"]
        if not rows:
            lines.append("Nothing recorded yet.")
        for index, row in enumerate(rows, start=1):
            company = self._link(str(row["url"] or ""), str(row["company"] or "?"))
            title = self._title(str(row["title"] or ""), str(row["company"] or ""))
            lines.append(f"{index}. <b>{company}</b> \u00b7 {esc(title)}")
            icon = Status.ICONS.get(row["status"], "")
            label = Status.LABELS.get(row["status"], row["status"])
            meta = f"{icon} {esc(label)}"
            if row["response"]:
                meta += f" \u00b7 <b>{esc(str(row['response']).title())}</b>"
            when = self._when(row["applied_at"])
            if when:
                meta += f" \u00b7 {when}"
            lines.append(f"   {meta}")
        counts = self._store.response_counts()
        if counts:
            order = {"pending": 0, "accepted": 1, "not accepted": 2}
            parts = [
                f"{key.title()} {count}"
                for key, count in sorted(
                    counts.items(), key=lambda item: order.get(item[0], 9)
                )
            ]
            lines.append("")
            lines.append("\U0001f4ec " + esc(" \u00b7 ".join(parts)))
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "\U0001f504 Refresh", "callback_data": "p:apps"},
                    {"text": "\U0001f4ca Status", "callback_data": "p:status"},
                ],
                [{"text": "\u2b05\ufe0f Back", "callback_data": "p:back"}],
            ]
        }
        return "\n".join(lines), keyboard

    def _panel_more(self) -> tuple[str, dict]:
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "\u26a0\ufe0f Issues", "callback_data": "p:issues"},
                    {"text": "\U0001f9e9 Roles", "callback_data": "p:roles"},
                ],
                [
                    {"text": "\u2753 Help", "callback_data": "p:help"},
                    {"text": "\u2b05\ufe0f Back", "callback_data": "p:back"},
                ],
            ]
        }
        return "\u2699\ufe0f <b>More</b>\nChoose a section.", keyboard

    def _panel_issues(self) -> tuple[str, dict]:
        rows = self._store.recent(
            (Status.FAILED, Status.NEEDS_REVIEW, Status.UNVERIFIED, Status.EXTERNAL),
            5,
        )
        lines = ["\u26a0\ufe0f <b>Issues</b>"]
        if not rows:
            lines.append("No errors or reviews pending.")
        for row in rows:
            company = self._link(str(row["url"] or ""), str(row["company"] or "?"))
            title = self._title(str(row["title"] or ""), str(row["company"] or ""))
            icon = Status.ICONS.get(row["status"], "")
            label = Status.LABELS.get(row["status"], row["status"])
            when = self._when(row["applied_at"])
            lines.append(f"{icon} <b>{company}</b> \u00b7 {esc(title)}")
            meta = f"{esc(label)}"
            if when:
                meta += f" \u00b7 {when}"
            lines.append(f"   {meta}")
            if row["detail"]:
                detail = str(row["detail"]).replace("\n", " ").strip()
                lines.append(f"   <i>{esc(detail[:120])}</i>")
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "\U0001f504 Refresh", "callback_data": "p:issues"},
                    {"text": "\U0001f4ca Status", "callback_data": "p:status"},
                ],
                [{"text": "\u2b05\ufe0f Back", "callback_data": "p:more"}],
            ]
        }
        return "\n".join(lines), keyboard

    def _panel_roles(self) -> tuple[str, dict]:
        cfg = self.cfg
        lines = ["\U0001f9e9 <b>Hunting</b>"]
        for role in cfg.roles:
            lines.append(f"\u2022 {esc(role.label)}")
        lines.append("")
        if cfg.accept_remote:
            remote = ", ".join(cfg.accept_remote_regions)
            lines.append(f"\U0001f4cd Remote: <b>{esc(remote)}</b>")
        onsite = ", ".join(cfg.accept_locations) or "none"
        lines.append(f"\U0001f3e2 Onsite: <b>{esc(onsite)}</b>")
        age = (
            f"\u2264 {cfg.freshness_label}"
            if cfg.max_job_age_days > 0
            else "any age"
        )
        lines.append(
            f"\U0001f3ed <b>{cfg.company_size_min}-{cfg.company_size_max}</b> employees "
            f"\u00b7 {age}"
        )
        run_cap = f"{cfg.max_apps_per_run}/run" if cfg.max_apps_per_run else "unlimited/run"
        day_cap = f"{cfg.max_apps_per_day}/day" if cfg.max_apps_per_day else "unlimited/day"
        lines.append(
            f"\u23f1 <b>{run_cap}</b> \u00b7 <b>{day_cap}</b> \u00b7 "
            f"{cfg.company_cooldown_days}-day cooldown"
        )
        lines.append(
            f"\U0001f4b8 {'paid only' if cfg.skip_unpaid else 'paid + unpaid'}"
        )
        keyboard = {
            "inline_keyboard": [
                [{"text": "\u2b05\ufe0f Back", "callback_data": "p:more"}]
            ]
        }
        return "\n".join(lines), keyboard

    def _panel_help(self) -> tuple[str, dict]:
        keyboard = {
            "inline_keyboard": [
                [{"text": "\u2b05\ufe0f Back", "callback_data": "p:more"}]
            ]
        }
        return HELP_TEXT, keyboard

    def _panel_result(self, text: str) -> tuple[str, dict]:
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "\U0001f4ca Status", "callback_data": "p:status"},
                    {"text": "\U0001f4cb Applications", "callback_data": "p:apps"},
                ],
                [{"text": "\u2b05\ufe0f Back", "callback_data": "p:back"}],
            ]
        }
        return text, keyboard

    async def _show_panel(
        self, text: str, keyboard: dict | None, message_id: int | None = None
    ) -> None:
        if message_id is not None:
            # An empty inline keyboard clears the actions on a panel that is
            # being replaced by the Status view. The six reply-keyboard
            # buttons remain the single place for those actions.
            inline = keyboard if keyboard is not None else {"inline_keyboard": []}
            if await self._edit_message(message_id, text, inline):
                return
        await self.send(text, keyboard=keyboard or self.main_keyboard())

    async def _sync_panel(self) -> tuple[str, dict | None]:
        if self.state.running:
            return self._panel_main("A run is in progress - try Sync after it finishes.")
        if self._wf is None:
            return self._panel_main("Browser is not ready yet.")
        try:
            result = await self._typing_for(self._wf.sync_applications())
        except Exception as exc:  # noqa: BLE001 - report it, do not kill the bot
            log.exception("application sync failed")
            return self._panel_result(
                f"\u274c <b>Sync failed</b>\n<i>{esc(exc)}</i>"
            )
        return self._panel_result(
            "\U0001f4e5 <b>Synced</b>\n"
            f"{result['found']} seen \u00b7 {result['imported']} imported \u00b7 "
            f"{result['updated']} updated"
        )

    async def _panel_action(self, action: str, message_id: int | None = None) -> None:
        await self._typing()
        if action in {"status", "back", "refresh"}:
            text, keyboard = self._panel_main()
        elif action == "apps":
            text, keyboard = self._panel_apps()
        elif action == "more":
            text, keyboard = self._panel_more()
        elif action == "issues":
            text, keyboard = self._panel_issues()
        elif action == "roles":
            text, keyboard = self._panel_roles()
        elif action == "help":
            text, keyboard = self._panel_help()
        elif action == "toggle":
            store = self._store
            if store.paused:
                store.set_paused(False)
                store.set_consecutive_failures(0)
            else:
                store.set_paused(True)
            # The Bot Status section already reflects the new state; a note
            # here would just repeat it.
            text, keyboard = self._panel_main()
        elif action == "run":
            trigger = self._trigger_run
            if self.state.running or (trigger is not None and trigger.is_set()):
                note = "A run is already in progress."
            elif self._store.paused:
                note = "Paused - tap Resume first."
            elif trigger is not None:
                trigger.set()
                note = "\U0001f680 Run triggered - watch for updates."
            else:
                note = "The scheduler is not ready yet."
            text, keyboard = self._panel_main(note)
        elif action == "sync":
            text, keyboard = await self._sync_panel()
        else:
            text, keyboard = self._panel_main("Unknown action.")
        await self._show_panel(text, keyboard, message_id)

    async def _handle_main_button(self, text: str) -> bool:
        # Some clients drop the emoji variation selector (U+FE0F), so compare
        # labels with those invisible selectors removed.
        cleaned = (text or "").strip().replace("\ufe0f", "").replace("\ufe0e", "")
        for label, action in MAIN_BUTTON_ACTIONS.items():
            if cleaned == label.replace("\ufe0f", "").replace("\ufe0e", ""):
                await self._panel_action(action)
                return True
        return False

    async def _handle_callback(self, callback: dict) -> None:
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        callback_id = str(callback.get("id") or "")
        if chat_id != self.cfg.telegram_chat_id:
            log.warning("ignoring callback from unauthorized chat %s", chat_id)
            await self._answer_callback(callback_id)
            return
        if data.startswith("q:skip"):
            parts = data.split(":", 2)
            token = parts[2] if len(parts) == 3 else ""
            pending = self._pending_question
            if (
                pending is not None
                and not pending.done()
                and token
                and token == self._pending_question_token
            ):
                pending.set_result("skip")
                await self._answer_callback(callback_id, "Skipped")
            else:
                await self._answer_callback(callback_id, "Already handled")
            return
        await self._answer_callback(callback_id)
        if data.startswith("p:"):
            await self._panel_action(data[2:], message.get("message_id"))

    async def ask(self, text: str, *, timeout: float | None = None) -> str | None:
        """Send a question and wait for the operator's next plain-text reply.

        This is what turns a form field the profile cannot answer into an
        interactive prompt instead of a "needs review" dead end. ``None`` means
        no reply arrived in time.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str | None] = loop.create_future()
        token = os.urandom(4).hex()
        self._pending_question = future
        self._pending_question_token = token
        await self._typing()
        await self.send(
            text,
            keyboard={
                "inline_keyboard": [
                    [
                        {
                            "text": "\u23ed\ufe0f Skip application",
                            "callback_data": f"q:skip:{token}",
                        }
                    ]
                ]
            },
        )
        try:
            return await asyncio.wait_for(
                future, timeout or self.cfg.question_timeout_seconds
            )
        except asyncio.TimeoutError:
            await self.send(
                "\u23f0 <b>No answer yet</b> - I marked that application for "
                "review and will move on. You can still apply manually from "
                "the link above."
            )
            return None
        finally:
            if self._pending_question is future:
                self._pending_question = None
                self._pending_question_token = ""

    async def send_photo(self, path: str, caption: str = "") -> bool:
        """Send a screenshot as a photo with an HTML caption.

        Returns ``False`` (instead of raising) when Telegram rejects the file,
        so the caller can fall back to a plain text message.
        """
        if not path or not os.path.exists(path):
            return False
        try:
            with open(path, "rb") as handle:
                async with self._send_lock:
                    response = await self._client().post(
                        "/sendPhoto",
                        data={
                            "chat_id": self.cfg.telegram_chat_id,
                            "caption": caption[:1024],
                            "parse_mode": "HTML",
                        },
                        files={"photo": handle},
                    )
            payload = response.json()
            if payload.get("ok"):
                return True
            raise RuntimeError(payload.get("description"))
        except Exception as exc:  # noqa: BLE001 - a photo is optional
            log.warning("telegram sendPhoto failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # notifications
    # ------------------------------------------------------------------
    async def startup_notice(
        self, *, history_note: str = "", profile_note: str = ""
    ) -> None:
        """Send the welcome card only on the first start or for real news.

        A restart used to produce another "Online" card even when nothing had
        changed. The daily summary is the normal heartbeat; the welcome is
        reserved for the first run and for imported history or profile
        warnings that need attention.
        """
        store = self._store
        already_sent = bool(
            store is not None and store.get_meta("startup_notice_sent", "")
        )
        if already_sent and not (history_note or profile_note):
            log.info("startup notice suppressed (already sent; no new warnings)")
            return

        if already_sent:
            # A restart with news should not repeat the welcome. Send only the
            # actionable lines.
            lines = []
            if history_note:
                lines.append(f"\U0001f4e5 {esc(history_note)}")
            if profile_note:
                lines.append(f"\u26a0\ufe0f {esc(profile_note)}")
            await self.send("\n".join(lines), keyboard=self.main_keyboard())
            return

        cfg = self.cfg
        cap = f"{cfg.max_apps_per_day}/day" if cfg.max_apps_per_day else "no cap"
        lines = [
            "\U0001f7e2 <b>Wellfound Bot is online</b>",
            f"{self._cadence_text()} \u00b7 {len(cfg.roles)} roles \u00b7 {cap}",
            f"{cfg.freshness_label} freshness \u00b7 "
            f"{cfg.company_size_min}-{cfg.company_size_max} employees \u00b7 "
            f"{'paid only' if cfg.skip_unpaid else 'paid + unpaid'}",
        ]
        if history_note:
            lines.append(f"\U0001f4e5 {esc(history_note)}")
        if profile_note:
            lines.append(f"\u26a0\ufe0f {esc(profile_note)}")
        lines.extend(["", "Tap a button below when you need it."])
        await self.send("\n".join(lines), keyboard=self.main_keyboard())
        if store is not None:
            store.set_meta("startup_notice_sent", "1")

    async def run_started(self) -> None:
        # No chat message here on purpose: a "started" ping every daily pass
        # was pure noise. /info is the live view.
        self.state.running = True
        self.state.run_started_at = datetime.now().astimezone()

    async def outcome(self, status: str, job, detail: str = "") -> None:
        """One message per application that actually matters.

        Skips (size, location, closed, already applied) are counted in the run
        card instead of being messaged one by one, so the chat stays readable.
        """
        if status not in NOTIFY_STATUSES:
            return

        icon = Status.ICONS.get(status, "\U0001f4e6")
        label = Status.LABELS.get(status, status)
        company = esc(job.company or "Unknown company")
        if status == Status.APPLIED:
            header = f"\u2705 <b>Applied</b> \u2014 {company}"
        else:
            header = f"{icon} <b>{esc(label)}</b> \u2014 {company}"

        lines = [header, f"<i>{esc(self._title(job.title, job.company))}</i>"]

        facts = []
        if job.remote:
            facts.append("Remote")
        elif job.location:
            facts.append(esc(job.location[:60]))
        if job.company_size:
            facts.append(f"{esc(job.company_size)} employees")
        if getattr(job, "job_type", ""):
            facts.append(esc(job.job_type))
        if getattr(job, "stipend", ""):
            facts.append(esc(job.stipend))
        posted_days = getattr(job, "posted_days", None)
        if posted_days is not None:
            facts.append(f"posted {format_age(posted_days)} ago")
        if facts:
            lines.append(" \u00b7 ".join(facts))

        lines.append(f"\U0001f517 {self._link(str(job.url), 'Open job')}")
        if detail and status != Status.APPLIED:
            lines.append(f"<i>{esc(detail)}</i>")
        text = "\n".join(lines)
        proof = getattr(job, "proof_path", "")
        if status == Status.APPLIED and proof:
            if await self.send_photo(proof, text):
                return
        await self.send(text, preview=True)

    async def run_finished(self, summary, next_run: datetime | None) -> None:
        self.state.running = False
        self.state.run_started_at = None
        if next_run:
            self.state.next_run_at = next_run

        # A quiet daily pass (nothing applied, nothing broken) sends nothing:
        # the single evening summary is the heartbeat.
        if not (summary.applied or summary.failures or summary.errors):
            log.info("run finished quietly: %s", summary.one_line())
            return

        lines = [
            f"\U0001f3c1 <b>Run finished</b> \u00b7 {summary.duration}",
            "",
            summary.one_line(),
        ]
        if summary.errors:
            lines.append("")
        for err in summary.errors[:3]:
            lines.append(f"\u26a0\ufe0f <i>{esc(err)}</i>")
        if summary.stopped_reason:
            lines.append(f"<i>{esc(summary.stopped_reason)}</i>")
        when = self._clock(next_run) if next_run else "n/a"
        lines.extend(["", f"Next run \u00b7 <b>{when}</b>"])
        await self.send("\n".join(lines))

    async def error(self, where: str, exc: BaseException) -> None:
        detail = f"{type(exc).__name__}: {exc}"
        await self.send(
            f"\u26a0\ufe0f <b>Error</b> during <i>{esc(where)}</i>\n<code>{esc(detail)}</code>"
        )

    async def challenge(self, where: str, url: str, wait_seconds: int) -> None:
        await self.send(
            f"\U0001f6d1 <b>Anti-bot check detected</b> ({esc(where)})\n"
            f"<i>{esc(url)}</i>\n"
            f"Solve it in the open browser window - I will wait up to "
            f"{wait_seconds}s, then pause the schedule."
        )

    async def challenge_cleared(self) -> None:
        await self.send("\u2705 Check cleared - continuing.")

    async def paused_automatically(self, reason: str) -> None:
        await self.send(
            f"\u23f8\ufe0f <b>Bot paused</b>: {esc(reason)}\n"
            f"Tap \u25b6\ufe0f Resume when you want it to continue.",
            keyboard=self.main_keyboard(),
        )

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    async def run_updates(self) -> None:
        """Long-poll getUpdates forever. Only the configured chat is obeyed."""
        while True:
            try:
                updates = await self._api(
                    "getUpdates",
                    offset=self._offset,
                    timeout=25,
                    # Telegram expects a JSON-serialized array; httpx would
                    # otherwise encode a Python list as a bare string.
                    allowed_updates='["message","callback_query"]',
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("getUpdates failed: %s", exc)
                await asyncio.sleep(3)
                continue
            for update in updates:
                self._offset = update["update_id"] + 1
                callback = update.get("callback_query")
                if callback:
                    try:
                        await self._handle_callback(callback)
                    except Exception:  # noqa: BLE001 - a button must not kill polling
                        log.exception("callback handling failed")
                        await self.send(
                            "\u26a0\ufe0f Button action failed - try Status or More again."
                        )
                    continue
                message = update.get("message") or {}
                chat_id = str((message.get("chat") or {}).get("id", ""))
                text = (message.get("text") or "").strip()
                if not text:
                    continue
                if chat_id != self.cfg.telegram_chat_id:
                    log.warning("ignoring command from unauthorized chat %s", chat_id)
                    continue

                # The persistent reply keyboard sends labels, not commands.
                # Handle those before the question flow so tapping Pause while
                # a form is waiting does not accidentally submit "Pause".
                try:
                    if await self._handle_main_button(text):
                        continue
                except Exception:  # noqa: BLE001 - a button must not kill polling
                    log.exception("main button handling failed")
                    await self.send(
                        "\u26a0\ufe0f Button action failed - try Status or More again."
                    )
                    continue

                pending = self._pending_question
                if pending is not None and not pending.done():
                    if text.lower() in {"skip", "/skip", "cancel", "/cancel"}:
                        pending.set_result("skip")
                        await self.send("\u23ed\ufe0f Skipping this application.")
                        continue
                    if not text.startswith("/"):
                        pending.set_result(text)
                        continue

                await self._dispatch(text)

    async def _dispatch(self, text: str) -> None:
        name, _, arg = text.partition(" ")
        # Handle "/status@MyBot" the same as "/status" (group chats add the suffix).
        name = name.lstrip("/").split("@", 1)[0].lower()
        handler = self._commands.get(name)
        if handler is None:
            if text.lstrip().startswith("/"):
                await self.send(
                    f"Unknown command <code>/{esc(name)}</code>. "
                    "Tap a button or try /help"
                )
            else:
                await self.send("Tap a button below, or use /help.")
            return
        await self._typing()
        try:
            reply = await handler(arg.strip())
        except Exception as exc:  # noqa: BLE001 - a command must never kill the loop
            log.exception("command /%s failed", name)
            reply = f"\u274c Command failed: {esc(f'{type(exc).__name__}: {exc}')}"
        if reply:
            # Attach the reply keyboard after commands that either need it or
            # change the Pause/Resume label.
            keyboard = self.main_keyboard() if name in {
                "start", "help", "status", "pause", "resume", "run"
            } else None
            await self.send(reply, keyboard=keyboard)

    # -- the actual commands (registered by app.py) -----------------------
    async def cmd_start(self, arg: str) -> str:
        return START_TEXT

    async def cmd_help(self, arg: str) -> str:
        return HELP_TEXT

    async def cmd_status(self, arg: str) -> str:
        return self._panel_main()[0]

    async def cmd_last_applied(self, arg: str) -> str:
        try:
            limit = max(1, min(20, int(arg) if arg else 5))
        except ValueError:
            limit = 5
        return self._panel_apps(limit)[0]

    async def cmd_errors(self, arg: str) -> str:
        return self._panel_issues()[0]

    async def cmd_sync(self, arg: str) -> str:
        wf = self._wf
        if wf is None:
            return "Browser is not ready yet; try again after the bot has started."
        if self.state.running:
            return "A run is in progress. Try Sync again when it finishes."
        try:
            result = await self._typing_for(wf.sync_applications())
        except Exception as exc:  # noqa: BLE001 - report it, do not kill the bot
            log.exception("application sync failed")
            return f"\u274c Sync failed: <i>{esc(exc)}</i>"
        return (
            "\U0001f4e5 <b>Synced</b>\n"
            f"{result['found']} seen \u00b7 {result['imported']} imported \u00b7 "
            f"{result['updated']} updated"
        )

    async def cmd_responses(self, arg: str) -> str:
        counts = self._store.response_counts()
        if not counts:
            return "\U0001f4ec <b>Responses</b>\nNothing recorded yet - tap Sync."
        order = {"pending": 0, "accepted": 1, "not accepted": 2}
        parts = [
            f"{esc(key.title())} <b>{count}</b>"
            for key, count in sorted(counts.items(), key=lambda item: order.get(item[0], 9))
        ]
        return "\U0001f4ec <b>Responses</b>\n" + " \u00b7 ".join(parts)

    async def cmd_answers(self, arg: str) -> str:
        rows = self._store.list_answers()
        if not rows:
            return (
                "No saved answers yet. When a form asks something reusable "
                "(LinkedIn, GitHub, years of experience), answer once here and "
                "I will fill it automatically next time."
            )
        lines = ["\U0001f4da <b>Saved answers</b>"]
        for row in rows:
            answer = str(row["answer"])
            if len(answer) > 120:
                answer = answer[:117] + "..."
            lines.append(f"<b>#{row['id']}</b> {esc(row['question'])}")
            lines.append(f"  \u2192 {esc(answer)}")
        lines.append("")
        lines.append("Remove one with <code>/forget &lt;id&gt;</code>.")
        return "\n".join(lines)

    async def cmd_forget(self, arg: str) -> str:
        try:
            answer_id = int((arg or "").strip())
        except ValueError:
            return "Usage: <code>/forget 3</code> (see /answers for the id)."
        if self._store.forget_answer(answer_id):
            return f"\U0001f5d1\ufe0f Forgot saved answer #{answer_id}."
        return f"No saved answer #{answer_id}."

    async def cmd_report(self, arg: str) -> str:
        data = self._store.report(
            goal=self.cfg.apply_goal,
            deadline=self.cfg.goal_deadline,
            daily_cap=self.cfg.max_apps_per_day,
        )
        return self._format_report(data)

    def _format_report(self, d: dict) -> str:
        now: datetime = d["now"]
        today = d["today"]
        last_48h = d["last_48h"]
        week = d["week"]
        all_time = d["all"]
        counts = all_time["counts"]

        today_line = f"Today: <b>{today['applied']}</b>"
        if d.get("daily_cap"):
            today_line += f" / {d['daily_cap']} daily cap"
        lines = [
            "\U0001f4ca <b>WELLFOUND REPORT</b>",
            f"<i>{now:%a %d %b %Y}, {self._clock(now)}</i>",
            "",
            "\u2705 <b>Applications submitted</b>",
            today_line,
            f"Last 48 hours: <b>{last_48h['applied']}</b>",
            f"Last 7 days: <b>{week['applied']}</b>",
            f"All time: <b>{all_time['applied']}</b>",
        ]
        if d.get("streak"):
            lines.append(
                f"Current streak: <b>{d['streak']} day{'s' if d['streak'] != 1 else ''}</b>"
            )
        lines.append("")

        goal = d["goal"]
        done = all_time["applied"]
        lines.append("\U0001f3af <b>Goal</b>")
        if goal > 0:
            pct = min(100.0, done / goal * 100)
            remaining = max(0, goal - done)
            filled = min(12, int(round(pct / 100 * 12)))
            bar = "\u2588" * filled + "\u2591" * (12 - filled)
            lines.append(f"<b>{done} / {goal}</b> applied ({pct:.0f}%)")
            lines.append(bar)
            if done >= goal:
                lines.append("\U0001f389 <b>Goal complete!</b> Set a new one: /goal 200")
            else:
                lines.append(f"Remaining: <b>{remaining}</b>")
                if d["days_left"] is not None:
                    if d["days_left"] <= 0:
                        lines.append("Deadline passed - set a new one: /goal N YYYY-MM-DD")
                    else:
                        lines.append(
                            f"By <b>{d['deadline']}</b> ({d['days_left']} days left) "
                            f"you need <b>{d['required_per_day']:.1f}/day</b>"
                        )
                if d["avg_per_day"]:
                    lines.append(f"Your pace: <b>{d['avg_per_day']:.1f}/day</b>")
                if d["projected_date"]:
                    lines.append(
                        f"At this pace: around <b>{d['projected_date']:%d %b %Y}</b>"
                    )
        else:
            lines.append("No goal set. Try <code>/goal 100 2026-12-31</code>")
        lines.append("")

        roles_all = d["roles"]["all"]
        if roles_all:
            roles_week = d["roles"]["week"]
            lines.append("\U0001f9e9 <b>By role</b> (all time \u00b7 last 7 days)")
            for key, count in list(roles_all.items())[:8]:
                lines.append(
                    f"\u2022 {esc(self._role_label(key))}: <b>{count}</b> "
                    f"(7d: {roles_week.get(key, 0)})"
                )
            lines.append("")

        lines.append("\U0001f4c8 <b>Last 7 days</b>")
        peak = max([day["count"] for day in d["daily"]] + [1])
        for day in d["daily"]:
            width = int(round(day["count"] / peak * 8)) if day["count"] else 0
            bar = "\u2588" * width or "\u00b7"
            lines.append(f"{day['label']}: {bar} {day['count']}")
        best = d.get("best_day") or {}
        if best.get("count"):
            lines.append(
                f"<i>Best day: {best['label']} ({best['count']} applications)</i>"
            )
        lines.append("")

        lines.append("\U0001f9ea <b>Quality</b>")
        confirmed = int(d.get("confirmed", d["submitted"]))
        unconfirmed = int(d.get("unconfirmed", 0))
        lines.append(
            f"Confirmed: <b>{confirmed}</b> \u00b7 unverified: <b>{unconfirmed}</b> "
            f"\u00b7 confirmation rate: <b>{d['success_rate']:.0f}%</b>"
        )
        for status in (
            Status.UNVERIFIED,
            Status.NEEDS_REVIEW,
            Status.FAILED,
            Status.EXTERNAL,
        ):
            count = counts.get(status, 0)
            if count:
                lines.append(
                    f"{Status.ICONS.get(status, '')} {Status.LABELS.get(status, status)}: "
                    f"<b>{count}</b>"
                )
        responses = d.get("responses") or {}
        if responses:
            order = {"pending": 0, "accepted": 1, "not accepted": 2}
            parts = [
                f"{key.title()}: {count}"
                for key, count in sorted(
                    responses.items(), key=lambda item: order.get(item[0], 9)
                )
            ]
            lines.append("\U0001f4ec <b>Responses</b> " + esc(" \u00b7 ".join(parts)))
        already_applied = counts.get(Status.ALREADY_APPLIED, 0)
        if already_applied:
            lines.append(
                f"\U0001f4e5 History: <b>{already_applied}</b> application(s) "
                "already on Wellfound"
            )
        skips = (
            Status.SKIPPED_SIZE,
            Status.SKIPPED_LOCATION,
            Status.SKIPPED_CLOSED,
            Status.SKIPPED_AGE,
            Status.SKIPPED_PAY,
            Status.SKIPPED_COMPANY,
            Status.SKIPPED_MANUAL,
        )
        parts = []
        for status in skips:
            count = counts.get(status, 0)
            if count:
                label = (
                    Status.LABELS.get(status, status)
                    .replace("Skipped (", "")
                    .replace(")", "")
                )
                parts.append(f"{label}: {count}")
        if parts:
            lines.append("<i>Skipped \u2014 " + esc(" \u00b7 ".join(parts)) + "</i>")
        lines.append("")

        if d["top_companies"]:
            lines.append("\U0001f3e2 <b>Top companies</b>")
            lines.append(
                esc(", ".join(f"{name} ({count})" for name, count in d["top_companies"]))
            )
            lines.append("")

        if d["first_at"]:
            local_first = d["first_at"].astimezone()
            days = d["days_since_first"]
            lines.append(
                f"<i>Tracking since {local_first:%d %b %Y} \u00b7 "
                f"{days} day{'s' if days != 1 else ''}</i>"
            )
        return "\n".join(lines).strip()

    def _role_label(self, key: str) -> str:
        for role in self.cfg.roles:
            if role.key == key:
                return role.label
        return key.replace("_", " ").title()

    def _cadence_text(self) -> str:
        if self.cfg.daily_run_clock is not None:
            return f"daily at {self._clock_text(self.cfg.daily_run_at)} local"
        if self.cfg.interval_minutes <= 0:
            return (
                f"continuous (gap {self.cfg.continuous_gap_seconds}-"
                f"{self.cfg.continuous_gap_seconds * 2}s)"
            )
        return (
            f"every {self.cfg.interval_minutes} min "
            f"(\u00b1{self.cfg.interval_jitter_seconds}s jitter)"
        )

    @staticmethod
    def _parse_date(text: str):
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    async def cmd_goal(self, arg: str) -> str:
        parts = arg.split()
        if not parts:
            data = self._store.report(
                goal=self.cfg.apply_goal, deadline=self.cfg.goal_deadline
            )
            if data["goal"] <= 0:
                return (
                    "No goal set. Use <code>/goal 100 2026-12-31</code> "
                    "to set a target and deadline."
                )
            line = f"\U0001f3af Goal: <b>{data['all']['applied']} / {data['goal']}</b> applied"
            if data["deadline"]:
                line += f" by <b>{data['deadline']}</b>"
            if data["required_per_day"] is not None and data["days_left"] > 0:
                line += f" \u00b7 need <b>{data['required_per_day']:.1f}/day</b>"
            return line

        if parts[0].lower() in {"off", "clear", "none", "0"}:
            self._store.set_goal(0, "")
            return "\U0001f3af Goal cleared."

        try:
            goal = int(parts[0].replace(",", "").replace("_", ""))
            if goal <= 0:
                raise ValueError
        except ValueError:
            return "Usage: <code>/goal 100 [YYYY-MM-DD]</code> or <code>/goal off</code>"

        deadline = self._store.get_meta("goal_deadline", self.cfg.goal_deadline)
        if len(parts) > 1:
            parsed = self._parse_date(parts[1])
            if parsed is None:
                return "Deadline must look like <code>2026-12-31</code>."
            deadline = parsed.isoformat()

        self._store.set_goal(goal, deadline)
        data = self._store.report(goal=goal, deadline=deadline)
        lines = [f"\U0001f3af Goal set: <b>{goal}</b> applications"]
        if deadline:
            lines.append(f"Deadline: <b>{deadline}</b>")
        done = data["all"]["applied"]
        remaining = max(0, goal - done)
        lines.append(f"Already applied: <b>{done}</b> \u00b7 remaining: <b>{remaining}</b>")
        if done >= goal:
            lines.append("\U0001f389 You already passed it - set a bigger goal!")
        elif (
            data["required_per_day"] is not None
            and data["days_left"] is not None
            and data["days_left"] > 0
        ):
            lines.append(
                f"That needs <b>{data['required_per_day']:.1f}/day</b> for the "
                f"{data['days_left']} days left."
            )
        return "\n".join(lines)

    def _daily_summary(self) -> str:
        data = self._report_data()
        now: datetime = data["now"]
        today = data["today"]["applied"]
        week = data["week"]["applied"]
        confirmed = int(data.get("confirmed", data["submitted"]))
        rate = float(data.get("success_rate", 0.0))
        today_cap = (
            f"/{self.cfg.max_apps_per_day}" if self.cfg.max_apps_per_day else ""
        )
        lines = [
            f"\U0001f4ca <b>Daily summary</b> \u00b7 {now:%a %d %b}",
            f"Today <b>{today}{today_cap}</b> \u00b7 7d <b>{week}</b>",
            f"Confirmed <b>{confirmed}</b> \u00b7 {rate:.0f}%",
        ]
        responses = data.get("responses") or {}
        if responses:
            order = {"pending": 0, "accepted": 1, "not accepted": 2}
            parts = [
                f"{key.title()} {count}"
                for key, count in sorted(
                    responses.items(), key=lambda item: order.get(item[0], 9)
                )
            ]
            lines.append("\U0001f4ec " + esc(" \u00b7 ".join(parts)))
        goal = int(data.get("goal") or 0)
        if goal > 0:
            done = data["all"]["applied"]
            pct = min(100.0, done / goal * 100)
            lines.append(f"\U0001f3af <b>{done}/{goal}</b> \u00b7 {pct:.0f}%")
        return "\n".join(lines)

    async def daily_report_loop(self) -> None:
        """Send one compact daily summary at the configured local hour.

        The loop wakes every 30s and compares against the most recent scheduled
        slot that has not been sent yet, so a report is not missed if the bot
        starts a few minutes after the hour (or was busy applying).
        """
        while True:
            try:
                if self.cfg.daily_report and self._store is not None:
                    now = datetime.now().astimezone()
                    due = None
                    for hour in sorted(self.cfg.daily_report_hours):
                        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                        if slot <= now:
                            due = f"{slot.date().isoformat()}T{hour:02d}"
                    if due and self._store.get_meta("last_daily_report_slot") != due:
                        self._store.set_meta("last_daily_report_slot", due)
                        await self.send(self._daily_summary())
            except Exception:  # noqa: BLE001 - a failed report must not kill the bot
                log.exception("scheduled report failed")
            await asyncio.sleep(30)

    @staticmethod
    def _local_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value)).astimezone()
        except ValueError:
            return None

    async def cmd_info(self, arg: str) -> str:
        cfg = self.cfg
        store = self._store
        state = self.state
        now = datetime.now().astimezone()

        lines = [
            "\u2139\ufe0f <b>BOT INFO</b>",
            f"<i>{now:%a %d %b %Y}, {self._clock(now, seconds=True)}</i>",
            "",
        ]

        if state.running:
            status = "\U0001f7e2 running an application pass now"
        elif store.paused:
            status = "\u23f8\ufe0f paused"
        else:
            status = "\u26aa idle"
        lines.append(f"State: <b>{status}</b>")
        lines.append(
            f"Bot up since: <b>{self._stamp(state.started_at.astimezone())}</b>"
        )

        lines.append("")
        lines.append("\U0001f553 <b>Last run</b>")
        last = store.last_run()
        if last:
            started = self._local_dt(last["started_at"])
            finished = self._local_dt(last["finished_at"])
            lines.append(
                f"Started: <b>{self._stamp(started, seconds=True)}</b>"
                if started
                else "Started: -"
            )
            if finished:
                lines.append(
                    f"Finished: <b>{self._stamp(finished, seconds=True)}</b>"
                )
                if started:
                    seconds = int((finished - started).total_seconds())
                    lines.append(f"Duration: <b>{seconds // 60}m {seconds % 60:02d}s</b>")
            lines.append(
                f"Result: <b>{last['applied']}</b> applied \u00b7 "
                f"<b>{last['failed']}</b> failed \u00b7 "
                f"{last['attempted']} attempted of {last['candidates']} candidates"
            )
            if last["note"]:
                lines.append(f"Note: <i>{esc(last['note'])}</i>")
        else:
            lines.append("No run has finished yet.")

        lines.append("")
        lines.append("\u23ed\ufe0f <b>Next run</b>")
        if state.running:
            lines.append("A run is in progress right now.")
        if state.next_run_at:
            delta = max(0, int((state.next_run_at - now).total_seconds()))
            lines.append(
                f"At: <b>{self._stamp(state.next_run_at, seconds=True)}</b>"
            )
            lines.append(f"In: <b>{delta // 3600}h {delta % 3600 // 60}m {delta % 60:02d}s</b>")
        elif store.paused:
            lines.append("Paused - no run will start until /resume.")
        elif not state.running:
            lines.append("Not scheduled yet (starting up or just between runs).")

        lines.append("")
        lines.append("\u2699\ufe0f <b>Schedule</b>")
        lines.append(
            f"Mode: <b>{self._cadence_text()}</b>"
        )
        report_hours = ", ".join(f"{hour:02d}:00" for hour in cfg.daily_report_hours)
        lines.append(
            f"Reports at <b>{report_hours}</b> local "
            f"({'on' if cfg.daily_report else 'off'})"
        )
        run_cap = cfg.max_apps_per_run or "unlimited"
        day_cap = cfg.max_apps_per_day or "unlimited"
        lines.append(
            f"Max <b>{run_cap}</b>/run \u00b7 <b>{day_cap}</b>/day "
            f"\u00b7 freshness {cfg.freshness_label} \u00b7 backoff \u00d7"
            f"{state.backoff_multiplier:g}"
        )
        freshness = (
            f"newest first, posted \u2264 <b>{cfg.freshness_label}</b> ago"
            if cfg.max_job_age_days > 0
            else "newest first, any age"
        )
        lines.append(f"Freshness: {freshness}")
        lines.append(
            f"Company cooldown: <b>{cfg.company_cooldown_days} days</b> \u00b7 "
            f"failed retries: <b>{cfg.max_retries}</b>"
        )
        lines.append(
            f"History sync: <b>{'on' if cfg.reconcile_applications else 'off'}</b> "
            f"\u00b7 profile preflight: <b>{'on' if cfg.profile_preflight else 'off'}</b>"
        )
        return "\n".join(lines)

    async def cmd_roles(self, arg: str) -> str:
        cfg = self.cfg
        lines = ["\U0001f3af <b>Configured roles</b>"]
        for role in cfg.roles:
            lines.append(f"\u2022 {esc(role.label)}")
        lines.append("")
        lines.append(f"\U0001f4cd <b>Location filter</b>")
        lines.append(
            "Remote: <b>{}</b> ({}) \u00b7 onsite cities: <b>{}</b>".format(
                "yes" if cfg.accept_remote else "no",
                esc(", ".join(cfg.accept_remote_regions)) if cfg.accept_remote else "-",
                esc(", ".join(cfg.accept_locations)) or "none",
            )
        )
        lines.append("")
        lines.append("\U0001f3e2 <b>Company size</b>")
        if cfg.allow_size_fallback:
            lines.append(
                f"Apply: <b>{cfg.company_size_min}-{cfg.company_size_max} employees</b> \u00b7 "
                f"fallback: <b>{cfg.company_size_max + 1}-{cfg.company_size_fallback_max}</b> (on)"
            )
        else:
            lines.append(
                f"Apply: <b>{cfg.company_size_min}-{cfg.company_size_max} employees only</b> "
                "(larger companies skipped)"
            )
        lines.append(
            f"Applications: <b>{cfg.max_apps_per_run or 'unlimited'}</b>/run "
            f"\u00b7 <b>{cfg.max_apps_per_day or 'unlimited'}</b>/day "
            f"\u00b7 fresh window <b>{cfg.freshness_label}</b>"
        )
        lines.append(
            f"Same-company cooldown: <b>{cfg.company_cooldown_days} days</b> "
            f"\u00b7 failed retries: <b>{cfg.max_retries}</b>"
        )
        return "\n".join(lines)

    async def cmd_pause(self, arg: str) -> str:
        self._store.set_paused(True)
        return "\u23f8\ufe0f <b>Paused</b>\nTap \u25b6\ufe0f Resume when you want to continue."

    async def cmd_resume(self, arg: str) -> str:
        self._store.set_paused(False)
        self._store.set_consecutive_failures(0)
        return "\u25b6\ufe0f <b>Resumed</b>\nFailure backoff cleared."

    async def cmd_run(self, arg: str) -> str:
        if self.state.running:
            return "\u26a1 A run is already in progress."
        if self._store.paused:
            return "\u23f8\ufe0f Paused - tap \u25b6\ufe0f Resume first."
        assert self._trigger_run is not None
        self._trigger_run.set()
        return "\U0001f680 <b>Run triggered</b>\nWatch for updates."

    async def cmd_cancel(self, arg: str) -> str:
        pending = self._pending_question
        if pending is not None and not pending.done():
            pending.set_result("skip")
            return "\u23ed\ufe0f Cancelled - skipping this application."
        return "Nothing is waiting for an answer right now."

    async def close(self) -> None:
        pending = self._pending_question
        if pending is not None and not pending.done():
            pending.set_result("skip")
        client, self._http = self._http, None
        self._http_loop = None
        if client is None:
            return
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 - the loop may already be gone
            log.debug("telegram client close failed: %s", exc)

"""Local FastAPI UI for the Apollo email tool."""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import settings
from .db import Database
from .excel_io import InputError, normalize_domain, read_input
from .runner import RunAlreadyActive, RunManager

app = FastAPI(title="Apollo Email Finder", docs_url=None, redoc_url=None)
db = Database(settings.db_path)
manager = RunManager(settings, db)

TEMPLATES = Path(__file__).resolve().parent / "templates"
STATIC = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# Shared by the Telegram bot and the little desktop window.  This is only an
# in-memory, local-only activity snapshot; it is not used by the ledger.
last_upload: dict[str, Any] | None = None


class StartRequest(BaseModel):
    path: str
    limit: int = Field(default=5, ge=0, le=100)
    dry_run: bool = False
    hide_browser: bool = True


class ResetRequest(BaseModel):
    domain: str = Field(min_length=1, max_length=253)
    confirm: str = Field(default="")


def _safe_filename(name: str) -> str:
    name = Path(name or "input.xlsx").name
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name or "input.xlsx"


def _allowed_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = settings.root / path
    path = path.resolve()
    allowed_roots = [settings.uploads_dir.resolve(), settings.imports_dir.resolve()]
    if not any(root == path.parent or root in path.parents for root in allowed_roots):
        raise HTTPException(status_code=400, detail="File must be inside the imports/uploads folder")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Input file not found")
    return path


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(TEMPLATES / "index.html")


@app.get("/desktop")
async def desktop() -> FileResponse:
    return FileResponse(TEMPLATES / "desktop.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "profile_dir": str(settings.profile_dir),
        "db_path": str(settings.db_path),
    }


@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    source: str = Form("desktop"),
) -> dict[str, Any]:
    global last_upload
    filename = _safe_filename(file.filename or "input.xlsx")
    source = "telegram" if source == "telegram" else "desktop"
    started_at = time.time()
    last_upload = {
        "filename": filename,
        "source": source,
        "status": "uploading",
        "size": 0,
        "started_at": started_at,
        "uploaded_at": started_at,
    }

    def remember_error(message: str) -> None:
        global last_upload
        last_upload = {
            **(last_upload or {}),
            "filename": filename,
            "source": source,
            "status": "error",
            "error": message,
            "uploaded_at": time.time(),
        }

    suffix = Path(filename).suffix.lower()
    if suffix not in {".xlsx", ".xlsm", ".csv", ".json"}:
        remember_error("Upload an .xlsx, .xlsm, .csv or .json file")
        raise HTTPException(status_code=400, detail="Upload an .xlsx, .xlsm, .csv or .json file")
    data = await file.read()
    if last_upload is not None:
        last_upload["size"] = len(data)
    if len(data) > MAX_UPLOAD_BYTES:
        remember_error("File is larger than 25 MB")
        raise HTTPException(status_code=400, detail="File is larger than 25 MB")
    if not data:
        remember_error("File is empty")
        raise HTTPException(status_code=400, detail="File is empty")

    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = settings.uploads_dir / f"{stamp}_{filename}"
    counter = 1
    while target.exists():
        target = settings.uploads_dir / f"{stamp}_{counter}_{filename}"
        counter += 1
    try:
        target.write_bytes(data)
    except OSError as exc:
        remember_error(f"Could not save the uploaded file: {exc}")
        raise HTTPException(status_code=500, detail="Could not save the uploaded file") from exc
    try:
        workbook = await asyncio.to_thread(read_input, target)
    except InputError as exc:
        target.unlink(missing_ok=True)
        remember_error(str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    domains = [row.domain for row in workbook.rows if row.domain]
    duplicates = len(domains) - len(set(domains))
    missing = sum(1 for row in workbook.rows if not row.domain)
    preview = []
    for row in workbook.rows[:6]:
        preview.append(
            {
                "company_name": row.company_name,
                "domain": row.domain,
                "team_size": str(row.data.get("team_size") or ""),
                "niche": str(row.data.get("niche") or ""),
            }
        )
    result = {
        "path": str(target),
        "filename": filename,
        "source": source,
        "status": "ready",
        "size": len(data),
        "started_at": started_at,
        "uploaded_at": time.time(),
        "sheet": workbook.sheet_name,
        "rows": len(workbook.rows),
        "unique_domains": len(set(domains)),
        "duplicates": duplicates,
        "missing_domains": missing,
        "columns": workbook.headers,
        "preview": preview,
    }
    last_upload = result
    return result


@app.post("/api/dismiss-upload")
async def dismiss_upload() -> dict[str, Any]:
    global last_upload
    if manager.running:
        raise HTTPException(status_code=409, detail="Stop the current run first")
    last_upload = None
    return {"ok": True}


@app.post("/api/start")
async def start(request: StartRequest) -> dict[str, Any]:
    path = _allowed_path(request.path)
    try:
        return await manager.start(
            path, request.limit, request.dry_run, request.hide_browser
        )
    except RunAlreadyActive as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/status")
async def status() -> dict[str, Any]:
    data = manager.status()
    data["last_upload"] = last_upload
    return data


@app.post("/api/stop")
async def stop() -> dict[str, Any]:
    return await manager.stop()


@app.post("/api/reset")
async def reset_company(request: ResetRequest) -> dict[str, Any]:
    if request.confirm.strip().upper() != "RESET":
        raise HTTPException(status_code=400, detail="Reset was not confirmed")
    if manager.running:
        raise HTTPException(status_code=409, detail="Stop the current run before resetting a company")
    domain = normalize_domain(request.domain)
    if not domain or "." not in domain:
        raise HTTPException(status_code=400, detail="Enter a valid domain, for example linear.app")
    removed = await asyncio.to_thread(db.reset_domain, domain)
    manager.state.last_error = ""
    manager._log(
        f"Manual reset for {domain} ({'ledger row deleted' if removed else 'no ledger row found'})"
    )
    return {"ok": True, "domain": domain, "removed": removed}


@app.post("/api/close-browser")
async def close_browser() -> dict[str, Any]:
    await manager.close_browser()
    return {"ok": True}


@app.post("/api/show-browser")
async def show_browser() -> dict[str, Any]:
    if not await manager.show_browser():
        raise HTTPException(status_code=409, detail="Apollo browser is not open")
    return {"ok": True, "hidden": manager.browser.window_hidden}


@app.post("/api/reveal-output")
async def reveal_output() -> dict[str, Any]:
    raw = manager.state.output_path
    if not raw:
        raise HTTPException(status_code=404, detail="No output file yet")
    path = Path(raw).resolve()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Output file not found")
    if settings.exports_dir.resolve() not in path.parents:
        raise HTTPException(status_code=400, detail="Output is not in the exports folder")
    try:
        if os.name == "nt":
            os.startfile(str(path.parent))  # type: ignore[attr-defined]
        else:
            import subprocess

            subprocess.Popen(["xdg-open", str(path.parent)])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Could not open folder: {exc}") from exc
    return {"ok": True}

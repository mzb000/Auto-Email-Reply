from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, List

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette import status

from auto_reply import AutoReplyService
from config import (
    AUTO_REPLY_POLL_INTERVAL,
    AUTO_REPLY_SEARCH_CRITERIA,
    BATCH_SIZE,
    DELAY,
    EMAIL,
)
from send_emails import EmailTemplate, send_bulk_emails

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates_web"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Email Automation Control Center")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

auto_reply_service = AutoReplyService()
app.state.last_send_log: List[str] = []
app.state.auto_reply_log: List[str] = []
app.state.flash_message: dict | None = None


def _push_log(target: List[str], message: str, prefix: str | None = None) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {prefix + ' ' if prefix else ''}{message}"
    target.append(line)
    if len(target) > 200:
        del target[: len(target) - 200]


def _set_flash(text: str, level: str = "info") -> None:
    app.state.flash_message = {"text": text, "level": level}


def _consume_flash() -> dict | None:
    message = app.state.flash_message
    app.state.flash_message = None
    return message


def _redirect_home() -> RedirectResponse:
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/")
async def dashboard(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user_email": EMAIL,
            "default_delay": DELAY,
            "default_batch_size": BATCH_SIZE,
            "auto_reply_running": auto_reply_service.is_running,
            "auto_reply_poll_interval": AUTO_REPLY_POLL_INTERVAL,
            "auto_reply_search": AUTO_REPLY_SEARCH_CRITERIA,
            "send_log": list(app.state.last_send_log),
            "auto_reply_log": list(app.state.auto_reply_log),
            "flash": _consume_flash(),
        },
    )


@app.post("/send")
async def send_campaign(
    recipients_text: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    delay: str | None = Form(None),
    batch_size: str | None = Form(None),
    dry_run: bool = Form(False),
):
    recipients = [line.strip() for line in recipients_text.splitlines() if line.strip()]
    if not recipients:
        _set_flash("Please provide at least one recipient email address.", "error")
        return _redirect_home()

    try:
        subject_value = subject.strip()
        body_value = body.strip()
        template = EmailTemplate(subject=subject_value, body=body_value)

        delay_value = float(delay) if delay not in (None, "") else None
        batch_value = int(batch_size) if batch_size not in (None, "") else None
    except ValueError as exc:
        _set_flash(f"Invalid numeric value: {exc}", "error")
        return _redirect_home()

    send_log: List[str] = []

    def capture(message: str) -> None:
        _push_log(send_log, message, prefix="Campaign")

    try:
        send_bulk_emails(
            recipients,
            template=template,
            delay=delay_value,
            batch_size=batch_value,
            dry_run=dry_run,
            status_callback=capture,
        )
        if dry_run:
            _set_flash("Dry-run completed successfully.", "success")
        else:
            _set_flash("Emails sent successfully.", "success")
    except Exception as exc:  # pragma: no cover - runtime safety
        _push_log(send_log, f"Error: {exc}", prefix="Campaign")
        _set_flash(f"Failed to send campaign: {exc}", "error")
    finally:
        app.state.last_send_log = send_log

    return _redirect_home()


@app.post("/auto-reply/start")
async def start_auto_reply():
    if auto_reply_service.is_running:
        _set_flash("Auto-reply is already running.", "info")
        return _redirect_home()

    def capture(message: str) -> None:
        _push_log(app.state.auto_reply_log, message, prefix="AutoReply")

    auto_reply_service.start(status_callback=capture)
    _set_flash("Auto-reply service started.", "success")
    return _redirect_home()


@app.post("/auto-reply/stop")
async def stop_auto_reply():
    if not auto_reply_service.is_running:
        _set_flash("Auto-reply is not running.", "info")
        return _redirect_home()

    auto_reply_service.stop()
    _set_flash("Auto-reply service stopped.", "success")
    return _redirect_home()

from __future__ import annotations

from datetime import datetime
from email.utils import parseaddr
import json
from pathlib import Path
import re
from threading import Lock
from typing import Callable, List

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader
from starlette import status

from auto_reply import AutoReplyService
from config import (
    AUTO_REPLY_POLL_INTERVAL,
    AUTO_REPLY_SEARCH_CRITERIA,
    BATCH_SIZE,
    DELAY,
    EMAIL,
)
from mailbox import get_mailbox_snapshot
from send_emails import EmailTemplate, send_bulk_emails

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates_web"
STATIC_DIR = BASE_DIR / "static"
DRAFT_STORE_PATH = BASE_DIR / "saved_emails.json"

app = FastAPI(title="Email Automation Control Center")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
from jinja2 import Environment, FileSystemLoader

import json as _json

_jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    auto_reload=True,
    cache_size=0,  # disable cache — workaround for Python 3.14 + Jinja2 3.1.x
)
_jinja_env.filters["tojson"] = lambda v: _json.dumps(v, ensure_ascii=False)


def _render(name: str, context: dict) -> HTMLResponse:
    tmpl = _jinja_env.get_template(name)
    return HTMLResponse(tmpl.render(**context))

auto_reply_service = AutoReplyService()
app.state.last_send_log: List[str] = []
app.state.auto_reply_log: List[str] = []
app.state.flash_message: dict | None = None
app.state.saved_drafts: list[dict] = []
app.state.draft_lock = Lock()


def _load_saved_drafts() -> list[dict]:
    if not DRAFT_STORE_PATH.exists():
        return []
    try:
        data = json.loads(DRAFT_STORE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, list):
        cleaned: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            cleaned.append(
                {
                    "signature": str(item.get("signature") or ""),
                    "subject": str(item.get("subject") or ""),
                    "body": str(item.get("body") or ""),
                    "updated_at": str(item.get("updated_at") or ""),
                }
            )
        return cleaned[:30]
    return []


def _persist_saved_drafts() -> None:
    try:
        DRAFT_STORE_PATH.write_text(
            json.dumps(app.state.saved_drafts, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return


def _normalize_text(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def _draft_signature(subject: str, body: str) -> str:
    return _normalize_text(subject)[:120] + "|" + _normalize_text(body)[:400]


def _save_draft(subject: str, body: str) -> None:
    subject_value = (subject or "").strip()
    body_value = (body or "").strip()
    if not subject_value and not body_value:
        return

    signature = _draft_signature(subject_value, body_value)
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    with app.state.draft_lock:
        for item in app.state.saved_drafts:
            if item.get("signature") == signature:
                item.update({"subject": subject_value, "body": body_value, "updated_at": now_iso})
                _persist_saved_drafts()
                return

        app.state.saved_drafts.insert(
            0,
            {"signature": signature, "subject": subject_value, "body": body_value, "updated_at": now_iso},
        )
        if len(app.state.saved_drafts) > 30:
            del app.state.saved_drafts[30:]
        _persist_saved_drafts()


app.state.saved_drafts = _load_saved_drafts()


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


_EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)


def _extract_recipients(raw_text: str) -> tuple[list[str], list[str]]:
    if not raw_text:
        return [], []

    separators_normalized = (
        raw_text.replace(";", ",")
        .replace("\t", ",")
        .replace("\r", "\n")
    )

    candidates: list[str] = []
    for line in separators_normalized.splitlines():
        for token in line.split(","):
            cleaned = token.strip()
            if cleaned:
                candidates.append(cleaned)

    valid: list[str] = []
    invalid: list[str] = []
    seen = set()
    for item in candidates:
        _, addr = parseaddr(item)
        addr = (addr or "").strip()
        if addr and _EMAIL_RE.fullmatch(addr):
            if addr not in seen:
                valid.append(addr)
                seen.add(addr)
        else:
            invalid.append(item)

    return valid, invalid


@app.get("/")
async def dashboard(request: Request):
    return _render(
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
            "mailbox": get_mailbox_snapshot(),
            "saved_drafts": list(app.state.saved_drafts),
        },
    )


@app.post("/draft/save")
async def save_draft(subject: str = Form(""), body: str = Form("")):
    _save_draft(subject, body)
    return {"ok": True}


@app.get("/draft/suggest")
async def suggest_draft(q: str = ""):
    query = _normalize_text(q)
    if not query:
        return {"items": []}

    def score_item(item: dict) -> float:
        subject = str(item.get("subject", ""))
        body = str(item.get("body", ""))
        haystack = _normalize_text(subject + " " + body)
        if not haystack:
            return 0.0

        score = 0.0
        if query in haystack:
            score += 2.0

        query_tokens = {t for t in query.split() if t}
        hay_tokens = {t for t in haystack.split() if t}
        if query_tokens and hay_tokens:
            overlap = len(query_tokens & hay_tokens) / max(1, len(query_tokens))
            score += overlap

        q = query[:200]
        h = haystack[:800]
        if q and h:
            score += (len(set(q) & set(h)) / max(1, len(set(q)))) * 0.5

        return score

    with app.state.draft_lock:
        scored = [
            (score_item(item), item)
            for item in app.state.saved_drafts
        ]

    scored.sort(key=lambda pair: pair[0], reverse=True)
    results: list[dict] = []
    for score, item in scored:
        if score <= 0:
            continue
        results.append(
            {
                "subject": item.get("subject", ""),
                "body": item.get("body", ""),
                "updated_at": item.get("updated_at"),
                "score": score,
            }
        )
        if len(results) >= 5:
            break
    return {"items": results}


@app.post("/send")
async def send_campaign(
    recipients_text: str | None = Form(None),
    attachments: List[UploadFile] | None = File(None),
    subject: str = Form(...),
    body: str = Form(...),
    delay: str | None = Form(None),
    batch_size: str | None = Form(None),
    dry_run: bool = Form(False),
):
    recipients: List[str] = []
    invalid: List[str] = []

    if recipients_text:
        valid_text, invalid_text = _extract_recipients(recipients_text)
        recipients.extend(valid_text)
        invalid.extend(invalid_text)

    seen = set()
    recipients = [r for r in recipients if not (r in seen or seen.add(r))]

    if not recipients:
        if invalid:
            sample = ", ".join(invalid[:6])
            more = "" if len(invalid) <= 6 else f" (+{len(invalid) - 6} more)"
            _set_flash(f"No valid recipient emails found. Invalid entries: {sample}{more}", "error")
            return _redirect_home()
        _set_flash("Please provide at least one recipient email address.", "error")
        return _redirect_home()

    if invalid:
        sample = ", ".join(invalid[:6])
        more = "" if len(invalid) <= 6 else f" (+{len(invalid) - 6} more)"
        _set_flash(f"Ignored invalid recipient entries: {sample}{more}", "info")

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
        attachment_payload: list[tuple[str, bytes, str | None]] = []
        if attachments:
            for upload in attachments:
                if upload is None or not upload.filename:
                    continue
                data = await upload.read()
                attachment_payload.append((upload.filename, data, upload.content_type))

        send_bulk_emails(
            recipients,
            template=template,
            attachments=attachment_payload,
            delay=delay_value,
            batch_size=batch_value,
            dry_run=dry_run,
            status_callback=capture,
        )
        if dry_run:
            _set_flash("Dry-run completed successfully.", "success")
        else:
            _set_flash("Emails sent successfully.", "success")
            _save_draft(subject_value, body_value)
    except Exception as exc:  # pragma: no cover - runtime safety
        _push_log(send_log, f"Error: {exc}", prefix="Campaign")
        _set_flash(f"Failed to send campaign: {exc}", "error")
    finally:
        app.state.last_send_log = send_log

    if dry_run:
        return _redirect_home()
    return RedirectResponse("/?sent=1", status_code=status.HTTP_303_SEE_OTHER)


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

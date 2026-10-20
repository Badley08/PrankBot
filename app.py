import os
import re
import time
import hmac
import json
import secrets
import sqlite3
import logging
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urljoin

import requests
from flask import Flask, abort, jsonify, render_template, request, url_for

# -------------------- Configuration --------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "PrankhimBot").strip().lstrip("@")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/tmp/prankbot.sqlite3")
LINK_TTL_MINUTES = int(os.environ.get("LINK_TTL_MINUTES", "60"))
MAX_PHOTO_BYTES = int(os.environ.get("MAX_PHOTO_BYTES", str(25 * 1024 * 1024)))
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "24"))
PORT = int(os.environ.get("PORT", "10000"))

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("prank-selfie-bot")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_PHOTO_BYTES + 256 * 1024

if not BOT_TOKEN:
    logger.warning("TELEGRAM_BOT_TOKEN is not configured. Telegram API interactions will fail.")
if not WEBHOOK_SECRET:
    logger.warning("TELEGRAM_WEBHOOK_SECRET is not configured. Configure a secret prior to production deployment.")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""


# -------------------- Database --------------------
def db():
    conn = sqlite3.connect(DATABASE_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS links (
                token TEXT PRIMARY KEY,
                owner_chat_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                used_at INTEGER,
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT,
                event_type TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                details TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rate_limits (
                client_key TEXT NOT NULL,
                window_start INTEGER NOT NULL,
                hits INTEGER NOT NULL,
                PRIMARY KEY(client_key, window_start)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_links_expiry ON links(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at)")


def log_event(event_type, token=None, details=None):
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO events(token, event_type, created_at, details) VALUES (?, ?, ?, ?)",
                (token, event_type, int(time.time()), json.dumps(details or {}, ensure_ascii=False)[:1000])
            )
    except Exception:
        logger.exception("Failed to persist event log")


init_db()


# -------------------- Telegram Helpers --------------------
def telegram_call(method, payload=None, files=None, timeout=20):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")
    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        data=payload or {},
        files=files,
        timeout=timeout
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API response error: {data.get('description', 'Unknown error')}")
    return data.get("result")


def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": str(chat_id), "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    return telegram_call("sendMessage", payload)


def answer_callback(callback_query_id, text=""):
    try:
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text[:180]})
    except Exception:
        logger.exception("Failed to answer callback query")


def create_link(owner_chat_id):
    now = int(time.time())
    token = secrets.token_urlsafe(24)
    with db() as conn:
        conn.execute(
            "INSERT INTO links(token, owner_chat_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, str(owner_chat_id), now, now + LINK_TTL_MINUTES * 60)
        )
    log_event("link_created", token, {"owner_chat_id": str(owner_chat_id)})
    base = PUBLIC_BASE_URL or request_host_fallback()
    return f"{base}/p/{token}"


def request_host_fallback():
    return request.url_root.rstrip("/")


def get_link(token):
    with db() as conn:
        return conn.execute("SELECT * FROM links WHERE token = ?", (token,)).fetchone()


def link_status(token):
    row = get_link(token)
    if row is None:
        return "not_found"
    now = int(time.time())
    if row["status"] != "active":
        return "inactive"
    if row["used_at"] is not None:
        return "used"
    if row["expires_at"] < now:
        return "expired"
    return "active"


def consume_link(token):
    now = int(time.time())
    with db() as conn:
        cur = conn.execute(
            "UPDATE links SET used_at = ? WHERE token = ? AND status = 'active' "
            "AND used_at IS NULL AND expires_at >= ?",
            (now, token, now)
        )
        return cur.rowcount == 1


def owner_only(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not OWNER_CHAT_ID:
            return jsonify({"ok": False, "error": "OWNER_CHAT_ID is not configured"}), 503
        supplied = request.headers.get("X-Owner-Key", "")
        expected = os.environ.get("OWNER_ADMIN_KEY", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


# -------------------- Rate Limiting --------------------
def rate_limited(key):
    now = int(time.time())
    window = now // 60
    with db() as conn:
        row = conn.execute(
            "SELECT hits FROM rate_limits WHERE client_key = ? AND window_start = ?",
            (key, window)
        ).fetchone()
        if row and row["hits"] >= RATE_LIMIT_PER_MINUTE:
            return True
        conn.execute(
            "INSERT INTO rate_limits(client_key, window_start, hits) VALUES (?, ?, 1) "
            "ON CONFLICT(client_key, window_start) DO UPDATE SET hits = hits + 1",
            (key, window)
        )
        conn.execute("DELETE FROM rate_limits WHERE window_start < ?", (window - 3,))
    return False


# -------------------- Telegram Webhook --------------------
def is_owner_chat(chat_id):
    return bool(OWNER_CHAT_ID) and str(chat_id) == str(OWNER_CHAT_ID)


def process_message(message):
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if chat.get("type") != "private" or chat_id is None:
        return

    text = (message.get("text") or "").strip()
    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

    if command in ("/start", "/help"):
        send_message(
            chat_id,
            "PrankBot — Consent-based selfie link generator.\n\n"
            "Available commands:\n"
            "/link — Generate a single-use selfie link\n"
            "/stats — View link generation metrics\n"
            "/cancel — Deactivate all currently active links\n"
            "/help — Show this help message\n\n"
            "Notice: Recipients receive an explicit notice explaining that the captured photo "
            "will be forwarded to the bot administrator upon confirmation."
        )
        return

    if command == "/link":
        if not is_owner_chat(chat_id):
            send_message(chat_id, "Access denied. Link generation is restricted to the administrator.")
            return
        link = create_link(chat_id)
        send_message(
            chat_id,
            f"Selfie link generated (valid for {LINK_TTL_MINUTES} minutes, single use only):\n{link}\n\n"
            "The webpage requires camera authorization and explicit confirmation prior to photo transmission."
        )
        return

    if command == "/cancel":
        if not is_owner_chat(chat_id):
            send_message(chat_id, "Access denied. This command is restricted to the administrator.")
            return
        with db() as conn:
            cur = conn.execute(
                "UPDATE links SET status = 'cancelled' WHERE owner_chat_id = ? AND status = 'active' AND used_at IS NULL",
                (str(chat_id),)
            )
        send_message(chat_id, f"Operation confirmed. {cur.rowcount} active link(s) cancelled.")
        return

    if command == "/stats":
        if not is_owner_chat(chat_id):
            send_message(chat_id, "Access denied. This command is restricted to the administrator.")
            return
        with db() as conn:
            total = conn.execute("SELECT COUNT(*) n FROM links").fetchone()["n"]
            active = conn.execute(
                "SELECT COUNT(*) n FROM links WHERE status='active' AND used_at IS NULL AND expires_at >= ?",
                (int(time.time()),)
            ).fetchone()["n"]
            used = conn.execute("SELECT COUNT(*) n FROM links WHERE used_at IS NOT NULL").fetchone()["n"]
        send_message(
            chat_id,
            f"Link Metrics:\n"
            f"Total generated: {total}\n"
            f"Active: {active}\n"
            f"Used: {used}"
        )
        return

    if command:
        send_message(chat_id, "Unrecognized command. Use /help to display available commands.")


@app.post("/telegram/webhook")
def telegram_webhook():
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not WEBHOOK_SECRET or not hmac.compare_digest(supplied, WEBHOOK_SECRET):
        abort(403)
    update = request.get_json(silent=True) or {}
    try:
        if "message" in update:
            process_message(update["message"])
        elif "callback_query" in update:
            answer_callback(update["callback_query"].get("id"), "Action completed.")
    except Exception:
        logger.exception("Failed to process Telegram update")
    return jsonify({"ok": True})


# -------------------- Public Selfie Flow --------------------
@app.get("/")
def index():
    return render_template("home.html")


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "prank-selfie-bot"})


@app.get("/p/<token>")
def selfie_page(token):
    status = link_status(token)
    if status != "active":
        return render_template("status.html", status=status), 410 if status in ("expired", "used", "inactive") else 404
    return render_template("selfie.html", token=token)


@app.get("/api/session/<token>")
def session_info(token):
    if rate_limited("info:" + request.remote_addr):
        return jsonify({"ok": False, "error": "Rate limit exceeded. Please retry in one minute."}), 429
    status = link_status(token)
    if status != "active":
        return jsonify({"ok": False, "error": status}), 410 if status != "not_found" else 404
    return jsonify({
        "ok": True,
        "message": "Camera access is optional. The photo is submitted to the bot administrator only upon explicit confirmation."
    })


@app.post("/api/session/<token>/submit")
def submit_selfie(token):
    if rate_limited("submit:" + request.remote_addr):
        return jsonify({"ok": False, "error": "Rate limit exceeded. Please retry in one minute."}), 429

    status = link_status(token)
    if status != "active":
        return jsonify({"ok": False, "error": "Link is expired, inactive, or already used."}), 410

    if request.form.get("consent") != "yes":
        return jsonify({"ok": False, "error": "Explicit user confirmation is required."}), 400

    upload = request.files.get("photo")
    if upload is None or not upload.filename:
        return jsonify({"ok": False, "error": "No photo payload received."}), 400

    mimetype = (upload.mimetype or "").lower()
    if mimetype not in ("image/jpeg", "image/png", "image/webp"):
        return jsonify({"ok": False, "error": "Unsupported file format. Supported formats: JPEG, PNG, WebP."}), 415

    photo = upload.read(MAX_PHOTO_BYTES + 1)
    if not photo or len(photo) > MAX_PHOTO_BYTES:
        return jsonify({"ok": False, "error": "File is empty or exceeds the maximum permitted size."}), 413

    if not consume_link(token):
        return jsonify({"ok": False, "error": "Link has already been consumed or has expired."}), 410

    row = get_link(token)
    owner = row["owner_chat_id"] if row else OWNER_CHAT_ID
    if not owner:
        return jsonify({"ok": False, "error": "Bot administrator chat ID is not configured."}), 503

    try:
        caption = (
            "Selfie submitted via web interface with explicit user consent.\n"
            f"Reference: {token[:8]}..."
        )
        telegram_call(
            "sendPhoto",
            payload={"chat_id": owner, "caption": caption},
            files={"photo": ("selfie.jpg", photo, mimetype)},
            timeout=30
        )
    except Exception:
        logger.exception("Failed to forward photo payload")
        log_event("photo_forward_failed", token)
        return jsonify({"ok": False, "error": "Transmission failed. Please notify the administrator."}), 502

    log_event("photo_sent_with_consent", token, {"bytes": len(photo), "mimetype": mimetype})
    return jsonify({"ok": True, "message": "Photo submitted successfully. You may close this page."})


# -------------------- Webhook Setup Endpoints --------------------
@app.post("/admin/set-webhook")
@owner_only
def set_webhook():
    if not PUBLIC_BASE_URL or not WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "PUBLIC_BASE_URL and TELEGRAM_WEBHOOK_SECRET must be configured."}), 400
    webhook_url = f"{PUBLIC_BASE_URL}/telegram/webhook"
    try:
        result = telegram_call("setWebhook", {
            "url": webhook_url,
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": json.dumps(["message", "callback_query"]),
            "drop_pending_updates": "true"
        })
        return jsonify({"ok": True, "webhook_url": webhook_url, "result": result})
    except Exception as exc:
        logger.exception("setWebhook call failed")
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.post("/admin/delete-webhook")
@owner_only
def delete_webhook():
    try:
        result = telegram_call("deleteWebhook", {"drop_pending_updates": "false"})
        return jsonify({"ok": True, "result": result})
    except Exception as exc:
        logger.exception("deleteWebhook call failed")
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.errorhandler(413)
def too_large(_):
    return jsonify({"ok": False, "error": "Payload exceeds allowed size limit."}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

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
    logger.warning("TELEGRAM_BOT_TOKEN is not set. Telegram API calls will fail until configured.")
if not WEBHOOK_SECRET:
    logger.warning("TELEGRAM_WEBHOOK_SECRET is not set. Set a long random secret before deployment.")

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
        logger.exception("Could not write event")


init_db()


# -------------------- Telegram helpers --------------------
def telegram_call(method, payload=None, files=None, timeout=20):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        data=payload or {},
        files=files,
        timeout=timeout
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data.get('description', 'unknown error')}")
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
        logger.exception("Could not answer callback")


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
    # Used only if PUBLIC_BASE_URL wasn't configured; set PUBLIC_BASE_URL in production.
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


# -------------------- Rate limiting --------------------
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


# -------------------- Telegram webhook --------------------
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
            "Salut ! Je crée des liens de selfie volontaire.\n\n"
            "Commandes :\n"
            "/link — créer un lien de selfie\n"
            "/help — afficher cette aide\n"
            "/cancel — annuler les liens actifs que tu as créés\n\n"
            "Important : la personne qui ouvre le lien voit une explication, "
            "doit autoriser la caméra, puis confirmer explicitement l'envoi de la photo."
        )
        return

    if command == "/link":
        if not is_owner_chat(chat_id):
            send_message(chat_id, "Désolé, la création de liens est réservée au propriétaire de ce bot.")
            return
        link = create_link(chat_id)
        send_message(
            chat_id,
            f"Voici ton lien (valable {LINK_TTL_MINUTES} minutes et utilisable une seule fois) :\n{link}\n\n"
            "Partage-le uniquement dans un contexte honnête. La page indique clairement que la photo sera envoyée au propriétaire du bot."
        )
        return

    if command == "/cancel":
        if not is_owner_chat(chat_id):
            send_message(chat_id, "Cette commande est réservée au propriétaire du bot.")
            return
        with db() as conn:
            cur = conn.execute(
                "UPDATE links SET status = 'cancelled' WHERE owner_chat_id = ? AND status = 'active' AND used_at IS NULL",
                (str(chat_id),)
            )
        send_message(chat_id, f"OK, {cur.rowcount} lien(s) actif(s) annulé(s).")
        return

    if command == "/stats":
        if not is_owner_chat(chat_id):
            send_message(chat_id, "Cette commande est réservée au propriétaire du bot.")
            return
        with db() as conn:
            total = conn.execute("SELECT COUNT(*) n FROM links").fetchone()["n"]
            active = conn.execute(
                "SELECT COUNT(*) n FROM links WHERE status='active' AND used_at IS NULL AND expires_at >= ?",
                (int(time.time()),)
            ).fetchone()["n"]
            used = conn.execute("SELECT COUNT(*) n FROM links WHERE used_at IS NOT NULL").fetchone()["n"]
        send_message(chat_id, f"Statistiques\nLiens créés : {total}\nLiens actifs : {active}\nLiens utilisés : {used}")
        return

    if command:
        send_message(chat_id, "Commande inconnue. Utilise /help pour voir les commandes disponibles.")


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
            answer_callback(update["callback_query"].get("id"), "Aucune action disponible.")
    except Exception:
        # Avoid retries being stuck on app-level errors; details go to server logs.
        logger.exception("Failed to process Telegram update")
    return jsonify({"ok": True})


# -------------------- Public selfie flow --------------------
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
        return jsonify({"ok": False, "error": "Trop de requêtes. Réessaie dans une minute."}), 429
    status = link_status(token)
    if status != "active":
        return jsonify({"ok": False, "error": status}), 410 if status != "not_found" else 404
    return jsonify({
        "ok": True,
        "message": "Tu peux choisir de prendre un selfie. La photo sera envoyée au propriétaire du bot seulement après ta confirmation."
    })


@app.post("/api/session/<token>/submit")
def submit_selfie(token):
    if rate_limited("submit:" + request.remote_addr):
        return jsonify({"ok": False, "error": "Trop de tentatives. Réessaie dans une minute."}), 429

    status = link_status(token)
    if status != "active":
        return jsonify({"ok": False, "error": "Ce lien est expiré, déjà utilisé ou invalide."}), 410

    # The client must affirm consent explicitly. This is not a substitute for the visible consent UI.
    if request.form.get("consent") != "yes":
        return jsonify({"ok": False, "error": "Tu dois confirmer l'envoi de la photo."}), 400

    upload = request.files.get("photo")
    if upload is None or not upload.filename:
        return jsonify({"ok": False, "error": "Aucune photo reçue."}), 400

    mimetype = (upload.mimetype or "").lower()
    if mimetype not in ("image/jpeg", "image/png", "image/webp"):
        return jsonify({"ok": False, "error": "Format refusé. Utilise JPEG, PNG ou WebP."}), 415

    photo = upload.read(MAX_PHOTO_BYTES + 1)
    if not photo or len(photo) > MAX_PHOTO_BYTES:
        return jsonify({"ok": False, "error": "Photo vide ou trop volumineuse."}), 413

    # Consume atomically before forwarding, preventing reuse/race conditions.
    if not consume_link(token):
        return jsonify({"ok": False, "error": "Ce lien a déjà été utilisé ou a expiré."}), 410

    row = get_link(token)
    owner = row["owner_chat_id"] if row else OWNER_CHAT_ID
    if not owner:
        return jsonify({"ok": False, "error": "Le propriétaire du bot n'est pas configuré."}), 503

    try:
        caption = (
            "Selfie envoyé avec confirmation explicite depuis la page web.\n"
            f"Référence : {token[:8]}…"
        )
        telegram_call(
            "sendPhoto",
            payload={"chat_id": owner, "caption": caption},
            files={"photo": ("selfie.jpg", photo, mimetype)},
            timeout=30
        )
    except Exception:
        logger.exception("Failed to forward confirmed photo")
        log_event("photo_forward_failed", token)
        return jsonify({"ok": False, "error": "Envoi impossible pour le moment. Contacte le propriétaire du bot."}), 502

    log_event("photo_sent_with_consent", token, {"bytes": len(photo), "mimetype": mimetype})
    return jsonify({"ok": True, "message": "Photo envoyée. Tu peux fermer cette page."})


# -------------------- Webhook setup helper endpoint --------------------
@app.post("/admin/set-webhook")
@owner_only
def set_webhook():
    if not PUBLIC_BASE_URL or not WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "Configure PUBLIC_BASE_URL et TELEGRAM_WEBHOOK_SECRET."}), 400
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
        logger.exception("setWebhook failed")
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.post("/admin/delete-webhook")
@owner_only
def delete_webhook():
    try:
        result = telegram_call("deleteWebhook", {"drop_pending_updates": "false"})
        return jsonify({"ok": True, "result": result})
    except Exception as exc:
        logger.exception("deleteWebhook failed")
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.errorhandler(413)
def too_large(_):
    return jsonify({"ok": False, "error": "Fichier trop volumineux."}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

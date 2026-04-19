import os
import io
import json
import uuid
import base64
import datetime
from functools import wraps

import psycopg
import anthropic
import openpyxl
import requests
from flask import (
    Flask, request, jsonify, render_template, session,
    redirect, url_for, send_file, abort
)
from authlib.integrations.flask_client import OAuth
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")

DATABASE_URL     = os.environ.get("DATABASE_URL", "")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_SECRET    = os.environ.get("GOOGLE_CLIENT_SECRET", "")
APP_URL          = os.environ.get("APP_URL", "http://localhost:5000").rstrip("/")

# Normalize postgres:// -> postgresql:// (Railway sometimes gives the old scheme)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

CATEGORIES = {
    "food":     "غذاء",
    "tech":     "تقنية",
    "services": "خدمات",
    "health":   "صحة",
    "retail":   "تجزئة",
    "other":    "أخرى",
}

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

# ---------------------------------------------------------------------------
# Database helpers (psycopg 3, uses %s placeholders)
# ---------------------------------------------------------------------------
def db_connect():
    return psycopg.connect(DATABASE_URL, sslmode="require")


def db_exec(sql, params=()):
    """Execute a statement with no return (INSERT/UPDATE/DELETE/DDL)."""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


def db_one(sql, params=()):
    """Fetch a single row as a dict, or None."""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


def db_all(sql, params=()):
    """Fetch all rows as a list of dicts."""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]


def init_db():
    db_exec("""
    CREATE TABLE IF NOT EXISTS users (
        id              SERIAL PRIMARY KEY,
        google_id       TEXT UNIQUE NOT NULL,
        email           TEXT NOT NULL,
        name            TEXT,
        picture         TEXT,
        access_token    TEXT,
        refresh_token   TEXT,
        token_expiry    TIMESTAMP,
        folder_id       TEXT,
        folder_name     TEXT,
        channel_id      TEXT,
        resource_id     TEXT,
        channel_expiry  TIMESTAMP,
        created_at      TIMESTAMP DEFAULT NOW()
    )
    """)
    db_exec("""
    CREATE TABLE IF NOT EXISTS invoices (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
        file_id     TEXT,
        file_name   TEXT,
        category    TEXT,
        category_ar TEXT,
        confidence  INTEGER,
        vendor      TEXT,
        total       TEXT,
        invoice_date TEXT,
        summary     TEXT,
        processed_at TIMESTAMP DEFAULT NOW()
    )
    """)


# ---------------------------------------------------------------------------
# OAuth setup
# ---------------------------------------------------------------------------
oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={
        "scope": (
            "openid email profile "
            "https://www.googleapis.com/auth/drive.readonly "
            "https://www.googleapis.com/auth/drive.metadata.readonly"
        ),
        "prompt": "consent",
    },
)


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return db_one("SELECT * FROM users WHERE id = %s", (uid,))


# ---------------------------------------------------------------------------
# Routes — auth
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/login")
def login():
    redirect_uri = f"{APP_URL}/auth/callback"
    return oauth.google.authorize_redirect(redirect_uri, access_type="offline")


@app.route("/auth/callback")
def auth_callback():
    try:
        token = oauth.google.authorize_access_token()
    except Exception as e:
        return f"OAuth error: {e}", 400

    userinfo = token.get("userinfo") or {}
    if not userinfo:
        # Fallback: fetch userinfo manually
        resp = oauth.google.get("https://openidconnect.googleapis.com/v1/userinfo", token=token)
        userinfo = resp.json()

    google_id = userinfo.get("sub")
    email     = userinfo.get("email", "")
    name      = userinfo.get("name", "")
    picture   = userinfo.get("picture", "")

    access_token  = token.get("access_token")
    refresh_token = token.get("refresh_token")
    expires_in    = token.get("expires_in", 3600)
    expiry = datetime.datetime.utcnow() + datetime.timedelta(seconds=int(expires_in))

    existing = db_one("SELECT * FROM users WHERE google_id = %s", (google_id,))
    if existing:
        # Keep existing refresh_token if Google didn't return a new one
        new_refresh = refresh_token or existing.get("refresh_token")
        db_exec(
            """UPDATE users
               SET email=%s, name=%s, picture=%s,
                   access_token=%s, refresh_token=%s, token_expiry=%s
               WHERE google_id=%s""",
            (email, name, picture, access_token, new_refresh, expiry, google_id),
        )
        user_id = existing["id"]
    else:
        db_exec(
            """INSERT INTO users
               (google_id, email, name, picture, access_token, refresh_token, token_expiry)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (google_id, email, name, picture, access_token, refresh_token, expiry),
        )
        row = db_one("SELECT id FROM users WHERE google_id = %s", (google_id,))
        user_id = row["id"]

    session["user_id"] = user_id
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Routes — dashboard & data
# ---------------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    invoices = db_all(
        "SELECT * FROM invoices WHERE user_id = %s ORDER BY processed_at DESC LIMIT 50",
        (user["id"],),
    )
    return render_template("dashboard.html", user=user, invoices=invoices)


@app.route("/api/invoices")
@login_required
def api_invoices():
    user = current_user()
    rows = db_all(
        "SELECT * FROM invoices WHERE user_id = %s ORDER BY processed_at DESC LIMIT 100",
        (user["id"],),
    )
    # Stringify datetimes for JSON
    for r in rows:
        if isinstance(r.get("processed_at"), datetime.datetime):
            r["processed_at"] = r["processed_at"].isoformat()
    return jsonify(rows)


@app.route("/api/folders")
@login_required
def api_folders():
    user = current_user()
    creds = user_credentials(user)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    results = service.files().list(
        q="mimeType='application/vnd.google-apps.folder' and trashed=false",
        pageSize=50,
        fields="files(id, name)",
    ).execute()
    return jsonify(results.get("files", []))


@app.route("/api/select-folder", methods=["POST"])
@login_required
def api_select_folder():
    user = current_user()
    data = request.get_json() or {}
    folder_id   = data.get("folder_id")
    folder_name = data.get("folder_name", "")
    if not folder_id:
        return jsonify({"error": "folder_id required"}), 400

    db_exec(
        "UPDATE users SET folder_id=%s, folder_name=%s WHERE id=%s",
        (folder_id, folder_name, user["id"]),
    )
    return jsonify({"ok": True})


@app.route("/api/export")
@login_required
def api_export():
    user = current_user()
    rows = db_all(
        "SELECT * FROM invoices WHERE user_id = %s ORDER BY processed_at DESC",
        (user["id"],),
    )
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoices"
    headers = ["التاريخ", "الملف", "الفئة", "المورد", "الإجمالي", "تاريخ الفاتورة", "الثقة %", "الملخص"]
    ws.append(headers)
    for r in rows:
        ws.append([
            (r.get("processed_at").strftime("%Y-%m-%d %H:%M") if r.get("processed_at") else ""),
            r.get("file_name", ""),
            r.get("category_ar", ""),
            r.get("vendor", ""),
            r.get("total", ""),
            r.get("invoice_date", ""),
            r.get("confidence", ""),
            r.get("summary", ""),
        ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"invoices_{datetime.date.today()}.xlsx",
    )


# ---------------------------------------------------------------------------
# Google Drive helpers
# ---------------------------------------------------------------------------
def user_credentials(user):
    return Credentials(
        token=user["access_token"],
        refresh_token=user["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_SECRET,
        scopes=[
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/drive.metadata.readonly",
        ],
    )


# ---------------------------------------------------------------------------
# Claude classification
# ---------------------------------------------------------------------------
def classify_with_claude(file_name, file_bytes, mime_type):
    prompt = (
        "Analyze this invoice and respond ONLY with valid JSON (no markdown), with keys:\n"
        '{"category":"food|tech|services|health|retail|other",'
        '"confidence":85,'
        '"vendor":"name or غير محدد",'
        '"total":"amount+currency or غير محدد",'
        '"date":"date or غير محدد",'
        '"summary":"2-3 sentences in Arabic"}'
    )
    b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

    if mime_type == "application/pdf":
        content = [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
            {"type": "text", "text": prompt},
        ]
    elif mime_type and mime_type.startswith("image/"):
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
            {"type": "text", "text": prompt},
        ]
    else:
        try:
            text = file_bytes.decode("utf-8", errors="ignore")[:10000]
        except Exception:
            text = ""
        content = [{"type": "text", "text": f"{text}\n\n{prompt}"}]

    msg = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{"role": "user", "content": content}],
    )
    raw = "".join(b.text for b in msg.content if hasattr(b, "text"))
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Templates fallback — render a simple page if templates are missing
# ---------------------------------------------------------------------------
@app.errorhandler(500)
def on_500(e):
    return "Internal server error. Check Railway logs.", 500


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------
with app.app_context():
    try:
        init_db()
    except Exception as e:
        print(f"[init_db] warning: {e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

import os
import json
import base64
import uuid
import datetime
import anthropic
import openpyxl
import io
from pathlib import Path
from functools import wraps

from flask import (
    Flask, request, jsonify, render_template,
    session, redirect, url_for, send_file, abort
)
from flask_sqlalchemy import SQLAlchemy
from authlib.integrations.flask_client import OAuth

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///invoices.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
oauth = OAuth(app)

APP_URL = os.environ.get("APP_URL", "http://localhost:5000")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/drive",
]

google = oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent"},
)

CATEGORY_MAP = {
    "food": "Food & Grocery",
    "tech": "Technology",
    "services": "Services",
    "health": "Health & Medical",
    "retail": "Retail",
    "other": "Other",
}

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(255))
    picture = db.Column(db.String(512))
    google_id = db.Column(db.String(255), unique=True)
    access_token = db.Column(db.Text)
    refresh_token = db.Column(db.Text)
    token_expiry = db.Column(db.DateTime)
    drive_folder_id = db.Column(db.String(255))
    drive_folder_name = db.Column(db.String(255))
    webhook_channel_id = db.Column(db.String(255))
    webhook_expiry = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    invoices = db.relationship("Invoice", backref="user", lazy=True, cascade="all, delete-orphan")


class Invoice(db.Model):
    __tablename__ = "invoices"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False)
    filename = db.Column(db.String(512))
    drive_file_id = db.Column(db.String(255))
    category = db.Column(db.String(50))
    vendor = db.Column(db.String(255))
    total = db.Column(db.String(100))
    invoice_date = db.Column(db.String(100))
    confidence = db.Column(db.Integer)
    summary = db.Column(db.Text)
    source = db.Column(db.String(20), default="manual")  # manual | drive
    processed_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------
def get_drive_service(user):
    if not user.access_token:
        return None
    creds = Credentials(
        token=user.access_token,
        refresh_token=user.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds)


def register_webhook(user):
    service = get_drive_service(user)
    if not service or not user.drive_folder_id:
        return False
    channel_id = str(uuid.uuid4())
    expiry = datetime.datetime.utcnow() + datetime.timedelta(days=7)
    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": f"{APP_URL.rstrip('/')}/webhook/{user.id}",
        "expiration": str(int(expiry.timestamp() * 1000)),
    }
    try:
        service.files().watch(fileId=user.drive_folder_id, body=body).execute()
        user.webhook_channel_id = channel_id
        user.webhook_expiry = expiry
        db.session.commit()
        return True
    except Exception as e:
        app.logger.error(f"Webhook registration failed for {user.email}: {e}")
        return False


def classify_drive_file(user, file_id, filename, mime_type):
    service = get_drive_service(user)
    if not service:
        raise Exception("Drive not connected")

    req = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    raw = buf.read()
    b64 = base64.standard_b64encode(raw).decode("utf-8")

    content_blocks = []
    if mime_type == "application/pdf":
        content_blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}})
    elif mime_type.startswith("image/"):
        content_blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}})
    else:
        content_blocks.append({"type": "text", "text": raw.decode("utf-8", errors="ignore")})

    return run_claude(content_blocks)


def run_claude(content_blocks):
    content_blocks.append({"type": "text", "text": """Analyze this invoice. Respond ONLY with valid JSON (no markdown, no extra text):
{"category":"food|tech|services|health|retail|other","confidence":85,"vendor":"name or N/A","total":"amount+currency or N/A","date":"date or N/A","summary":"2-3 sentences describing the invoice and why you chose this category"}"""})
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{"role": "user", "content": content_blocks}],
    )
    raw = "".join(b.text for b in msg.content if hasattr(b, "text"))
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)


def save_invoice(user, result, filename, drive_file_id=None, source="manual"):
    inv = Invoice(
        user_id=user.id,
        filename=filename,
        drive_file_id=drive_file_id,
        category=result.get("category", "other"),
        vendor=result.get("vendor", ""),
        total=result.get("total", ""),
        invoice_date=result.get("date", ""),
        confidence=int(result.get("confidence", 0)),
        summary=result.get("summary", ""),
        source=source,
    )
    db.session.add(inv)
    db.session.commit()
    return inv


def build_excel(user):
    invoices = Invoice.query.filter_by(user_id=user.id).order_by(Invoice.processed_at.desc()).all()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoices"
    headers = ["Processed At", "File", "Category", "Vendor", "Total", "Invoice Date", "Confidence %", "Summary", "Source"]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = openpyxl.styles.Font(bold=True, color="FFFFFF")
        cell.fill = openpyxl.styles.PatternFill("solid", fgColor="5046E4")
    for inv in invoices:
        ws.append([
            inv.processed_at.strftime("%Y-%m-%d %H:%M"),
            inv.filename,
            CATEGORY_MAP.get(inv.category, inv.category),
            inv.vendor, inv.total, inv.invoice_date,
            inv.confidence, inv.summary, inv.source,
        ])
    for col in range(1, 10):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = 22
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    user = current_user()
    if user:
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/login")
def login_page():
    return render_template("landing.html")


@app.route("/auth/google")
def auth_google():
    redirect_uri = url_for("auth_callback", _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = google.authorize_access_token()
    userinfo = token.get("userinfo") or google.userinfo()

    user = User.query.filter_by(google_id=userinfo["sub"]).first()
    if not user:
        user = User(
            google_id=userinfo["sub"],
            email=userinfo["email"],
            name=userinfo.get("name", ""),
            picture=userinfo.get("picture", ""),
        )
        db.session.add(user)

    user.access_token = token.get("access_token")
    if token.get("refresh_token"):
        user.refresh_token = token["refresh_token"]
    if token.get("expires_at"):
        user.token_expiry = datetime.datetime.fromtimestamp(token["expires_at"])

    db.session.commit()
    session["user_id"] = user.id
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    invoices = Invoice.query.filter_by(user_id=user.id).order_by(Invoice.processed_at.desc()).limit(50).all()
    total = Invoice.query.filter_by(user_id=user.id).count()
    now = datetime.datetime.utcnow()
    month_count = Invoice.query.filter(
        Invoice.user_id == user.id,
        Invoice.processed_at >= datetime.datetime(now.year, now.month, 1)
    ).count()
    webhook_ok = (
        user.webhook_channel_id is not None and
        user.webhook_expiry is not None and
        user.webhook_expiry > datetime.datetime.utcnow()
    )
    return render_template(
        "dashboard.html",
        user=user,
        invoices=invoices,
        total=total,
        month_count=month_count,
        webhook_ok=webhook_ok,
        category_map=CATEGORY_MAP,
    )


# ---------------------------------------------------------------------------
# Routes — Drive setup
# ---------------------------------------------------------------------------
@app.route("/drive/folders")
@login_required
def list_drive_folders():
    user = current_user()
    service = get_drive_service(user)
    if not service:
        return jsonify({"error": "Drive not connected"}), 400
    try:
        query = "mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = service.files().list(q=query, fields="files(id,name)", pageSize=50).execute()
        folders = results.get("files", [])
        return jsonify({"folders": folders})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/drive/set-folder", methods=["POST"])
@login_required
def set_drive_folder():
    user = current_user()
    data = request.get_json()
    folder_id = data.get("folder_id")
    folder_name = data.get("folder_name", "")
    if not folder_id:
        return jsonify({"error": "folder_id required"}), 400
    user.drive_folder_id = folder_id
    user.drive_folder_name = folder_name
    db.session.commit()
    ok = register_webhook(user)
    return jsonify({"ok": ok, "folder_name": folder_name, "webhook": ok})


@app.route("/drive/reconnect", methods=["POST"])
@login_required
def reconnect_webhook():
    user = current_user()
    if not user.drive_folder_id:
        return jsonify({"error": "No folder selected yet"}), 400
    ok = register_webhook(user)
    return jsonify({"ok": ok})


# ---------------------------------------------------------------------------
# Routes — Webhook receiver (per user)
# ---------------------------------------------------------------------------
@app.route("/webhook/<user_id>", methods=["POST"])
def receive_webhook(user_id):
    resource_state = request.headers.get("X-Goog-Resource-State", "")
    if resource_state in ("sync", ""):
        return "", 200

    user = User.query.get(user_id)
    if not user or not user.drive_folder_id:
        return "", 200

    try:
        service = get_drive_service(user)
        if not service:
            return "", 200

        # Get already-processed Drive file IDs for this user
        processed = {inv.drive_file_id for inv in Invoice.query.filter_by(user_id=user.id).all() if inv.drive_file_id}

        query = f"'{user.drive_folder_id}' in parents and trashed=false"
        files = service.files().list(
            q=query,
            fields="files(id,name,mimeType,createdTime)",
            orderBy="createdTime desc",
            pageSize=20,
        ).execute().get("files", [])

        for f in files:
            if f["id"] in processed:
                continue
            ext = f["name"].rsplit(".", 1)[-1].lower() if "." in f["name"] else ""
            if ext not in {"pdf", "jpg", "jpeg", "png", "xlsx", "csv", "txt"}:
                continue
            try:
                result = classify_drive_file(user, f["id"], f["name"], f["mimeType"])
                save_invoice(user, result, f["name"], drive_file_id=f["id"], source="drive")
            except Exception as e:
                app.logger.error(f"Failed to process {f['name']} for {user.email}: {e}")

    except Exception as e:
        app.logger.error(f"Webhook handler error for user {user_id}: {e}")

    return "", 200


# ---------------------------------------------------------------------------
# Routes — Manual classify
# ---------------------------------------------------------------------------
@app.route("/classify", methods=["POST"])
@login_required
def classify_manual():
    user = current_user()
    content_blocks = []
    filename = "direct-text"
    try:
        if "file" in request.files and request.files["file"].filename:
            from werkzeug.utils import secure_filename
            f = request.files["file"]
            filename = secure_filename(f.filename)
            raw = f.read()
            b64 = base64.standard_b64encode(raw).decode("utf-8")
            mime = f.mimetype
            if mime == "application/pdf":
                content_blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}})
            elif mime.startswith("image/"):
                content_blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
            else:
                content_blocks.append({"type": "text", "text": raw.decode("utf-8", errors="ignore")})
        else:
            text = request.form.get("text", "").strip()
            if not text:
                return jsonify({"error": "Send a file or text"}), 400
            content_blocks.append({"type": "text", "text": text})

        result = run_claude(content_blocks)
        inv = save_invoice(user, result, filename)
        return jsonify({**result, "filename": filename, "id": inv.id})

    except anthropic.AuthenticationError:
        return jsonify({"error": "Invalid Anthropic API key — check server config"}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Routes — Data
# ---------------------------------------------------------------------------
@app.route("/invoices")
@login_required
def get_invoices():
    user = current_user()
    page = int(request.args.get("page", 1))
    per_page = 20
    q = Invoice.query.filter_by(user_id=user.id).order_by(Invoice.processed_at.desc())
    total = q.count()
    items = q.offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({
        "total": total,
        "page": page,
        "invoices": [{
            "id": i.id,
            "filename": i.filename,
            "category": i.category,
            "vendor": i.vendor,
            "total": i.total,
            "date": i.invoice_date,
            "confidence": i.confidence,
            "summary": i.summary,
            "source": i.source,
            "processed_at": i.processed_at.isoformat(),
        } for i in items]
    })


@app.route("/export")
@login_required
def export_excel():
    user = current_user()
    buf = build_excel(user)
    return send_file(
        buf,
        as_attachment=True,
        download_name=f"invoices_{datetime.date.today()}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------
with app.app_context():
    db.create_all()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

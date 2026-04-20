import os, json, base64, uuid, datetime, anthropic, openpyxl, io, ssl
from pathlib import Path
from functools import wraps
from urllib.parse import urlparse

from flask import Flask, request, jsonify, render_template, session, redirect, url_for, send_file
from authlib.integrations.flask_client import OAuth
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")

APP_URL       = os.environ.get("APP_URL", "http://localhost:5000")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
G_CLIENT_ID   = os.environ.get("GOOGLE_CLIENT_ID", "")
G_CLIENT_SEC  = os.environ.get("GOOGLE_CLIENT_SECRET", "")
DATABASE_URL  = os.environ.get("DATABASE_URL", "")

SCOPES = ["openid","email","profile","https://www.googleapis.com/auth/drive"]
oauth  = OAuth(app)
google = oauth.register(
    name="google", client_id=G_CLIENT_ID, client_secret=G_CLIENT_SEC,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope":" ".join(SCOPES),"access_type":"offline","prompt":"consent"},
)
CATEGORY_MAP = {
    "food":"Food & Grocery","tech":"Technology","services":"Services",
    "health":"Health & Medical","retail":"Retail","other":"Other",
}

# ── DB ────────────────────────────────────────────────────────────────────────
def _use_pg():
    return bool(DATABASE_URL and DATABASE_URL.startswith("postgres"))

def _pg_conn():
    import pg8000.dbapi
    p = urlparse(DATABASE_URL.replace("postgres://","postgresql://",1))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return pg8000.dbapi.connect(
        host=p.hostname, port=p.port or 5432,
        database=p.path.lstrip("/"),
        user=p.username, password=p.password,
        ssl_context=ctx,
    )

def _get_conn():
    if _use_pg():
        return _pg_conn(), "pg"
    import sqlite3
    c = sqlite3.connect("invoices.db")
    c.row_factory = sqlite3.Row
    return c, "sq"

def _pg_sql(sql):
    """Convert ?-placeholders to %s for pg8000 dbapi."""
    return sql.replace("?", "%s")

def db_run(sql, params=()):
    conn, kind = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(_pg_sql(sql) if kind=="pg" else sql, params)
        conn.commit()
    finally:
        conn.close()

def db_one(sql, params=()):
    conn, kind = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(_pg_sql(sql) if kind=="pg" else sql, params)
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    finally:
        conn.close()

def db_all(sql, params=()):
    conn, kind = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(_pg_sql(sql) if kind=="pg" else sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()

def db_count(sql, params=()):
    conn, kind = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(_pg_sql(sql) if kind=="pg" else sql, params)
        return cur.fetchone()[0]
    finally:
        conn.close()

def init_db():
    db_run("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT, picture TEXT,
        google_id TEXT UNIQUE, access_token TEXT, refresh_token TEXT,
        drive_folder_id TEXT, drive_folder_name TEXT,
        webhook_channel_id TEXT, webhook_expiry TEXT, created_at TEXT)""")
    db_run("""CREATE TABLE IF NOT EXISTS invoices (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, filename TEXT,
        drive_file_id TEXT, category TEXT, vendor TEXT, total TEXT,
        invoice_date TEXT, confidence INTEGER, summary TEXT,
        source TEXT DEFAULT 'manual', processed_at TEXT)""")

with app.app_context():
    try:
        init_db()
    except Exception as e:
        app.logger.error(f"DB init: {e}")

# ── Auth ──────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def g(*a, **kw):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        return f(*a, **kw)
    return g

def current_user():
    uid = session.get("user_id")
    return db_one("SELECT * FROM users WHERE id=?", (uid,)) if uid else None

# ── Drive ─────────────────────────────────────────────────────────────────────
def drive_svc(user):
    if not user or not user.get("access_token"):
        return None
    creds = Credentials(
        token=user["access_token"], refresh_token=user.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=G_CLIENT_ID, client_secret=G_CLIENT_SEC,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds)

def register_webhook(user):
    svc = drive_svc(user)
    if not svc or not user.get("drive_folder_id"):
        return False
    cid    = str(uuid.uuid4())
    expiry = datetime.datetime.utcnow() + datetime.timedelta(days=7)
    try:
        svc.files().watch(fileId=user["drive_folder_id"], body={
            "id":cid, "type":"web_hook",
            "address":f"{APP_URL.rstrip('/')}/webhook/{user['id']}",
            "expiration":str(int(expiry.timestamp()*1000)),
        }).execute()
        db_run("UPDATE users SET webhook_channel_id=?,webhook_expiry=? WHERE id=?",
               (cid, expiry.isoformat(), user["id"]))
        return True
    except Exception as e:
        app.logger.error(f"webhook: {e}")
        return False

# ── Claude ────────────────────────────────────────────────────────────────────
def run_claude(blocks):
    blocks.append({"type":"text","text":"""Analyze this invoice. Respond ONLY with valid JSON (no markdown):
{"category":"food|tech|services|health|retail|other","confidence":85,"vendor":"name or N/A","total":"amount+currency or N/A","date":"date or N/A","summary":"2-3 sentences"}"""})
    msg = anthropic.Anthropic(api_key=ANTHROPIC_KEY).messages.create(
        model="claude-sonnet-4-20250514", max_tokens=800,
        messages=[{"role":"user","content":blocks}],
    )
    raw = "".join(b.text for b in msg.content if hasattr(b,"text"))
    return json.loads(raw.replace("```json","").replace("```","").strip())

def save_invoice(user_id, r, filename, drive_file_id=None, source="manual"):
    iid = str(uuid.uuid4())
    db_run(
        "INSERT INTO invoices (id,user_id,filename,drive_file_id,category,vendor,total,invoice_date,confidence,summary,source,processed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (iid, user_id, filename, drive_file_id,
         r.get("category","other"), r.get("vendor",""), r.get("total",""),
         r.get("date",""), int(r.get("confidence",0)), r.get("summary",""),
         source, datetime.datetime.utcnow().isoformat())
    )
    return iid

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return redirect(url_for("dashboard")) if session.get("user_id") else render_template("landing.html")

@app.route("/login")
def login_page():
    return render_template("landing.html")

@app.route("/auth/google")
def auth_google():
    return google.authorize_redirect(url_for("auth_callback", _external=True))

@app.route("/auth/callback")
def auth_callback():
    token    = google.authorize_access_token()
    userinfo = token.get("userinfo") or google.userinfo()
    existing = db_one("SELECT * FROM users WHERE google_id=?", (userinfo["sub"],))
    if not existing:
        uid = str(uuid.uuid4())
        db_run(
            "INSERT INTO users (id,email,name,picture,google_id,access_token,refresh_token,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (uid, userinfo["email"], userinfo.get("name",""), userinfo.get("picture",""),
             userinfo["sub"], token.get("access_token"), token.get("refresh_token"),
             datetime.datetime.utcnow().isoformat())
        )
        session["user_id"] = uid
    else:
        db_run("UPDATE users SET access_token=?,refresh_token=COALESCE(?,refresh_token) WHERE google_id=?",
               (token.get("access_token"), token.get("refresh_token"), userinfo["sub"]))
        session["user_id"] = existing["id"]
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    if not user:
        return redirect(url_for("login_page"))
    invoices    = db_all("SELECT * FROM invoices WHERE user_id=? ORDER BY processed_at DESC LIMIT 50",(user["id"],))
    total       = db_count("SELECT COUNT(*) FROM invoices WHERE user_id=?",(user["id"],))
    now         = datetime.datetime.utcnow()
    month_start = datetime.datetime(now.year, now.month, 1).isoformat()
    month_count = db_count("SELECT COUNT(*) FROM invoices WHERE user_id=? AND processed_at>=?",(user["id"],month_start))
    webhook_ok  = bool(user.get("webhook_channel_id") and user.get("webhook_expiry") and
                       user["webhook_expiry"] > datetime.datetime.utcnow().isoformat())
    return render_template("dashboard.html", user=user, invoices=invoices,
                           total=total, month_count=month_count,
                           webhook_ok=webhook_ok, category_map=CATEGORY_MAP)

@app.route("/drive/folders")
@login_required
def list_drive_folders():
    svc = drive_svc(current_user())
    if not svc:
        return jsonify({"error":"Drive not connected"}),400
    try:
        res = svc.files().list(q="mimeType='application/vnd.google-apps.folder' and trashed=false",
                               fields="files(id,name)",pageSize=50).execute()
        return jsonify({"folders":res.get("files",[])})
    except Exception as e:
        return jsonify({"error":str(e)}),500

@app.route("/drive/set-folder", methods=["POST"])
@login_required
def set_drive_folder():
    user = current_user()
    data = request.get_json()
    fid  = data.get("folder_id")
    if not fid:
        return jsonify({"error":"folder_id required"}),400
    db_run("UPDATE users SET drive_folder_id=?,drive_folder_name=? WHERE id=?",
           (fid,data.get("folder_name",""),user["id"]))
    ok = register_webhook(db_one("SELECT * FROM users WHERE id=?",(user["id"],)))
    return jsonify({"ok":ok,"folder_name":data.get("folder_name","")})

@app.route("/drive/reconnect", methods=["POST"])
@login_required
def reconnect_webhook():
    user = current_user()
    if not user.get("drive_folder_id"):
        return jsonify({"error":"No folder selected"}),400
    return jsonify({"ok":register_webhook(user)})

@app.route("/webhook/<user_id>", methods=["POST"])
def receive_webhook(user_id):
    if request.headers.get("X-Goog-Resource-State","") in ("sync",""):
        return "",200
    user = db_one("SELECT * FROM users WHERE id=?",(user_id,))
    if not user or not user.get("drive_folder_id"):
        return "",200
    try:
        svc = drive_svc(user)
        if not svc: return "",200
        done_ids = {r["drive_file_id"] for r in db_all(
            "SELECT drive_file_id FROM invoices WHERE user_id=? AND drive_file_id IS NOT NULL",(user_id,))}
        files = svc.files().list(
            q=f"'{user['drive_folder_id']}' in parents and trashed=false",
            fields="files(id,name,mimeType)",orderBy="createdTime desc",pageSize=20
        ).execute().get("files",[])
        for f in files:
            if f["id"] in done_ids: continue
            ext = f["name"].rsplit(".",1)[-1].lower() if "." in f["name"] else ""
            if ext not in {"pdf","jpg","jpeg","png","xlsx","csv","txt"}: continue
            try:
                buf = io.BytesIO()
                dl  = MediaIoBaseDownload(buf,svc.files().get_media(fileId=f["id"]))
                done=False
                while not done: _,done=dl.next_chunk()
                buf.seek(0); raw=buf.read()
                b64=base64.standard_b64encode(raw).decode()
                mime=f["mimeType"]; blks=[]
                if mime=="application/pdf":
                    blks.append({"type":"document","source":{"type":"base64","media_type":"application/pdf","data":b64}})
                elif mime.startswith("image/"):
                    blks.append({"type":"image","source":{"type":"base64","media_type":mime,"data":b64}})
                else:
                    blks.append({"type":"text","text":raw.decode("utf-8",errors="ignore")})
                save_invoice(user_id,run_claude(blks),f["name"],drive_file_id=f["id"],source="drive")
            except Exception as e:
                app.logger.error(f"file {f['name']}: {e}")
    except Exception as e:
        app.logger.error(f"webhook: {e}")
    return "",200

@app.route("/classify", methods=["POST"])
@login_required
def classify_manual():
    user=current_user(); blocks=[]; filename="direct-text"
    try:
        if "file" in request.files and request.files["file"].filename:
            from werkzeug.utils import secure_filename
            f=request.files["file"]; filename=secure_filename(f.filename)
            raw=f.read(); b64=base64.standard_b64encode(raw).decode(); mime=f.mimetype
            if mime=="application/pdf":
                blocks.append({"type":"document","source":{"type":"base64","media_type":"application/pdf","data":b64}})
            elif mime.startswith("image/"):
                blocks.append({"type":"image","source":{"type":"base64","media_type":mime,"data":b64}})
            else:
                blocks.append({"type":"text","text":raw.decode("utf-8",errors="ignore")})
        else:
            text=request.form.get("text","").strip()
            if not text: return jsonify({"error":"Send a file or text"}),400
            blocks.append({"type":"text","text":text})
        result=run_claude(blocks)
        iid=save_invoice(user["id"],result,filename)
        return jsonify({**result,"filename":filename,"id":iid})
    except anthropic.AuthenticationError:
        return jsonify({"error":"Invalid Anthropic API key"}),401
    except Exception as e:
        return jsonify({"error":str(e)}),500

@app.route("/invoices")
@login_required
def get_invoices():
    user=current_user(); page=int(request.args.get("page",1)); per=20
    return jsonify({
        "total":db_count("SELECT COUNT(*) FROM invoices WHERE user_id=?",(user["id"],)),
        "page":page,
        "invoices":db_all("SELECT * FROM invoices WHERE user_id=? ORDER BY processed_at DESC LIMIT ? OFFSET ?",
                          (user["id"],per,(page-1)*per)),
    })

@app.route("/export")
@login_required
def export_excel():
    user=current_user()
    rows=db_all("SELECT * FROM invoices WHERE user_id=? ORDER BY processed_at DESC",(user["id"],))
    wb=openpyxl.Workbook(); ws=wb.active; ws.title="Invoices"
    hdr=["Processed At","File","Category","Vendor","Total","Invoice Date","Confidence %","Summary","Source"]
    for c,h in enumerate(hdr,1):
        cell=ws.cell(row=1,column=c,value=h)
        cell.font=openpyxl.styles.Font(bold=True,color="FFFFFF")
        cell.fill=openpyxl.styles.PatternFill("solid",fgColor="5046E4")
    for inv in rows:
        ws.append([inv.get("processed_at",""),inv.get("filename",""),
                   CATEGORY_MAP.get(inv.get("category",""),inv.get("category","")),
                   inv.get("vendor",""),inv.get("total",""),inv.get("invoice_date",""),
                   inv.get("confidence",""),inv.get("summary",""),inv.get("source","")])
    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf,as_attachment=True,
                     download_name=f"invoices_{datetime.date.today()}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.route("/health")
def health():
    return jsonify({"status":"ok"})

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)),debug=False)

import os
import re
import json
import asyncio
import base64
import secrets
import string
import random
import hashlib
import email.utils
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

import aiosqlite
import httpx
from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tempmail.db")

# ─── Environment Variables ───────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_KEY)
USE_GMAIL = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN)

FIRST_NAMES = [
    "james", "john", "robert", "michael", "david", "william", "richard",
    "joseph", "thomas", "charles", "mary", "patricia", "jennifer", "linda",
    "elizabeth", "barbara", "susan", "jessica", "sarah", "karen", "emma",
    "olivia", "noah", "liam", "sophia", "jackson", "aiden", "lucas",
    "caden", "mason", "harper", "ella", "aria", "riley", "zoey",
]

LAST_NAMES = [
    "smith", "johnson", "williams", "brown", "jones", "garcia", "miller",
    "davis", "rodriguez", "martinez", "hernandez", "lopez", "gonzalez",
    "wilson", "anderson", "thomas", "taylor", "moore", "jackson", "martin",
    "lee", "perez", "thompson", "white", "harris", "sanchez", "clark",
    "ramirez", "lewis", "robinson", "walker", "young", "allen", "king",
]


def _format_date(val: str | None) -> str:
    if not val:
        return ""
    try:
        dt = datetime.fromisoformat(val)
        return dt.strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return val


# ─── Gmail API Helpers ────────────────────────────────────────────

_gmail_token_cache: dict = {"access_token": "", "expires_at": 0.0}


async def _get_gmail_access_token() -> str:
    now = datetime.now(timezone.utc).timestamp()
    if _gmail_token_cache["access_token"] and _gmail_token_cache["expires_at"] > now + 60:
        return _gmail_token_cache["access_token"]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post("https://oauth2.googleapis.com/token", data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": GOOGLE_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        })
        resp.raise_for_status()
        data = resp.json()
        _gmail_token_cache["access_token"] = data["access_token"]
        _gmail_token_cache["expires_at"] = now + data.get("expires_in", 3600)
        return data["access_token"]


def _extract_gmail_body(payload: dict) -> tuple[str, str]:
    """Return (body, content_type) preferring HTML over plain text."""
    res: dict = {}
    _collect_gmail_parts(payload, res)
    if res.get("html"):
        return res["html"], "text/html"
    if res.get("plain"):
        return res["plain"], "text/plain"
    return "", "text/plain"


def _collect_gmail_parts(payload: dict, result: dict) -> None:
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/html" and not result.get("html"):
        data = payload.get("body", {}).get("data", "")
        if data:
            result["html"] = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    elif mime_type == "text/plain" and not result.get("plain"):
        data = payload.get("body", {}).get("data", "")
        if data:
            result["plain"] = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    elif mime_type.startswith("multipart/"):
        for part in payload.get("parts", []):
            _collect_gmail_parts(part, result)


def _parse_gmail_message(msg_id: str, msg: dict) -> dict:
    msg_headers = msg.get("payload", {}).get("headers", [])
    sender = subject = date_str = ""
    for h in msg_headers:
        name = h["name"].lower()
        if name == "from":
            sender = h["value"]
        elif name == "subject":
            subject = h["value"]
        elif name == "date":
            date_str = h["value"]
    body, content_type = _extract_gmail_body(msg.get("payload", {}))
    received_at = date_str
    if date_str:
        try:
            parsed = email.utils.parsedate_to_datetime(date_str)
            received_at = parsed.isoformat()
        except Exception:
            pass
    return {
        "id": f"gmail-{msg_id}",
        "sender": sender,
        "subject": subject,
        "body": body,
        "content_type": content_type,
        "received_at": received_at,
    }


async def _fetch_gmail_emails(email_addr: str) -> list[dict]:
    try:
        token = await _get_gmail_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        query = f"to:{email_addr} after:{cutoff.strftime('%Y/%m/%d')}"
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                headers=headers,
                params={"q": query, "maxResults": 10},
            )
            if resp.status_code != 200:
                return []
            messages = resp.json().get("messages", [])
            if not messages:
                return []

            async def fetch_one(msg_ref: dict) -> dict | None:
                try:
                    r = await client.get(
                        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_ref['id']}",
                        headers=headers,
                        params={"format": "full"},
                    )
                    if r.status_code != 200:
                        return None
                    return _parse_gmail_message(msg_ref["id"], r.json())
                except Exception:
                    return None

            results = await asyncio.gather(*[fetch_one(m) for m in messages[:5]])
            return [r for r in results if r is not None]
    except Exception:
        return []


# ─── Supabase REST Client ────────────────────────────────────────

class SupabaseClient:
    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.key = key
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    async def select(self, table: str, params: dict | None = None) -> list[dict]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.url}/rest/v1/{table}",
                headers=self.headers,
                params=params or {},
            )
            resp.raise_for_status()
            return resp.json()

    async def insert(self, table: str, data: dict) -> dict | None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.url}/rest/v1/{table}",
                headers=self.headers,
                json=data,
            )
            if resp.status_code == 409:
                return None
            if resp.status_code >= 400:
                detail = resp.text
                raise Exception(f"Supabase {resp.status_code}: {detail}")
            result = resp.json()
            return result[0] if result else data

    async def delete(self, table: str, params: dict) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{self.url}/rest/v1/{table}",
                headers=self.headers,
                params=params,
            )
            resp.raise_for_status()

    async def count(self, table: str, params: dict | None = None) -> int:
        headers = {**self.headers, "Prefer": "count=exact"}
        query = {"select": "id", "limit": "0"}
        if params:
            query.update(params)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.url}/rest/v1/{table}",
                headers=headers,
                params=query,
            )
            resp.raise_for_status()
            content_range = resp.headers.get("content-range", "")
            # Format: "*/total" when limit=0
            if "/" in content_range:
                total = content_range.split("/")[-1]
                if total != "*":
                    return int(total)
            return 0

    async def upsert(self, table: str, data: dict, on_conflict: str = "key") -> dict:
        headers = {**self.headers, "Prefer": "resolution=merge-duplicates,return=representation"}
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.url}/rest/v1/{table}?on_conflict={on_conflict}",
                headers=headers,
                json=data,
            )
            resp.raise_for_status()
            result = resp.json()
            return result[0] if result else data


supabase: SupabaseClient | None = SupabaseClient(SUPABASE_URL, SUPABASE_KEY) if USE_SUPABASE else None


async def get_db():
    if USE_SUPABASE:
        yield supabase
        return
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS domains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT UNIQUE NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                domain TEXT NOT NULL,
                token TEXT UNIQUE NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient TEXT NOT NULL,
                sender TEXT NOT NULL,
                subject TEXT DEFAULT '',
                body TEXT DEFAULT '',
                received_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_hash TEXT UNIQUE NOT NULL,
                key_prefix TEXT NOT NULL,
                domains TEXT NOT NULL DEFAULT '[]',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Default settings
        await db.execute("""
            INSERT OR IGNORE INTO settings (key, value)
            VALUES ('site_name', 'TempMail')
        """)
        await db.execute("""
            INSERT OR IGNORE INTO settings (key, value)
            VALUES ('admin_username', 'admin')
        """)
        await db.execute("""
            INSERT OR IGNORE INTO settings (key, value)
            VALUES ('admin_password', 'admin123')
        """)
        # Default domains
        for d in ["beking.online", "evoprem.store"]:
            await db.execute(
                "INSERT OR IGNORE INTO domains (domain) VALUES (?)", (d,)
            )
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not USE_SUPABASE:
        await init_db()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=500,
            content={"detail": f"Server error: {type(exc).__name__}: {exc}"},
        )
    raise exc


async def get_setting(db, key: str) -> str:
    if USE_SUPABASE:
        rows = await db.select("app_config", {"key": f"eq.{key}", "select": "value"})
        return rows[0]["value"] if rows else ""
    row = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    result = await row.fetchone()
    return result["value"] if result else ""


async def get_all_settings(db) -> dict:
    if USE_SUPABASE:
        rows = await db.select("app_config", {"select": "key,value"})
        return {r["key"]: r["value"] for r in rows}
    rows = await db.execute("SELECT key, value FROM settings")
    results = await rows.fetchall()
    return {r["key"]: r["value"] for r in results}


# ─── Public Pages ────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db=Depends(get_db)):
    site_name = await get_setting(db, "site_name")
    return templates.TemplateResponse(
        request, "index.html", {"site_name": site_name}
    )


@app.post("/api/scan")
async def scan_token(token: str = Form(...), db=Depends(get_db)):
    if USE_SUPABASE:
        rows = await db.select("tokens", {"token_id": f"eq.{token}", "select": "email"})
        if not rows:
            raise HTTPException(status_code=404, detail="Invalid token")
        email_addr = rows[0]["email"]
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=24)
        email_rows = await db.select("emails", {
            "recipient": f"eq.{email_addr}",
            "received_at": f"gte.{cutoff_dt.isoformat()}",
            "select": "id,sender,subject,body,received_at",
            "order": "received_at.desc",
        })
        if USE_GMAIL:
            try:
                gmail_emails = await _fetch_gmail_emails(email_addr)
                existing_ids = {str(e.get("id")) for e in email_rows}
                for ge in gmail_emails:
                    if ge["id"] not in existing_ids:
                        email_rows.append(ge)
                email_rows.sort(key=lambda x: x.get("received_at", ""), reverse=True)
            except Exception:
                pass
        return {"email": email_addr, "emails": email_rows}

    row = await db.execute(
        "SELECT email FROM accounts WHERE token = ?", (token,)
    )
    account = await row.fetchone()
    if not account:
        raise HTTPException(status_code=404, detail="Invalid token")

    email_addr = account["email"]
    cutoff = datetime.now(timezone.utc).timestamp() - 86400  # 24h

    rows = await db.execute(
        """SELECT id, sender, subject, body, received_at FROM emails
           WHERE recipient = ? AND strftime('%s', received_at) > ?
           ORDER BY received_at DESC""",
        (email_addr, str(int(cutoff))),
    )
    all_emails = [dict(r) for r in await rows.fetchall()]
    if USE_GMAIL:
        try:
            gmail_emails = await _fetch_gmail_emails(email_addr)
            existing_ids = {str(e.get("id")) for e in all_emails}
            for ge in gmail_emails:
                if ge["id"] not in existing_ids:
                    all_emails.append(ge)
            all_emails.sort(key=lambda x: x.get("received_at", ""), reverse=True)
        except Exception:
            pass
    return {"email": email_addr, "emails": all_emails}


# ─── Admin Auth ──────────────────────────────────────────────────

@app.get("/gatekeeper", response_class=HTMLResponse)
async def admin_login_page(request: Request, db=Depends(get_db)):
    site_name = await get_setting(db, "site_name")
    return templates.TemplateResponse(
        request, "login.html", {"site_name": site_name}
    )


@app.post("/gatekeeper")
async def admin_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db=Depends(get_db),
):
    admin_user = await get_setting(db, "admin_username")
    admin_pass = await get_setting(db, "admin_password")
    if username == admin_user and password == admin_pass:
        resp = RedirectResponse("/panel", status_code=303)
        resp.set_cookie("admin_session", "authenticated", httponly=True, path="/", samesite="lax", max_age=86400)
        return resp
    site_name = await get_setting(db, "site_name")
    return templates.TemplateResponse(
        request, "login.html",
        {"site_name": site_name, "error": "Invalid credentials"},
    )


def require_admin(request: Request):
    if request.cookies.get("admin_session") != "authenticated":
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=401, detail="Session expired, please login again")
        raise HTTPException(status_code=303, headers={"Location": "/gatekeeper"})


@app.get("/stok", response_class=HTMLResponse)
async def stok_page(request: Request):
    require_admin(request)
    return templates.TemplateResponse(request, "stok.html", {})


@app.get("/panel", response_class=HTMLResponse)
async def admin_panel(request: Request, db=Depends(get_db)):
    require_admin(request)
    settings = await get_all_settings(db)
    if USE_SUPABASE:
        domain_rows = await db.select("app_domains", {"select": "domain", "order": "domain"})
        domains = [r["domain"] for r in domain_rows]
        total_accounts = await db.count("tokens")
        account_rows = await db.select("tokens", {
            "select": "id,email,token_id,created_at",
            "order": "id.desc",
        })
        accounts = [
            {"id": r["id"], "email": r["email"], "domain": r["email"].split("@")[1] if "@" in r["email"] else "", "token": r["token_id"], "created_at": _format_date(r["created_at"])}
            for r in account_rows
        ]
    else:
        rows = await db.execute("SELECT domain FROM domains ORDER BY domain")
        domains = [r["domain"] for r in await rows.fetchall()]
        row = await db.execute("SELECT COUNT(*) as cnt FROM accounts")
        result = await row.fetchone()
        total_accounts = result["cnt"] if result else 0
        rows = await db.execute(
            "SELECT id, email, domain, token, created_at FROM accounts ORDER BY id DESC"
        )
        accounts = [dict(r) for r in await rows.fetchall()]
    return templates.TemplateResponse(
        request, "panel.html",
        {"settings": settings, "domains": domains, "accounts": accounts, "total_accounts": total_accounts},
    )


# ─── Admin API ───────────────────────────────────────────────────

@app.get("/api/domains")
async def list_domains(request: Request, db=Depends(get_db)):
    require_admin(request)
    if USE_SUPABASE:
        rows = await db.select("app_domains", {"select": "domain", "order": "domain"})
        return [r["domain"] for r in rows]
    rows = await db.execute("SELECT domain FROM domains ORDER BY domain")
    return [r["domain"] for r in await rows.fetchall()]


@app.post("/api/domains")
async def add_domain(request: Request, domain: str = Form(...), db=Depends(get_db)):
    require_admin(request)
    domain = domain.strip().lstrip("@").lower()
    if not domain:
        raise HTTPException(400, "Domain cannot be empty")
    if USE_SUPABASE:
        result = await db.insert("app_domains", {
            "domain": domain,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        if result is None:
            raise HTTPException(400, "Domain already exists")
        return {"ok": True, "domain": domain}
    try:
        await db.execute("INSERT INTO domains (domain) VALUES (?)", (domain,))
        await db.commit()
    except Exception:
        raise HTTPException(400, "Domain already exists")
    return {"ok": True, "domain": domain}


@app.delete("/api/domains/{domain}")
async def delete_domain(domain: str, request: Request, db=Depends(get_db)):
    require_admin(request)
    if USE_SUPABASE:
        await db.delete("app_domains", {"domain": f"eq.{domain}"})
        return {"ok": True}
    await db.execute("DELETE FROM domains WHERE domain = ?", (domain,))
    await db.commit()
    return {"ok": True}


@app.post("/api/generate")
async def generate_accounts(
    request: Request,
    domain: str = Form(...),
    mode: str = Form(...),
    count: int = Form(1),
    digits: int = Form(8),
    db=Depends(get_db),
):
    require_admin(request)
    if count < 1 or count > 100:
        raise HTTPException(400, "Count must be 1-100")

    created = []
    last_error = None
    attempts = 0
    max_attempts = count * 10
    while len(created) < count and attempts < max_attempts:
        attempts += 1
        if mode == "random":
            username = "".join(
                random.choices(string.ascii_lowercase + string.digits, k=digits)
            )
        else:
            first = random.choice(FIRST_NAMES)
            last = random.choice(LAST_NAMES)
            num = random.randint(10, 999)
            username = f"{first}{last}{num}"

        email_addr = f"{username}@{domain}"
        token = secrets.token_hex(8).upper()

        if USE_SUPABASE:
            try:
                result = await db.insert("tokens", {
                    "email": email_addr, "token_id": token,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                if result is None:
                    continue
                created.append({"email": email_addr, "token": token})
            except Exception as e:
                last_error = str(e)
                continue
        else:
            # Check for duplicate email
            row = await db.execute(
                "SELECT id FROM accounts WHERE email = ?", (email_addr,)
            )
            if await row.fetchone():
                continue
            row = await db.execute(
                "SELECT id FROM accounts WHERE token = ?", (token,)
            )
            if await row.fetchone():
                continue
            try:
                await db.execute(
                    "INSERT INTO accounts (email, domain, token) VALUES (?, ?, ?)",
                    (email_addr, domain, token),
                )
            except Exception:
                continue
            created.append({"email": email_addr, "token": token})

    if not USE_SUPABASE:
        await db.commit()
    if not created and last_error:
        raise HTTPException(500, f"Failed to generate accounts: {last_error}")
    return {"created": created}


@app.post("/api/manual-create")
async def manual_create(
    request: Request,
    email: str = Form(...),
    db=Depends(get_db),
):
    require_admin(request)
    email = email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Invalid email format")
    domain = email.split("@")[1]

    token = secrets.token_hex(8).upper()

    if USE_SUPABASE:
        existing = await db.select("tokens", {
            "email": f"eq.{email}", "select": "email,token_id",
        })
        if existing:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "Email already exists",
                    "email": existing[0]["email"],
                    "token": existing[0]["token_id"],
                },
            )
        await db.insert("tokens", {
            "email": email, "token_id": token,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"email": email, "token": token}

    # SQLite path
    row = await db.execute(
        "SELECT email, token FROM accounts WHERE email = ?", (email,)
    )
    existing = await row.fetchone()
    if existing:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Email already exists",
                "email": existing["email"],
                "token": existing["token"],
            },
        )

    row = await db.execute(
        "SELECT id FROM accounts WHERE token = ?", (token,)
    )
    if await row.fetchone():
        token = secrets.token_hex(8).upper()

    await db.execute(
        "INSERT INTO accounts (email, domain, token) VALUES (?, ?, ?)",
        (email, domain, token),
    )
    await db.commit()
    return {"email": email, "token": token}


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str, request: Request, db=Depends(get_db)):
    require_admin(request)
    if USE_SUPABASE:
        await db.delete("tokens", {"id": f"eq.{account_id}"})
        return {"ok": True}
    await db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    await db.commit()
    return {"ok": True}


@app.get("/api/accounts")
async def list_accounts(request: Request, db=Depends(get_db)):
    require_admin(request)
    if USE_SUPABASE:
        rows = await db.select("tokens", {
            "select": "id,email,token_id,created_at",
            "order": "id.desc",
        })
        return [
            {"id": r["id"], "email": r["email"], "domain": r["email"].split("@")[1] if "@" in r["email"] else "", "token": r["token_id"], "created_at": _format_date(r["created_at"])}
            for r in rows
        ]
    rows = await db.execute(
        "SELECT id, email, domain, token, created_at FROM accounts ORDER BY id DESC"
    )
    return [dict(r) for r in await rows.fetchall()]


@app.get("/api/accounts/count")
async def count_accounts(request: Request, db=Depends(get_db)):
    require_admin(request)
    if USE_SUPABASE:
        total = await db.count("tokens")
        return {"count": total}
    row = await db.execute("SELECT COUNT(*) as cnt FROM accounts")
    result = await row.fetchone()
    return {"count": result["cnt"] if result else 0}


@app.post("/api/settings")
async def update_settings(request: Request, db=Depends(get_db)):
    require_admin(request)
    data = await request.json()
    if USE_SUPABASE:
        for key, value in data.items():
            await db.upsert("app_config", {"key": key, "value": str(value)})
        return {"ok": True}
    for key, value in data.items():
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
    await db.commit()
    return {"ok": True}


@app.get("/api/settings")
async def get_settings(request: Request, db=Depends(get_db)):
    require_admin(request)
    return await get_all_settings(db)


# ─── Webhook for incoming email ──────────────────────────────────

@app.post("/api/webhook/incoming")
async def webhook_incoming(request: Request, db=Depends(get_db)):
    data = await request.json()
    recipient = data.get("recipient", "").lower()
    sender = data.get("sender", "")
    subject = data.get("subject", "")
    body = data.get("body", "")

    if not recipient:
        raise HTTPException(400, "recipient is required")

    if USE_SUPABASE:
        await db.insert("emails", {
            "recipient": recipient,
            "sender": sender,
            "subject": subject,
            "body": body,
            "received_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"ok": True}

    await db.execute(
        "INSERT INTO emails (recipient, sender, subject, body) VALUES (?, ?, ?, ?)",
        (recipient, sender, subject, body),
    )
    await db.commit()
    return {"ok": True}


# ─── API Key Helpers ─────────────────────────────────────────────

def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _extract_otp(text: str) -> str | None:
    """Extract OTP code (4-8 digits) from email body."""
    patterns = [
        r'\b(\d{6})\b',
        r'\b(\d{4})\b',
        r'\b(\d{8})\b',
        r'\b(\d{5})\b',
        r'\b(\d{7})\b',
    ]
    for p in patterns:
        matches = re.findall(p, text)
        if matches:
            return matches[0]
    return None


async def _validate_api_key(db, api_key: str) -> dict:
    """Validate API key and return key record with allowed domains."""
    key_hash = _hash_api_key(api_key)
    if USE_SUPABASE:
        rows = await db.select("api_keys", {
            "key_hash": f"eq.{key_hash}",
            "select": "id,key_prefix,domains,created_at",
        })
        if not rows:
            raise HTTPException(401, "Invalid API key")
        row = rows[0]
        domains = row["domains"] if isinstance(row["domains"], list) else json.loads(row["domains"])
        return {"id": row["id"], "key_prefix": row["key_prefix"], "domains": domains}
    else:
        cursor = await db.execute(
            "SELECT id, key_prefix, domains FROM api_keys WHERE key_hash = ?", (key_hash,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(401, "Invalid API key")
        return {"id": row["id"], "key_prefix": row["key_prefix"], "domains": json.loads(row["domains"])}


# ─── API Key Management (Admin) ──────────────────────────────────

@app.post("/api/apikeys")
async def create_api_key(
    request: Request,
    db=Depends(get_db),
):
    require_admin(request)
    data = await request.json()
    domains = data.get("domains", [])
    if not domains:
        raise HTTPException(400, "At least one domain must be selected")

    raw_key = "sk-" + secrets.token_hex(24)
    key_hash = _hash_api_key(raw_key)
    key_prefix = raw_key[:12]

    if USE_SUPABASE:
        await db.insert("api_keys", {
            "key_hash": key_hash,
            "key_prefix": key_prefix,
            "domains": domains,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    else:
        await db.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, domains) VALUES (?, ?, ?)",
            (key_hash, key_prefix, json.dumps(domains)),
        )
        await db.commit()

    return {"key": raw_key, "key_prefix": key_prefix, "domains": domains}


@app.get("/api/apikeys")
async def list_api_keys(request: Request, db=Depends(get_db)):
    require_admin(request)
    if USE_SUPABASE:
        rows = await db.select("api_keys", {
            "select": "id,key_prefix,domains,created_at",
            "order": "id.desc",
        })
        result = []
        for r in rows:
            domains = r["domains"] if isinstance(r["domains"], list) else json.loads(r["domains"])
            result.append({
                "id": r["id"],
                "key_prefix": r["key_prefix"],
                "domains": domains,
                "created_at": _format_date(r["created_at"]),
            })
        return result
    rows = await db.execute(
        "SELECT id, key_prefix, domains, created_at FROM api_keys ORDER BY id DESC"
    )
    return [
        {"id": r["id"], "key_prefix": r["key_prefix"], "domains": json.loads(r["domains"]), "created_at": r["created_at"]}
        for r in await rows.fetchall()
    ]


@app.delete("/api/apikeys/{key_id}")
async def delete_api_key(key_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    if USE_SUPABASE:
        await db.delete("api_keys", {"id": f"eq.{key_id}"})
        return {"ok": True}
    await db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    await db.commit()
    return {"ok": True}


# ─── Bot API v1 Endpoints ────────────────────────────────────────

@app.post("/api/v1/generate")
async def api_v1_generate(
    request: Request,
    db=Depends(get_db),
):
    api_key = request.headers.get("x-api-key", "")
    if not api_key:
        raise HTTPException(401, "Missing X-API-Key header")
    key_info = await _validate_api_key(db, api_key)
    allowed_domains = key_info["domains"]

    data = await request.json()
    domain = data.get("domain", "random")
    count = min(max(data.get("count", 1), 1), 10)

    if domain == "random":
        domain = random.choice(allowed_domains)
    elif domain not in allowed_domains:
        raise HTTPException(403, f"Domain '{domain}' not allowed for this API key. Allowed: {', '.join(allowed_domains)}")

    created = []
    attempts = 0
    max_attempts = count * 10
    while len(created) < count and attempts < max_attempts:
        attempts += 1
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        email_addr = f"{username}@{domain}"
        token = secrets.token_hex(8).upper()

        if USE_SUPABASE:
            result = await db.insert("tokens", {
                "email": email_addr, "token_id": token,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            if result is None:
                continue
            created.append({"email": email_addr, "token": token})
        else:
            row = await db.execute("SELECT id FROM accounts WHERE email = ?", (email_addr,))
            if await row.fetchone():
                continue
            await db.execute(
                "INSERT INTO accounts (email, domain, token) VALUES (?, ?, ?)",
                (email_addr, domain, token),
            )
            created.append({"email": email_addr, "token": token})

    if not USE_SUPABASE and created:
        await db.commit()

    if not created:
        raise HTTPException(500, "Failed to generate accounts")
    return {"ok": True, "created": created}


@app.post("/api/v1/inbox")
async def api_v1_inbox(
    request: Request,
    db=Depends(get_db),
):
    api_key = request.headers.get("x-api-key", "")
    if not api_key:
        raise HTTPException(401, "Missing X-API-Key header")
    key_info = await _validate_api_key(db, api_key)

    data = await request.json()
    token_val = data.get("token", "")
    wait_seconds = min(max(data.get("wait", 0), 0), 60)

    if not token_val:
        raise HTTPException(400, "token is required")

    if USE_SUPABASE:
        rows = await db.select("tokens", {"token_id": f"eq.{token_val}", "select": "email"})
        if not rows:
            raise HTTPException(404, "Invalid token")
        email_addr = rows[0]["email"]
    else:
        row = await db.execute("SELECT email FROM accounts WHERE token = ?", (token_val,))
        account = await row.fetchone()
        if not account:
            raise HTTPException(404, "Invalid token")
        email_addr = account["email"]

    async def _fetch_emails():
        all_emails = []
        if USE_SUPABASE:
            cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=24)
            email_rows = await db.select("emails", {
                "recipient": f"eq.{email_addr}",
                "received_at": f"gte.{cutoff_dt.isoformat()}",
                "select": "id,sender,subject,body,received_at",
                "order": "received_at.desc",
            })
            all_emails.extend(email_rows)
        else:
            cutoff = datetime.now(timezone.utc).timestamp() - 86400
            cursor = await db.execute(
                """SELECT id, sender, subject, body, received_at FROM emails
                   WHERE recipient = ? AND strftime('%s', received_at) > ?
                   ORDER BY received_at DESC""",
                (email_addr, str(int(cutoff))),
            )
            all_emails.extend([dict(r) for r in await cursor.fetchall()])

        if USE_GMAIL:
            try:
                gmail_emails = await _fetch_gmail_emails(email_addr)
                existing_ids = {str(e.get("id")) for e in all_emails}
                for ge in gmail_emails:
                    if ge["id"] not in existing_ids:
                        all_emails.append(ge)
                all_emails.sort(key=lambda x: x.get("received_at", ""), reverse=True)
            except Exception:
                pass
        return all_emails

    emails = await _fetch_emails()

    if not emails and wait_seconds > 0:
        elapsed = 0
        interval = 5
        while elapsed < wait_seconds:
            await asyncio.sleep(interval)
            elapsed += interval
            emails = await _fetch_emails()
            if emails:
                break

    otp = None
    if emails:
        for em in emails:
            body = em.get("body", "")
            if em.get("content_type") == "text/html":
                plain = re.sub(r'<[^>]+>', ' ', body)
            else:
                plain = body
            extracted = _extract_otp(plain)
            if extracted:
                otp = extracted
                break

    return {
        "ok": True,
        "email": email_addr,
        "otp": otp,
        "emails": [
            {
                "sender": e.get("sender", ""),
                "subject": e.get("subject", ""),
                "body": e.get("body", ""),
                "received_at": e.get("received_at", ""),
            }
            for e in emails
        ],
    }


# ─── Stock Management ─────────────────────────────────────────────

STOK_DIR = os.path.join(BASE_DIR, "stok_data")


def _ensure_stok_dir():
    try:
        os.makedirs(STOK_DIR, exist_ok=True)
    except OSError:
        pass


def _stok_settings_path():
    return os.path.join(STOK_DIR, "settings.json")


def _stok_customers_path():
    return os.path.join(STOK_DIR, "customers.json")


def _stok_db_path(layanan: str):
    safe = re.sub(r'[^a-z0-9-]', '-', layanan.lower().strip())
    return os.path.join(STOK_DIR, f"db_{safe}.json")


def _read_json(path: str, default=None):
    if default is None:
        default = []
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json(path: str, data):
    _ensure_stok_dir()
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except OSError:
        pass


# ─── Head profiles (workspaces) ──────────────────────────────────

async def _read_heads():
    return await _stok_read("_heads", [])

async def _save_heads(data):
    await _stok_write("_heads", data)

def _head_prefix(head: str) -> str:
    return f"h_{head}_" if head else ""

# ─── Supabase stok_data helpers ──────────────────────────────────

async def _stok_read(key: str, default=None):
    if default is None:
        default = []
    if USE_SUPABASE and supabase:
        try:
            rows = await supabase.select("stok_data", {"key": f"eq.{key}", "select": "value"})
            if rows and rows[0].get("value") is not None:
                val = rows[0]["value"]
                return val if isinstance(val, (list, dict)) else json.loads(val)
        except Exception:
            pass
        return default
    if key == "_settings":
        path = _stok_settings_path()
    elif key == "_customers":
        path = _stok_customers_path()
    else:
        path = _stok_db_path(key[3:] if key.startswith("db_") else key)
    return _read_json(path, default)


async def _stok_write(key: str, data):
    if USE_SUPABASE and supabase:
        try:
            await supabase.upsert("stok_data", {"key": key, "value": json.dumps(data, default=str)}, on_conflict="key")
        except Exception:
            pass
        return
    if key == "_settings":
        path = _stok_settings_path()
    elif key == "_customers":
        path = _stok_customers_path()
    else:
        path = _stok_db_path(key[3:] if key.startswith("db_") else key)
    _write_json(path, data)


async def _read_stok_settings(head: str = ""):
    return await _stok_read(f"{_head_prefix(head)}_settings", {"storeName": "STORE", "storeTag": "Management", "loginPassword": "admin123"})


async def _save_stok_settings(data, head: str = ""):
    await _stok_write(f"{_head_prefix(head)}_settings", data)


async def _read_customers(head: str = ""):
    return await _stok_read(f"{_head_prefix(head)}_customers", [])


async def _save_customers(data, head: str = ""):
    await _stok_write(f"{_head_prefix(head)}_customers", data)


async def _get_layanan_list(head: str = ""):
    prefix = _head_prefix(head)
    db_prefix = f"{prefix}db_"
    if USE_SUPABASE and supabase:
        try:
            rows = await supabase.select("stok_data", {"key": f"like.{db_prefix}*", "select": "key", "order": "key"})
            return sorted([r["key"][len(db_prefix):] for r in rows if r["key"].startswith(db_prefix)])
        except Exception:
            return []
    _ensure_stok_dir()
    try:
        safe_prefix = re.sub(r'[^a-z0-9-]', '-', db_prefix.lower())
        file_prefix = f"db_{safe_prefix}"
        files = [f for f in os.listdir(STOK_DIR) if f.startswith(file_prefix) and f.endswith(".json")]
        strip = len(file_prefix)
        return sorted([f[strip:-5] for f in files])
    except OSError:
        return []


async def _read_stok_db(layanan: str, head: str = ""):
    return await _stok_read(f"{_head_prefix(head)}db_{layanan}", [])


async def _write_stok_db(layanan: str, data, head: str = ""):
    await _stok_write(f"{_head_prefix(head)}db_{layanan}", data)


async def _delete_stok_db(layanan: str, head: str = ""):
    key = f"{_head_prefix(head)}db_{layanan}"
    if USE_SUPABASE and supabase:
        try:
            await supabase.delete("stok_data", {"key": f"eq.{key}"})
        except Exception:
            pass
        return
    raw = f"{_head_prefix(head)}db_{layanan}"
    safe = re.sub(r'[^a-z0-9-]', '-', raw.lower().strip())
    path = os.path.join(STOK_DIR, f"db_{safe}.json")
    if os.path.exists(path):
        os.remove(path)


async def _stok_db_exists(layanan: str, head: str = "") -> bool:
    key = f"{_head_prefix(head)}db_{layanan}"
    if USE_SUPABASE and supabase:
        try:
            rows = await supabase.select("stok_data", {"key": f"eq.{key}", "select": "key"})
            return len(rows) > 0
        except Exception:
            return False
    raw = f"{_head_prefix(head)}db_{layanan}"
    safe = re.sub(r'[^a-z0-9-]', '-', raw.lower().strip())
    return os.path.exists(os.path.join(STOK_DIR, f"db_{safe}.json"))


def _sanitize_layanan(name: str) -> str:
    return re.sub(r'[^a-z0-9-]', '-', name.lower().strip())


# ─── Head Profile CRUD ──────────────────────────────────────────

@app.get("/api/stok/heads")
async def stok_list_heads(request: Request):
    require_admin(request)
    return {"heads": await _read_heads()}


@app.post("/api/stok/heads")
async def stok_add_head(request: Request):
    require_admin(request)
    data = await request.json()
    name = data.get("name", "").strip()
    if not name:
        raise HTTPException(400, "Name required")
    heads = await _read_heads()
    head = {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "avatar": data.get("avatar", ""),
        "pin": data.get("pin", ""),
    }
    heads.append(head)
    await _save_heads(heads)
    return {"ok": True, "head": head}


@app.put("/api/stok/heads/{head_id}")
async def stok_update_head(head_id: str, request: Request):
    require_admin(request)
    data = await request.json()
    heads = await _read_heads()
    for h in heads:
        if h["id"] == head_id:
            if "name" in data:
                h["name"] = data["name"]
            if "avatar" in data:
                h["avatar"] = data["avatar"]
            if "pin" in data:
                h["pin"] = data["pin"]
            await _save_heads(heads)
            return {"ok": True, "head": h}
    raise HTTPException(404, "Head profile not found")


@app.delete("/api/stok/heads/{head_id}")
async def stok_delete_head(head_id: str, request: Request):
    require_admin(request)
    heads = await _read_heads()
    heads = [h for h in heads if h["id"] != head_id]
    await _save_heads(heads)
    return {"ok": True}


@app.post("/api/stok/heads/verify")
async def stok_verify_head_pin(request: Request):
    require_admin(request)
    data = await request.json()
    head_id = data.get("headId", "")
    pin = data.get("pin", "")
    heads = await _read_heads()
    for h in heads:
        if h["id"] == head_id:
            if not h.get("pin"):
                return {"ok": True}
            if h["pin"] == pin:
                return {"ok": True}
            raise HTTPException(403, "Wrong PIN")
    raise HTTPException(404, "Head profile not found")


# ─── Stock API (all head-aware) ──────────────────────────────────

@app.get("/api/stok/layanan")
async def stok_list_layanan(request: Request, head: str = ""):
    require_admin(request)
    return {"layanan": await _get_layanan_list(head)}


@app.post("/api/stok/layanan")
async def stok_add_layanan(request: Request):
    require_admin(request)
    data = await request.json()
    name = _sanitize_layanan(data.get("name", ""))
    head = data.get("head", "")
    if not name:
        raise HTTPException(400, "Name is required")
    if await _stok_db_exists(name, head):
        raise HTTPException(400, "Service already exists")
    await _write_stok_db(name, [], head)
    return {"ok": True, "name": name}


@app.post("/api/stok/layanan/rename")
async def stok_rename_layanan(request: Request):
    require_admin(request)
    data = await request.json()
    old = _sanitize_layanan(data.get("old", ""))
    new = _sanitize_layanan(data.get("new", ""))
    head = data.get("head", "")
    if not old or not new:
        raise HTTPException(400, "Both old and new names required")
    if not await _stok_db_exists(old, head):
        raise HTTPException(404, "Service not found")
    if await _stok_db_exists(new, head):
        raise HTTPException(400, "New name already exists")
    old_data = await _read_stok_db(old, head)
    await _write_stok_db(new, old_data, head)
    await _delete_stok_db(old, head)
    customers = await _read_customers(head)
    for c in customers:
        if c.get("layanan") == old:
            c["layanan"] = new
    await _save_customers(customers, head)
    return {"ok": True, "name": new}


@app.delete("/api/stok/layanan/{name}")
async def stok_delete_layanan(name: str, request: Request, head: str = ""):
    require_admin(request)
    safe = _sanitize_layanan(name)
    await _delete_stok_db(safe, head)
    return {"ok": True}


@app.get("/api/stok/dashboard")
async def stok_dashboard(request: Request, view: str = "", head: str = ""):
    require_admin(request)
    layanan_list = await _get_layanan_list(head)
    current = view if view in layanan_list else (layanan_list[0] if layanan_list else "")
    accounts = []
    alerts = []
    summary = {}

    if current:
        all_accounts = await _read_stok_db(current, head)
        accounts = [a for a in all_accounts if not a.get("isSold", False)]
        now = datetime.now(timezone.utc)
        for a in accounts:
            if a.get("expiryDate"):
                try:
                    exp = datetime.fromisoformat(a["expiryDate"])
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    days_left = (exp - now).days
                    if days_left <= 7:
                        alerts.append({
                            "email": a["email"],
                            "daysLeft": days_left,
                            "expiryDate": a["expiryDate"],
                        })
                except (ValueError, TypeError):
                    pass

    for lay in layanan_list:
        all_acc = await _read_stok_db(lay, head)
        available = len([a for a in all_acc if not a.get("isSold", False)])
        sold = len([a for a in all_acc if a.get("isSold", False)])
        summary[lay] = {"available": available, "sold": sold, "total": len(all_acc)}

    return {
        "layananList": layanan_list,
        "currentView": current,
        "accounts": accounts,
        "alerts": alerts,
        "summary": summary,
    }


@app.post("/api/stok/import")
async def stok_bulk_import(request: Request):
    require_admin(request)
    data = await request.json()
    layanan = _sanitize_layanan(data.get("layanan", ""))
    bulk_data = data.get("bulkData", "")
    durasi = int(data.get("durasi", 30))
    profile_count = int(data.get("profileCount", 5))
    head = data.get("head", "")

    if not layanan or not bulk_data:
        raise HTTPException(400, "layanan and bulkData required")

    accounts = await _read_stok_db(layanan, head)

    lines = [l.strip() for l in bulk_data.strip().split("\n") if l.strip()]
    imported = 0
    for line in lines:
        parts = line.split("|")
        if len(parts) < 2:
            continue
        acc_email = parts[0].strip()
        acc_pass = parts[1].strip()
        expiry = (datetime.now(timezone.utc) + timedelta(days=durasi)).isoformat()
        profiles = [{"id": i, "name": f"PROFILE {i}", "user": None, "pin": "", "avatar": ""} for i in range(1, profile_count + 1)]
        accounts.append({
            "id": str(uuid.uuid4()),
            "email": acc_email,
            "password": acc_pass,
            "expiryDate": expiry,
            "isSold": False,
            "profiles": profiles,
        })
        imported += 1

    await _write_stok_db(layanan, accounts, head)
    return {"ok": True, "imported": imported}


@app.post("/api/stok/profile/{layanan}/{acc_id}/{profile_id}")
async def stok_update_profile(layanan: str, acc_id: str, profile_id: int, request: Request):
    require_admin(request)
    data = await request.json()
    head = data.get("head", "")
    safe = _sanitize_layanan(layanan)
    accounts = await _read_stok_db(safe, head)

    for acc in accounts:
        if acc["id"] == acc_id:
            for p in acc.get("profiles", []):
                if p["id"] == profile_id:
                    if "profileName" in data:
                        p["name"] = data["profileName"]
                    if "buyerName" in data:
                        p["user"] = data["buyerName"]
                    if "pin" in data:
                        p["pin"] = data["pin"]
                    if "avatar" in data:
                        p["avatar"] = data["avatar"]
                    break
            break

    await _write_stok_db(safe, accounts, head)
    return {"ok": True}


@app.post("/api/stok/verify-pin/{layanan}/{acc_id}/{profile_id}")
async def stok_verify_pin(layanan: str, acc_id: str, profile_id: int, request: Request):
    require_admin(request)
    data = await request.json()
    pin = data.get("pin", "")
    head = data.get("head", "")
    safe = _sanitize_layanan(layanan)
    accounts = await _read_stok_db(safe, head)

    for acc in accounts:
        if acc["id"] == acc_id:
            for p in acc.get("profiles", []):
                if p["id"] == profile_id:
                    if not p.get("pin"):
                        return {"ok": True, "profile": p, "account": {"email": acc["email"], "password": acc["password"], "expiryDate": acc.get("expiryDate", "")}}
                    if p["pin"] == pin:
                        return {"ok": True, "profile": p, "account": {"email": acc["email"], "password": acc["password"], "expiryDate": acc.get("expiryDate", "")}}
                    raise HTTPException(403, "Wrong PIN")
            raise HTTPException(404, "Profile not found")
    raise HTTPException(404, "Account not found")


@app.post("/api/stok/sell/{layanan}/{acc_id}")
async def stok_sell_account(layanan: str, acc_id: str, request: Request):
    require_admin(request)
    data = await request.json()
    pembeli = data.get("pembeli", "Unknown")
    is_slot = data.get("isSlot", False)
    slot_id = data.get("slotId", None)
    head = data.get("head", "")

    safe = _sanitize_layanan(layanan)
    accounts = await _read_stok_db(safe, head)

    for acc in accounts:
        if acc["id"] == acc_id:
            if is_slot and slot_id is not None:
                for p in acc.get("profiles", []):
                    if p["id"] == slot_id:
                        p["user"] = pembeli
                        break
                tipe = f"Slot (Profile {slot_id})"
            else:
                acc["isSold"] = True
                for p in acc.get("profiles", []):
                    p["user"] = pembeli
                tipe = "Full Account"

            customers = await _read_customers(head)
            customers.append({
                "id": str(uuid.uuid4()),
                "name": pembeli,
                "layanan": safe,
                "email": acc["email"],
                "tipe": tipe,
                "date": datetime.now(timezone.utc).isoformat(),
            })
            await _save_customers(customers, head)
            break

    await _write_stok_db(safe, accounts, head)
    return {"ok": True}


@app.post("/api/stok/bulk-action")
async def stok_bulk_action(request: Request):
    require_admin(request)
    data = await request.json()
    action = data.get("action", "")
    layanan = _sanitize_layanan(data.get("layanan", ""))
    ids = data.get("ids", [])
    pembeli = data.get("pembeli", "Unknown")
    head = data.get("head", "")

    accounts = await _read_stok_db(layanan, head)

    if action == "delete":
        accounts = [a for a in accounts if a["id"] not in ids]
    elif action == "sell":
        customers = await _read_customers(head)
        for acc in accounts:
            if acc["id"] in ids:
                acc["isSold"] = True
                for p in acc.get("profiles", []):
                    p["user"] = pembeli
                customers.append({
                    "id": str(uuid.uuid4()),
                    "name": pembeli,
                    "layanan": layanan,
                    "email": acc["email"],
                    "tipe": "Full Account",
                    "date": datetime.now(timezone.utc).isoformat(),
                })
        await _save_customers(customers, head)

    await _write_stok_db(layanan, accounts, head)
    return {"ok": True}


@app.delete("/api/stok/account/{layanan}/{acc_id}")
async def stok_delete_account(layanan: str, acc_id: str, request: Request, head: str = ""):
    require_admin(request)
    safe = _sanitize_layanan(layanan)
    accounts = await _read_stok_db(safe, head)
    accounts = [a for a in accounts if a["id"] != acc_id]
    await _write_stok_db(safe, accounts, head)
    return {"ok": True}


@app.get("/api/stok/history")
async def stok_history(request: Request, head: str = ""):
    require_admin(request)
    layanan_list = await _get_layanan_list(head)
    sold = []
    for lay in layanan_list:
        all_acc = await _read_stok_db(lay, head)
        for acc in all_acc:
            if acc.get("isSold", False):
                buyer = "Unknown"
                for p in acc.get("profiles", []):
                    if p.get("user"):
                        buyer = p["user"]
                        break
                sold.append({
                    "layanan": lay,
                    "email": acc["email"],
                    "tipe": "Full Account",
                    "pembeli": buyer,
                    "date": acc.get("soldDate", ""),
                })
            for p in acc.get("profiles", []):
                if p.get("user") and not acc.get("isSold", False):
                    sold.append({
                        "layanan": lay,
                        "email": acc["email"],
                        "tipe": f"Slot (Profile {p['id']})",
                        "pembeli": p["user"],
                        "date": "",
                    })

    customers = await _read_customers(head)
    for c in customers:
        found = False
        for s in sold:
            if s["email"] == c.get("email") and s["layanan"] == c.get("layanan"):
                s["date"] = c.get("date", "")
                found = True
                break
        if not found:
            sold.append({
                "layanan": c.get("layanan", ""),
                "email": c.get("email", ""),
                "tipe": c.get("tipe", ""),
                "pembeli": c.get("name", "Unknown"),
                "date": c.get("date", ""),
            })

    seen = set()
    unique = []
    for s in sold:
        key = f"{s['layanan']}|{s['email']}|{s['tipe']}|{s['pembeli']}"
        if key not in seen:
            seen.add(key)
            unique.append(s)

    unique.sort(key=lambda x: x.get("date", ""), reverse=True)
    return {"history": unique}


@app.get("/api/stok/customers")
async def stok_customers_api(request: Request, head: str = ""):
    require_admin(request)
    customers = await _read_customers(head)
    unique = {}
    for c in customers:
        name = c.get("name", "")
        if name not in unique:
            unique[name] = {"name": name, "count": 0, "lastDate": c.get("date", "")}
        unique[name]["count"] += 1
        if c.get("date", "") > unique[name]["lastDate"]:
            unique[name]["lastDate"] = c.get("date", "")
    result = sorted(unique.values(), key=lambda x: x["lastDate"], reverse=True)
    return result


@app.get("/api/stok/customer-check")
async def stok_customer_check(request: Request, name: str = "", head: str = ""):
    require_admin(request)
    if not name:
        return {"purchases": []}
    customers = await _read_customers(head)
    purchases = [c for c in customers if c.get("name", "").lower() == name.lower()]
    for p in purchases:
        layanan = p.get("layanan", "")
        acc_email = p.get("email", "")
        if layanan:
            all_acc = await _read_stok_db(layanan, head)
            for a in all_acc:
                if a.get("email") == acc_email:
                    if a.get("expiryDate"):
                        try:
                            exp = datetime.fromisoformat(a["expiryDate"])
                            if exp.tzinfo is None:
                                exp = exp.replace(tzinfo=timezone.utc)
                            days_left = (exp - datetime.now(timezone.utc)).days
                            p["daysLeft"] = days_left
                        except (ValueError, TypeError):
                            pass
                    break
    purchases.sort(key=lambda x: x.get("date", ""), reverse=True)
    return {"purchases": purchases}


@app.get("/api/stok/settings")
async def stok_get_settings(request: Request, head: str = ""):
    require_admin(request)
    return await _read_stok_settings(head)


@app.post("/api/stok/settings")
async def stok_save_settings(request: Request):
    require_admin(request)
    data = await request.json()
    head = data.get("head", "")
    settings = await _read_stok_settings(head)
    settings.update(data)
    await _save_stok_settings(settings, head)
    return {"ok": True}


@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("admin_session", path="/")
    return resp

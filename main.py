import os
import secrets
import string
import random
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
    emails = [dict(r) for r in await rows.fetchall()]
    return {"email": email_addr, "emails": emails}


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


@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("admin_session", path="/")
    return resp

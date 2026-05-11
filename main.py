import os
import secrets
import string
import random
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tempmail.db")

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


async def get_db():
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
    await init_db()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


async def get_setting(db, key: str) -> str:
    row = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    result = await row.fetchone()
    return result["value"] if result else ""


async def get_all_settings(db) -> dict:
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
        resp.set_cookie("admin_session", "authenticated", httponly=True)
        return resp
    site_name = await get_setting(db, "site_name")
    return templates.TemplateResponse(
        request, "login.html",
        {"site_name": site_name, "error": "Invalid credentials"},
    )


def require_admin(request: Request):
    if request.cookies.get("admin_session") != "authenticated":
        raise HTTPException(status_code=303, headers={"Location": "/gatekeeper"})


@app.get("/panel", response_class=HTMLResponse)
async def admin_panel(request: Request, db=Depends(get_db)):
    require_admin(request)
    settings = await get_all_settings(db)
    rows = await db.execute("SELECT domain FROM domains ORDER BY domain")
    domains = [r["domain"] for r in await rows.fetchall()]
    rows = await db.execute(
        "SELECT id, email, domain, token, created_at FROM accounts ORDER BY created_at DESC"
    )
    accounts = [dict(r) for r in await rows.fetchall()]
    return templates.TemplateResponse(
        request, "panel.html",
        {"settings": settings, "domains": domains, "accounts": accounts},
    )


# ─── Admin API ───────────────────────────────────────────────────

@app.get("/api/domains")
async def list_domains(request: Request, db=Depends(get_db)):
    require_admin(request)
    rows = await db.execute("SELECT domain FROM domains ORDER BY domain")
    return [r["domain"] for r in await rows.fetchall()]


@app.post("/api/domains")
async def add_domain(request: Request, domain: str = Form(...), db=Depends(get_db)):
    require_admin(request)
    domain = domain.strip().lstrip("@").lower()
    if not domain:
        raise HTTPException(400, "Domain cannot be empty")
    try:
        await db.execute("INSERT INTO domains (domain) VALUES (?)", (domain,))
        await db.commit()
    except Exception:
        raise HTTPException(400, "Domain already exists")
    return {"ok": True, "domain": domain}


@app.delete("/api/domains/{domain}")
async def delete_domain(domain: str, request: Request, db=Depends(get_db)):
    require_admin(request)
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
    for _ in range(count):
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
        token = secrets.token_hex(16)
        try:
            await db.execute(
                "INSERT INTO accounts (email, domain, token) VALUES (?, ?, ?)",
                (email_addr, domain, token),
            )
        except Exception:
            continue
        created.append({"email": email_addr, "token": token})

    await db.commit()
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
    token = secrets.token_hex(16)
    try:
        await db.execute(
            "INSERT INTO accounts (email, domain, token) VALUES (?, ?, ?)",
            (email, domain, token),
        )
        await db.commit()
    except Exception:
        raise HTTPException(400, "Email already exists")
    return {"email": email, "token": token}


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    await db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    await db.commit()
    return {"ok": True}


@app.get("/api/accounts")
async def list_accounts(request: Request, db=Depends(get_db)):
    require_admin(request)
    rows = await db.execute(
        "SELECT id, email, domain, token, created_at FROM accounts ORDER BY created_at DESC"
    )
    return [dict(r) for r in await rows.fetchall()]


@app.post("/api/settings")
async def update_settings(request: Request, db=Depends(get_db)):
    require_admin(request)
    data = await request.json()
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

    await db.execute(
        "INSERT INTO emails (recipient, sender, subject, body) VALUES (?, ?, ?, ?)",
        (recipient, sender, subject, body),
    )
    await db.commit()
    return {"ok": True}


@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("admin_session")
    return resp

# TempMail

A temporary email service with token-based access and an admin panel for managing email accounts and domains.

## Features

- **Token-based email access** — Enter a token to view incoming emails (Gmail-like layout)
- **30-second scan** — Click "Scan Email" to wait for new incoming messages
- **24-hour email retention** — Emails older than 24 hours are automatically hidden
- **Admin panel** — Hidden admin login at `/gatekeeper`
  - Generate emails (random or human name + numeric)
  - Manual create with custom email@domain
  - Manage email accounts (copy email|token, scan, delete)
  - Domain management (add/remove domains)
  - Settings (site name, admin credentials)

## Quick Start

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

- Main page: `http://localhost:8000`
- Admin login: `http://localhost:8000/gatekeeper` (default: admin / admin123)

## Setup Guide

See [SETUP_GUIDE.md](SETUP_GUIDE.md) for detailed setup instructions including Supabase and Gmail API configuration.

## Tech Stack

- **Backend**: FastAPI + SQLite (default) / Supabase (optional)
- **Frontend**: HTML/CSS/JS with dark theme
- **Template Engine**: Jinja2

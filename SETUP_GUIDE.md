# TempMail - Setup Guide

## Table of Contents
1. [Quick Start (SQLite - Default)](#quick-start)
2. [Supabase Setup](#supabase-setup)
3. [Gmail API Setup](#gmail-api-setup)
4. [Admin Configuration](#admin-configuration)
5. [Webhook Configuration](#webhook-configuration)

---

## Quick Start

The app works out of the box with SQLite. No external services required.

```bash
# Install dependencies
pip install -r requirements.txt
# or with uv:
uv sync

# Run the server
uvicorn main:app --host 0.0.0.0 --port 8000

# Access the app
# Main page: http://localhost:8000
# Admin login: http://localhost:8000/gatekeeper
# Default admin credentials: admin / admin123
```

---

## Supabase Setup

### 1. Create a Supabase Project
- Go to [https://supabase.com/dashboard](https://supabase.com/dashboard)
- Click **"New Project"**
- Choose your organization, name, and region
- Wait for the project to be provisioned

### 2. Get Your Credentials
- Go to **Project Settings** → **API**
- Copy the following:
  - **Project URL** (e.g., `https://xxxxxxxxxxxx.supabase.co`)
  - **anon public key** (for client-side access)
  - **service_role key** (for server-side access - keep this secret!)

### 3. Run the SQL Schema
Go to **SQL Editor** in Supabase dashboard and run the following SQL:

```sql
-- Create domains table
CREATE TABLE IF NOT EXISTS domains (
    id BIGSERIAL PRIMARY KEY,
    domain TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create accounts table
CREATE TABLE IF NOT EXISTS accounts (
    id BIGSERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    domain TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create emails table
CREATE TABLE IF NOT EXISTS emails (
    id BIGSERIAL PRIMARY KEY,
    recipient TEXT NOT NULL,
    sender TEXT NOT NULL,
    subject TEXT DEFAULT '',
    body TEXT DEFAULT '',
    received_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create settings table
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Insert default settings
INSERT INTO settings (key, value) VALUES ('site_name', 'TempMail') ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('admin_username', 'admin') ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('admin_password', 'admin123') ON CONFLICT (key) DO NOTHING;

-- Insert default domains
INSERT INTO domains (domain) VALUES ('beking.online') ON CONFLICT (domain) DO NOTHING;
INSERT INTO domains (domain) VALUES ('evoprem.store') ON CONFLICT (domain) DO NOTHING;

-- Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_emails_recipient ON emails(recipient);
CREATE INDEX IF NOT EXISTS idx_emails_received_at ON emails(received_at);
CREATE INDEX IF NOT EXISTS idx_accounts_token ON accounts(token);
CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email);

-- Enable Row Level Security (optional but recommended)
ALTER TABLE domains ENABLE ROW LEVEL SECURITY;
ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE emails ENABLE ROW LEVEL SECURITY;
ALTER TABLE settings ENABLE ROW LEVEL SECURITY;

-- Create policies for service_role access (full access)
CREATE POLICY "Service role full access" ON domains FOR ALL USING (true);
CREATE POLICY "Service role full access" ON accounts FOR ALL USING (true);
CREATE POLICY "Service role full access" ON emails FOR ALL USING (true);
CREATE POLICY "Service role full access" ON settings FOR ALL USING (true);

-- Stock Management data (JSONB key-value store)
CREATE TABLE IF NOT EXISTS stok_data (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE stok_data ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON stok_data FOR ALL USING (true);
```

### 4. Configure Environment Variables
Create a `.env` file in the project root:

```env
# Supabase Configuration
SUPABASE_URL=https://xxxxxxxxxxxx.supabase.co
SUPABASE_ANON_KEY=your-anon-key-here
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key-here

# Set database mode to supabase
DB_MODE=supabase
```

### 5. Auto-Delete Emails Older Than 24 Hours (Optional)
You can set up a Supabase scheduled function to clean up old emails:

Go to **SQL Editor** and run:

```sql
-- Create a function to delete old emails
CREATE OR REPLACE FUNCTION delete_old_emails()
RETURNS void AS $$
BEGIN
    DELETE FROM emails WHERE received_at < NOW() - INTERVAL '24 hours';
END;
$$ LANGUAGE plpgsql;

-- Schedule it to run every hour (requires pg_cron extension)
-- Enable pg_cron in Supabase: Database → Extensions → pg_cron
SELECT cron.schedule('delete-old-emails', '0 * * * *', 'SELECT delete_old_emails()');
```

---

## Gmail API Setup

### 1. Create a Google Cloud Project
- Go to [Google Cloud Console](https://console.cloud.google.com/)
- Create a new project or select an existing one
- Enable the **Gmail API**: Go to **APIs & Services** → **Library** → Search "Gmail API" → **Enable**

### 2. Create OAuth 2.0 Credentials
- Go to **APIs & Services** → **Credentials**
- Click **"Create Credentials"** → **"OAuth client ID"**
- Application type: **Web application**
- Authorized redirect URIs: Add `https://developers.google.com/oauthplayground`
- Click **Create** and note down the **Client ID** and **Client Secret**

### 3. Get a Refresh Token
- Go to [OAuth 2.0 Playground](https://developers.google.com/oauthplayground/)
- Click the ⚙️ (gear icon) in the top right
- Check **"Use your own OAuth credentials"**
- Enter your **Client ID** and **Client Secret**
- In the left panel, select **Gmail API v1** → `https://mail.google.com/`
- Click **"Authorize APIs"** → Sign in with the Gmail account you want to use
- Click **"Exchange authorization code for tokens"**
- Copy the **Refresh Token**

### 4. Configure Environment Variables
Add to your `.env` file:

```env
# Gmail API Configuration
GMAIL_CLIENT_ID=your-client-id.apps.googleusercontent.com
GMAIL_CLIENT_SECRET=your-client-secret
GMAIL_REFRESH_TOKEN=your-refresh-token

# The email address that will receive all incoming emails
GMAIL_EMAIL=your-email@gmail.com
```

### 5. Important Notes
- The Gmail account must have **"Less secure app access"** enabled, OR you must use OAuth 2.0 (which this setup uses)
- The refresh token may expire if not used for 6 months or if the user revokes access
- For production, consider using a **Google Workspace** account with a custom domain

---

## Admin Configuration

### Default Credentials
- **URL**: `http://your-domain/gatekeeper`
- **Username**: `admin`
- **Password**: `admin123`

### Changing Admin Credentials
1. Log into the admin panel
2. Go to **Settings** tab
3. Update the **Admin Username** and **Admin Password** fields
4. Click **Save Settings**

### Customizing Site Name
1. Log into the admin panel
2. Go to **Settings** tab
3. Change the **Site Name** (e.g., "xincro", "MyMail", etc.)
4. Click **Save Settings**
5. The name updates everywhere: main page, topbar, admin panel, browser title

---

## Webhook Configuration

The app provides a webhook endpoint for receiving incoming emails from an external mail server.

### Endpoint
```
POST /api/webhook/incoming
Content-Type: application/json
```

### Request Body
```json
{
    "recipient": "user@domain.com",
    "sender": "sender@example.com",
    "subject": "Email subject line",
    "body": "Email body content in plain text"
}
```

### Example with cURL
```bash
curl -X POST http://localhost:8000/api/webhook/incoming \
  -H "Content-Type: application/json" \
  -d '{
    "recipient": "user@beking.online",
    "sender": "noreply@example.com",
    "subject": "Welcome!",
    "body": "Thank you for signing up."
  }'
```

### Integrating with Mail Server
You can configure your mail server (Postfix, Haraka, etc.) to forward incoming emails to this webhook. Example Postfix transport configuration:

```
# /etc/postfix/transport
beking.online   webhook:
evoprem.store   webhook:
```

Then create a script that POSTs the email data to the webhook endpoint.

---

## Environment Variables Summary

| Variable | Description | Required |
|----------|-------------|----------|
| `DB_MODE` | Database mode: `sqlite` (default) or `supabase` | No |
| `SUPABASE_URL` | Supabase project URL | If using Supabase |
| `SUPABASE_ANON_KEY` | Supabase anonymous key | If using Supabase |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key | If using Supabase |
| `GMAIL_CLIENT_ID` | Google OAuth Client ID | If using Gmail API |
| `GMAIL_CLIENT_SECRET` | Google OAuth Client Secret | If using Gmail API |
| `GMAIL_REFRESH_TOKEN` | Gmail OAuth Refresh Token | If using Gmail API |
| `GMAIL_EMAIL` | Gmail email address | If using Gmail API |

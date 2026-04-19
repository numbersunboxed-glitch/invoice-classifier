# Invoice Classifier — Multi-User Setup Guide

Each user signs in with Google, picks their own Drive folder, and gets their own private invoice feed. Fully automated.

---

## What changed from single-user

| Single-user | Multi-user (this version) |
|---|---|
| One API key in env var | Same — one key, shared by all users |
| One Drive folder | Each user picks their own folder |
| Flat Excel file | PostgreSQL database, per-user data |
| No login | Google OAuth (sign in with Google) |
| One webhook | One webhook per user, auto-registered |

---

## Step 1 — Get your Anthropic API Key

Go to https://console.anthropic.com/api-keys → Create Key. Copy it.

---

## Step 2 — Create a Google OAuth App

This is what lets users click "Sign in with Google".

1. Go to https://console.cloud.google.com
2. Create a new project (or reuse existing)
3. Go to **APIs & Services → Library** → enable **Google Drive API**
4. Go to **APIs & Services → OAuth consent screen**
   - User type: **External**
   - Fill in App name, support email, developer email → Save
   - Scopes: add `drive` (Google Drive API)
   - Test users: add your own email while testing
5. Go to **APIs & Services → Credentials → Create Credentials → OAuth Client ID**
   - Application type: **Web application**
   - Authorized redirect URIs — add:
     `https://your-app.up.railway.app/auth/callback`
     (and `http://localhost:5000/auth/callback` for local testing)
6. Copy the **Client ID** and **Client Secret**

---

## Step 3 — Set up PostgreSQL on Railway

1. Go to https://railway.app → New Project
2. Add a **PostgreSQL** database (click + → Database → PostgreSQL)
3. Railway gives you a `DATABASE_URL` — it's automatically available to your app

---

## Step 4 — Deploy the app on Railway

1. Push this folder to a GitHub repo
2. In Railway → your project → **New Service → GitHub repo** → select it
3. Railway will build and deploy automatically
4. Go to **Settings → Domains** → generate a domain
   Copy the URL, e.g. `https://invoice-classifier.up.railway.app`

---

## Step 5 — Set Environment Variables

In Railway → your service → **Variables**, add:

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your key from Step 1 |
| `FLASK_SECRET` | Any random 32-char string |
| `APP_URL` | Your Railway domain from Step 4 (with https://) |
| `GOOGLE_CLIENT_ID` | From Step 2 |
| `GOOGLE_CLIENT_SECRET` | From Step 2 |
| `DATABASE_URL` | Auto-set by Railway when you add PostgreSQL |

After saving, Railway redeploys automatically.

---

## Step 6 — Update your Google OAuth redirect URI

Go back to Google Cloud Console → Credentials → your OAuth client → add your real Railway URL:
`https://your-app.up.railway.app/auth/callback`

---

## Step 7 — Test the full flow

1. Open your Railway URL
2. Click **Continue with Google** — sign in
3. You land on the dashboard
4. Click **Browse my folders** → pick your invoices folder
5. The webhook registers automatically — status turns green
6. Drop an invoice in the folder → it appears classified within seconds

---

## How it works for each new user

1. They visit your URL and click "Continue with Google"
2. Google asks for permission to access their Drive
3. They land on the dashboard and pick their invoices folder
4. A webhook is registered for their specific folder
5. Every invoice they drop there gets classified and saved to their private feed
6. They can export their own Excel at any time

---

## Webhook renewal

Drive webhooks expire every 7 days. The dashboard shows a warning when it expires and users can click "Reconnect" to renew. You can also add a background job to auto-renew — ask Claude if you want that.

---

## Going to production (publish to all Google users)

While your app is in "testing" mode in Google Cloud, only the test emails you added can sign in. To open it to everyone:

1. Google Cloud Console → OAuth consent screen
2. Click **Publish App** → Submit for verification
3. Google reviews it (takes a few days for apps requesting Drive access)

For internal company use, set user type to **Internal** instead — no review needed.

---

## File structure

```
invoice-saas/
├── app.py                 # Full Flask app — auth, DB, Drive, Claude, webhooks
├── templates/
│   ├── landing.html       # Sign-in page
│   └── dashboard.html     # Per-user dashboard
├── requirements.txt
├── Procfile
└── README.md
```

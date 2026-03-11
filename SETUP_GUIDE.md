# NSE Screener Pro — Deploy to Render.com (Phone-Only Guide)

## What you'll get
A fully hosted NSE screener accessible from any browser, anywhere,
no laptop or local server needed. Free forever on Render's free tier.

---

## Step 1 — Create a GitHub account (if you don't have one)
1. Open **github.com** on your phone
2. Tap **Sign up** → enter email, password, username
3. Verify your email

---

## Step 2 — Create a new GitHub repository
1. On GitHub, tap the **+** button (top right) → **New repository**
2. Name it: `nse-screener`
3. Set to **Public**
4. Tap **Create repository**

---

## Step 3 — Upload the files
You need to upload these 5 files from the zip:
- `app.py`
- `requirements.txt`
- `Procfile`
- `render.yaml`
- `nse_screener_live.html`

On GitHub:
1. Tap **uploading an existing file** (on the repo page)
2. Select all 5 files from your phone storage
3. Tap **Commit changes**

---

## Step 4 — Create a Render account
1. Open **render.com** on your phone
2. Tap **Get Started for Free**
3. Sign up with your GitHub account (easiest)

---

## Step 5 — Deploy on Render
1. On Render dashboard, tap **New +** → **Web Service**
2. Connect your GitHub account if prompted
3. Select your `nse-screener` repository
4. Render auto-detects the settings from `render.yaml`
5. Confirm these settings:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
   - **Instance Type:** Free
6. Tap **Create Web Service**

---

## Step 6 — Wait for deployment (~2 minutes)
Render will build and deploy. You'll see a green **Live** badge.
Your URL will be something like:
`https://nse-screener.onrender.com`

---

## Step 7 — Install as a home screen app
1. Open your Render URL in **Chrome on Android**
2. Tap the **three dots menu** (top right)
3. Tap **Add to Home screen**
4. Tap **Install**

You now have a full-screen NSE Screener app on your phone! 🎉

---

## Important notes
- **Free tier sleeps after 15 minutes of inactivity** — first load after sleep
  takes ~30 seconds to wake up. Subsequent loads are instant.
- **To avoid sleep:** Upgrade to Render's $7/month plan, or use UptimeRobot
  (free) to ping your URL every 10 minutes and keep it awake.
- **Data:** Live from Yahoo Finance + NSE India. Updates in real-time.

---

## Keep it awake for free (optional)
1. Go to **uptimerobot.com** → Sign up free
2. Add a new monitor: HTTP(s), your Render URL, every 5 minutes
3. Your app will never sleep again — completely free!


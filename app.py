"""
NSE Screener Proxy — Render.com
Stock data + WhatsApp price alerts via CallMeBot
"""
import json, time, os, csv, io, re, threading
from datetime import datetime, timezone
import requests
from flask import Flask, request as freq, jsonify, send_file, Response
from flask_cors import CORS

app  = Flask(__name__)
CORS(app)

CACHE_TTL   = 300
FII_TTL     = 1800

_stock_cache: dict = {}
_fii_cache   = {"ts": 0, "data": None}

# ── WhatsApp alert store ───────────────────────────────────────────────────────
# { phone: { key, alerts: [{id,sym,cond,price,triggered,at}], updated } }
_wa_store: dict = {}
_wa_lock  = threading.Lock()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")

def _sess():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


# ── Stock data ─────────────────────────────────────────────────────────────────

def fetch_yahoo(symbol: str):
    now = time.time()
    if symbol in _stock_cache:
        ts, data = _stock_cache[symbol]
        if now - ts < CACHE_TTL:
            return data
    for base in ["https://query1.finance.yahoo.com",
                 "https://query2.finance.yahoo.com"]:
        try:
            r = requests.get(
                f"{base}/v8/finance/chart/{symbol}.NS?interval=1d&range=1y",
                headers={"User-Agent": UA, "Accept": "application/json"},
                timeout=15)
            if r.ok:
                _stock_cache[symbol] = (now, r.content)
                return r.content
        except Exception:
            continue
    return None


def get_live_price(symbol: str) -> float | None:
    """Fetch latest price for a single symbol (uses 1m interval for freshness)."""
    for base in ["https://query1.finance.yahoo.com",
                 "https://query2.finance.yahoo.com"]:
        try:
            r = requests.get(
                f"{base}/v8/finance/chart/{symbol}.NS?interval=1m&range=1d",
                headers={"User-Agent": UA, "Accept": "application/json"},
                timeout=10)
            if r.ok:
                d = r.json()
                price = d.get("chart",{}).get("result",[{}])[0].get("meta",{}).get("regularMarketPrice")
                if price:
                    return float(price)
        except Exception:
            continue
    return None


# ── WhatsApp via CallMeBot ─────────────────────────────────────────────────────

def send_whatsapp(phone: str, key: str, message: str) -> tuple[bool, str]:
    """Send a WhatsApp message via CallMeBot. Returns (success, error_msg)."""
    try:
        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": phone, "text": message, "apikey": key},
            timeout=15
        )
        # CallMeBot returns 200 even on error — check body
        body = r.text.lower()
        if r.ok and ("message queued" in body or "message sent" in body
                     or "queued" in body or r.status_code == 200 and "error" not in body):
            return True, ""
        return False, r.text[:120]
    except Exception as e:
        return False, str(e)[:120]


# ── Market hours check ─────────────────────────────────────────────────────────

def is_market_open() -> bool:
    """True if current time is within NSE market hours IST (Mon-Fri 9:15-15:35)."""
    # IST = UTC+5:30
    now_utc = datetime.now(timezone.utc)
    ist_hour   = (now_utc.hour + 5) % 24
    ist_minute = (now_utc.minute + 30) % 60
    if (now_utc.minute + 30) >= 60:
        ist_hour = (ist_hour + 1) % 24
    ist_total = ist_hour * 60 + ist_minute
    # Mon=0 ... Fri=4
    if now_utc.weekday() > 4:
        return False
    return (9 * 60 + 15) <= ist_total <= (15 * 60 + 35)


# ── Background price checker ───────────────────────────────────────────────────

def _check_all_alerts():
    """Called every 5 minutes. Check all registered alerts, fire WhatsApp if triggered."""
    with _wa_lock:
        snapshot = {p: dict(v) for p, v in _wa_store.items()}

    for phone, info in snapshot.items():
        key    = info.get("key", "")
        alerts = info.get("alerts", [])
        active = [a for a in alerts if not a.get("triggered")]
        if not active:
            continue

        for alert in active:
            sym   = alert.get("sym", "")
            cond  = alert.get("cond", "above")
            target= float(alert.get("price", 0))
            aid   = alert.get("id")

            price = get_live_price(sym)
            if price is None:
                continue

            triggered = (cond == "above" and price >= target) or \
                        (cond == "below" and price <= target)

            if triggered:
                arrow  = "📈" if cond == "above" else "📉"
                dirstr = "crossed above" if cond == "above" else "crossed below"
                now_ist = datetime.now().strftime("%d %b %H:%M")
                msg = (f"{arrow} *NSE Alert* — *{sym}*\n"
                       f"Price ₹{price:.1f} {dirstr} your target ₹{target}\n"
                       f"Time: {now_ist} IST")

                ok, err = send_whatsapp(phone, key, msg)
                print(f"[WA] {sym} {cond} ₹{target} → price ₹{price:.1f} "
                      f"→ {'sent ✓' if ok else 'failed: '+err}")

                # Update store
                with _wa_lock:
                    if phone in _wa_store:
                        for a in _wa_store[phone]["alerts"]:
                            if a.get("id") == aid:
                                a["triggered"] = True
                                a["at"] = now_ist
                                a["sentOk"] = ok
                                break


def _alert_loop():
    """Background thread — checks alerts every 5 minutes."""
    print("[WA] Alert checker started")
    while True:
        time.sleep(300)   # 5 minutes
        try:
            if is_market_open():
                print(f"[WA] Checking alerts ({datetime.now().strftime('%H:%M')})")
                _check_all_alerts()
            else:
                print(f"[WA] Market closed — skipping check")
        except Exception as e:
            print(f"[WA] Checker error: {e}")


# Start background thread on import (Render/gunicorn will call this)
_checker = threading.Thread(target=_alert_loop, daemon=True)
_checker.start()


# ── FII/DII (kept minimal — just links in frontend now) ───────────────────────

def _safe(v):
    try: return float(str(v).replace(",","").replace("(","").replace(")","").strip() or 0)
    except: return 0.0

def _fmt(d):
    for f in ("%d-%b-%Y","%d/%m/%Y","%Y-%m-%dT%H:%M:%S","%Y-%m-%d",
              "%d-%m-%Y","%b %d, %Y","%d-%b-%y"):
        try: return datetime.strptime(str(d).strip()[:20], f).strftime("%d-%b-%y")
        except: continue
    return str(d).strip()[:11]


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file("nse_screener_live.html")

@app.route("/api/chart")
def chart():
    sym = "".join(c for c in freq.args.get("symbol","").upper()
                  if c.isalnum() or c in "-_.%")
    if not sym:
        return jsonify({"error": "missing symbol"}), 400
    data = fetch_yahoo(sym)
    if not data:
        return jsonify({"error": "upstream failed"}), 502
    return Response(data, mimetype="application/json")

@app.route("/api/health")
def health():
    with _wa_lock:
        wa_phones = len(_wa_store)
        wa_alerts = sum(len(v.get("alerts",[])) for v in _wa_store.values())
    return jsonify({
        "status": "ok",
        "stock_cache": len(_stock_cache),
        "wa_phones": wa_phones,
        "wa_alerts": wa_alerts,
        "market_open": is_market_open(),
    })


# ── WhatsApp API routes ────────────────────────────────────────────────────────

@app.route("/api/wa/test", methods=["POST"])
def wa_test():
    """Send a test WhatsApp message to verify phone + API key."""
    body = freq.get_json(silent=True) or {}
    phone = str(body.get("phone","")).strip()
    key   = str(body.get("key","")).strip()
    if not phone or not key:
        return jsonify({"ok": False, "error": "phone and key required"}), 400

    msg = ("✅ *NSE Screener Pro* connected!\n"
           "You will now receive WhatsApp alerts when your price targets are hit.\n"
           "Built by Saurabh Kumar 🚀")
    ok, err = send_whatsapp(phone, key, msg)
    if ok:
        # Register in store
        with _wa_lock:
            if phone not in _wa_store:
                _wa_store[phone] = {"key": key, "alerts": []}
            else:
                _wa_store[phone]["key"] = key
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": err})


@app.route("/api/wa/alerts", methods=["POST"])
def wa_set_alerts():
    """Sync alert list from frontend to server."""
    body   = freq.get_json(silent=True) or {}
    phone  = str(body.get("phone","")).strip()
    key    = str(body.get("key","")).strip()
    alerts = body.get("alerts", [])
    if not phone or not key:
        return jsonify({"ok": False, "error": "phone and key required"}), 400

    with _wa_lock:
        existing = _wa_store.get(phone, {})
        # Merge: keep server-side triggered state
        old_alerts = {a["id"]: a for a in existing.get("alerts", [])}
        merged = []
        for a in alerts:
            aid = a.get("id")
            if aid in old_alerts and old_alerts[aid].get("triggered"):
                merged.append(old_alerts[aid])   # keep triggered state
            else:
                merged.append(a)
        _wa_store[phone] = {"key": key, "alerts": merged, "updated": time.time()}

    return jsonify({"ok": True, "count": len(alerts)})


@app.route("/api/wa/triggered")
def wa_triggered():
    """Return list of alerts that were triggered server-side."""
    phone = freq.args.get("phone","").strip()
    if not phone:
        return jsonify({"triggered": []})
    with _wa_lock:
        info = _wa_store.get(phone, {})
    triggered = [
        {"id": a["id"], "sym": a["sym"], "at": a.get("at","")}
        for a in info.get("alerts", [])
        if a.get("triggered") and a.get("sentOk", True)
    ]
    return jsonify({"triggered": triggered})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


# ── Visitor tracking ───────────────────────────────────────────────────────────
import hashlib
from collections import defaultdict

_visitor_lock = threading.Lock()
_visitors = {
    "today_date":  "",
    "today_ips":   set(),
    "active":      {},   # ip_hash -> last_seen timestamp
}
_ACTIVE_TIMEOUT = 300   # seconds — "live" if seen in last 5 min


def _visitor_ip(req) -> str:
    """Anonymised visitor fingerprint (hashed IP)."""
    raw = req.headers.get("X-Forwarded-For", req.remote_addr or "unknown")
    ip  = raw.split(",")[0].strip()
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


def _record_visit(req):
    now      = time.time()
    today    = datetime.utcnow().strftime("%Y-%m-%d")
    ip_hash  = _visitor_ip(req)
    with _visitor_lock:
        # Roll daily counter at midnight UTC
        if _visitors["today_date"] != today:
            _visitors["today_date"] = today
            _visitors["today_ips"]  = set()
        _visitors["today_ips"].add(ip_hash)
        _visitors["active"][ip_hash] = now
        # Prune stale active sessions
        cutoff = now - _ACTIVE_TIMEOUT
        _visitors["active"] = {k: v for k, v in _visitors["active"].items() if v >= cutoff}


@app.before_request
def track_visitor():
    # Don't count health/asset requests
    if freq.path in ("/api/health", "/api/visitors") or freq.path.startswith("/static"):
        return
    _record_visit(freq)


@app.route("/api/visitors")
def visitors():
    now = time.time()
    with _visitor_lock:
        cutoff = now - _ACTIVE_TIMEOUT
        live   = sum(1 for v in _visitors["active"].values() if v >= cutoff)
        today  = len(_visitors["today_ips"])
    return jsonify({"live": live, "today": today})

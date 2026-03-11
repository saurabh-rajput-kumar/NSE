"""
NSE Screener Proxy — Render.com deployment
Uses requests library for better session/cookie handling with NSE India.
"""
import json, time, os, csv, io
from datetime import datetime
import requests
from flask import Flask, request as freq, jsonify, send_file, Response
from flask_cors import CORS

app  = Flask(__name__)
CORS(app)

CACHE_TTL = 300     # 5 min — stock data
FII_TTL   = 1800    # 30 min — FII (updates once daily after market close)

_stock_cache: dict = {}
_fii_cache = {"ts": 0, "data": None}

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

# ── Stock data ─────────────────────────────────────────────────────────────────

def fetch_yahoo(symbol: str):
    now = time.time()
    if symbol in _stock_cache:
        ts, data = _stock_cache[symbol]
        if now - ts < CACHE_TTL:
            return data
    for base in ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]:
        url = f"{base}/v8/finance/chart/{symbol}.NS?interval=1d&range=1y"
        try:
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
            if r.ok:
                _stock_cache[symbol] = (now, r.content)
                return r.content
        except Exception:
            continue
    return None


# ── FII/DII helpers ────────────────────────────────────────────────────────────

def _safe_num(v):
    try:
        return float(str(v).replace(",", "").replace("(", "-").replace(")", "").strip())
    except:
        return 0.0

def _fmt_date(d):
    for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%b %d, %Y", "%d-%b-%y"):
        try:
            return datetime.strptime(d.strip(), fmt).strftime("%d-%b-%y")
        except:
            continue
    return d.strip()[:11]

def _sort_days(days):
    def key(d):
        try: return datetime.strptime(d["d"], "%d-%b-%y")
        except: return datetime.min
    return sorted(days, key=key, reverse=True)


# ── Source 1: NSE fiidiiTradeReact (FII + DII, best data) ─────────────────────

def fetch_nse_json(session: requests.Session) -> list | None:
    try:
        # Warm up the session — NSE checks Referer + cookies
        session.get("https://www.nseindia.com/", timeout=10, headers={
            **BROWSER_HEADERS,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        time.sleep(0.5)

        r = session.get(
            "https://www.nseindia.com/api/fiidiiTradeReact",
            timeout=12,
            headers={
                **BROWSER_HEADERS,
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.nseindia.com/market-data/fii-dii-activity",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        if not r.ok:
            print(f"[NSE-JSON] HTTP {r.status_code}")
            return None

        raw = r.json()
        if not isinstance(raw, list) or not raw:
            return None

        days = []
        for rec in raw[:20]:
            d = _fmt_date(rec.get("date") or rec.get("tradeDate") or "")
            fn = _safe_num(rec.get("fiiNet") or rec.get("netFII") or
                           _safe_num(rec.get("fiiBuy","0")) - _safe_num(rec.get("fiiSell","0")))
            dn = _safe_num(rec.get("diiNet") or rec.get("netDII") or
                           _safe_num(rec.get("diiBuy","0")) - _safe_num(rec.get("diiSell","0")))
            if d:
                days.append({
                    "d": d, "fiiNet": round(fn,2), "diiNet": round(dn,2),
                    "fiiBuy":  round(_safe_num(rec.get("fiiBuy",0)),2),
                    "fiiSell": round(_safe_num(rec.get("fiiSell",0)),2),
                    "diiBuy":  round(_safe_num(rec.get("diiBuy",0)),2),
                    "diiSell": round(_safe_num(rec.get("diiSell",0)),2),
                })
        return days or None
    except Exception as e:
        print(f"[NSE-JSON] {e}")
        return None


# ── Source 2: NSE archives CSV (no JS, no cookies needed) ─────────────────────

def fetch_nse_csv(session: requests.Session, url: str, has_dii: bool) -> list | None:
    try:
        r = session.get(url, timeout=12, headers={
            **BROWSER_HEADERS,
            "Accept": "text/csv,text/plain,*/*",
            "Referer": "https://www.nseindia.com/",
        })
        if not r.ok:
            return None
        reader = csv.DictReader(io.StringIO(r.text))
        rows   = list(reader)
        if not rows:
            return None
        days = []
        for row in rows[-20:]:
            keys = list(row.keys())
            date_val = (row.get("Date") or row.get("date") or
                        (row.get(keys[0]) if keys else "")).strip()
            if not date_val:
                continue
            # FII columns
            fii_net = _safe_num(
                row.get("Net Value") or row.get("FII Net") or row.get("FII_NET") or
                str(_safe_num(row.get("Buy Value (Rs Cr)") or row.get(keys[1] if len(keys)>1 else "","0")) -
                    _safe_num(row.get("Sell Value (Rs Cr)") or row.get(keys[2] if len(keys)>2 else "","0")))
            )
            entry = {"d": _fmt_date(date_val), "fiiNet": round(fii_net,2), "diiNet": 0.0}
            if has_dii:
                entry["diiNet"] = round(_safe_num(
                    row.get("DII Net") or row.get("DII_NET") or "0"
                ), 2)
            days.append(entry)
        return days or None
    except Exception as e:
        print(f"[NSE-CSV] {url[-30:]} — {e}")
        return None


# ── Source 3: Tickertape public API (no auth, JSON) ───────────────────────────

def fetch_tickertape(session: requests.Session) -> list | None:
    """
    Tickertape exposes institutional activity in a simple JSON.
    URL: https://api.tickertape.in/market/fii-dii
    """
    try:
        r = session.get(
            "https://api.tickertape.in/market/fii-dii",
            timeout=10,
            headers={**BROWSER_HEADERS, "Accept": "application/json",
                     "Origin": "https://www.tickertape.in",
                     "Referer": "https://www.tickertape.in/"},
        )
        if not r.ok:
            return None
        raw = r.json()
        # Tickertape format: { data: [ { date, fiiNet, diiNet } ] }
        records = raw.get("data") or raw if isinstance(raw, list) else []
        days = []
        for rec in records[:20]:
            d = _fmt_date(str(rec.get("date","")).split("T")[0])
            fn = _safe_num(rec.get("fiiNet") or rec.get("fii_net") or 0)
            dn = _safe_num(rec.get("diiNet") or rec.get("dii_net") or 0)
            if d:
                days.append({"d": d, "fiiNet": round(fn,2), "diiNet": round(dn,2)})
        return days or None
    except Exception as e:
        print(f"[Tickertape] {e}")
        return None


# ── Main waterfall ─────────────────────────────────────────────────────────────

def fetch_fii() -> dict:
    now = time.time()
    if _fii_cache["data"] and now - _fii_cache["ts"] < FII_TTL:
        return _fii_cache["data"]

    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    days    = None
    src     = None
    has_dii = False

    steps = [
        ("NSE Live (JSON)",       lambda: fetch_nse_json(session),                          True),
        ("Tickertape",            lambda: fetch_tickertape(session),                         True),
        ("NSE Archives (FII+DII)",lambda: fetch_nse_csv(session,
            "https://archives.nseindia.com/content/equities/fiiDiiData.csv", True),         True),
        ("NSE Archives (FII)",    lambda: fetch_nse_csv(session,
            "https://archives.nseindia.com/content/equities/fiiData.csv",    False),        False),
    ]

    for name, fn, dii in steps:
        print(f"[FII] Trying {name}...")
        try:
            result = fn()
            if result:
                days    = result
                src     = name
                has_dii = dii
                print(f"[FII] ✓ {name} — {len(days)} days")
                break
        except Exception as e:
            print(f"[FII] {name} error: {e}")

    if not days:
        print("[FII] All sources failed")
        return {"live": False, "error": True,
                "source": "NSE unavailable", "days": []}

    for d in days:
        if d.get("diiNet") is None:
            d["diiNet"] = 0.0

    result = {
        "live":    True,
        "source":  src,
        "hasDII":  has_dii,
        "updated": datetime.now().strftime("%d %b %Y %H:%M IST"),
        "days":    _sort_days(days),
    }
    _fii_cache.update({"ts": now, "data": result})
    return result


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file("nse_screener_live.html")

@app.route("/api/chart")
def chart():
    sym = "".join(c for c in freq.args.get("symbol","").upper() if c.isalnum() or c in "-_.%")
    if not sym:
        return jsonify({"error": "missing symbol"}), 400
    data = fetch_yahoo(sym)
    if not data:
        return jsonify({"error": "upstream failed"}), 502
    return Response(data, mimetype="application/json")

@app.route("/api/fii")
def fii():
    return jsonify(fetch_fii())

@app.route("/api/health")
def health():
    age = int(time.time() - _fii_cache["ts"]) if _fii_cache["ts"] else -1
    return jsonify({"status": "ok", "stock_cache": len(_stock_cache), "fii_age_sec": age})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

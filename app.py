"""
NSE Screener Proxy — Render.com deployment
Fetches stock data from Yahoo Finance + FII/DII from multiple NSE/SEBI sources
"""
import json, time, os, csv, io, re
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS

app  = Flask(__name__)
CORS(app)

CACHE_TTL = 300    # 5 min — stock data
FII_TTL   = 1800   # 30 min — FII data (updates once a day after market close)

_stock_cache: dict = {}
_fii_cache = {"ts": 0, "data": None}

# ── Common headers ────────────────────────────────────────────────────────────

YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

NSE_JSON_HEADERS = dict(NSE_HEADERS, **{
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
})


# ── Stock data ────────────────────────────────────────────────────────────────

def fetch_yahoo(symbol: str):
    now = time.time()
    if symbol in _stock_cache:
        ts, data = _stock_cache[symbol]
        if now - ts < CACHE_TTL:
            return data
    for base in ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]:
        url = f"{base}/v8/finance/chart/{symbol}.NS?interval=1d&range=1y"
        try:
            data = urlopen(Request(url, headers=YF_HEADERS), timeout=15).read()
            _stock_cache[symbol] = (now, data)
            return data
        except Exception:
            continue
    return None


# ── FII/DII data — multi-source waterfall ─────────────────────────────────────

def _safe_num(v):
    try:
        return float(str(v).replace(",", "").replace("(", "-").replace(")", "").strip())
    except:
        return 0.0

def _fmt_date(d):
    """Normalise various date formats to DD-Mon-YY"""
    for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(d.strip(), fmt).strftime("%d-%b-%y")
        except:
            continue
    return d.strip()[:11]


def fetch_fii_nse_csv() -> list | None:
    """
    Source 1: NSE archives — fiiData.csv (Cash market FII)
    URL: https://archives.nseindia.com/content/equities/fiiData.csv
    No auth needed. Updated daily after market close.
    Columns: Date, Buy Value (Rs Cr), Sell Value (Rs Cr), Net Value
    """
    try:
        url = "https://archives.nseindia.com/content/equities/fiiData.csv"
        req = Request(url, headers=NSE_HEADERS)
        raw = urlopen(req, timeout=12).read().decode("utf-8", errors="ignore")
        reader = csv.DictReader(io.StringIO(raw))
        rows = list(reader)
        if not rows:
            return None
        days = []
        for row in rows[-20:]:  # last 20 trading days
            keys = list(row.keys())
            # Column names vary — find by position or keyword
            date_val = row.get("Date") or row.get(keys[0], "")
            buy_val  = _safe_num(row.get("Buy Value (Rs Cr)") or row.get(keys[1], "0"))
            sell_val = _safe_num(row.get("Sell Value (Rs Cr)") or row.get(keys[2], "0"))
            net_val  = _safe_num(row.get("Net Value") or row.get(keys[3], "0")) if len(keys) > 3 else buy_val - sell_val
            if date_val:
                days.append({
                    "d": _fmt_date(date_val),
                    "fiiNet": round(net_val, 2),
                    "fiiBuy": round(buy_val, 2),
                    "fiiSell": round(sell_val, 2),
                    "diiNet": None,   # this source has FII only
                })
        return days if days else None
    except Exception as e:
        print(f"[FII-CSV] {e}")
        return None


def fetch_fii_nse_json() -> list | None:
    """
    Source 2: NSE fiidiiTradeReact JSON (has both FII + DII)
    Requires 2-step cookie auth.
    """
    try:
        # Step 1: get cookies
        sess_req  = Request("https://www.nseindia.com/", headers=NSE_HEADERS)
        sess_resp = urlopen(sess_req, timeout=10)
        cookies   = sess_resp.headers.get("Set-Cookie", "")

        # Step 2: call API with cookies
        hdrs = dict(NSE_JSON_HEADERS)
        if cookies:
            hdrs["Cookie"] = "; ".join(
                c.split(";")[0] for c in cookies.split(",") if "=" in c
            )
        api_url = "https://www.nseindia.com/api/fiidiiTradeReact"
        raw     = json.loads(urlopen(Request(api_url, headers=hdrs), timeout=12).read())

        if not isinstance(raw, list) or not raw:
            return None

        days = []
        for rec in raw[:20]:
            date_str = _fmt_date(rec.get("date") or rec.get("tradeDate") or "")
            fii_net  = _safe_num(rec.get("fiiNet") or rec.get("netFII") or
                                  str(_safe_num(rec.get("fiiBuy","0")) - _safe_num(rec.get("fiiSell","0"))))
            dii_net  = _safe_num(rec.get("diiNet") or rec.get("netDII") or
                                  str(_safe_num(rec.get("diiBuy","0")) - _safe_num(rec.get("diiSell","0"))))
            fii_buy  = _safe_num(rec.get("fiiBuy", 0))
            fii_sell = _safe_num(rec.get("fiiSell", 0))
            dii_buy  = _safe_num(rec.get("diiBuy", 0))
            dii_sell = _safe_num(rec.get("diiSell", 0))
            if date_str:
                days.append({
                    "d": date_str,
                    "fiiNet":  round(fii_net, 2),
                    "diiNet":  round(dii_net, 2),
                    "fiiBuy":  round(fii_buy, 2),
                    "fiiSell": round(fii_sell, 2),
                    "diiBuy":  round(dii_buy, 2),
                    "diiSell": round(dii_sell, 2),
                })
        return days if days else None
    except Exception as e:
        print(f"[FII-JSON] {e}")
        return None


def fetch_fii_nse_archive_dii() -> list | None:
    """
    Source 3: NSE archives — combined FII+DII report CSV
    https://archives.nseindia.com/content/equities/fiiDiiData.csv
    """
    try:
        url = "https://archives.nseindia.com/content/equities/fiiDiiData.csv"
        req = Request(url, headers=NSE_HEADERS)
        raw = urlopen(req, timeout=12).read().decode("utf-8", errors="ignore")
        reader = csv.DictReader(io.StringIO(raw))
        rows   = list(reader)
        if not rows:
            return None
        days = []
        for row in rows[-20:]:
            keys = list(row.keys())
            date_val = row.get("Date") or (row.get(keys[0], "") if keys else "")
            # Try common column name patterns
            fii_net  = _safe_num(row.get("FII Net") or row.get("FII_NET") or
                                  row.get("Net FII", "0"))
            dii_net  = _safe_num(row.get("DII Net") or row.get("DII_NET") or
                                  row.get("Net DII", "0"))
            if date_val:
                days.append({
                    "d": _fmt_date(date_val),
                    "fiiNet": round(fii_net, 2),
                    "diiNet": round(dii_net, 2),
                })
        return days if days else None
    except Exception as e:
        print(f"[FII-DII-CSV] {e}")
        return None


def fetch_fii() -> dict:
    """
    Waterfall: try 3 sources, merge best available data, return structured result.
    """
    now = time.time()
    if _fii_cache["data"] and now - _fii_cache["ts"] < FII_TTL:
        return _fii_cache["data"]

    days  = None
    src   = "Unknown"
    has_dii = False

    # Try sources in order of reliability
    print("[FII] Trying NSE fiidiiTradeReact JSON...")
    days = fetch_fii_nse_json()
    if days:
        src = "NSE India (Live)"
        has_dii = any(d.get("diiNet") is not None for d in days)
        print(f"[FII] JSON success — {len(days)} days, DII: {has_dii}")

    if not days:
        print("[FII] Trying NSE fiiDiiData.csv...")
        days = fetch_fii_nse_archive_dii()
        if days:
            src = "NSE Archives (CSV)"
            has_dii = True
            print(f"[FII] fiiDiiData.csv success — {len(days)} days")

    if not days:
        print("[FII] Trying NSE fiiData.csv (FII only)...")
        days = fetch_fii_nse_csv()
        if days:
            src = "NSE Archives (FII only)"
            has_dii = False
            print(f"[FII] fiiData.csv success — {len(days)} days")

    if not days:
        print("[FII] All sources failed — returning error")
        return {"live": False, "error": True, "source": "All sources failed", "days": []}

    # Fill missing diiNet with 0 if source doesn't have it
    for d in days:
        if d.get("diiNet") is None:
            d["diiNet"] = 0.0

    # Sort newest first
    def sort_key(d):
        try:
            return datetime.strptime(d["d"], "%d-%b-%y")
        except:
            return datetime.min
    days = sorted(days, key=sort_key, reverse=True)

    result = {
        "live":    True,
        "source":  src,
        "hasDII":  has_dii,
        "updated": datetime.now().strftime("%d %b %Y %H:%M"),
        "days":    days,
    }
    _fii_cache.update({"ts": now, "data": result})
    return result


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file("nse_screener_live.html")

@app.route("/api/chart")
def chart():
    sym = "".join(c for c in request.args.get("symbol","").upper() if c.isalnum() or c in "-_.%")
    if not sym:
        return jsonify({"error": "missing symbol"}), 400
    data = fetch_yahoo(sym)
    if not data:
        return jsonify({"error": "upstream failed"}), 502
    return Response(data, mimetype="application/json")

@app.route("/api/fii")
def fii():
    data = fetch_fii()
    return jsonify(data)

@app.route("/api/health")
def health():
    fii_age = int(time.time() - _fii_cache["ts"]) if _fii_cache["ts"] else -1
    return jsonify({
        "status": "ok",
        "stock_cache": len(_stock_cache),
        "fii_cache_age_sec": fii_age,
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

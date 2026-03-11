"""
NSE Screener Proxy — Render.com
FII/DII strategy: persistent cache (never expire) + SEBI govt servers + BSE + NSE
"""
import json, time, os, csv, io, re
from datetime import datetime
import requests
from flask import Flask, request as freq, jsonify, send_file, Response
from flask_cors import CORS

app  = Flask(__name__)
CORS(app)

CACHE_TTL     = 300    # stock data — 5 min
FII_REFRESH   = 3600   # try refreshing FII every hour
# FII data is NEVER fully expired — always serve last known good data

_stock_cache: dict = {}
# Persistent FII store: survives indefinitely between refreshes
_fii_store = {"ts": 0, "last_good": None, "source": None}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")

def _sess():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s

def _safe(v):
    try:
        return float(str(v).replace(",","").replace("(","").replace(")","").strip() or 0)
    except:
        return 0.0

def _fmt(d):
    for f in ("%d-%b-%Y","%d/%m/%Y","%Y-%m-%dT%H:%M:%S","%Y-%m-%d",
              "%d-%m-%Y","%b %d, %Y","%d-%b-%y"):
        try:
            return datetime.strptime(str(d).strip()[:20], f).strftime("%d-%b-%y")
        except:
            continue
    return str(d).strip()[:11]

def _sort(days):
    def k(d):
        try: return datetime.strptime(d["d"], "%d-%b-%y")
        except: return datetime.min
    return sorted(days, key=k, reverse=True)


# ── Stock data ─────────────────────────────────────────────────────────────────

def fetch_yahoo(symbol):
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


# ── FII Sources ────────────────────────────────────────────────────────────────

def src_sebi(s) -> list | None:
    """
    SEBI FPI (=FII) data — govt server, no Cloudflare.
    SEBI publishes daily FPI buy/sell in HTML table at sebiweb.
    """
    try:
        r = s.get(
            "https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doFiiDii=yes",
            headers={"Accept": "text/html", "Referer": "https://www.sebi.gov.in/"},
            timeout=14)
        if not r.ok:
            return None
        # Parse HTML table — rows like: Date | Gross Buy | Gross Sell | Net
        rows = re.findall(
            r'<tr[^>]*>.*?</tr>', r.text, re.DOTALL | re.IGNORECASE)
        days = []
        for row in rows:
            cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row,
                               re.DOTALL | re.IGNORECASE)
            cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
            if len(cells) >= 4:
                try:
                    d   = _fmt(cells[0])
                    buy = _safe(cells[1])
                    sel = _safe(cells[2])
                    net = _safe(cells[3]) if cells[3] not in ('','-') else buy - sel
                    if d and buy > 0:
                        days.append({"d": d, "fiiNet": round(net,2),
                                     "diiNet": 0.0,
                                     "fiiBuy": round(buy,2),
                                     "fiiSell": round(sel,2)})
                except Exception:
                    continue
        return days[:20] if days else None
    except Exception as e:
        print(f"[SEBI] {e}")
        return None


def src_bse(s) -> list | None:
    """BSE India API — usually reachable from cloud IPs."""
    try:
        r = s.get(
            "https://api.bseindia.com/BseIndiaAPI/api/FIIDIITradeReact/w",
            headers={"Referer": "https://www.bseindia.com/",
                     "Accept": "application/json"},
            timeout=12)
        if not r.ok:
            return None
        raw = r.json()
        records = raw if isinstance(raw,list) else raw.get("Table") or raw.get("data") or []
        days = []
        for rec in records[:20]:
            d   = _fmt(str(rec.get("Dt") or rec.get("date") or rec.get("TradeDate") or ""))
            fn  = _safe(rec.get("FIINetPurchase") or rec.get("fiiNet") or
                        _safe(rec.get("FIIPurchase","0")) - _safe(rec.get("FIISales","0")))
            dn  = _safe(rec.get("DIINetPurchase") or rec.get("diiNet") or
                        _safe(rec.get("DIIPurchase","0")) - _safe(rec.get("DIISales","0")))
            if d:
                days.append({
                    "d": d, "fiiNet": round(fn,2), "diiNet": round(dn,2),
                    "fiiBuy":  round(_safe(rec.get("FIIPurchase","0")),2),
                    "fiiSell": round(_safe(rec.get("FIISales","0")),2),
                    "diiBuy":  round(_safe(rec.get("DIIPurchase","0")),2),
                    "diiSell": round(_safe(rec.get("DIISales","0")),2),
                })
        return days or None
    except Exception as e:
        print(f"[BSE] {e}")
        return None


def src_nse_csv(s, url, has_dii=False) -> list | None:
    """NSE static archive CSV — no JS, minimal protection."""
    try:
        r = s.get(url, headers={"Referer": "https://www.nseindia.com/",
                                  "Accept": "text/csv,*/*"}, timeout=12)
        if not r.ok:
            return None
        reader = csv.DictReader(io.StringIO(r.text))
        rows   = list(reader)
        if not rows:
            return None
        days = []
        for row in rows[-20:]:
            keys = list(row.keys())
            dv = (row.get("Date") or (row[keys[0]] if keys else "")).strip()
            if not dv:
                continue
            fn = _safe(row.get("Net Value") or row.get("FII Net") or
                       str(_safe(row.get("Buy Value (Rs Cr)",
                           row.get(keys[1],"0") if len(keys)>1 else "0")) -
                           _safe(row.get("Sell Value (Rs Cr)",
                           row.get(keys[2],"0") if len(keys)>2 else "0"))))
            dn = _safe(row.get("DII Net","0")) if has_dii else 0.0
            days.append({"d": _fmt(dv), "fiiNet": round(fn,2), "diiNet": round(dn,2)})
        return days or None
    except Exception as e:
        print(f"[CSV] {e}")
        return None


def src_nse_json(s) -> list | None:
    """NSE JSON API with session warm-up."""
    try:
        s.get("https://www.nseindia.com/", timeout=10,
              headers={"Accept": "text/html,*/*"})
        time.sleep(0.6)
        r = s.get("https://www.nseindia.com/api/fiidiiTradeReact",
                  headers={"Referer": "https://www.nseindia.com/market-data/fii-dii-activity",
                           "Accept": "application/json",
                           "X-Requested-With": "XMLHttpRequest"},
                  timeout=14)
        if not r.ok:
            return None
        raw = r.json()
        if not isinstance(raw, list) or not raw:
            return None
        days = []
        for rec in raw[:20]:
            d  = _fmt(str(rec.get("date") or rec.get("tradeDate") or ""))
            fn = _safe(rec.get("fiiNet") or
                       _safe(rec.get("fiiBuy","0")) - _safe(rec.get("fiiSell","0")))
            dn = _safe(rec.get("diiNet") or
                       _safe(rec.get("diiBuy","0")) - _safe(rec.get("diiSell","0")))
            if d:
                days.append({
                    "d": d, "fiiNet": round(fn,2), "diiNet": round(dn,2),
                    "fiiBuy":  round(_safe(rec.get("fiiBuy",0)),2),
                    "fiiSell": round(_safe(rec.get("fiiSell",0)),2),
                    "diiBuy":  round(_safe(rec.get("diiBuy",0)),2),
                    "diiSell": round(_safe(rec.get("diiSell",0)),2),
                })
        return days or None
    except Exception as e:
        print(f"[NSE-JSON] {e}")
        return None


# ── Main waterfall ─────────────────────────────────────────────────────────────

def try_fetch_fii() -> tuple[list | None, str, bool]:
    """Try all sources. Returns (days, source_name, has_dii)."""
    s = _sess()
    # Try cloudscraper if available
    try:
        import cloudscraper
        cs = cloudscraper.create_scraper(
            browser={"browser":"chrome","platform":"windows","mobile":False})
    except Exception:
        cs = s

    sources = [
        ("SEBI (Govt)",             lambda: src_sebi(s),                                    False),
        ("BSE India API",           lambda: src_bse(s),                                     True),
        ("NSE JSON",                lambda: src_nse_json(cs),                               True),
        ("NSE Archives (FII+DII)",  lambda: src_nse_csv(s,
            "https://archives.nseindia.com/content/equities/fiiDiiData.csv", True),         True),
        ("NSE Archives (FII)",      lambda: src_nse_csv(s,
            "https://archives.nseindia.com/content/equities/fiiData.csv",    False),        False),
    ]

    for name, fn, has_dii in sources:
        print(f"[FII] Trying {name}...")
        try:
            days = fn()
            if days:
                print(f"[FII] ✓ {name} — {len(days)} days")
                for d in days:
                    if d.get("diiNet") is None:
                        d["diiNet"] = 0.0
                return _sort(days), name, has_dii
        except Exception as e:
            print(f"[FII] {name} error: {e}")

    return None, "unavailable", False


def fetch_fii() -> dict:
    now = time.time()
    needs_refresh = (now - _fii_store["ts"]) > FII_REFRESH

    if needs_refresh:
        days, src, has_dii = try_fetch_fii()
        if days:
            # Persist this — never expire it
            _fii_store.update({
                "ts": now,
                "last_good": days,
                "source": src,
                "has_dii": has_dii,
                "fetched_at": datetime.now().strftime("%d %b %Y %H:%M IST"),
            })

    if _fii_store["last_good"]:
        age_min = int((now - _fii_store["ts"]) / 60)
        age_str = (f"{age_min}m ago" if age_min < 60
                   else f"{age_min//60}h ago" if age_min < 1440
                   else f"{age_min//1440}d ago")
        return {
            "live":     True,
            "source":   _fii_store["source"],
            "hasDII":   _fii_store.get("has_dii", False),
            "updated":  _fii_store.get("fetched_at",""),
            "age":      age_str,
            "days":     _fii_store["last_good"],
        }

    # Nothing ever fetched successfully
    return {"live": False, "error": True,
            "source": "All sources blocked", "days": []}


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file("nse_screener_live.html")

@app.route("/api/chart")
def chart():
    sym = "".join(c for c in freq.args.get("symbol","").upper()
                  if c.isalnum() or c in "-_.%")
    if not sym:
        return jsonify({"error":"missing symbol"}), 400
    data = fetch_yahoo(sym)
    if not data:
        return jsonify({"error":"upstream failed"}), 502
    return Response(data, mimetype="application/json")

@app.route("/api/fii")
def fii():
    return jsonify(fetch_fii())

@app.route("/api/health")
def health():
    age = int(time.time() - _fii_store["ts"]) if _fii_store["ts"] else -1
    return jsonify({"status":"ok", "stock_cache":len(_stock_cache),
                    "fii_age_sec":age, "fii_source":_fii_store.get("source")})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))

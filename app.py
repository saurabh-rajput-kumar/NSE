"""
NSE Screener Proxy — Render.com deployment
Flask app that proxies Yahoo Finance + NSE FII/DII data
"""
import json, time, os
from urllib.request import urlopen, Request
from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS

app  = Flask(__name__)
CORS(app)

CACHE_TTL = 300
FII_TTL   = 600
_stock_cache: dict = {}
_fii_cache = {"ts": 0, "data": None}

YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

def fetch_yahoo(symbol):
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

def _safe_num(v):
    try: return float(str(v).replace(",",""))
    except: return 0.0

def fetch_fii():
    now = time.time()
    if _fii_cache["data"] and now - _fii_cache["ts"] < FII_TTL:
        return _fii_cache["data"]
    try:
        home_resp = urlopen(Request("https://www.nseindia.com/", headers=NSE_HEADERS), timeout=10)
        cookies   = home_resp.headers.get("Set-Cookie", "")
        hdrs = dict(NSE_HEADERS)
        if cookies: hdrs["Cookie"] = cookies
        raw  = json.loads(urlopen(Request("https://www.nseindia.com/api/fiidiiTradeReact", headers=hdrs), timeout=12).read())
        days = []
        for rec in raw[:20]:
            d = (rec.get("date") or rec.get("tradeDate") or "")[:11]
            fn = rec.get("fiiNet") or _safe_num(rec.get("fiiBuy","0")) - _safe_num(rec.get("fiiSell","0"))
            dn = rec.get("diiNet") or _safe_num(rec.get("diiBuy","0")) - _safe_num(rec.get("diiSell","0"))
            if d: days.append({"d": d, "fiiNet": round(float(fn),2), "diiNet": round(float(dn),2)})
        result = {"live": True, "source": "NSE India", "days": days}
        _fii_cache.update({"ts": now, "data": result})
        return result
    except Exception as e:
        print(f"[FII] {e}")
        return None

@app.route("/")
def index():
    return send_file("nse_screener_live.html")

@app.route("/api/chart")
def chart():
    sym = "".join(c for c in request.args.get("symbol","").upper() if c.isalnum() or c in "-_.%")
    if not sym: return jsonify({"error":"missing symbol"}), 400
    data = fetch_yahoo(sym)
    if not data: return jsonify({"error":"upstream failed"}), 502
    return Response(data, mimetype="application/json")

@app.route("/api/fii")
def fii():
    data = fetch_fii()
    if not data: return jsonify({"error":"NSE fetch failed","live":False}), 502
    return jsonify(data)

@app.route("/api/health")
def health():
    return jsonify({"status":"ok","cache":len(_stock_cache)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))

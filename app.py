"""
NSE Screener Proxy — Render.com
Stock data + WhatsApp price alerts via CallMeBot + AI Strategy Analyzer
"""
import json, time, os, csv, io, re, threading, math
from datetime import datetime, timezone
import requests
from flask import Flask, request as freq, jsonify, send_file, Response
from flask_cors import CORS

app  = Flask(__name__)
CORS(app)

CACHE_TTL   = 300
FII_TTL     = 1800
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

_stock_cache: dict = {}
_fii_cache   = {"ts": 0, "data": None}

# ── TV signal store ────────────────────────────────────────────────────────────
# Stores last 200 webhook signals from TradingView
_tv_signals: list = []
_tv_lock = threading.Lock()
MAX_TV_SIGNALS = 200

# ── WhatsApp alert store ───────────────────────────────────────────────────────
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

def fetch_yahoo(symbol: str, interval: str = "1d", range_: str = "2y"):
    """Fetch OHLCV from Yahoo Finance. interval: 1d/60m/15m. range: 1d/5d/1mo/6mo/1y/2y"""
    cache_key = f"{symbol}_{interval}_{range_}"
    now = time.time()
    if cache_key in _stock_cache:
        ts, data = _stock_cache[cache_key]
        ttl = 60 if interval in ("15m","60m") else CACHE_TTL
        if now - ts < ttl:
            return data
    for base in ["https://query1.finance.yahoo.com",
                 "https://query2.finance.yahoo.com"]:
        try:
            r = requests.get(
                f"{base}/v8/finance/chart/{symbol}?interval={interval}&range={range_}",
                headers={"User-Agent": UA, "Accept": "application/json"},
                timeout=20)
            if r.ok:
                _stock_cache[cache_key] = (now, r.content)
                return r.content
        except Exception:
            continue
    return None


def get_live_price(symbol: str) -> float | None:
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


# ── AI Strategy Engine ─────────────────────────────────────────────────────────

def _claude(system: str, user: str, max_tokens: int = 2000) -> str:
    """Call Claude claude-sonnet-4-20250514 and return text response."""
    if not ANTHROPIC_API_KEY:
        return ""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=60,
        )
        if r.ok:
            return r.json()["content"][0]["text"]
    except Exception as e:
        print(f"[Claude] Error: {e}")
    return ""


def _parse_ohlcv(raw: bytes, symbol: str):
    """Parse Yahoo Finance response into list of bar dicts."""
    try:
        d = json.loads(raw)
        res = d["chart"]["result"][0]
        q   = res["indicators"]["quote"][0]
        ts  = res.get("timestamp", [])
        closes  = q.get("close",  [])
        opens   = q.get("open",   [])
        highs   = q.get("high",   [])
        lows    = q.get("low",    [])
        volumes = q.get("volume", [])
        bars = []
        for i, t in enumerate(ts):
            c = closes[i]
            if c is None or c <= 0:
                continue
            bars.append({
                "t": t,
                "d": datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"),
                "o": round(opens[i]  or c, 2),
                "h": round(highs[i]  or c, 2),
                "l": round(lows[i]   or c, 2),
                "c": round(c,              2),
                "v": int(volumes[i]  or 0),
            })
        return bars
    except Exception as e:
        print(f"[parse_ohlcv] {e}")
        return []


def _compute_indicators(bars: list) -> dict:
    """Compute EMA, RSI, ATR, BB for the latest bar."""
    if len(bars) < 20:
        return {}
    closes  = [b["c"] for b in bars]
    highs   = [b["h"] for b in bars]
    lows    = [b["l"] for b in bars]
    volumes = [b["v"] for b in bars]

    def ema(arr, p):
        k = 2/(p+1); e = arr[0]
        for x in arr[1:]: e = x*k + e*(1-k)
        return round(e, 2)

    def rsi(arr, p=14):
        if len(arr) < p+1: return 50.0
        gains, losses = [], []
        for i in range(1, p+1):
            d = arr[i]-arr[i-1]
            gains.append(max(d,0)); losses.append(max(-d,0))
        ag, al = sum(gains)/p, sum(losses)/p
        for i in range(p+1, len(arr)):
            d = arr[i]-arr[i-1]
            ag = (ag*(p-1)+max(d,0))/p; al = (al*(p-1)+max(-d,0))/p
        return round(100-100/(1+ag/al) if al else 100, 1)

    n = len(closes)
    atr_vals = []
    for i in range(1, min(15, n)):
        atr_vals.append(max(highs[-i]-lows[-i],
                            abs(highs[-i]-closes[-i-1]),
                            abs(lows[-i]-closes[-i-1])))
    atr = round(sum(atr_vals)/len(atr_vals), 2) if atr_vals else 0

    avg_vol = sum(volumes[-21:-1])/20 if n >= 21 else sum(volumes)/max(len(volumes),1)

    return {
        "ema9":   ema(closes[-15:],   9),
        "ema20":  ema(closes[-25:],  20),
        "ema50":  ema(closes[-60:],  50) if n >= 55 else None,
        "ema200": ema(closes[-210:],200) if n >= 205 else None,
        "rsi14":  rsi(closes[-20:],  14),
        "atr14":  atr,
        "avg_vol_20": int(avg_vol),
        "vol_ratio":  round(volumes[-1]/avg_vol, 2) if avg_vol else 1.0,
        "hi52":   max(closes[-252:]) if n >= 252 else max(closes),
        "lo52":   min(closes[-252:]) if n >= 252 else min(closes),
        "price":  closes[-1],
    }


def _generate_strategy(symbol: str, bars: list, indicators: dict, timeframe: str) -> dict:
    """Ask Claude to analyze chart and return a strategy JSON."""
    # Send compact representation of last 120 bars
    sample = bars[-120:]
    bars_txt = json.dumps([{
        "d": b["d"], "o": b["o"], "h": b["h"],
        "l": b["l"], "c": b["c"], "v": b["v"]
    } for b in sample], separators=(",",":"))

    ind_txt = json.dumps(indicators, separators=(",",":"))

    system = """You are an expert quantitative trading strategist specializing in Indian NSE stocks.
Your task: analyze OHLCV data and design the BEST mechanical strategy for this specific stock.

CRITICAL RULES:
1. Target win rate >= 55%. If you cannot achieve this, return {"viable": false}.
2. Every trade MUST have minimum 1:2 risk-reward ratio. No exceptions.
3. Strategy must be 100% rule-based (no subjective interpretation).
4. Use only standard indicators: EMA, RSI, Volume, ATR, Bollinger Bands, VWAP.
5. Entry must require at least 2 confirming conditions (never single indicator).
6. Return ONLY valid JSON, no markdown, no explanation text.

JSON schema to return:
{
  "viable": true,
  "name": "strategy name",
  "type": "trend_following | mean_reversion | breakout | momentum",
  "timeframe": "daily | 15min",
  "entry_conditions": [
    {"indicator": "EMA9", "operator": "crosses_above", "value": "EMA21", "description": "..."},
    {"indicator": "RSI14", "operator": "greater_than", "value": 55, "description": "..."},
    {"indicator": "volume", "operator": "greater_than", "value": "1.5x_avg", "description": "..."}
  ],
  "exit_conditions": [
    {"trigger": "stop_loss", "method": "atr_multiple", "multiplier": 1.5, "description": "1.5x ATR below entry"},
    {"trigger": "target", "method": "risk_multiple", "multiplier": 2.5, "description": "2.5x risk above entry"},
    {"trigger": "trailing", "method": "ema_cross", "value": "EMA9_below_EMA21", "description": "..."}
  ],
  "filters": [
    "Only trade when EMA50 > EMA200 (uptrend filter)",
    "Skip if RSI > 78 at entry (overbought)"
  ],
  "hold_period_bars": 20,
  "expected_win_rate": 58,
  "expected_rr": 2.5,
  "rationale": "2-3 sentence explanation of why this works for this stock"
}"""

    user = f"""Analyze {symbol} ({timeframe}) and design the optimal strategy.

Current indicators: {ind_txt}

Last {len(sample)} bars of OHLCV: {bars_txt}

Design a strategy that:
- Has win rate >= 55%
- Has risk:reward >= 1:2 on every trade
- Works specifically for this stock's volatility and trend characteristics
- Uses the ATR of {indicators.get('atr14','?')} for position sizing

Return only the JSON strategy object."""

    raw = _claude(system, user, max_tokens=1500)
    if not raw:
        return {"viable": False, "error": "Claude API unavailable"}
    # Strip markdown fences if present
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    try:
        return json.loads(raw)
    except Exception:
        # Try to extract JSON object
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except:
                pass
        return {"viable": False, "error": "Failed to parse strategy JSON", "raw": raw[:300]}


def _run_backtest(bars: list, strategy: dict, indicators: dict) -> dict:
    """Run the strategy against historical OHLCV bars. Returns trade list + stats."""
    if not strategy.get("viable"):
        return {"trades": [], "stats": {}}

    closes  = [b["c"] for b in bars]
    highs   = [b["h"] for b in bars]
    lows    = [b["l"] for b in bars]
    volumes = [b["v"] for b in bars]
    n = len(bars)
    if n < 60:
        return {"trades": [], "stats": {"error": "Not enough bars"}}

    def ema_arr(arr, p):
        """Return array of EMA values same length as arr."""
        k = 2/(p+1); e = arr[0]; result = [e]
        for x in arr[1:]:
            e = x*k + e*(1-k)
            result.append(round(e, 3))
        return result

    def rsi_arr(arr, p=14):
        result = [50.0]*p
        gains=[0.0]; losses=[0.0]
        for i in range(1,p+1):
            d=arr[i]-arr[i-1]
            gains.append(max(d,0)); losses.append(max(-d,0))
        ag=sum(gains[1:])/p; al=sum(losses[1:])/p
        result.append(round(100-100/(1+ag/al),1) if al else 100.0)
        for i in range(p+1, len(arr)):
            d=arr[i]-arr[i-1]
            ag=(ag*(p-1)+max(d,0))/p; al=(al*(p-1)+max(-d,0))/p
            result.append(round(100-100/(1+ag/al),1) if al else 100.0)
        return result

    # Pre-compute indicator arrays
    e9   = ema_arr(closes,  9)
    e21  = ema_arr(closes, 21)
    e20  = ema_arr(closes, 20)
    e50  = ema_arr(closes, 50) if n >= 55 else [closes[0]]*n
    e200 = ema_arr(closes,200) if n >= 205 else [closes[0]]*n
    rsi  = rsi_arr(closes, 14)
    avg_vol_arr = []
    for i in range(n):
        window = volumes[max(0,i-20):i]
        avg_vol_arr.append(sum(window)/len(window) if window else volumes[i])

    strategy_type = strategy.get("type","trend_following")
    hold_max = int(strategy.get("hold_period_bars", 20))
    atr_mult = 1.5  # default SL
    rr_target = 2.5  # default target
    for ex in strategy.get("exit_conditions",[]):
        if ex.get("trigger")=="stop_loss" and "multiplier" in ex:
            atr_mult = float(ex["multiplier"])
        if ex.get("trigger")=="target" and "multiplier" in ex:
            rr_target = float(ex["multiplier"])

    def atr(i, p=14):
        trs=[]
        for j in range(max(1,i-p+1), i+1):
            trs.append(max(highs[j]-lows[j],
                           abs(highs[j]-closes[j-1]),
                           abs(lows[j]-closes[j-1])))
        return sum(trs)/len(trs) if trs else closes[i]*0.02

    def check_entry(i):
        """Returns True if entry conditions are met at bar i."""
        if i < 21: return False
        c = closes[i]; prev_c = closes[i-1]

        # Universal filters: price must be above longer-term EMAs for long trades
        if strategy_type in ("trend_following","breakout","momentum"):
            if c < e50[i] * 0.98: return False   # below EMA50 — skip

        # Parse entry conditions from strategy
        conds = strategy.get("entry_conditions", [])
        if not conds:
            # Fallback: basic EMA9 cross + RSI
            ema_cross = e9[i] > e21[i] and e9[i-1] <= e21[i-1]
            rsi_ok    = 50 <= rsi[i] <= 72
            vol_ok    = volumes[i] >= avg_vol_arr[i] * 1.2
            return ema_cross and rsi_ok and vol_ok

        score = 0; required = max(2, len(conds)-1)
        for cond in conds:
            ind   = cond.get("indicator","").lower()
            op    = cond.get("operator","")
            val   = cond.get("value","")

            # Map indicator to value
            iv = None
            if   "ema9"   in ind: iv = e9[i]
            elif "ema21"  in ind: iv = e21[i]
            elif "ema20"  in ind: iv = e20[i]
            elif "ema50"  in ind: iv = e50[i]
            elif "ema200" in ind: iv = e200[i]
            elif "rsi"    in ind: iv = rsi[i]
            elif "volume" in ind: iv = volumes[i]
            elif "price"  in ind: iv = c

            if iv is None: score+=1; continue   # unknown indicator → skip (benefit of doubt)

            # Evaluate operator
            try:
                ref = None
                if isinstance(val, (int,float)):
                    ref = float(val)
                elif "ema9"   in str(val).lower(): ref = e9[i]
                elif "ema21"  in str(val).lower(): ref = e21[i]
                elif "ema20"  in str(val).lower(): ref = e20[i]
                elif "ema50"  in str(val).lower(): ref = e50[i]
                elif "ema200" in str(val).lower(): ref = e200[i]
                elif "avg"    in str(val).lower():
                    mult = float(re.findall(r"[\d.]+", str(val))[0]) if re.findall(r"[\d.]+",str(val)) else 1.5
                    ref = avg_vol_arr[i] * mult
                elif "atr"    in str(val).lower():
                    ref = atr(i)
                else:
                    ref = float(val) if str(val).replace(".","").isdigit() else None

                if ref is None: score+=1; continue

                if   op in ("crosses_above","cross_above"):
                    if iv > ref and (e9[i-1] <= e21[i-1] or closes[i-1] <= ref): score+=1
                elif op in ("crosses_below","cross_below"):
                    if iv < ref and closes[i-1] >= ref: score+=1
                elif op in ("greater_than","above",">"): 
                    if iv > ref: score+=1
                elif op in ("less_than","below","<"):
                    if iv < ref: score+=1
                elif op in ("between",):
                    pass  # skip range ops for now
            except:
                score+=1  # parsing error → benefit of doubt

        return score >= required

    # Run simulation
    trades = []
    in_trade = False
    entry_price = sl = target = 0.0
    entry_bar = entry_date = ""
    trail_sl = None

    for i in range(22, n):
        if not in_trade:
            if check_entry(i):
                entry_price = closes[i]
                a = atr(i)
                sl      = round(entry_price - atr_mult * a, 2)
                target  = round(entry_price + rr_target * atr_mult * a, 2)
                trail_sl = sl
                entry_bar  = i
                entry_date = bars[i]["d"]
                in_trade   = True
        else:
            bar_hi, bar_lo = highs[i], lows[i]

            # Update trailing stop (ratchet up only)
            new_trail = round(closes[i] - atr_mult * atr(i), 2)
            if new_trail > trail_sl:
                trail_sl = new_trail

            exit_price = None; exit_reason = "time"

            # Target hit
            if bar_hi >= target:
                exit_price = target; exit_reason = "target"
            # Trail/SL hit
            elif bar_lo <= trail_sl:
                exit_price = trail_sl; exit_reason = "sl"
            # Time exit
            elif (i - entry_bar) >= hold_max:
                exit_price = closes[i]; exit_reason = "time"

            if exit_price is not None:
                ret_pct = round((exit_price - entry_price)/entry_price*100, 2)
                risk    = entry_price - sl
                ach_rr  = round((exit_price - entry_price)/risk, 2) if risk > 0 else 0
                trades.append({
                    "sym":        "ANALYZED",
                    "entry_date": entry_date,
                    "exit_date":  bars[i]["d"],
                    "entry":      round(entry_price,2),
                    "exit":       round(exit_price,2),
                    "sl":         round(sl,2),
                    "target":     round(target,2),
                    "trail_sl":   round(trail_sl,2),
                    "ret_pct":    ret_pct,
                    "won":        ret_pct > 0,
                    "exit_reason":exit_reason,
                    "rr_achieved":ach_rr,
                })
                in_trade = False

    if not trades:
        return {"trades": [], "stats": {"total": 0, "win_rate": 0}}

    wins   = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    avg_win  = round(sum(t["ret_pct"] for t in wins)  /len(wins),2)  if wins   else 0
    avg_loss = round(sum(t["ret_pct"] for t in losses)/len(losses),2) if losses else 0
    pf = round(abs(len(wins)*avg_win / (len(losses)*avg_loss)), 2) if losses and avg_loss != 0 else 99.0

    stats = {
        "total":     len(trades),
        "wins":      len(wins),
        "losses":    len(losses),
        "win_rate":  round(len(wins)/len(trades)*100, 1),
        "avg_win":   avg_win,
        "avg_loss":  avg_loss,
        "avg_ret":   round(sum(t["ret_pct"] for t in trades)/len(trades), 2),
        "profit_factor": pf,
        "target_hits":   sum(1 for t in trades if t["exit_reason"]=="target"),
        "sl_hits":       sum(1 for t in trades if t["exit_reason"]=="sl"),
        "time_exits":    sum(1 for t in trades if t["exit_reason"]=="time"),
    }
    return {"trades": trades[-50:], "stats": stats}  # last 50 trades for display


def _gen_pine_script(symbol: str, strategy: dict, timeframe: str) -> str:
    """Ask Claude to convert strategy JSON to Pine Script v5."""
    strat_txt = json.dumps(strategy, indent=2)
    tf_str    = "15" if timeframe == "15min" else "D"

    system = """You are a Pine Script v5 expert. Convert the provided strategy JSON into complete, 
working Pine Script v5 code. 

Requirements:
- Use Pine Script v5 syntax (indicator() or strategy())
- Use strategy() function with default_qty_type and commission settings
- Include all entry conditions exactly as specified
- Include proper stop loss, take profit, and trailing stop
- Add alert conditions for both entry and exit
- Include a clean table showing key stats
- Code must compile without errors
- Return ONLY the Pine Script code, no explanation"""

    user = f"""Convert this strategy to Pine Script v5 for {symbol} on {timeframe} timeframe:

{strat_txt}

Requirements:
- strategy("{strategy_name}", overlay=true, default_qty_type=strategy.percent_of_equity, default_qty_value=10)
- Include entry alert: "BUY {{symbol}} - Entry Signal"  
- Include exit alert: "SELL {{symbol}} - Exit Signal"
- Webhook message format for alerts: 
  {{\"symbol\": \"{symbol}\", \"action\": \"{{action}}\", \"price\": \"{{close}}\", \"timeframe\": \"{timeframe}\"}}
- Add a small info table in top-right corner showing strategy name and timeframe"""

    code = _claude(system, user, max_tokens=3000)
    if not code:
        return "// Pine Script generation failed — ANTHROPIC_API_KEY not set"
    # Clean up markdown fences
    code = re.sub(r"```(?:pine)?script?", "", code, flags=re.IGNORECASE).strip().rstrip("`").strip()
    return code


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
    data = fetch_yahoo(sym + ".NS" if not sym.endswith(".NS") else sym)
    if not data:
        return jsonify({"error": "upstream failed"}), 502
    return Response(data, mimetype="application/json")

@app.route("/api/health")
def health():
    with _wa_lock:
        wa_phones = len(_wa_store)
        wa_alerts = sum(len(v.get("alerts",[])) for v in _wa_store.values())
    with _tv_lock:
        tv_count = len(_tv_signals)
    return jsonify({
        "status": "ok",
        "stock_cache": len(_stock_cache),
        "wa_phones": wa_phones,
        "wa_alerts": wa_alerts,
        "tv_signals": tv_count,
        "market_open": is_market_open(),
        "claude_ready": bool(ANTHROPIC_API_KEY),
    })


# ── AI Analyze endpoint ────────────────────────────────────────────────────────

@app.route("/api/analyze", methods=["POST"])
def analyze():
    """Main AI analysis endpoint. Fetches data, generates strategy, backtests, writes Pine Script."""
    body      = freq.get_json(silent=True) or {}
    symbol    = str(body.get("symbol","")).upper().strip()
    timeframe = str(body.get("timeframe","daily")).lower()  # "daily" or "15min"

    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured on server"}), 503

    # Clean symbol — add .NS if not present
    sym_yf = symbol + ".NS" if not symbol.endswith(".NS") else symbol

    # ── Step 1: Fetch data ──────────────────────────────────────────────────
    if timeframe == "15min":
        raw = fetch_yahoo(sym_yf, interval="15m", range_="60d")
    else:
        raw = fetch_yahoo(sym_yf, interval="1d",  range_="2y")

    if not raw:
        return jsonify({"error": f"Could not fetch data for {symbol}. Check symbol and try again."}), 404

    bars = _parse_ohlcv(raw, symbol)
    if len(bars) < 50:
        return jsonify({"error": f"Insufficient data for {symbol} ({len(bars)} bars). Need at least 50."}), 400

    # ── Step 2: Compute indicators ──────────────────────────────────────────
    indicators = _compute_indicators(bars)

    # ── Step 3: Generate strategy via Claude ───────────────────────────────
    strategy = _generate_strategy(symbol, bars, indicators, timeframe)
    if not strategy.get("viable"):
        return jsonify({
            "symbol": symbol,
            "timeframe": timeframe,
            "viable": False,
            "message": strategy.get("error", "No viable strategy found for this stock with the required win rate and R:R constraints."),
            "indicators": indicators,
        })

    # ── Step 4: Backtest ────────────────────────────────────────────────────
    bt = _run_backtest(bars, strategy, indicators)

    # ── Step 5: Generate Pine Script ───────────────────────────────────────
    pine = _gen_pine_script(symbol, strategy, timeframe)

    return jsonify({
        "symbol":     symbol,
        "timeframe":  timeframe,
        "viable":     True,
        "bars_analyzed": len(bars),
        "indicators": indicators,
        "strategy":   strategy,
        "backtest":   bt,
        "pine_script":pine,
    })


# ── TV Webhook receiver ────────────────────────────────────────────────────────

@app.route("/api/tv-signal", methods=["POST"])
def tv_signal_receive():
    """Receive webhook alerts from TradingView Pine Script."""
    try:
        # Try JSON first, then raw text
        body = freq.get_json(silent=True)
        if body is None:
            raw_text = freq.get_data(as_text=True)
            try:
                body = json.loads(raw_text)
            except:
                body = {"raw": raw_text}
    except:
        body = {}

    signal = {
        "id":        int(time.time()*1000),
        "ts":        time.time(),
        "dt":        datetime.now().strftime("%d %b %H:%M"),
        "symbol":    str(body.get("symbol","UNKNOWN")).upper(),
        "action":    str(body.get("action","signal")).upper(),  # BUY/SELL/EXIT
        "price":     body.get("price",""),
        "timeframe": str(body.get("timeframe","?")),
        "strategy":  str(body.get("strategy","")),
        "raw":       body,
    }

    with _tv_lock:
        _tv_signals.insert(0, signal)
        if len(_tv_signals) > MAX_TV_SIGNALS:
            _tv_signals.pop()

    print(f"[TV] Signal: {signal['symbol']} {signal['action']} @ {signal['price']}")
    return jsonify({"ok": True, "received": signal["dt"]})


@app.route("/api/tv-signals")
def tv_signals_get():
    """Return stored TradingView signals for frontend polling."""
    limit = min(int(freq.args.get("limit", 50)), 200)
    symbol = freq.args.get("symbol","").upper()
    with _tv_lock:
        sigs = list(_tv_signals[:limit])
    if symbol:
        sigs = [s for s in sigs if s["symbol"] == symbol]
    return jsonify({"signals": sigs, "total": len(_tv_signals)})


@app.route("/api/tv-signals/clear", methods=["POST"])
def tv_signals_clear():
    with _tv_lock:
        _tv_signals.clear()
    return jsonify({"ok": True})


# ── WhatsApp via CallMeBot ─────────────────────────────────────────────────────

def send_whatsapp(phone: str, key: str, message: str) -> tuple[bool, str]:
    try:
        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": phone, "text": message, "apikey": key},
            timeout=15
        )
        body = r.text.lower()
        if r.ok and ("message queued" in body or "message sent" in body
                     or "queued" in body or r.status_code == 200 and "error" not in body):
            return True, ""
        return False, r.text[:120]
    except Exception as e:
        return False, str(e)[:120]


# ── Market hours check ─────────────────────────────────────────────────────────

def is_market_open() -> bool:
    now_utc = datetime.now(timezone.utc)
    ist_hour   = (now_utc.hour + 5) % 24
    ist_minute = (now_utc.minute + 30) % 60
    if (now_utc.minute + 30) >= 60:
        ist_hour = (ist_hour + 1) % 24
    ist_total = ist_hour * 60 + ist_minute
    if now_utc.weekday() > 4:
        return False
    return (9 * 60 + 15) <= ist_total <= (15 * 60 + 35)


# ── Background alert checker ───────────────────────────────────────────────────

def _check_all_alerts():
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
            triggered = (cond == "above" and price >= target) or                         (cond == "below" and price <= target)
            if triggered:
                arrow  = "📈" if cond == "above" else "📉"
                dirstr = "crossed above" if cond == "above" else "crossed below"
                now_ist = datetime.now().strftime("%d %b %H:%M")
                msg = (f"{arrow} *NSE Alert* — *{sym}*\n"
                       f"Price ₹{price:.1f} {dirstr} your target ₹{target}\n"
                       f"Time: {now_ist} IST")
                ok, err = send_whatsapp(phone, key, msg)
                with _wa_lock:
                    if phone in _wa_store:
                        for a in _wa_store[phone]["alerts"]:
                            if a.get("id") == aid:
                                a["triggered"] = True
                                a["at"] = now_ist
                                a["sentOk"] = ok
                                break

def _alert_loop():
    print("[WA] Alert checker started")
    while True:
        time.sleep(300)
        try:
            if is_market_open():
                _check_all_alerts()
        except Exception as e:
            print(f"[WA] Checker error: {e}")

_checker = threading.Thread(target=_alert_loop, daemon=True)
_checker.start()


# ── FII helpers ────────────────────────────────────────────────────────────────

def _safe(v):
    try: return float(str(v).replace(",","").replace("(","").replace(")","").strip() or 0)
    except: return 0.0

def _fmt(d):
    for f in ("%d-%b-%Y","%d/%m/%Y","%Y-%m-%dT%H:%M:%S","%Y-%m-%d",
              "%d-%m-%Y","%b %d, %Y","%d-%b-%y"):
        try: return datetime.strptime(str(d).strip()[:20], f).strftime("%d-%b-%y")
        except: continue
    return str(d).strip()[:11]


# ── WhatsApp routes ────────────────────────────────────────────────────────────

@app.route("/api/wa/test", methods=["POST"])
def wa_test():
    body = freq.get_json(silent=True) or {}
    phone = str(body.get("phone","")).strip()
    key   = str(body.get("key","")).strip()
    if not phone or not key:
        return jsonify({"ok": False, "error": "phone and key required"}), 400
    msg = ("✅ *NSE Screener Pro* connected!\n"
           "You will now receive WhatsApp alerts when your price targets are hit.")
    ok, err = send_whatsapp(phone, key, msg)
    if ok:
        with _wa_lock:
            if phone not in _wa_store:
                _wa_store[phone] = {"key": key, "alerts": []}
            else:
                _wa_store[phone]["key"] = key
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": err})

@app.route("/api/wa/alerts", methods=["POST"])
def wa_set_alerts():
    body   = freq.get_json(silent=True) or {}
    phone  = str(body.get("phone","")).strip()
    key    = str(body.get("key","")).strip()
    alerts = body.get("alerts", [])
    if not phone or not key:
        return jsonify({"ok": False, "error": "phone and key required"}), 400
    with _wa_lock:
        existing  = _wa_store.get(phone, {})
        old_alerts = {a["id"]: a for a in existing.get("alerts", [])}
        merged = []
        for a in alerts:
            aid = a.get("id")
            if aid in old_alerts and old_alerts[aid].get("triggered"):
                merged.append(old_alerts[aid])
            else:
                merged.append(a)
        _wa_store[phone] = {"key": key, "alerts": merged, "updated": time.time()}
    return jsonify({"ok": True, "count": len(alerts)})

@app.route("/api/wa/triggered")
def wa_triggered():
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


# ── Visitor tracking ───────────────────────────────────────────────────────────
import hashlib

_visitor_lock = threading.Lock()
_visitors = {
    "today_date":  "",
    "today_ips":   set(),
    "active":      {},
}
_ACTIVE_TIMEOUT = 300

def _visitor_ip(req) -> str:
    raw = req.headers.get("X-Forwarded-For", req.remote_addr or "unknown")
    ip  = raw.split(",")[0].strip()
    return hashlib.sha256(ip.encode()).hexdigest()[:16]

def _record_visit(req):
    now     = time.time()
    today   = datetime.utcnow().strftime("%Y-%m-%d")
    ip_hash = _visitor_ip(req)
    with _visitor_lock:
        if _visitors["today_date"] != today:
            _visitors["today_date"] = today
            _visitors["today_ips"]  = set()
        _visitors["today_ips"].add(ip_hash)
        _visitors["active"][ip_hash] = now
        cutoff = now - _ACTIVE_TIMEOUT
        _visitors["active"] = {k: v for k, v in _visitors["active"].items() if v >= cutoff}

@app.before_request
def track_visitor():
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

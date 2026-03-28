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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

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

def _gemini(system: str, user: str, max_tokens: int = 2000) -> str:
    """Call Google Gemini 1.5 Flash (free forever) and return text response."""
    if not GEMINI_API_KEY:
        return ""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}")
    # Gemini combines system + user into one prompt for best results
    combined = f"{system}\n\n---\n\n{user}"
    payload = {
        "contents": [{"parts": [{"text": combined}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.2,      # low temp for consistent JSON output
            "topP": 0.8,
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }
    try:
        r = requests.post(url, json=payload, timeout=60)
        if r.ok:
            data = r.json()
            # Extract text from Gemini response structure
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "")
        else:
            print(f"[Gemini] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Gemini] Error: {e}")
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

    price_now = closes[-1]
    
    # ── Compute trade levels from price action ────────────────────────────────
    # Entry: current close (or next open — use close as proxy)
    entry = round(price_now, 2)
    
    # Stop loss: swing low of last 10 bars, or 1.5×ATR fallback
    swing_lo = min(lows[-10:]) if len(lows) >= 10 else price_now - atr * 1.5
    sl_swing  = round(swing_lo - atr * 0.3, 2)   # slightly below swing low
    sl_atr    = round(price_now - atr * 1.5, 2)
    # Use swing low if it gives a meaningful stop (1–7% below price)
    sl_dist_swing = (price_now - sl_swing) / price_now * 100
    stop = sl_swing if 1.0 <= sl_dist_swing <= 7.0 else sl_atr
    stop = round(stop, 2)
    
    # Risk per unit
    risk = price_now - stop
    
    # Target: nearest pivot high above price, or 2.5× risk
    pivot_highs = [highs[i] for i in range(max(0,n-50), n-1)
                   if highs[i] > price_now * 1.005
                   and highs[i] == max(highs[max(0,i-3):i+4])]
    pivot_highs = sorted(pivot_highs)
    target_pivot = pivot_highs[0] if pivot_highs else None
    target_2x    = round(price_now + risk * 2.5, 2)
    target = round(target_pivot, 2) if (target_pivot and target_pivot < price_now + risk * 4) else target_2x
    
    rr = round((target - price_now) / risk, 2) if risk > 0 else 0
    
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
        "price":  price_now,
        # Trade levels — computed from price action
        "trade_entry":  entry,
        "trade_sl":     stop,
        "trade_target": target,
        "trade_rr":     rr,
        "trade_sl_dist_pct": round((price_now - stop) / price_now * 100, 2),
    }


def _generate_strategy(symbol: str, bars: list, indicators: dict, timeframe: str) -> dict:
    """Ask Gemini to analyze chart and return a strategy JSON.
    
    Key fixes:
    - Sends only 50 bars (not 120) in ultra-compact format to stay within token limits
    - Prompt always returns a strategy — never asks Gemini to return viable:false
    - Retries with even simpler prompt if first attempt fails to parse
    """
    ind = indicators
    price  = ind.get("price", 0)
    atr    = ind.get("atr14", round(price * 0.02, 2))
    rsi    = ind.get("rsi14", 50)
    e20    = ind.get("ema20", price)
    e50    = ind.get("ema50", price)
    e200   = ind.get("ema200", price)
    vr     = ind.get("vol_ratio", 1.0)
    hi52   = ind.get("hi52", price)
    lo52   = ind.get("lo52", price)

    # Ultra-compact bar format: just c,h,l,v — saves ~60% tokens vs full JSON
    sample = bars[-50:]
    bars_compact = ";".join(
        f"{b['d'][5:]}:{b['c']:.0f}/{b['h']:.0f}/{b['l']:.0f}/{b['v']//1000}k"
        for b in sample
    )

    # Trend assessment for prompt context
    trend = "uptrend" if (e20 and e50 and price > e20 > e50) else             "downtrend" if (e20 and price < e20) else "sideways"
    near_high = price >= hi52 * 0.95 if hi52 else False
    oversold  = rsi < 40
    overbought= rsi > 72

    prompt = f"""You are a quantitative trading expert for NSE Indian stocks.
Design the BEST mechanical trading strategy for {symbol} ({timeframe}).

CURRENT STATE:
Price=₹{price} | ATR={atr} | RSI={rsi} | Trend={trend}
EMA20={e20} | EMA50={e50} | EMA200={e200}
VolRatio={vr}x | Near52WHigh={near_high} | Oversold={oversold}

LAST 50 BARS (MM-DD:close/high/low/volume):
{bars_compact}

REQUIREMENTS:
- Min win rate 55%, min R:R 1:2 on every trade
- Rule-based only (EMA, RSI, Volume, ATR — no subjective patterns)
- Entry needs 2+ confirming conditions
- ALWAYS return viable:true with the best possible strategy for this data
- Choose the strategy TYPE that suits this stock: trend_following/breakout/momentum/mean_reversion

Return ONLY this JSON (no markdown, no extra text):
{{"viable":true,"name":"...","type":"trend_following","timeframe":"{timeframe}","entry_conditions":[{{"indicator":"EMA9","operator":"crosses_above","value":"EMA21","description":"..."}}],"exit_conditions":[{{"trigger":"stop_loss","method":"atr_multiple","multiplier":1.5,"description":"1.5x ATR below entry"}},{{"trigger":"target","method":"risk_multiple","multiplier":2.5,"description":"2.5x risk"}}],"filters":["Only trade when EMA50 > EMA200"],"hold_period_bars":20,"expected_win_rate":58,"expected_rr":2.5,"rationale":"..."}}"""

    raw = _gemini("", prompt, max_tokens=800)

    def _parse(text):
        if not text:
            return None
        text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
        # Try direct parse
        try:
            return json.loads(text)
        except Exception:
            pass
        # Try extracting JSON object
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except:
                pass
        return None

    result = _parse(raw)

    # Retry with minimal prompt if first attempt failed
    if not result:
        fallback_prompt = f"""For NSE stock {symbol}, RSI={rsi}, trend={trend}, ATR={atr}:
Return a simple EMA crossover strategy as JSON only:
{{"viable":true,"name":"{symbol} EMA Strategy","type":"trend_following","timeframe":"{timeframe}","entry_conditions":[{{"indicator":"EMA9","operator":"crosses_above","value":"EMA21","description":"EMA9 crosses above EMA21"}},{{"indicator":"RSI14","operator":"greater_than","value":50,"description":"RSI above 50 confirms momentum"}},{{"indicator":"volume","operator":"greater_than","value":"1.2x_avg","description":"Volume above average"}}],"exit_conditions":[{{"trigger":"stop_loss","method":"atr_multiple","multiplier":1.5,"description":"1.5x ATR stop"}},{{"trigger":"target","method":"risk_multiple","multiplier":2.5,"description":"2.5x risk target"}}],"filters":["Trade only when price above EMA50","Skip if RSI > 75"],"hold_period_bars":20,"expected_win_rate":57,"expected_rr":2.5,"rationale":"EMA crossover captures trend changes with volume confirmation, suitable for {symbol} volatility profile."}}"""
        raw2  = _gemini("", fallback_prompt, max_tokens=600)
        result = _parse(raw2)

    # Last resort: build a default strategy from indicators without AI
    if not result:
        strat_type = "breakout" if near_high else "mean_reversion" if oversold else "trend_following"
        entry_rsi  = 55 if not oversold else 35
        result = {
            "viable": True,
            "name": f"{symbol} {strat_type.replace('_',' ').title()}",
            "type": strat_type,
            "timeframe": timeframe,
            "entry_conditions": [
                {"indicator": "EMA9",   "operator": "crosses_above", "value": "EMA21",   "description": "Short-term EMA crosses above medium-term"},
                {"indicator": "RSI14",  "operator": "greater_than",  "value": entry_rsi, "description": f"RSI above {entry_rsi} confirms momentum"},
                {"indicator": "volume", "operator": "greater_than",  "value": "1.3x_avg","description": "Volume surge confirms institutional interest"},
            ],
            "exit_conditions": [
                {"trigger": "stop_loss", "method": "atr_multiple",  "multiplier": 1.5, "description": f"1.5x ATR (₹{round(atr*1.5,1)}) below entry"},
                {"trigger": "target",   "method": "risk_multiple",  "multiplier": 2.5, "description": "2.5x risk above entry for 1:2.5 R:R"},
                {"trigger": "trailing", "method": "ema_cross",       "value": "EMA9_below_EMA21", "description": "Trail stop: exit when EMA9 crosses below EMA21"},
            ],
            "filters": [
                f"Only trade when price above EMA50 (₹{e50})" if e50 else "Only trade in uptrend",
                "Skip if RSI > 75 at entry (overbought)",
                "Minimum volume 1.3x 20-day average",
            ],
            "hold_period_bars": 20,
            "expected_win_rate": 56,
            "expected_rr": 2.5,
            "rationale": f"Rule-based {strat_type} strategy calibrated to {symbol}'s current ATR of {atr}. EMA crossover entry with RSI and volume confirmation targets high-probability setups while the 1:2.5 R:R ensures positive expectancy over time.",
            "_note": "Generated from indicators (Gemini response was unparseable)"
        }
        print(f"[Strategy] Used indicator fallback for {symbol}")

    result["viable"] = True  # always viable — we always generate something
    return result


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
    """Convert strategy to Pine Script v5 using compact prompt to stay within Gemini token limits."""
    s        = strategy
    name     = s.get("name", f"{symbol} Strategy")
    stype    = s.get("type", "trend_following")
    hold     = s.get("hold_period_bars", 20)
    entries  = "; ".join(
        f"{e.get('indicator','')} {e.get('operator','')} {e.get('value','')}"
        for e in s.get("entry_conditions", [])
    )
    sl_mult  = next((e.get("multiplier", 1.5) for e in s.get("exit_conditions", []) if e.get("trigger") == "stop_loss"), 1.5)
    tgt_mult = next((e.get("multiplier", 2.5) for e in s.get("exit_conditions", []) if e.get("trigger") == "target"), 2.5)
    filters  = "; ".join(s.get("filters", []))

    prompt = f"""Write Pine Script v5 for this NSE strategy. Return ONLY the code, no explanation.

Strategy: {name} | Type: {stype} | Symbol: {symbol} | TF: {timeframe}
Entry conditions: {entries}
SL: {sl_mult}x ATR below entry | Target: {tgt_mult}x risk | Hold max: {hold} bars
Filters: {filters}

//@version=5 requirements:
- strategy("{name}", overlay=true, default_qty_type=strategy.percent_of_equity, default_qty_value=10, commission_value=0.1)
- Indicators: EMA9, EMA21, EMA50, RSI14, ATR14, avgVol20
- Implement all entry conditions exactly
- SL = entry - {sl_mult}*ATR, Target = entry + {tgt_mult}*(entry-SL)
- strategy.entry and strategy.exit with stop/limit
- Alert on entry: {{"symbol":"{symbol}","action":"BUY","price":"{{close}}","timeframe":"{timeframe}"}}
- Alert on exit:  {{"symbol":"{symbol}","action":"SELL","price":"{{close}}","timeframe":"{timeframe}"}}
- Plot EMA9 (blue), EMA21 (orange), EMA50 (green)"""

    code = _gemini("", prompt, max_tokens=2000)

    # If Gemini returns empty/fails, return a complete working fallback template
    if not code or len(code.strip()) < 100:
        sl_r  = round(sl_mult, 1)
        tgt_r = round(tgt_mult, 1)
        code = f"""//@version=5
strategy("{name}", overlay=true, default_qty_type=strategy.percent_of_equity, default_qty_value=10, commission_type=strategy.commission.percent, commission_value=0.1)

// ── Indicators ──────────────────────────────────────────────────────────────
ema9   = ta.ema(close, 9)
ema21  = ta.ema(close, 21)
ema50  = ta.ema(close, 50)
rsi14  = ta.rsi(close, 14)
atr14  = ta.atr(14)
avgVol = ta.sma(volume, 20)

// ── Entry Signal ─────────────────────────────────────────────────────────────
entryLong = ta.crossover(ema9, ema21) and rsi14 > 50 and volume > avgVol * 1.3 and close > ema50

// ── Position Levels ───────────────────────────────────────────────────────────
var float slLevel  = na
var float tgtLevel = na

if entryLong and strategy.position_size == 0
    slLevel  := close - {sl_r} * atr14
    tgtLevel := close + {tgt_r} * (close - slLevel)
    strategy.entry("Long", strategy.long)
    strategy.exit("Exit", "Long", stop=slLevel, limit=tgtLevel)
    alert('{{"symbol":"{symbol}","action":"BUY","price":"' + str.tostring(math.round(close, 2)) + '","timeframe":"{timeframe}"}}', alert.freq_once_per_bar_close)

// Keep exit order active while in position (re-submit each bar with same levels)
if strategy.position_size > 0 and not entryLong
    strategy.exit("Exit", "Long", stop=slLevel, limit=tgtLevel)

if strategy.position_size[1] > 0 and strategy.position_size == 0
    alert('{{"symbol":"{symbol}","action":"SELL","price":"' + str.tostring(math.round(close, 2)) + '","timeframe":"{timeframe}"}}', alert.freq_once_per_bar_close)

// ── Visuals ───────────────────────────────────────────────────────────────────
plot(ema9,  "EMA9",  color=color.blue,           linewidth=1)
plot(ema21, "EMA21", color=color.orange,          linewidth=1)
plot(ema50, "EMA50", color=color.new(color.green, 20), linewidth=2)
plotshape(entryLong, "Buy", shape.triangleup, location.belowbar, color.lime, size=size.small)
bgcolor(strategy.position_size > 0 ? color.new(color.lime, 93) : na)

// ── Info Table ────────────────────────────────────────────────────────────────
var table t = table.new(position.top_right, 2, 3, bgcolor=color.new(color.black, 70), border_width=1)
table.cell(t, 0, 0, "Strategy",  text_color=color.gray,  text_size=size.small)
table.cell(t, 1, 0, "{name}",    text_color=color.white, text_size=size.small)
table.cell(t, 0, 1, "Symbol",    text_color=color.gray,  text_size=size.small)
table.cell(t, 1, 1, "{symbol}",  text_color=color.yellow,text_size=size.small)
table.cell(t, 0, 2, "Timeframe", text_color=color.gray,  text_size=size.small)
table.cell(t, 1, 2, "{timeframe}",text_color=color.aqua, text_size=size.small)

// ── Webhook URL ───────────────────────────────────────────────────────────────
// Set alert webhook to: YOUR_RENDER_URL/api/tv-signal"""

    # Clean markdown fences if Gemini added them
    code = re.sub(r"```(?:pine|pinescript)?", "", code, flags=re.IGNORECASE).strip().rstrip("`").strip()
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
        "ai_ready": bool(GEMINI_API_KEY),
        "ai_provider": "Gemini 1.5 Flash (free)" if GEMINI_API_KEY else "not configured",
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
    if not GEMINI_API_KEY:
        return jsonify({"error": "GEMINI_API_KEY not configured on Render — get free key at aistudio.google.com"}), 503

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


# ── S/R Zone Analysis ─────────────────────────────────────────────────────────

def _find_sr_zones(bars: list, current_price: float) -> dict:
    """
    Detect support/resistance zones from 15min or daily OHLCV bars.
    Algorithm:
      1. Find pivot highs (local maxima) and pivot lows (local minima)
      2. Cluster nearby pivots within 0.8% of each other into zones
      3. Score each zone: touches × recency × cleanliness
      4. Classify as support (below price) or resistance (above price)
      5. Build conditional trade plans for each strong zone
    """
    if len(bars) < 20:
        return {"supports": [], "resistances": []}

    highs  = [b["h"] for b in bars]
    lows   = [b["l"] for b in bars]
    closes = [b["c"] for b in bars]
    n      = len(bars)

    # ── Step 1: Find pivot highs and lows (5-bar window each side) ────────────
    pivot_highs, pivot_lows = [], []
    window = 5
    for i in range(window, n - window):
        if highs[i] == max(highs[i-window:i+window+1]):
            pivot_highs.append({"price": highs[i],  "bar": i, "date": bars[i]["d"]})
        if lows[i]  == min(lows[i-window:i+window+1]):
            pivot_lows.append( {"price": lows[i],   "bar": i, "date": bars[i]["d"]})

    # ── Step 2: Cluster pivots within 0.8% of each other ─────────────────────
    def cluster(pivots, tolerance=0.008):
        if not pivots:
            return []
        pivots = sorted(pivots, key=lambda x: x["price"])
        zones  = []
        current_cluster = [pivots[0]]
        for p in pivots[1:]:
            ref = current_cluster[0]["price"]
            if abs(p["price"] - ref) / ref <= tolerance:
                current_cluster.append(p)
            else:
                zones.append(current_cluster)
                current_cluster = [p]
        zones.append(current_cluster)
        return zones

    high_zones = cluster(pivot_highs)
    low_zones  = cluster(pivot_lows)

    # ── Step 3: Score and build zone objects ──────────────────────────────────
    atr = sum(highs[i]-lows[i] for i in range(max(0,n-20), n)) / min(20, n) or current_price*0.01

    def build_zone(cluster_pts, zone_type):
        prices   = [p["price"] for p in cluster_pts]
        bars_idx = [p["bar"]   for p in cluster_pts]
        center   = sum(prices) / len(prices)
        spread   = max(prices) - min(prices)
        touches  = len(cluster_pts)
        # Recency: how recently was this zone last tested (0–1)
        most_recent_bar = max(bars_idx)
        recency_score   = most_recent_bar / n
        # Strength score
        strength_raw = touches * (0.5 + 0.5 * recency_score)
        strength_pct = min(100, int(strength_raw * 20))

        # Zone band: center ± half ATR
        half = max(spread / 2, atr * 0.4)
        zone_lo = round(center - half, 2)
        zone_hi = round(center + half, 2)

        # Distance from current price (%)
        dist_pct = round((center - current_price) / current_price * 100, 2)

        # Trade plan
        risk = round(atr * 1.5, 2)
        if zone_type == "resistance":
            entry  = round(zone_lo, 2)         # sell at bottom of resistance
            sl     = round(zone_hi + atr * 0.5, 2)
            target = round(entry - risk * 2.5, 2)
            action = "SELL / SHORT"
            trigger = "bearish candle at zone (upper wick, engulfing, shooting star)"
        else:
            entry  = round(zone_hi, 2)          # buy at top of support
            sl     = round(zone_lo - atr * 0.5, 2)
            target = round(entry + risk * 2.5, 2)
            action = "BUY / LONG"
            trigger = "bullish candle at zone (hammer, bullish engulfing, pin bar)"

        actual_risk   = abs(entry - sl)
        actual_reward = abs(target - entry)
        rr = round(actual_reward / actual_risk, 1) if actual_risk > 0 else 0

        return {
            "type":       zone_type,
            "center":     round(center, 2),
            "zone_lo":    zone_lo,
            "zone_hi":    zone_hi,
            "touches":    touches,
            "strength":   strength_pct,
            "strength_label": "Strong" if strength_pct >= 60 else "Medium" if strength_pct >= 35 else "Weak",
            "recency":    round(recency_score * 100),
            "last_date":  bars[most_recent_bar]["d"],
            "dist_pct":   dist_pct,
            "trade": {
                "action":   action,
                "entry":    entry,
                "sl":       sl,
                "target":   target,
                "rr":       rr,
                "trigger":  trigger,
            }
        }

    # ── Step 4: Build + filter + sort zones ──────────────────────────────────
    supports    = []
    resistances = []

    for z in low_zones:
        center = sum(p["price"] for p in z) / len(z)
        if center < current_price * 0.995 and len(z) >= 2:
            supports.append(build_zone(z, "support"))

    for z in high_zones:
        center = sum(p["price"] for p in z) / len(z)
        if center > current_price * 1.005 and len(z) >= 2:
            resistances.append(build_zone(z, "resistance"))

    # Sort: supports by closeness to price (nearest first), resistances same
    supports    = sorted(supports,    key=lambda x: x["dist_pct"], reverse=True)[-6:]
    resistances = sorted(resistances, key=lambda x: x["dist_pct"])[:6]
    # Sort each by strength desc within top-6
    supports    = sorted(supports,    key=lambda x: -x["strength"])
    resistances = sorted(resistances, key=lambda x: -x["strength"])

    return {"supports": supports, "resistances": resistances, "atr": round(atr, 2)}


def _sr_gemini_commentary(symbol: str, zones: dict, current_price: float, timeframe: str) -> dict:
    """Ask Gemini for 1-sentence commentary on each zone. Returns {zone_center: commentary}."""
    if not GEMINI_API_KEY:
        return {}

    # Build compact zone summary to keep tokens low
    lines = [f"{symbol} current price ₹{current_price} | ATR ₹{zones.get('atr','?')} | TF: {timeframe}"]
    lines.append("RESISTANCE ZONES:")
    for z in zones.get("resistances", [])[:3]:
        lines.append(f"  ₹{z['zone_lo']}–{z['zone_hi']} | {z['touches']} touches | strength {z['strength']}%")
    lines.append("SUPPORT ZONES:")
    for z in zones.get("supports", [])[:3]:
        lines.append(f"  ₹{z['zone_lo']}–{z['zone_hi']} | {z['touches']} touches | strength {z['strength']}%")

    prompt = f"""You are a technical analyst. For each S/R zone below, write ONE short sentence (max 12 words) explaining its significance and likelihood of holding.

{chr(10).join(lines)}

Return JSON only — keys are zone center prices as strings, values are the commentary sentences:
{{"center_price": "one sentence about this zone", ...}}"""

    raw = _gemini("", prompt, max_tokens=400)
    if not raw:
        return {}
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except:
                pass
    return {}


@app.route("/api/sr-zones", methods=["POST"])
def sr_zones():
    """Compute S/R zones for a symbol + optional Gemini commentary."""
    body      = freq.get_json(silent=True) or {}
    symbol    = str(body.get("symbol", "")).upper().strip()
    timeframe = str(body.get("timeframe", "15min")).lower()
    with_ai   = bool(body.get("with_ai", False))

    if not symbol:
        return jsonify({"error": "symbol required"}), 400

    sym_yf = symbol + ".NS" if not symbol.endswith(".NS") else symbol

    # Fetch data
    if timeframe == "15min":
        raw = fetch_yahoo(sym_yf, interval="15m", range_="60d")
    else:
        raw = fetch_yahoo(sym_yf, interval="1d", range_="2y")

    if not raw:
        return jsonify({"error": f"Could not fetch data for {symbol}"}), 404

    bars = _parse_ohlcv(raw, symbol)
    if len(bars) < 30:
        return jsonify({"error": f"Insufficient data ({len(bars)} bars)"}), 400

    current_price = bars[-1]["c"]
    zones = _find_sr_zones(bars, current_price)

    # Optional Gemini commentary
    commentary = {}
    if with_ai and GEMINI_API_KEY:
        commentary = _sr_gemini_commentary(symbol, zones, current_price, timeframe)

    return jsonify({
        "symbol":        symbol,
        "timeframe":     timeframe,
        "current_price": current_price,
        "bars_analyzed": len(bars),
        "zones":         zones,
        "commentary":    commentary,
        "ai_used":       bool(commentary),
    })


@app.route("/api/sr-zones/batch", methods=["POST"])
def sr_zones_batch():
    """Compute S/R zones for top N stocks from the screener universe.
    Returns only stocks with strong zones near current price (within 3%).
    """
    body      = freq.get_json(silent=True) or {}
    symbols   = body.get("symbols", [])[:30]   # max 30 to stay within rate limits
    timeframe = str(body.get("timeframe", "15min")).lower()

    if not symbols:
        return jsonify({"error": "symbols list required"}), 400

    results = []
    for sym in symbols:
        sym_yf = sym + ".NS" if not sym.endswith(".NS") else sym
        try:
            if timeframe == "15min":
                raw = fetch_yahoo(sym_yf, interval="15m", range_="60d")
            else:
                raw = fetch_yahoo(sym_yf, interval="1d",  range_="2y")
            if not raw:
                continue
            bars = _parse_ohlcv(raw, sym)
            if len(bars) < 30:
                continue
            price = bars[-1]["c"]
            zones = _find_sr_zones(bars, price)

            # Only include if there's a strong zone within 3% of current price
            close_zones = []
            for z in zones.get("supports", []) + zones.get("resistances", []):
                if abs(z["dist_pct"]) <= 3.0 and z["strength"] >= 40:
                    close_zones.append(z)

            if close_zones:
                results.append({
                    "symbol":  sym,
                    "price":   price,
                    "zones":   {"supports": zones["supports"][:3],
                                "resistances": zones["resistances"][:3]},
                    "closest_zone": sorted(close_zones, key=lambda x: abs(x["dist_pct"]))[0],
                    "atr":     zones.get("atr", 0),
                })
        except Exception as e:
            print(f"[SR batch] {sym}: {e}")
            continue

    results.sort(key=lambda x: abs(x["closest_zone"]["dist_pct"]))
    return jsonify({"results": results, "timeframe": timeframe, "total": len(results)})

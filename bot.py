# bot.py
# Multi-timeframe + Price Action (BOS) + ATR + 200EMA bot for Delta India

import os, time, math, json, hmac, hashlib, requests
from dotenv import load_dotenv

# ================== CONFIG ==================
load_dotenv()
API_KEY    = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")

BASE_URL       = "https://api.india.delta.exchange"
LIVE_TRADING   = False          # <<< set True to send live orders
LEVERAGE       = 100            # leverage ceiling (position sizing still uses RISK_USD)
RISK_USD       = 0.7            # fixed $ risk per trade
MIN_TP_USD     = 1.0            # minimum take-profit in $
WATCHLIST      = ["BTCUSD", "ETHUSD", "SOLUSD"]

# Indicators / logic
EMA_200_PERIOD   = 200
ATR_LEN          = 14
ATR_ENTRY_MULT   = 0.5
ATR_SL_MULT      = 1.0
TP_RR            = 2.0

# TFs
ENTRY_TFS        = ["15m", "30m", "1h", "2h"]   # must agree with 4h trend (>=2)
CANDLES_15M      = 300
CANDLES_4H       = 250
SLEEP_MINUTES    = 15
MAX_NOTIONAL_BUF = 0.95

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "delta-india-bot/1.0"})

# ================== SIGNING & HTTP ==================
def now_ts() -> str:
    return str(int(time.time()))

def sign_message(method: str, path: str, body: str = ""):
    ts  = now_ts()
    msg = method.upper() + ts + path + (body or "")
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    # print(f"🔑 sign: {method} {path} ts={ts} sig={sig[:16]}…")  # uncomment to debug
    return ts, sig

def _request(method: str, path: str, payload: dict | None = None, params: dict | None = None):
    url  = BASE_URL + path
    body = json.dumps(payload, separators=(",", ":")) if payload else ""
    ts, sig = sign_message(method, path, body)

    headers = {
        "api-key": API_KEY,
        "timestamp": ts,
        "signature": sig,
        "Content-Type": "application/json"
    }

    try:
        if method == "GET":
            r = SESSION.get(url, headers=headers, params=params, timeout=20)
        else:
            if LIVE_TRADING:
                r = SESSION.post(url, headers=headers, data=body, timeout=20)
            else:
                # Dry run: don't hit order endpoints in paper mode
                return {"success": True, "dry_run": True, "payload": payload, "path": path}

        j = r.json()
    except Exception as e:
        return {"error": "network_error", "detail": str(e)}

    # If unauthorized/ip issues, Delta usually returns the client_ip in context — print it plainly.
    if isinstance(j, dict) and "error" in j:
        ctx = j.get("error", {}).get("context", {})
        if "client_ip" in ctx:
            print(f"⚠️ Whitelist this IP in Delta India 👉 {ctx['client_ip']}")

    return j

def private_get(path: str, params: dict | None = None):
    return _request("GET", path, None, params)

def private_post(path: str, payload: dict):
    return _request("POST", path, payload, None)

# ================== PUBLIC HELPERS ==================
def get_products_map(watch):
    try:
        j = SESSION.get(BASE_URL + "/v2/products", timeout=15).json()
        mp = {p["symbol"].upper(): p["id"] for p in j.get("result", []) if p.get("symbol") and p["symbol"].upper() in watch}
        print("🔗 Product map:", mp)
        return mp
    except Exception as e:
        print("❌ get_products_map:", e)
        return {}

def get_tickers_map():
    try:
        j = SESSION.get(BASE_URL + "/v2/tickers", timeout=15).json()
        out = {}
        for t in j.get("result", []):
            sym = (t.get("symbol") or "").upper()
            if not sym: continue
            px = t.get("mark_price") or t.get("last_price") or t.get("spot_price")
            if px is not None:
                out[sym] = float(px)
        return out
    except Exception as e:
        print("❌ get_tickers_map:", e)
        return {}

# ================== CANDLES ==================
def fetch_candles(symbol: str, resolution: str, limit: int, retries: int = 2):
    res_map = {"1m":60,"5m":300,"15m":900,"30m":1800,"1h":3600,"2h":7200,"4h":14400,"1d":86400}
    interval = res_map.get(resolution, 900)
    for _ in range(retries+1):
        now   = int(time.time())
        start = now - limit * interval
        params = {"symbol": symbol, "resolution": resolution, "start": start, "end": now}
        try:
            j = SESSION.get(BASE_URL + "/v2/history/candles", params=params, timeout=20).json()
            if j.get("success"):
                return j.get("result", [])
            else:
                time.sleep(0.6)
        except Exception:
            time.sleep(0.6)
    print(f"❌ fetch_candles failed for {symbol} {resolution}")
    return []

def parse_candles(raw):
    o,h,l,c,v = [],[],[],[],[]
    for x in raw:
        try:
            if isinstance(x, list):
                # [ts, open, high, low, close, volume]
                o.append(float(x[1])); h.append(float(x[2])); l.append(float(x[3])); c.append(float(x[4])); v.append(float(x[5]) if len(x)>5 else 0.0)
            elif isinstance(x, dict):
                o.append(float(x["open"])); h.append(float(x["high"])); l.append(float(x["low"])); c.append(float(x["close"])); v.append(float(x.get("volume", 0.0)))
        except Exception:
            continue
    return o,h,l,c,v

# ================== INDICATORS ==================
def ema_list(vals, period):
    n = len(vals)
    if n < period: return []
    k = 2/(period+1)
    out = [None]*(period-1)
    sma = sum(vals[:period])/period
    prev = sma
    out.append(prev)
    for v in vals[period:]:
        prev = (v - prev)*k + prev
        out.append(prev)
    return out

def true_range(h,l,c):
    out=[]; prev=None
    for i in range(len(c)):
        if prev is None:
            out.append(h[i]-l[i])
        else:
            out.append(max(h[i]-l[i], abs(h[i]-prev), abs(l[i]-prev)))
        prev = c[i]
    return out

def atr_list(h,l,c,period):
    return ema_list(true_range(h,l,c), period)

# ================== PRICE ACTION LAYER ==================
def recent_swings(h, l, lookback=20):
    return max(h[-lookback:]), min(l[-lookback:])

def bullish_bos(o,h,l,c):
    """Bullish break of structure on the *just closed* candle:
       - close > previous swing high
       - body >= 60% of the candle range (strong close)
    """
    if len(c) < 25: return False
    swing_hi, _ = recent_swings(h, l, 20)
    last_o, last_h, last_l, last_c = o[-1], h[-1], l[-1], c[-1]
    rng = max(1e-9, last_h - last_l)
    body = abs(last_c - last_o)
    return (last_c > swing_hi) and (body / rng >= 0.6)

def bearish_bos(o,h,l,c):
    if len(c) < 25: return False
    _, swing_lo = recent_swings(h, l, 20)
    last_o, last_h, last_l, last_c = o[-1], h[-1], l[-1], c[-1]
    rng = max(1e-9, last_h - last_l)
    body = abs(last_c - last_o)
    return (last_c < swing_lo) and (body / rng >= 0.6)

# ================== SIGNALS ==================
def tf_ema_dir(symbol, tf, fast=9, slow=20, limit=160):
    raw = fetch_candles(symbol, tf, limit)
    if not raw: return None
    o,h,l,c,v = parse_candles(raw)
    e_fast = ema_list(c, fast)
    e_slow = ema_list(c, slow)
    if not e_fast or not e_slow or e_fast[-1] is None or e_slow[-1] is None:
        return None
    return "bull" if e_fast[-1] > e_slow[-1] else "bear"

def detect_signal(symbol):
    # 4h 200EMA trend filter
    raw4h = fetch_candles(symbol, "4h", CANDLES_4H)
    if not raw4h: return None
    o4,h4,l4,c4,v4 = parse_candles(raw4h)
    e200 = ema_list(c4, EMA_200_PERIOD)
    if not e200 or e200[-1] is None: return None
    master = "bull" if c4[-1] > e200[-1] else ("bear" if c4[-1] < e200[-1] else None)
    if not master: return None

    # Multi-TF EMA agreement
    agree = 0; dirs = {}
    for tf in ENTRY_TFS:
        d = tf_ema_dir(symbol, tf)
        dirs[tf] = d
        if d == master: agree += 1
    if agree < 2:
        # print("⚠️ EMA agreement insufficient:", dirs)
        return None

    # 15m price action BOS + ATR breakout
    raw15 = fetch_candles(symbol, "15m", CANDLES_15M)
    if not raw15: return None
    o15,h15,l15,c15,v15 = parse_candles(raw15)
    if len(c15) < 50: return None

    atrs = atr_list(h15, l15, c15, ATR_LEN)
    if not atrs or atrs[-1] is None: return None
    atr = atrs[-1]

    swing_hi, swing_lo = recent_swings(h15, l15, 20)
    long_trig  = (c15[-1] > (swing_hi + ATR_ENTRY_MULT*atr))  and bullish_bos(o15,h15,l15,c15)
    short_trig = (c15[-1] < (swing_lo - ATR_ENTRY_MULT*atr)) and bearish_bos(o15,h15,l15,c15)

    if master == "bull" and long_trig:
        entry = c15[-1]
        sl    = entry - ATR_SL_MULT*atr
        tp    = entry + TP_RR*(entry - sl)
        side  = "buy"
    elif master == "bear" and short_trig:
        entry = c15[-1]
        sl    = entry + ATR_SL_MULT*atr
        tp    = entry - TP_RR*(sl - entry)
        side  = "sell"
    else:
        return None

    return {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "atr": atr,
        "ema_agree": agree,
        "ema_dirs": dirs,
        "master_trend": master,
        "swing_high": swing_hi,
        "swing_low": swing_lo
    }

# ================== ACCOUNT / SIZING ==================
def get_balance_usd():
    data = private_get("/v2/wallet/balances")
    if not data or not data.get("success"):
        print("❌ Could not get balance:", data)
        return 0.0
    for r in data.get("result", []):
        if (r.get("asset_symbol") or "").upper() in ("USD","USDT","USDC"):
            try: return float(r.get("available_balance") or r.get("balance") or 0.0)
            except: pass
    try:
        return float(data["result"][0].get("available_balance") or 0.0)
    except: return 0.0

def compute_qty(entry, sl, balance_usd):
    # Risk-based sizing; leverage only caps notional
    dist = abs(entry - sl)
    if dist <= 0: return 0.0
    qty = RISK_USD / dist
    # Cap by leverage notional
    max_notional = balance_usd * LEVERAGE * MAX_NOTIONAL_BUF
    notional = qty * entry
    if notional > max_notional:
        qty = max_notional / entry
    return max(0.0, math.floor(qty*100000)/100000.0)

def ensure_min_tp(entry, tp, qty, side):
    if qty <= 0: return tp
    proj = abs(tp-entry)*qty
    if proj >= MIN_TP_USD: return tp
    needed = MIN_TP_USD/qty
    return entry + needed if side=="buy" else entry - needed

# ================== POSITIONS / ORDERS ==================
def get_open_positions():
    for p in ["/v2/positions/margined", "/v2/positions"]:
        j = private_get(p)
        if j and j.get("success"): return j.get("result", [])
    return []

def has_open_position(product_id: int):
    for p in get_open_positions():
        pid = int(p.get("product_id") or p.get("id") or 0)
        size = float(p.get("size") or p.get("quantity") or 0)
        if pid == int(product_id) and abs(size) > 0: return True
    return False

def place_market(product_id, side, size_qty):
    payload = {"order_type":"market","product_id":product_id,"size":str(size_qty),"side":side,"leverage":str(LEVERAGE),"reduce_only":False}
    return private_post("/v2/orders", payload)

def place_limit_reduce(product_id, side, size_qty, limit_price):
    payload = {"order_type":"limit","product_id":product_id,"size":str(size_qty),"limit_price":str(limit_price),"side":side,"reduce_only":True}
    return private_post("/v2/orders", payload)

def place_stop_market(product_id, side, size_qty, stop_price):
    payload = {"order_type":"stop_market","product_id":product_id,"size":str(size_qty),"stop_price":str(stop_price),"side":side,"reduce_only":True}
    return private_post("/v2/orders", payload)

# ================== RUN LOGIC ==================
def run_once(product_map):
    bal = get_balance_usd()
    print(f"💼 Balance: ${bal:.4f}")
    if bal < 0.01:
        print("❌ Balance too low."); return

    prices = get_tickers_map()

    for sym in WATCHLIST:
        pid = product_map.get(sym)
        if not pid: continue
        print(f"🔎 Scanning {sym}…")

        sig = detect_signal(sym)
        if not sig:
            print(f"⏭ No signal on {sym}")
            continue

        print("🔔 Signal:", {k:sig[k] for k in ("symbol","side","entry","sl","tp","atr","ema_agree","master_trend")})

        if has_open_position(pid):
            print("⚠️ Existing position for product", pid, "— skip")
            continue

        entry, sl, tp, side = sig["entry"], sig["sl"], sig["tp"], sig["side"]
        qty = compute_qty(entry, sl, bal)
        if qty <= 0:
            print("❌ Qty=0 — skip")
            continue

        tp = ensure_min_tp(entry, tp, qty, side)
        print(f"🧾 Order -> pid={pid} side={side} qty={qty} entry≈{entry} SL={sl} TP={tp}  | LIVE={LIVE_TRADING}")

        res = place_market(pid, side, qty)
        print("Entry resp:", res)

        if res.get("success"):
            reduce_side = "sell" if side=="buy" else "buy"
            tp_res = place_limit_reduce(pid, reduce_side, qty, tp)
            sl_res = place_stop_market(pid, reduce_side, qty, sl)
            print("TP resp:", tp_res)
            print("SL resp:", sl_res)
        else:
            print("❌ Entry failed:", res)

        break  # take only first valid signal per cycle

# ================== MAIN LOOP ==================
if __name__ == "__main__":
    print("🚀 Multi-TF + Price Action ATR Bot (LIVE)" if LIVE_TRADING else "🚀 Multi-TF + Price Action ATR Bot (DRY RUN)")
    PRODUCT_MAP = get_products_map(WATCHLIST)
    if not PRODUCT_MAP:
        print("❌ No product map; exiting"); raise SystemExit(1)

    while True:
        try:
            run_once(PRODUCT_MAP)
        except Exception as e:
            print("⚠️ Exception:", e)
        print(f"⏳ Sleeping {SLEEP_MINUTES}m…")
        time.sleep(SLEEP_MINUTES*60)

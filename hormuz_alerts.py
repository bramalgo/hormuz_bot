#!/usr/bin/env python3
"""
Hormuz Dashboard — Telegram Alert Bot
Runs 24/7, sends alerts and responds to commands.
Usage: python3 hormuz_alerts.py
"""

import os, json, time, requests, schedule, threading, asyncio
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ──
BOT_TOKEN   = os.getenv("TG_TOKEN",    "")
CHAT_ID     = os.getenv("TG_CHAT_ID",  "")
JSONBIN_ID  = os.getenv("JSONBIN_ID",  "")
JSONBIN_KEY    = os.getenv("JSONBIN_KEY",    "")
AISSTREAM_KEY  = os.getenv("AISSTREAM_KEY", "")

# Debug output — shows in Railway logs
print(f"TG_TOKEN: {'SET ('+BOT_TOKEN[:8]+'...)' if BOT_TOKEN else 'NOT SET'}")
print(f"TG_CHAT_ID: {'SET' if CHAT_ID else 'NOT SET'}")
print(f"JSONBIN_ID: {'SET' if JSONBIN_ID else 'NOT SET'}")
print(f"JSONBIN_KEY: {'SET' if JSONBIN_KEY else 'NOT SET'}")

BASES = {
    "brent": 71.32, "wti": 67.80, "gold": 2650, "tsy": 4.19,
    "spx": 5840, "btc": 95000, "bdi": 1850, "vlcc": 63000
}

PHASES = {
    0: {"lbl":"PHASE 0","name":"Cash preservation",
        "rec":"Hold cash. No triggers fired.",
        "alloc":"Cash 60% · Treasuries 22% · Gold 12% · Defence 6%"},
    1: {"lbl":"PHASE 1","name":"Partial re-risk",
        "rec":"Begin adding Energy & Asia exposure.",
        "alloc":"Cash 45% · Energy 13% · Asia 7% · Gold 10% · Tres 12% · Def 5% · EU 5% · EM 3%"},
    2: {"lbl":"PHASE 2","name":"Major re-risk",
        "rec":"Full deployment — ceasefire confirmed.",
        "alloc":"Energy 18% · Asia 14% · US eq 14% · EU 11% · EM 8% · BTC 7% · Gold 5% · Tres 6% · Def 5% · Cash 12%"},
    "bear": {"lbl":"BEAR CASE","name":"Full defensive",
        "rec":"Do NOT deploy equity. Extend gold and Treasuries.",
        "alloc":"Cash 50% · Treasuries 28% · Gold 20% · Defence 2%"}
}

# State — tracks previous values to detect changes
state = {
    "phase": None, "ceasefire": "none",
    "brent_120": False, "pi_withdrawn": True, "hormuz_40": False,
    "brent_high_days": 0
}

data = {
    # Seeded defaults — updated on each fetch
    "brent": 111.89, "wti": 99.80, "gold": 4428.0,
    "spx": 6506, "tsy": 4.39, "btc": 68900,
    "dxy": 99.5, "kospi": 5452, "nikkei": 35800, "bdi": 2056,
    "ttf": 60.20, "vlcc": 285000,
    "hormuz": None, "carriers_out": None, "carriers_total": None,
    "pi_withdrawn": True, "ceasefire": "none",
    "conflict_day": 22, "conflict_day_calc": 22, "ieaMb": 400
}

def send(msg):
    """Send a Telegram message."""
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[{now()}] NOT CONFIGURED — would send: {msg[:60]}...")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
        ok = r.json().get("ok", False)
        if ok: print(f"[{now()}] ✓ Sent: {msg[:60]}...")
        else:  print(f"[{now()}] ✗ Failed: {r.text[:100]}")
        return ok
    except Exception as e:
        print(f"[{now()}] Error sending: {e}")
        return False

def now():
    return datetime.now().strftime("%H:%M:%S")

def fetch_yahoo(symbol, range_="5d"):
    """Fetch price from Yahoo Finance — uses regularMarketPrice for live data."""
    # Try v10/quoteSummary first — gives live market price not delayed close
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=price"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}, timeout=10)
        d = r.json()
        result = d["quoteSummary"]["result"][0]["price"]
        price = result["regularMarketPrice"]["raw"]
        mkt_state = result.get("marketState","?")
        if price and price > 0:
            print(f"[{now()}] {symbol}: ${price:.2f} (state:{mkt_state}) via v10")
            return price
    except Exception as e:
        print(f"[{now()}] {symbol} v10 failed: {e}")
    # Fallback to chart API — use meta.regularMarketPrice
    for interval, rng in [("5m","1d"), ("1h","5d"), ("1d","5d")]:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={rng}"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            d = r.json()
            meta = d["chart"]["result"][0].get("meta", {})
            if meta.get("regularMarketPrice"):
                print(f"[{now()}] {symbol}: ${meta['regularMarketPrice']:.2f} via chart meta")
                return meta["regularMarketPrice"]
            closes = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if closes:
                print(f"[{now()}] {symbol}: ${closes[-1]:.2f} via chart close (stale)")
                return closes[-1]
        except:
            continue
    return None

def fetch_coingecko_btc():
    """Fetch BTC price from CoinGecko with fallback to Yahoo."""
    # Try CoinGecko
    for attempt in range(2):
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            d = r.json()
            price = d["bitcoin"]["usd"]
            print(f"[{now()}] CoinGecko BTC: ${price:,.0f}")
            return price
        except Exception as e:
            print(f"[{now()}] CoinGecko attempt {attempt+1} failed: {e}")
            time.sleep(2)
    # Fallback to Yahoo
    try:
        price = fetch_yahoo("BTC-USD")
        if price:
            print(f"[{now()}] BTC via Yahoo fallback: ${price:,.0f}")
            return price
    except:
        pass
    return None


# ── AIS VESSEL TRACKING ──
# Hormuz Strait bounding box
HORMUZ_BOX = {
    "min_lat": 26.024702, "max_lat": 27.211892,
    "min_lon": 56.075592, "max_lon": 56.749878
}

def fetch_ais_vessels():
    """Count vessels in Hormuz Strait using AISstream.io WebSocket API."""
    if not AISSTREAM_KEY:
        return None
    try:
        import websocket
        vessels_seen = set()
        result = {"count": None, "done": False}

        def on_message(ws, message):
            try:
                msg = json.loads(message)
                mmsi = msg.get("MetaData", {}).get("MMSI")
                if mmsi:
                    vessels_seen.add(mmsi)
            except:
                pass

        def on_open(ws):
            subscribe = {
                "APIKey": AISSTREAM_KEY,
                "BoundingBoxes": [[
                    [HORMUZ_BOX["min_lat"], HORMUZ_BOX["min_lon"]],
                    [HORMUZ_BOX["max_lat"], HORMUZ_BOX["max_lon"]]
                ]],
                "FilterMessageTypes": ["PositionReport"]
            }
            ws.send(json.dumps(subscribe))
            # Collect for 30 seconds then close
            def close_after():
                time.sleep(60)
                result["count"] = len(vessels_seen)
                result["done"] = True
                ws.close()
            t = threading.Thread(target=close_after, daemon=True)
            t.start()

        def on_error(ws, error):
            print(f"[{now()}] AIS WebSocket error: {error}")
            result["done"] = True

        def on_close(ws, *args):
            result["done"] = True

        ws = websocket.WebSocketApp(
            "wss://stream.aisstream.io/v0/stream",
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        # Run in thread with 45s timeout
        t = threading.Thread(target=ws.run_forever, daemon=True)
        t.start()
        timeout = 75
        start = time.time()
        while not result["done"] and time.time()-start < timeout:
            time.sleep(1)
        count = result.get("count")
        if count is not None:
            print(f"[{now()}] AIS vessels in Hormuz: {count} (30s window)")
        return count
    except ImportError:
        print(f"[{now()}] websocket-client not installed")
        return None
    except Exception as e:
        print(f"[{now()}] AIS error: {e}")
        return None

def fetch_hormuztracker():
    """Scrape key data from HormuzTracker."""
    result = {}
    try:
        r = requests.get("https://www.hormuztracker.com/",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        html = r.text
        import re

        # ── Vessels — strict matching only, no fallback guessing ──
        vm = (re.search(r'~\s*(\d+)\s*(?:ships?|vessels?|transits?)', html, re.I)
           or re.search(r'(\d{1,3})\s*vessels?\s*(?:detected|transiting|logged)\s*today', html, re.I)
           or re.search(r'vessels?\s*detected\s*today[^\d]{0,20}(\d+)', html, re.I))
        if vm:
            c = int(vm.group(1))
            start = max(0, vm.start()-40)
            end = min(len(html), vm.end()+40)
            ctx = html[start:end].strip()
            print(f"[{now()}] Vessel match: '{ctx}' → {c}")
            if 2 <= c <= 150:
                result["hormuz"] = c
            else:
                print(f"[{now()}] Vessel count {c} rejected — showing N/A")
                result["hormuz"] = None
        else:
            print(f"[{now()}] Hormuz vessels: no confident match — showing N/A")
            result["hormuz"] = None

        # ── TTF gas ──
        tm = (re.search(r'TTF[^\d]{0,20}([\d.]+)', html, re.I)
           or re.search(r'€\s*([\d.]+)\s*/\s*MWh', html, re.I)
           or re.search(r'EU\s*Gas[^\d]{0,30}([\d.]+)', html, re.I))
        if tm:
            v = float(tm.group(1))
            if 5 < v < 500:
                result["ttf"] = v

        # ── P&I ──
        # P&I — only set if we find clear co-occurrence of P&I + withdrawal language
        pi_withdrawn_match = (
            re.search(r'P.{0,3}I.{0,100}(withdrawn|cancelled|suspended)', html, re.I)
            or re.search(r'(withdrawn|cancelled|suspended).{0,100}P.{0,3}I', html, re.I)
        )
        pi_active_match = re.search(r'P.{0,3}I.{0,100}(reinstated|active|restored)', html, re.I)
        if pi_withdrawn_match and not pi_active_match:
            result["pi_withdrawn"] = True
            print(f"[{now()}] P&I: Withdrawn")
        elif pi_active_match:
            result["pi_withdrawn"] = False
            print(f"[{now()}] P&I: Active/Reinstated")
        else:
            result["pi_withdrawn"] = None
            print(f"[{now()}] P&I: no confident match — N/A")

        # ── Carriers — look for small total (9 major lines), reject large numbers ──
        cm = (re.search(r'(\d)\s*/\s*(9)\s*(?:major\s*)?(?:shipping\s*)?lines?\s*(?:suspended|halted|paused|stopped)', html, re.I)
           or re.search(r'(\d)\s*(?:of\s*)?9\s*(?:major\s*)?(?:carriers?|lines?)\s*(?:suspended|halted)', html, re.I)
           or re.search(r'(\d+)\s*/\s*(\d+)\s*(?:major\s*)?(?:shipping\s*)?lines?\s*(?:suspended|halted)', html, re.I))
        if cm:
            out = int(cm.group(1))
            total = int(cm.group(2))
            # Sanity check — major shipping lines should be single digits
            if total <= 20:
                result["carriers_out"] = out
                result["carriers_total"] = total
                print(f"[{now()}] Carriers: {out}/{total}")
            else:
                print(f"[{now()}] Carriers: rejected {out}/{total} (total too large, keeping seeded 9/9)")
        else:
            print(f"[{now()}] Carriers: no confident match — showing N/A")
            result["carriers_out"] = None
            result["carriers_total"] = None

        # ── Conflict day — try scraping, fallback to calculation ──
        dm = (re.search(r'day\s+(\d+)\s+of\s+conflict', html, re.I)
           or re.search(r'conflict\s+day\s*:?\s*(\d+)', html, re.I)
           or re.search(r'day\s+(\d+)', html, re.I))
        if dm:
            d = int(dm.group(1))
            if 0 < d < 1000:
                result["conflict_day"] = d
        # Always calculate as fallback
        conflict_start = datetime(2026, 2, 28).timestamp()
        result["conflict_day_calc"] = int((time.time() - conflict_start) / 86400)
        print(f"[{now()}] Conflict day: scrape={result.get('conflict_day','?')} calc={result['conflict_day_calc']}")

        # ── Ceasefire ──
        lc = html.lower()
        if "ceasefire holding" in lc:     result["ceasefire"] = "holding"
        elif "ceasefire announced" in lc:  result["ceasefire"] = "announced"
        elif "ceasefire talks" in lc or "peace talks" in lc: result["ceasefire"] = "talks"
        else:                              result["ceasefire"] = "none"

        # ── IEA reserves ──
        im = re.search(r'(\d+)\s*mb?\s*(?:released|authorised|authorized)', html, re.I)
        if im:
            v = int(im.group(1))
            if 0 < v <= 400: result["ieaMb"] = v

        print(f"[{now()}] HormuzTracker: {result}")

    except Exception as e:
        print(f"[{now()}] HormuzTracker error: {e}")
        # Fallback conflict day
        conflict_start = datetime(2026, 2, 28).timestamp()
        result["conflict_day_calc"] = int((time.time() - conflict_start) / 86400)
    return result

def push_prices_to_jsonbin():
    """Push latest prices to JSONBin so dashboard can read them."""
    if not JSONBIN_ID or not JSONBIN_KEY:
        return
    try:
        payload = {
            "ts": int(time.time()),
            "brent":  round(data.get("brent",  0), 2),
            "wti":    round(data.get("wti",    0), 2),
            "gold":   round(data.get("gold",   0), 2),
            "spx":    round(data.get("spx",    0), 2),
            "tsy":    round(data.get("tsy",    0), 3),
            "btc":    round(data.get("btc",    0), 0),
            "dxy":    round(data.get("dxy",    0), 2),
            "kospi":  round(data.get("kospi",  0), 0),
            "nikkei": round(data.get("nikkei", 0), 0),
            "bdi":    round(data.get("bdi",    0), 0),
            "ttf":    round(data.get("ttf",    0), 2),
            "vlcc":   round(data.get("vlcc",   285000), 0),
            "hormuz": data.get("hormuz"),  # None if scrape failed
            "carriersOut":   data.get("carriers_out"),
            "carriersTotal": data.get("carriers_total"),
            "piWithdrawn":   data.get("pi_withdrawn", True),
            "ceasefire":     data.get("ceasefire", "none"),
            "conflictDay":   data.get("conflict_day") or data.get("conflict_day_calc", 22),
            "ieaMb":         data.get("ieaMb", 400),
        }
        r = requests.put(
            f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "X-Master-Key": JSONBIN_KEY,
                "X-Bin-Versioning": "false"
            },
            timeout=10
        )
        if r.status_code == 200:
            print(f"[{now()}] ✓ Prices pushed to JSONBin")
        else:
            print(f"[{now()}] JSONBin error: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"[{now()}] JSONBin push error: {e}")


def calc_phase():
    b = data.get("brent", 92)
    h = data.get("hormuz", 0)
    cf = data.get("ceasefire", "none")
    pi = data.get("pi_withdrawn", True)

    if b >= 120 and state["brent_high_days"] >= 3: return "bear"
    if cf in ("holding", "announced"): return 2
    if b < 80 and h >= 80: return 2
    if h >= 40 or not pi: return 1
    if cf == "talks": return 1
    return 0

def refresh_data():
    """Fetch all market data."""
    print(f"[{now()}] Fetching data...")
    symbols = {
        "brent": "BZ=F", "wti": "CL=F", "gold": "GC=F",
        "spx": "^GSPC", "tsy": "^TNX",
        "dxy": "DX-Y.NYB", "kospi": "^KS11", "nikkei": "^N225", "bdi": "^BDI",
        "ttf": "TTF=F"
    }
    # Sanity bounds — reject values outside realistic ranges
    BOUNDS = {
        "brent":(50,200), "wti":(40,190), "gold":(1000,8000),
        "spx":(2000,10000), "tsy":(0.1,15), "btc":(1000,500000),
        "dxy":(70,140), "kospi":(1000,8000), "nikkei":(15000,55000),
        "bdi":(100,10000), "ttf":(5,500)
    }
    for key, sym in symbols.items():
        v = fetch_yahoo(sym)
        if v:
            lo, hi = BOUNDS.get(key, (0, float('inf')))
            if lo <= v <= hi:
                data[key] = v
                if key in ("kospi","nikkei","spx"):
                    print(f"[{now()}] {key}: {v:.0f}")
            else:
                print(f"[{now()}] {key} value {v} out of bounds ({lo}-{hi}), skipping")

    # Brent — try multiple contracts, use highest (spot > front month)
    brent_alts = ["BZK26=F", "BZJ26=F", "COIL.L"]
    for sym in brent_alts:
        v = fetch_yahoo(sym)
        if v and 50 < v < 200:
            if v > data.get("brent", 0):
                data["brent"] = v
                print(f"[{now()}] Brent updated from {sym}: ${v:.2f}")
            break

    # Gold — try spot XAUUSD via alternative
    gold_alts = ["XAUUSD=X", "GLD"]
    for sym in gold_alts:
        v = fetch_yahoo(sym)
        if v:
            # GLD ETF trades at ~1/10 of gold price
            if sym == "GLD": v = v * 10
            if 1000 < v < 8000 and v > data.get("gold", 0):
                data["gold"] = v
                print(f"[{now()}] Gold updated from {sym}: ${v:.2f}")
            break

    # BDI — try alternative symbols
    for sym in ["^BDI", "BDI", "BDIY"]:
        v = fetch_yahoo(sym)
        if v and 100 < v < 20000:
            data["bdi"] = v
            print(f"[{now()}] BDI from {sym}: {v:.0f}")
            break

    # BTC from CoinGecko — more reliable than Yahoo for crypto
    btc = fetch_coingecko_btc()
    if btc: data["btc"] = btc

    ht = fetch_hormuztracker()
    data.update(ht)

    # Override vessel count with AIS data if available (more accurate)
    if AISSTREAM_KEY:
        ais_count = fetch_ais_vessels()
        if ais_count is not None:
            data["hormuz"] = ais_count
            print(f"[{now()}] Using AIS vessel count: {ais_count}")
        else:
            print(f"[{now()}] AIS unavailable, using HormuzTracker scrape")

    # Track brent high days
    if data.get("brent", 0) >= 120:
        state["brent_high_days"] = state.get("brent_high_days", 0) + 1
    else:
        state["brent_high_days"] = 0

    print(f"[{now()}] Brent=${data.get('brent','?'):.1f} Hormuz={data.get('hormuz','?')}/day "
          f"Phase={calc_phase()} Ceasefire={data.get('ceasefire','?')}")
    state["last_fetch_time"] = now()

def check_alerts():
    """Check for trigger conditions and send alerts."""
    ph = calc_phase()

    # Phase change
    if state["phase"] is not None and ph != state["phase"]:
        old = PHASES[state["phase"]]
        new = PHASES[ph]
        send(
            f"⚡ <b>PHASE CHANGE: {old['lbl']} → {new['lbl']}</b>\n\n"
            f"<b>{new['name']}</b>\n{new['rec']}\n\n"
            f"Brent: ${data.get('brent',0):.1f} | Hormuz: {data.get('hormuz','?')}/day\n"
            f"🎯 <b>Action required within 24–48h</b>"
        )
    state["phase"] = ph

    # Ceasefire
    cf = data.get("ceasefire", "none")
    if cf != state["ceasefire"] and cf != "none":
        labels = {"talks":"Formal talks underway","announced":"Ceasefire ANNOUNCED","holding":"Ceasefire holding"}
        send(
            f"🕊 <b>CEASEFIRE: {labels.get(cf,cf)}</b>\n\n"
            f"Brent: ${data.get('brent',0):.1f} | Hormuz: {data.get('hormuz','?')}/day\n"
            f"⚡ Phase 2 window may open — monitor closely"
        )
    state["ceasefire"] = cf

    # Brent $120+
    b120 = data.get("brent", 0) >= 120
    if b120 and not state["brent_120"]:
        send(
            f"🔴 <b>BRENT ABOVE $120 — BEAR CASE RISK</b>\n"
            f"Brent: <b>${data.get('brent',0):.1f}</b>\n"
            f"If sustained 3+ days → Bear case. Day {state['brent_high_days']}/3."
        )
    state["brent_120"] = b120

    # P&I reinstatement
    pi = data.get("pi_withdrawn", True)
    if not pi and state["pi_withdrawn"]:
        send(
            f"✅ <b>P&I CLUB COVER REINSTATED</b>\n"
            f"At least one IG P&I Club reinstated war-risk cover.\n"
            f"Phase 1 trigger. Hormuz: {data.get('hormuz','?')}/day | Brent: ${data.get('brent',0):.1f}"
        )
    state["pi_withdrawn"] = pi

    # Hormuz 40+
    h40 = data.get("hormuz", 0) >= 40
    if h40 and not state["hormuz_40"]:
        send(
            f"🚢 <b>HORMUZ: {data.get('hormuz','?')} VESSELS/DAY</b>\n"
            f"Crossed the 40/day Phase 1 trigger.\n"
            f"Brent: ${data.get('brent',0):.1f}"
        )
    state["hormuz_40"] = h40

def send_summary():
    """Send full daily summary."""
    ph = calc_phase()
    p = PHASES[ph]
    day = data.get("conflict_day") or data.get("conflict_day_calc") or \
          int((time.time() - datetime(2026,2,28).timestamp()) / 86400)
    brent = data.get("brent", 0)
    pct = (brent - 71.32) / 71.32 * 100
    msg = (
        f"🛢 <b>Hormuz Dashboard — Summary</b>\n"
        f"Day {day} of conflict\n\n"
        f"📊 <b>{p['lbl']} — {p['name']}</b>\n"
        f"{p['rec']}\n\n"
        f"💰 <b>Markets</b>\n"
        f"Brent: ${brent:.1f} (+{pct:.0f}% vs pre-conflict)\n"
        f"WTI: ${data.get('wti',0):.1f} | Gold: ${data.get('gold',0):,.0f}\n"
        f"BTC: ${data.get('btc',0):,.0f} | SPX: {data.get('spx',0):,.0f}\n\n"
        f"🚢 <b>Strait</b>\n"
        f"Hormuz: {data.get('hormuz','?')}/day (need 40+ for Phase 1)\n"
        f"Carriers: {str(data.get('carriers_out'))+'/'+str(data.get('carriers_total'))+' suspended' if data.get('carriers_out') is not None else 'N/A'}\n"
        f"P&I: {'Cancelled' if data.get('pi_withdrawn') else 'Active'}\n"
        f"Ceasefire: {{'none':'None','talks':'Talks','announced':'Announced','holding':'Holding'}}.get(data.get('ceasefire','none'),'?')\n\n"
        f"🎯 <b>Target allocation</b>\n{p['alloc']}"
    )
    # fix the dict literal that can't be in f-string
    cf_label = {"none":"None","talks":"Talks","announced":"Announced","holding":"Holding"}.get(data.get("ceasefire","none"),"?")
    msg = msg.replace(
        "{'none':'None','talks':'Talks','announced':'Announced','holding':'Holding'}.get(data.get('ceasefire','none'),'?')",
        cf_label
    )
    send(msg)

def handle_commands():
    """Poll Telegram for bot commands."""
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        offset = state.get("tg_offset", 0)
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            f"?offset={offset}&timeout=0&limit=5",
            timeout=10
        )
        d = r.json()
        if not d.get("ok"): return
        for update in d.get("result", []):
            state["tg_offset"] = update["update_id"] + 1
            msg = update.get("message") or update.get("channel_post")
            if not msg or not msg.get("text"): continue
            if str(msg["chat"]["id"]) != str(CHAT_ID): continue
            cmd = msg["text"].lower().strip().split("@")[0]
            if cmd == "/summary": send_summary()
            elif cmd == "/phase":
                ph = calc_phase()
                p = PHASES[ph]
                send(f"📊 <b>{p['lbl']} — {p['name']}</b>\n\n{p['rec']}\n\n{p['alloc']}")
            elif cmd == "/brent":
                send(
                    f"🛢 <b>Oil prices</b>\n"
                    f"Brent: <b>${data.get('brent',0):.1f}</b>\n"
                    f"WTI: ${data.get('wti',0):.1f}\n"
                    f"TTF gas: €{data.get('ttf',0):.1f}/MWh\n"
                    f"IEA reserves: {data.get('ieaMb',400)}mb"
                )
            elif cmd == "/strait":
                cf_label = {"none":"None","talks":"Talks","announced":"Announced","holding":"Holding"}.get(data.get("ceasefire","none"),"?")
                send(
                    f"🚢 <b>Strait of Hormuz</b>\n"
                    f"Vessels: <b>{str(data.get('hormuz')) + '/day' if data.get('hormuz') is not None else 'N/A'}\n"
                    f"Carriers: {str(data.get('carriers_out'))+'/'+str(data.get('carriers_total'))+' suspended' if data.get('carriers_out') is not None else 'N/A'}\n"
                    f"P&I: {'Cancelled' if data.get('pi_withdrawn') else 'Active'}\n"
                    f"Ceasefire: {cf_label}"
                )
            elif cmd == "/status":
                ph = calc_phase()
                p = PHASES[ph]
                uptime = time.time() - state.get("start_time", time.time())
                hours = int(uptime // 3600)
                mins  = int((uptime % 3600) // 60)
                last  = state.get("last_fetch_time", "never")
                cf_label = {"none":"None","talks":"Talks","announced":"Announced","holding":"Holding"}.get(data.get("ceasefire","none"),"?")
                day = data.get("conflict_day") or data.get("conflict_day_calc") or \
                      int((time.time() - datetime(2026,2,28).timestamp()) / 86400)
                send(
                    f"✅ <b>Bot is running</b>\n"
                    f"Uptime: {hours}h {mins}m | Last fetch: {last}\n"
                    f"Conflict: Day {day}\n\n"
                    f"📊 <b>{p['lbl']} — {p['name']}</b>\n"
                    f"Brent: ${data.get('brent',0):.1f} | Hormuz: {data.get('hormuz','?')}/day\n"
                    f"Ceasefire: {cf_label}\n"
                    f"P&I: {'Cancelled' if data.get('pi_withdrawn') else 'Active'}\n\n"
                    f"🔔 Alerts: every 15 min | Commands: every 10s"
                )
            elif cmd == "/alerts":
                t = state.get("triggers", {
                    "phase":True,"brent120":True,"ceasefire":True,
                    "pi":True,"hormuz40":True,"daily":False
                })
                send(
                    "🔔 <b>Active alert triggers</b>\n\n"
                    f"{'✅' if t.get('phase') else '❌'} Phase change\n"
                    f"{'✅' if t.get('brent120') else '❌'} Brent above $120\n"
                    f"{'✅' if t.get('ceasefire') else '❌'} Ceasefire signal\n"
                    f"{'✅' if t.get('pi') else '❌'} P&I reinstatement\n"
                    f"{'✅' if t.get('hormuz40') else '❌'} Hormuz 40+ vessels\n"
                    f"{'✅' if t.get('daily') else '❌'} Daily 8am summary\n\n"
                    "Use /daily on or /daily off to toggle the daily summary."
                )
            elif cmd.startswith("/daily"):
                parts = cmd.split()
                if len(parts) > 1 and parts[1] == "on":
                    state["triggers"] = state.get("triggers", {})
                    state["triggers"]["daily"] = True
                    schedule.every().day.at("08:00").do(send_summary)
                    send("✅ Daily 8am summary enabled.")
                elif len(parts) > 1 and parts[1] == "off":
                    state["triggers"] = state.get("triggers", {})
                    state["triggers"]["daily"] = False
                    send("❌ Daily 8am summary disabled.")
                else:
                    daily_on = state.get("triggers", {}).get("daily", False)
                    send(f"Daily summary is currently {'✅ ON' if daily_on else '❌ OFF'}.\nUse /daily on or /daily off to change.")
            elif cmd == "/help":
                send(
                    "🤖 <b>Hormuz Alert Bot</b>\n\n"
                    "/summary — Full market snapshot\n"
                    "/phase — Current phase + action\n"
                    "/brent — Oil prices\n"
                    "/strait — Hormuz shipping\n"
                    "/status — Bot health + uptime\n"
                    "/alerts — Show active triggers\n"
                    "/daily on|off — Toggle daily summary\n"
                    "/help — This message"
                )
    except Exception as e:
        print(f"[{now()}] Command poll error: {e}")

def main():
    print("=" * 50)
    print("  Hormuz Dashboard Alert Bot")
    print("=" * 50)

    if not BOT_TOKEN or not CHAT_ID:
        print("\n⚠  TOKEN or CHAT_ID not set.")
        print("   Create a .env file with:")
        print("   TG_TOKEN=your_bot_token")
        print("   TG_CHAT_ID=your_chat_id")
        print("   Or edit this script directly.\n")
        return

    # Initial fetch
    refresh_data()
    state["start_time"] = time.time()
    state["last_fetch_time"] = now()
    push_prices_to_jsonbin()  # push on startup
    # Set baseline state so we don't fire spurious alerts on startup
    state["phase"] = calc_phase()
    state["ceasefire"] = data.get("ceasefire", "none")
    state["brent_120"] = data.get("brent", 0) >= 120
    state["pi_withdrawn"] = data.get("pi_withdrawn", True)
    state["hormuz_40"] = data.get("hormuz", 0) >= 40

    send(
        f"🟢 <b>Hormuz Alert Bot started</b>\n"
        f"Monitoring every 5 minutes.\n"
        f"Current: {PHASES[calc_phase()]['lbl']}\n"
        f"Brent: ${data.get('brent',0):.1f} | Hormuz: {data.get('hormuz','?')}/day\n"
        f"Send /help for commands."
    )

    # Schedule
    schedule.every(5).minutes.do(lambda: (refresh_data(), push_prices_to_jsonbin(), check_alerts(), state.update({"last_fetch_time": now()})))
    schedule.every().day.at("08:00").do(send_summary)

    print(f"[{now()}] Bot running. Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        handle_commands()
        time.sleep(10)  # poll commands every 10s

if __name__ == "__main__":
    main()

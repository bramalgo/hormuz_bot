#!/usr/bin/env python3
"""
Hormuz Dashboard — Telegram Alert Bot
Runs 24/7, sends alerts and responds to commands.
Usage: python3 hormuz_alerts.py
"""

import os, json, time, requests, schedule, threading, random, string
import websocket
from flask import Flask, request as freq, jsonify
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ──
BOT_TOKEN   = os.getenv("TG_TOKEN",    "")
CHAT_ID     = os.getenv("TG_CHAT_ID",  "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "hormuz2026")

# Debug output — shows in Railway logs
print(f"TG_TOKEN: {'SET ('+BOT_TOKEN[:8]+'...)' if BOT_TOKEN else 'NOT SET'}")
print(f"TG_CHAT_ID: {'SET' if CHAT_ID else 'NOT SET'}")

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
    # TV-sourced — start None, populated by WebSocket
    "brent": None, "wti": None, "gold": None,
    "spx": None, "tsy": None, "btc": None,
    "dxy": None, "kospi": None, "nikkei": None,
    "bdi": None, "ttf": None,
    # Non-TV — known facts
    "vlcc": 285000,
    "hormuz": None,
    "carriers_out": 9, "carriers_total": 9,
    "pi_withdrawn": True,
    "ceasefire": "none",
    "conflict_day": 23, "conflict_day_calc": 23,
    "ieaMb": 400
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
        # P&I — default withdrawn=True (known fact since 2 Mar 2026)
        # Only flip to False if we find very specific reinstatement language near P&I
        pi_reinstated = bool(
            re.search(r'P.{0,3}I.{0,50}(reinstated|cover restored|lifting)', html, re.I)
            or re.search(r'(reinstated|cover restored).{0,50}P.{0,3}I', html, re.I)
        )
        if pi_reinstated:
            result["pi_withdrawn"] = False
            print(f"[{now()}] P&I: REINSTATED — verify manually!")
        else:
            result["pi_withdrawn"] = True
            print(f"[{now()}] P&I: Withdrawn (confirmed/default)")

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
            print(f"[{now()}] Carriers: no match — using known 9/9")
            # Keep known fact: 9/9 major lines suspended since conflict start
            result["carriers_out"] = 9
            result["carriers_total"] = 9

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
        calc_day = int((time.time() - conflict_start) / 86400)
        result["conflict_day_calc"] = calc_day
        result["conflict_day"] = calc_day  # always use calculated — scrape unreliable
        print(f"[{now()}] Conflict day: {calc_day} (calculated from 28 Feb 2026)")

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

def calc_phase():
    b = data.get("brent") or 92
    h = data.get("hormuz", 0)
    cf = data.get("ceasefire", "none")
    pi = data.get("pi_withdrawn", True)

    if b >= 120 and state["brent_high_days"] >= 3: return "bear"
    if cf in ("holding", "announced"): return 2
    if b < 80 and h >= 80: return 2
    if h >= 40 or not pi: return 1
    if cf == "talks": return 1
    return 0

# ── AIS VESSEL TRACKING (Hormuz Strait) ──
AISSTREAM_KEY = os.getenv("AISSTREAM_KEY", "")

HORMUZ_BOX = {
    "min_lat": 26.165299, "max_lat": 26.924519,
    "min_lon": 56.181335, "max_lon": 56.634521
}

def fetch_ais_vessels():
    """Count vessels in Hormuz Strait using AISstream.io WebSocket."""
    if not AISSTREAM_KEY:
        return None
    try:
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
            def close_after():
                time.sleep(60)
                result["count"] = len(vessels_seen)
                result["done"] = True
                ws.close()
            threading.Thread(target=close_after, daemon=True).start()

        def on_error(ws, error):
            print(f"[{now()}] AIS error: {error}")
            result["done"] = True

        def on_close(ws, *args):
            result["done"] = True

        ws_app = websocket.WebSocketApp(
            "wss://stream.aisstream.io/v0/stream",
            on_open=on_open, on_message=on_message,
            on_error=on_error, on_close=on_close
        )
        t = threading.Thread(target=ws_app.run_forever, daemon=True)
        t.start()
        start = time.time()
        while not result["done"] and time.time()-start < 75:
            time.sleep(1)
        count = result.get("count")
        if count is not None:
            print(f"[{now()}] AIS vessels in Hormuz: {count} (60s window)")
        return count
    except Exception as e:
        print(f"[{now()}] AIS error: {e}")
        return None


def refresh_data():
    """Fetch HormuzTracker + AIS data only. Market prices come via TradingView webhooks."""
    print(f"[{now()}] Refreshing HormuzTracker + AIS...")

    # HormuzTracker scrape
    ht = fetch_hormuztracker()
    data.update(ht)

    # Track brent high days (uses last TV webhook price)
    if (data.get("brent") or 0) >= 120:
        state["brent_high_days"] = state.get("brent_high_days", 0) + 1
    else:
        state["brent_high_days"] = 0

    # AIS vessel count
    if AISSTREAM_KEY:
        ais_count = fetch_ais_vessels()
        if ais_count is not None:
            data["hormuz"] = ais_count

    # Always calculate conflict day
    data["conflict_day"] = int((time.time() - datetime(2026,2,28).timestamp()) / 86400)

    print(f"[{now()}] Brent=${(data.get('brent') or 0):.1f} Hormuz={data.get('hormuz','N/A')}/day Phase={calc_phase()}")
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
            f"Brent: ${(data.get('brent') or 0):.1f} | Hormuz: {data.get('hormuz','?')}/day\n"
            f"🎯 <b>Action required within 24–48h</b>"
        )
    state["phase"] = ph

    # Ceasefire
    cf = data.get("ceasefire", "none")
    if cf != state["ceasefire"] and cf != "none":
        labels = {"talks":"Formal talks underway","announced":"Ceasefire ANNOUNCED","holding":"Ceasefire holding"}
        send(
            f"🕊 <b>CEASEFIRE: {labels.get(cf,cf)}</b>\n\n"
            f"Brent: ${(data.get('brent') or 0):.1f} | Hormuz: {data.get('hormuz','?')}/day\n"
            f"⚡ Phase 2 window may open — monitor closely"
        )
    state["ceasefire"] = cf

    # Brent $120+
    b120 = (data.get("brent") or 0) >= 120
    if b120 and not state["brent_120"]:
        send(
            f"🔴 <b>BRENT ABOVE $120 — BEAR CASE RISK</b>\n"
            f"Brent: <b>${(data.get('brent') or 0):.1f}</b>\n"
            f"If sustained 3+ days → Bear case. Day {state['brent_high_days']}/3."
        )
    state["brent_120"] = b120

    # P&I reinstatement
    pi = data.get("pi_withdrawn", True)
    if not pi and state["pi_withdrawn"]:
        send(
            f"✅ <b>P&I CLUB COVER REINSTATED</b>\n"
            f"At least one IG P&I Club reinstated war-risk cover.\n"
            f"Phase 1 trigger. Hormuz: {data.get('hormuz','?')}/day | Brent: ${(data.get('brent') or 0):.1f}"
        )
    state["pi_withdrawn"] = pi

    # Hormuz 40+
    h40 = data.get("hormuz", 0) >= 40
    if h40 and not state["hormuz_40"]:
        send(
            f"🚢 <b>HORMUZ: {data.get('hormuz','?')} VESSELS/DAY</b>\n"
            f"Crossed the 40/day Phase 1 trigger.\n"
            f"Brent: ${(data.get('brent') or 0):.1f}"
        )
    state["hormuz_40"] = h40

def send_summary():
    """Send full daily summary."""
    ph = calc_phase()
    p = PHASES[ph]
    day = data.get("conflict_day") or data.get("conflict_day_calc") or \
          int((time.time() - datetime(2026,2,28).timestamp()) / 86400)
    brent = (data.get("brent") or 0)
    pct = (brent - 71.32) / 71.32 * 100
    msg = (
        f"🛢 <b>Hormuz Dashboard — Summary</b>\n"
        f"Day {day} of conflict\n\n"
        f"📊 <b>{p['lbl']} — {p['name']}</b>\n"
        f"{p['rec']}\n\n"
        f"💰 <b>Markets</b>\n"
        f"Brent: ${brent:.1f} (+{pct:.0f}% vs pre-conflict)\n"
        f"WTI: ${(data.get('wti') or 0):.1f} | Gold: ${(data.get('gold') or 0):,.0f}\n"
        f"BTC: ${(data.get('btc') or 0):,.0f} | SPX: {(data.get('spx') or 0):,.0f}\n\n"
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
                    f"Brent: <b>${(data.get('brent') or 0):.1f}</b>\n"
                    f"WTI: ${(data.get('wti') or 0):.1f}\n"
                    f"TTF gas: €{(data.get('ttf') or 0):.1f}/MWh\n"
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
                    f"Brent: ${(data.get('brent') or 0):.1f} | Hormuz: {data.get('hormuz','?')}/day\n"
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

# ── TRADINGVIEW WEBSOCKET FEED ──
tv_session = "qs_" + "".join(random.choices(string.ascii_lowercase, k=12))
tv_ws = None


TV_SYMBOLS = {
    "UKOIL":   "brent",
    "USOIL":   "wti",
    "GOLD":    "gold",
    "SPX":     "spx",
    "US10Y":   "tsy",
    "BTCUSD":  "btc",   # Bitstamp — may lag
    "BTCUSDT": "btc",   # Binance — more liquid
    "DXY":     "dxy",
    "KOSPI":   "kospi",
    "NI225":   "nikkei",
    "TTF1!":   "ttf",
    "BDI":     "bdi",
}

def tv_on_price(key, price):
    """Called when TradingView pushes a new price."""
    lo, hi = TV_BOUNDS.get(key, (0, float('inf')))
    if lo <= price <= hi:
        old = data.get(key, 0)
        data[key] = price
        tv_last_price_time[key] = time.time()
        if abs(price - old) > 0.001:  # only log on change
            print(f"[TV] {key}: {price:.2f}")
            check_alerts()

def tv_on_message(ws, msg):
    import re
    patterns = re.findall(r"~m~\d+~m~(.+?)(?=~m~|$)", msg)
    for p in patterns:
        try:
            d = json.loads(p)
            if d.get("m") == "qsd":
                p_data = d.get("p", [])
                if len(p_data) >= 2:
                    sym = p_data[1].get("n", "")
                    v = p_data[1].get("v", {})
                    price = v.get("lp") or v.get("last_price")
                    if price:
                        for tv_sym, key in TV_SYMBOLS.items():
                            if tv_sym in sym:
                                tv_on_price(key, float(price))
                                break
        except:
            pass
        if "~h~" in p:
            try: ws.send(f"~m~{len(p)}~m~{p}")
            except: pass

def tv_on_open(ws):
    print(f"[{now()}] TradingView WebSocket connected")
    def setup():
        time.sleep(0.3)
        ws.send(tv_format_msg("set_auth_token", ["unauthorized_user_token"]))
        time.sleep(0.3)
        ws.send(tv_format_msg("quote_create_session", [tv_session]))
        time.sleep(0.3)
        ws.send(tv_format_msg("quote_set_fields", [tv_session, "lp", "volume"]))
        time.sleep(0.3)
        for sym in TV_SYMBOLS.keys():
            ws.send(tv_format_msg("quote_add_symbols", [tv_session, sym]))
            time.sleep(0.05)
        print(f"[{now()}] TradingView: subscribed to {len(TV_SYMBOLS)} symbols")
    threading.Thread(target=setup, daemon=True).start()

def tv_on_error(ws, error):
    print(f"[{now()}] TradingView error: {error}")

def tv_on_close(ws, *args):
    print(f"[{now()}] TradingView disconnected — reconnecting in 30s")
    time.sleep(30)
    start_tv_feed()

def start_tv_feed():
    global tv_ws
    try:
        tv_ws = websocket.WebSocketApp(
            "wss://data.tradingview.com/socket.io/websocket?from=chart&date=2026_03_23",
            on_open=tv_on_open,
            on_message=tv_on_message,
            on_error=tv_on_error,
            on_close=tv_on_close,
            header={"Origin":"https://www.tradingview.com","User-Agent":"Mozilla/5.0"}
        )
        t = threading.Thread(target=tv_ws.run_forever, kwargs={"ping_interval":30,"ping_timeout":10}, daemon=True)
        t.start()
        print(f"[{now()}] TradingView feed starting...")
    except Exception as e:
        print(f"[{now()}] TradingView feed error: {e}")

# ── FLASK WEBHOOK SERVER ──
app = Flask(__name__)

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

@app.route('/health', methods=['OPTIONS'])
@app.route('/webhook', methods=['OPTIONS'])
def options():
    return '', 204

# Symbol map from TradingView ticker → our data key
TV_SYMBOL_MAP = {
    "UKOIL":   "brent",   # Brent crude
    "USOIL":   "wti",     # WTI crude
    "GOLD":    "gold",    # Gold spot
    "SPX":     "spx",     # S&P 500
    "US10Y":   "tsy",     # US 10yr yield
    "BTCUSD":  "btc",   # Bitstamp — may lag
    "BTCUSDT": "btc",   # Binance — more liquid     # Bitcoin
    "DXY":     "dxy",     # USD index
    "KOSPI":   "kospi",   # KOSPI
    "NI225":   "nikkei",  # Nikkei 225
    "TTF1!":   "ttf",     # EU TTF Gas front month
    "BDI":     "bdi",     # Baltic Dry Index
}

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        # Verify secret
        secret = freq.args.get('secret') or (freq.json or {}).get('secret','')
        if secret != WEBHOOK_SECRET:
            return jsonify({"error":"unauthorized"}), 401

        body = freq.json
        if not body:
            return jsonify({"error":"no body"}), 400

        BOUNDS = {
            "brent":(50,200),"wti":(40,190),"gold":(1000,8000),
            "spx":(2000,10000),"tsy":(0.1,15),"btc":(1000,500000),
            "dxy":(70,140),"kospi":(1000,8000),"nikkei":(15000,60000),
            "ttf":(5,500),"bdi":(100,20000)
        }

        def update_key(key, value):
            try:
                v = float(value)
                lo,hi = BOUNDS.get(key,(0,float('inf')))
                if lo <= v <= hi:
                    data[key] = v
                    return True
                else:
                    print(f"[{now()}] {key}={v} out of bounds, skipped")
                    return False
            except:
                return False

        updated = []

        # Multi-price message from Pine Script
        if body.get('multi'):
            keys = ["brent","wti","gold","spx","tsy","btc","dxy","kospi","nikkei","ttf","bdi"]
            for key in keys:
                if key in body and update_key(key, body[key]):
                    updated.append(f"{key}={body[key]}")
            print(f"[{now()}] Multi-price webhook: {', '.join(updated)}")

        # Single price message
        else:
            symbol = body.get('symbol','').upper()
            price  = body.get('price')
            if price is None:
                return jsonify({"error":"no price"}), 400
            key = TV_SYMBOL_MAP.get(symbol)
            if not key:
                for tv_sym, data_key in TV_SYMBOL_MAP.items():
                    if tv_sym in symbol or symbol in tv_sym:
                        key = data_key
                        break
            if not key:
                return jsonify({"error":f"unknown symbol {symbol}"}), 400
            if update_key(key, price):
                updated.append(f"{key}={price}")
                print(f"[{now()}] Webhook: {symbol} → {key} = {price}")

        if not updated:
            return jsonify({"error":"no valid prices"}), 400

        check_alerts()
        return jsonify({"ok":True,"updated":updated})
    except Exception as e:
        print(f"[{now()}] Webhook error: {e}")
        return jsonify({"error":str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    from flask import make_response
    resp = make_response(jsonify({
        "status": "ok",
        "ts": int(time.time()),
        "brent":   data.get("brent"),
        "wti":     data.get("wti"),
        "gold":    data.get("gold"),
        "spx":     data.get("spx"),
        "tsy":     data.get("tsy"),
        "btc":     data.get("btc"),
        "dxy":     data.get("dxy"),
        "kospi":   data.get("kospi"),
        "nikkei":  data.get("nikkei"),
        "ttf":     data.get("ttf"),
        "bdi":     data.get("bdi"),
        "vlcc":    data.get("vlcc"),
        "hormuz":  data.get("hormuz"),
        "carriersOut":   data.get("carriers_out"),
        "carriersTotal": data.get("carriers_total"),
        "piWithdrawn":   data.get("pi_withdrawn"),
        "ceasefire":     data.get("ceasefire","none"),
        "conflictDay":   data.get("conflict_day",23),
        "ieaMb":         data.get("ieaMb",400)
    }))
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp

@app.route('/health', methods=['OPTIONS'])
def health_options():
    from flask import make_response
    resp = make_response('', 204)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp

def run_flask():
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)  # suppress Flask dev server noise
    port = int(os.getenv("PORT", 8080))
    print(f"[{now()}] Flask webhook server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False, threaded=True)

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

    # Start TradingView WebSocket feed FIRST — gets prices immediately
    start_tv_feed()
    time.sleep(1)

    # Run initial HormuzTracker fetch in background — don't block startup
    threading.Thread(target=refresh_data, daemon=True).start()

    state["start_time"] = time.time()
    state["last_fetch_time"] = now()
    state["phase"] = calc_phase()
    state["ceasefire"] = data.get("ceasefire", "none")
    state["brent_120"] = (data.get("brent") or 0) >= 120
    state["pi_withdrawn"] = data.get("pi_withdrawn", True)
    state["hormuz_40"] = (data.get("hormuz") or 0) >= 40

    send(
        f"🟢 <b>Hormuz Alert Bot started</b>\n"
        f"TradingView feed connecting...\n"
        f"Send /help for commands."
    )

    # Schedule
    schedule.every(5).minutes.do(lambda: (refresh_data(), check_alerts(), state.update({"last_fetch_time": now()})))
    schedule.every().day.at("08:00").do(send_summary)

    # Run bot schedule + command polling in background thread
    def bot_loop():
        print(f"[{now()}] Bot loop running...")
        while True:
            try:
                schedule.run_pending()
                handle_commands()
            except Exception as e:
                print(f"[{now()}] Bot loop error: {e}")
            time.sleep(10)

    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()

    print(f"[{now()}] Bot running. Starting Flask on main thread...")
    run_flask()  # Flask runs on main thread — keeps Railway happy  # poll commands every 10s

if __name__ == "__main__":
    main()

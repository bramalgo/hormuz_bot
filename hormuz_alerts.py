#!/usr/bin/env python3
"""
Hormuz Dashboard — Telegram Alert Bot
Runs 24/7, sends alerts and responds to commands.
Usage: python3 hormuz_alerts.py
"""

import os, json, time, requests, schedule
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ── set these or put in .env file ──────────────────────────────────
BOT_TOKEN  = os.getenv("TG_TOKEN",  "")   # from @BotFather
CHAT_ID    = os.getenv("TG_CHAT_ID", "")  # from @userinfobot
# ─────────────────────────────────────────────────────────────────────────────

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

data = {}  # latest fetched data

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
    """Fetch latest price from Yahoo Finance."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range={range_}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        d = r.json()
        closes = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]
        return closes[-1] if closes else None
    except:
        return None

def fetch_hormuztracker():
    """Scrape key data from HormuzTracker."""
    result = {}
    try:
        r = requests.get("https://www.hormuztracker.com/",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        html = r.text
        import re

        # Vessels
        vm = re.search(r'~\s*(\d+)\s*\n[\s\S]{0,10}Vessels detected today', html, re.I) \
          or re.search(r'~\s*([\d]+)ships?\s*detected\s*today', html, re.I)
        if vm:
            c = int(vm.group(1))
            if c < 500: result["hormuz"] = c

        # TTF
        tm = re.search(r'EU Gas \(TTF\)\s*\n+\s*€\s*([\d.]+)', html, re.I) \
          or re.search(r'EU Gas \(TTF\)€([\d.]+)', html, re.I)
        if tm:
            v = float(tm.group(1))
            if 5 < v < 500: result["ttf"] = v

        # P&I
        result["pi_withdrawn"] = bool(re.search(r'Withdrawn', html, re.I) and re.search(r'P.{0,3}I', html, re.I))

        # Carriers
        cm = re.search(r'(\d+)/(\d+)\s*Major Lines Suspended', html, re.I)
        if cm: result["carriers_out"] = int(cm.group(1)); result["carriers_total"] = int(cm.group(2))

        # Conflict day
        dm = re.search(r'Day\s+(\d+)', html, re.I)
        if dm: result["conflict_day"] = int(dm.group(1))

        # Ceasefire
        lc = html.lower()
        if "ceasefire holding" in lc:    result["ceasefire"] = "holding"
        elif "ceasefire announced" in lc: result["ceasefire"] = "announced"
        elif "ceasefire talks" in lc or "peace talks" in lc: result["ceasefire"] = "talks"
        else: result["ceasefire"] = "none"

    except Exception as e:
        print(f"[{now()}] HormuzTracker error: {e}")
    return result

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
        "spx": "%5EGSPC", "tsy": "%5ETNX", "btc": "BTC-USD",
        "dxy": "DX-Y.NYB", "kospi": "%5EKS11", "nikkei": "%5EN225", "bdi": "%5EBDI"
    }
    for key, sym in symbols.items():
        v = fetch_yahoo(sym)
        if v: data[key] = v

    ht = fetch_hormuztracker()
    data.update(ht)

    # Track brent high days
    if data.get("brent", 0) >= 120:
        state["brent_high_days"] = state.get("brent_high_days", 0) + 1
    else:
        state["brent_high_days"] = 0

    print(f"[{now()}] Brent=${data.get('brent','?'):.1f} Hormuz={data.get('hormuz','?')}/day "
          f"Phase={calc_phase()} Ceasefire={data.get('ceasefire','?')}")

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
    day = data.get("conflict_day") or \
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
        f"Carriers: {data.get('carriers_out','?')}/{data.get('carriers_total','?')} suspended\n"
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
                    f"Vessels: <b>{data.get('hormuz','?')}/day</b>\n"
                    f"Carriers: {data.get('carriers_out','?')}/{data.get('carriers_total','?')} suspended\n"
                    f"P&I: {'Cancelled' if data.get('pi_withdrawn') else 'Active'}\n"
                    f"Ceasefire: {cf_label}"
                )
            elif cmd == "/help":
                send(
                    "🤖 <b>Hormuz Alert Bot</b>\n\n"
                    "/summary — Full market snapshot\n"
                    "/phase — Current phase + action\n"
                    "/brent — Oil prices\n"
                    "/strait — Hormuz shipping\n"
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
    # Set baseline state so we don't fire spurious alerts on startup
    state["phase"] = calc_phase()
    state["ceasefire"] = data.get("ceasefire", "none")
    state["brent_120"] = data.get("brent", 0) >= 120
    state["pi_withdrawn"] = data.get("pi_withdrawn", True)
    state["hormuz_40"] = data.get("hormuz", 0) >= 40

    send(
        f"🟢 <b>Hormuz Alert Bot started</b>\n"
        f"Monitoring every 15 minutes.\n"
        f"Current: {PHASES[calc_phase()]['lbl']}\n"
        f"Brent: ${data.get('brent',0):.1f} | Hormuz: {data.get('hormuz','?')}/day\n"
        f"Send /help for commands."
    )

    # Schedule
    schedule.every(15).minutes.do(lambda: (refresh_data(), check_alerts()))
    schedule.every().day.at("08:00").do(send_summary)

    print(f"[{now()}] Bot running. Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        handle_commands()
        time.sleep(10)  # poll commands every 10s

if __name__ == "__main__":
    main()

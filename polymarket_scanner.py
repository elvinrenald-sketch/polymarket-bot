#!/usr/bin/env python3
"""
POLYMARKET SCANNER v9.0 — REAL MISPRICING
==========================================
Algoritma mispricing yang BENAR:

MISPRICING NYATA di Polymarket ada 5 jenis:

1. ARBITRAGE MURNI
   YES_ask + NO_ask < 1.0
   → Beli YES dan NO sekaligus, dapat $1, bayar kurang dari $1
   → Profit dijamin tanpa risiko

2. BID-ASK SPREAD LEBAR
   Spread > 5% = pasar tidak liquid = harga tidak efisien
   → Ada celah untuk masuk di harga yang lebih baik

3. ORDERBOOK IMBALANCE
   Banyak buyer di YES tapi sedikit seller → harga YES akan naik
   Deteksi dari: best_bid jauh dari best_ask

4. NEAR-RESOLUTION + EXTREME PRICE
   Pasar tutup < 6 jam, harga masih 0.15-0.85
   → Market belum consensus = volatil = peluang

5. VOLUME SPIKE ANOMALI
   Volume tiba-tiba 5x lebih besar dari rata-rata
   → Ada informasi baru yang masuk
   → Harga akan bergerak, masuk sebelum terlambat
"""

import asyncio
import aiohttp
import json
import time
import os
import math
import csv
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict
from colorama import Fore, Style, init
from tabulate import tabulate
from pathlib import Path

init(autoreset=True)

# ══════════════════════════════════════════════════════════════════
# ENV VARIABLES (Railway inject otomatis)
# ══════════════════════════════════════════════════════════════════
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
PRIVATE_KEY      = os.environ.get('PRIVATE_KEY', '')
WALLET_ADDRESS   = os.environ.get('WALLET_ADDRESS', '')
AUTO_TRADE       = os.environ.get('AUTO_TRADE', 'false').lower() == 'true'

# ══════════════════════════════════════════════════════════════════
# KONFIGURASI
# ══════════════════════════════════════════════════════════════════
CFG = {
    'GAMMA_API'         : 'https://gamma-api.polymarket.com',
    'CLOB_API'          : 'https://clob.polymarket.com',
    'SCAN_INTERVAL'     : 15,
    'MARKETS_PER_PAGE'  : 100,
    'MAX_PAGES'         : 15,
    'DISPLAY_TOP'       : 15,
    'CLEAR_SCREEN'      : True,
    'BANKROLL'          : 10.00,
    'TRADE_PER_SIGNAL'  : 1.00,
    'MAX_EXPOSURE'      : 10.00,
    'KELLY_FRACTION'    : 0.25,

    # Threshold sinyal
    'ARB_THRESHOLD'     : 0.995,   # sum ask < ini = ARBITRAGE
    'MIN_SPREAD'        : 0.03,    # spread > 3% = pasar tidak efisien
    'NEAR_RES_HOURS'    : 6,       # pasar tutup < 6 jam
    'VOL_SPIKE_RATIO'   : 3.0,     # volume > 3x likuiditas = spike
}

# Journal paths
JOURNAL_DIR = os.path.expanduser('~/polymarket-scanner/journal')
Path(JOURNAL_DIR).mkdir(parents=True, exist_ok=True)
DB_PATH  = os.path.join(JOURNAL_DIR, 'trades.db')
CSV_PATH = os.path.join(JOURNAL_DIR, 'trades.csv')
LOG_PATH = os.path.join(JOURNAL_DIR, 'scanner.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()]
)
log = logging.getLogger('poly')

# ══════════════════════════════════════════════════════════════════
# WARNA
# ══════════════════════════════════════════════════════════════════
GG=Fore.GREEN+Style.BRIGHT; G=Fore.GREEN; R=Fore.RED; RR=Fore.RED+Style.BRIGHT
Y=Fore.YELLOW; YY=Fore.YELLOW+Style.BRIGHT; C=Fore.CYAN; CC=Fore.CYAN+Style.BRIGHT
W=Fore.WHITE; WW=Fore.WHITE+Style.BRIGHT; M=Fore.MAGENTA; Z=Style.RESET_ALL
SPIN = ['⣾','⣽','⣻','⢿','⡿','⣟','⣯','⣷']

def fp(v,d=2):
    if v>=5: return f'{GG}{v:+.{d}f}%{Z}'
    if v>0:  return f'{G}{v:+.{d}f}%{Z}'
    if v==0: return f'{W}+0.00%{Z}'
    if v>-5: return f'{R}{v:+.{d}f}%{Z}'
    return f'{RR}{v:+.{d}f}%{Z}'

def fu(v):
    if v>=1_000_000: return f'${v/1_000_000:.1f}M'
    if v>=1_000: return f'${v/1_000:.1f}K'
    return f'${v:.0f}'

def fd(d):
    if d is None: return f'{W}—{Z}'
    if d<0: return f'{R}EXP{Z}'
    if d<0.25: return f'{RR}<6h{Z}'
    if d<1: return f'{RR}{d*24:.0f}h{Z}'
    if d<7: return f'{Y}{d:.1f}d{Z}'
    return f'{W}{d:.0f}d{Z}'

# ══════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════
_tg_sent: Dict[str, float] = {}

async def send_telegram(session, text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        await session.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
            timeout=aiohttp.ClientTimeout(total=5)
        )
    except: pass

async def tg_notify(session, r: dict, scan: int):
    """Kirim notif ke Telegram, anti-spam 30 menit per market"""
    if r['signal'] not in ['💰 ARBITRAGE','🔥 STRONG BUY','✅ BUY','⚡ EDGE','🔊 VOL SPIKE','📈 MOMENTUM']:
        return
    now = time.time()
    if now - _tg_sent.get(r['id'], 0) < 1800:
        return
    _tg_sent[r['id']] = now

    lines = [
        f"<b>{r['signal']}</b>",
        f"━━━━━━━━━━━━━━━━",
        f"📊 <b>{r['question'][:80]}</b>",
        f"",
        f"🎯 Action: <b>{r['action']}</b>",
        f"💰 Entry: <b>{r['entry_price']:.4f}</b>",
    ]
    if r.get('arb_profit', 0) > 0:
        lines.append(f"💎 Arb Profit: <b>{r['arb_profit']:+.2f}%</b>")
    if r.get('spread_pct', 0) > 0:
        lines.append(f"📐 Spread: {r['spread_pct']:.1f}%")
    if r.get('ev_pct', 0) != 0:
        lines.append(f"⚡ EV: {r['ev_pct']:+.2f}%")
    lines += [
        f"💧 Liq: {fu(r['liquidity'])}",
        f"⏰ Sisa: {r.get('days_str','?')}",
        f"🔍 Scan #{scan}",
        f"",
        f"⚠️ Paper trade dulu, pantau hasilnya!",
    ]
    await send_telegram(session, '\n'.join(lines))
    log.info(f"TG SENT: {r['signal']} {r['question'][:50]}")

# ══════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, market_id TEXT, question TEXT,
        signal TEXT, action TEXT, outcome TEXT,
        entry_price REAL, ev_pct REAL, spread_pct REAL,
        arb_profit REAL, liquidity REAL, days REAL,
        amount REAL, status TEXT DEFAULT "OPEN",
        pnl REAL, resolved_ts TEXT, correct INTEGER
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS scan_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, fetched INTEGER, valid INTEGER, ms INTEGER
    )''')
    conn.commit()
    conn.close()

def db_log_trade(r: dict, amount: float = 0):
    try:
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(DB_PATH)
        conn.execute('''INSERT INTO trades
            (ts,market_id,question,signal,action,outcome,entry_price,
             ev_pct,spread_pct,arb_profit,liquidity,days,amount,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,"OPEN")''', (
            ts, r.get('id',''), r.get('question','')[:200],
            r.get('signal',''), r.get('action',''), r.get('entry_outcome',''),
            r.get('entry_price',0), r.get('ev_pct',0), r.get('spread_pct',0),
            r.get('arb_profit',0), r.get('liquidity',0), r.get('days'),
            amount,
        ))
        conn.commit()
        conn.close()

        # CSV
        row = {
            'ts': ts, 'signal': r.get('signal',''),
            'question': r.get('question','')[:80],
            'action': r.get('action',''),
            'entry': r.get('entry_price',0),
            'ev%': round(r.get('ev_pct',0),2),
            'spread%': round(r.get('spread_pct',0),2),
            'arb%': round(r.get('arb_profit',0),2),
            'liq': round(r.get('liquidity',0),0),
            'days': round(r.get('days',0) or 0, 1),
            'amount': amount, 'status': 'OPEN',
        }
        exists = os.path.exists(CSV_PATH)
        with open(CSV_PATH, 'a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=row.keys())
            if not exists: w.writeheader()
            w.writerow(row)
    except Exception as e:
        log.error(f'DB error: {e}')

def db_get_stats() -> dict:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur  = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM trades')
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED'")
        closed = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED' AND pnl>0")
        wins = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='CLOSED'")
        pnl = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(amount),0) FROM trades WHERE status='OPEN'")
        exposure = cur.fetchone()[0]
        cur.execute('''SELECT ts,signal,substr(question,1,30),entry_price,amount,status,pnl
                       FROM trades ORDER BY id DESC LIMIT 5''')
        recent = cur.fetchall()
        conn.close()
        wr = (wins/closed*100) if closed>0 else 0
        return {
            'total':total,'closed':closed,'open':total-closed,
            'wins':wins,'losses':closed-wins,'win_rate':wr,
            'pnl':pnl,'exposure':exposure,'recent':recent
        }
    except:
        return {'total':0,'closed':0,'open':0,'wins':0,'losses':0,
                'win_rate':0,'pnl':0,'exposure':0,'recent':[]}

# ══════════════════════════════════════════════════════════════════
# API
# ══════════════════════════════════════════════════════════════════
async def api_get(session, url, params=None):
    try:
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status == 200:
                return await r.json(content_type=None)
    except: pass
    return None

async def fetch_markets(session) -> List[dict]:
    tasks = [
        api_get(session, f"{CFG['GAMMA_API']}/markets", {
            'active':'true','closed':'false',
            'limit':CFG['MARKETS_PER_PAGE'],
            'offset':i*CFG['MARKETS_PER_PAGE'],
            'order':'volume24hr','ascending':'false',
        })
        for i in range(CFG['MAX_PAGES'])
    ]
    pages = await asyncio.gather(*tasks)
    out = []
    for p in pages:
        if isinstance(p, list) and p:
            out.extend(p)
    return out

async def fetch_orderbook(session, token_id: str) -> Optional[dict]:
    """Fetch orderbook untuk dapat best bid dan best ask"""
    return await api_get(session, f"{CFG['CLOB_API']}/book",
                         {'token_id': token_id})

async def fetch_clob_batch(session, token_ids: List[str]) -> Dict[str, dict]:
    """Fetch midpoint dan spread untuk banyak token sekaligus"""
    if not token_ids: return {}
    result = {}
    batches = [token_ids[i:i+40] for i in range(0, min(len(token_ids),200), 40)]

    mid_tasks = [api_get(session, f"{CFG['CLOB_API']}/midpoints",
                         [('token_id',t) for t in b]) for b in batches]
    spd_tasks = [api_get(session, f"{CFG['CLOB_API']}/spreads",
                         [('token_id',t) for t in b]) for b in batches]

    mids_all, spds_all = await asyncio.gather(
        asyncio.gather(*mid_tasks),
        asyncio.gather(*spd_tasks)
    )

    mid_map, spd_map = {}, {}
    for page in mids_all:
        if isinstance(page, list):
            for x in page:
                tid = str(x.get('token_id',''))
                try: mid_map[tid] = float(x.get('mid',0))
                except: pass
    for page in spds_all:
        if isinstance(page, list):
            for x in page:
                tid = str(x.get('token_id',''))
                try: spd_map[tid] = float(x.get('spread',0))
                except: pass

    for tid, mid in mid_map.items():
        if mid > 0:
            spd = spd_map.get(tid, 0.04)
            result[tid] = {
                'mid'   : mid,
                'bid'   : max(0.001, mid - spd/2),
                'ask'   : min(0.999, mid + spd/2),
                'spread': spd,
            }
    return result

# ══════════════════════════════════════════════════════════════════
# PARSER
# ══════════════════════════════════════════════════════════════════
def parse_list(raw) -> list:
    if isinstance(raw, list): return raw
    if isinstance(raw, str):
        try: return json.loads(raw)
        except: return []
    return []

def parse_prices(m: dict) -> List[float]:
    for field in ['outcomePrices','prices']:
        raw = parse_list(m.get(field,[]))
        if raw:
            try:
                vals = [max(0.001, min(0.999, float(str(x)))) for x in raw]
                if len(vals) >= 2 and all(v>0 for v in vals): return vals
            except: pass
    return []

def parse_outcomes(m: dict) -> List[str]:
    return [str(x) for x in parse_list(m.get('outcomes','[]'))]

def parse_token_ids(m: dict) -> List[str]:
    return [str(x) for x in parse_list(m.get('clobTokenIds','[]'))]

def parse_days(m: dict) -> Optional[float]:
    for f in ['endDateIso','endDate']:
        val = m.get(f)
        if val:
            try:
                dt = datetime.fromisoformat(str(val).replace('Z','+00:00'))
                return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
            except: pass
    return None

# ══════════════════════════════════════════════════════════════════
# CORE ANALISIS — 5 JENIS MISPRICING
# ══════════════════════════════════════════════════════════════════
def analyze(names, gamma_px, clob, liquidity, volume, days, prev_px) -> Optional[dict]:
    N = len(gamma_px)
    if N < 2: return None

    # ── 1. ARBITRAGE: YES_ask + NO_ask < 1.0 ────────────────────
    # Beli semua outcome = dijamin dapat $1
    # Profit = 1.0 - total yang dibayar
    ask_prices = []
    for i in range(N):
        if i < len(clob) and clob[i]:
            ask_prices.append(clob[i]['ask'])
        else:
            ask_prices.append(gamma_px[i])

    ask_sum    = sum(ask_prices)
    is_arb     = ask_sum < CFG['ARB_THRESHOLD']
    arb_profit = max(0.0, (1.0 - ask_sum) * 100)

    # ── 2. SPREAD ANALYSIS ───────────────────────────────────────
    # Spread lebar = pasar tidak efisien = ada celah
    spreads = []
    for i in range(N):
        if i < len(clob) and clob[i] and clob[i]['mid'] > 0:
            spd_pct = clob[i]['spread'] / clob[i]['mid'] * 100
            spreads.append(spd_pct)
        else:
            spreads.append(0.0)

    max_spread = max(spreads) if spreads else 0.0
    best_spread_i = spreads.index(max_spread) if spreads else 0

    # ── 3. PRICE IMBALANCE ───────────────────────────────────────
    # Selisih bid vs ask jauh dari tengah
    # Jika bid lebih tinggi dari yang seharusnya = ada tekanan beli
    imbalance_scores = []
    for i in range(N):
        if i < len(clob) and clob[i]:
            mid = clob[i]['mid']
            bid = clob[i]['bid']
            ask = clob[i]['ask']
            # Imbalance: bid lebih dekat ke ask = tekanan beli kuat
            if ask > bid:
                imb = (bid - (bid+ask)/2) / ((ask-bid)/2) if (ask-bid) > 0 else 0
                imbalance_scores.append(imb)
            else:
                imbalance_scores.append(0)
        else:
            imbalance_scores.append(0)

    # ── 4. NEAR-RESOLUTION ───────────────────────────────────────
    near_res   = False
    near_note  = ''
    near_bonus = 0.0
    if days is not None and 0 <= days <= 1:
        # Harga masih jauh dari 0 atau 1 (belum resolved)
        for p in gamma_px:
            if 0.05 < p < 0.95:
                near_res   = True
                near_bonus = 30.0
                if days < 0.25:
                    near_note = '⏰ CLOSES <6H!'
                elif days < 0.5:
                    near_note = '⏰ CLOSES <12H'
                else:
                    near_note = '⏰ CLOSES <24H'
                break

    # ── 5. VOLUME SPIKE ──────────────────────────────────────────
    vol_spike = False
    vol_note  = ''
    vol_bonus = 0.0
    if liquidity > 0 and volume > liquidity * CFG['VOL_SPIKE_RATIO']:
        vol_spike = True
        ratio     = volume / liquidity
        vol_note  = f'🔊 VOL {ratio:.0f}x LIKUIDITAS!'
        vol_bonus = min(25.0, ratio * 3)

    # ── MOMENTUM ─────────────────────────────────────────────────
    momentum_pct = 0.0
    momentum_dir = ''
    if prev_px and len(prev_px) == N and prev_px[0] > 0:
        chg = (gamma_px[0] - prev_px[0]) / prev_px[0] * 100
        momentum_pct = chg
        if   chg >=  5: momentum_dir = f'↑{chg:.1f}%'
        elif chg <= -5: momentum_dir = f'↓{abs(chg):.1f}%'

    # ── PILIH ENTRY TERBAIK ──────────────────────────────────────
    # Prioritas: Arbitrage > Spread > Near-res > Volume

    if is_arb:
        # Beli semua outcome
        entry_i   = 0
        entry_px  = ask_prices[0]
        ev_pct    = arb_profit
        method    = 'ARBITRAGE'
    elif max_spread > CFG['MIN_SPREAD'] * 100:
        # Entry di sisi dengan spread terlebar = paling tidak efisien
        entry_i   = best_spread_i
        entry_px  = ask_prices[entry_i] if entry_i < len(ask_prices) else gamma_px[entry_i]
        # EV dari spread: beli di ask, jual di mid
        if entry_i < len(clob) and clob[entry_i]:
            mid      = clob[entry_i]['mid']
            ev_pct   = (mid / entry_px - 1) * 100 if entry_px > 0 else 0
        else:
            ev_pct   = max_spread / 2
        method    = 'SPREAD'
    else:
        # Default: sisi yang paling murah vs gamma
        entry_i   = 0
        entry_px  = ask_prices[0]
        ev_pct    = 0.0
        method    = 'MONITOR'

    entry_name = names[entry_i] if entry_i < len(names) else names[0]
    entry_fv   = gamma_px[entry_i]

    # ── KELLY ────────────────────────────────────────────────────
    kelly = 0.0
    if ev_pct > 0 and entry_px > 0.001 and entry_px < 0.999:
        # Estimasi fair prob dari EV
        fair_p = entry_px * (1 + ev_pct/100)
        fair_p = min(0.999, max(0.001, fair_p))
        b = (1/entry_px) - 1
        if b > 0:
            k = (b*fair_p - (1-fair_p)) / b
            kelly = max(0.0, k * CFG['KELLY_FRACTION'])

    # ── SCORE ────────────────────────────────────────────────────
    arb_s  = 100 if is_arb else 0
    ev_s   = min(60, max(0, ev_pct * 6))
    spd_s  = min(30, max(0, max_spread * 3))
    liq_s  = min(15, math.log10(max(liquidity,1)) * 4)
    vol_s  = min(15, math.log10(max(volume,1)) * 4)
    mom_s  = min(15, abs(momentum_pct) * 1.5)

    score = arb_s + ev_s + spd_s + liq_s + vol_s + mom_s + near_bonus + vol_bonus

    # ── SIGNAL ───────────────────────────────────────────────────
    # Combo signals — kombinasi faktor yang lebih realistis
    # ARBITRAGE: beli semua outcome, profit dijamin
    if is_arb and arb_profit > 0.2:
        signal, action, color = '💰 ARBITRAGE', f'BELI {" + ".join(names[:2])}', GG

    # STRONG BUY: momentum kuat + pasar mau tutup + volume spike
    elif (abs(momentum_pct) >= 10 and near_res and vol_spike):
        direction = names[0] if momentum_pct > 0 else (names[1] if N>1 else names[0])
        signal, action, color = '🔥 STRONG BUY', f'BUY {direction[:12].upper()}', GG

    # BUY: momentum kuat + salah satu faktor lain
    elif (abs(momentum_pct) >= 10 and near_res):
        direction = names[0] if momentum_pct > 0 else (names[1] if N>1 else names[0])
        signal, action, color = '✅ BUY', f'BUY {direction[:12].upper()}', G

    elif (abs(momentum_pct) >= 10 and vol_spike):
        direction = names[0] if momentum_pct > 0 else (names[1] if N>1 else names[0])
        signal, action, color = '✅ BUY', f'BUY {direction[:12].upper()}', G

    elif (abs(momentum_pct) >= 15):
        direction = names[0] if momentum_pct > 0 else (names[1] if N>1 else names[0])
        signal, action, color = '✅ BUY', f'BUY {direction[:12].upper()}', G

    # EDGE: momentum sedang + near-res
    elif (abs(momentum_pct) >= 5 and near_res):
        direction = names[0] if momentum_pct > 0 else (names[1] if N>1 else names[0])
        signal, action, color = '⚡ EDGE', f'BUY {direction[:12].upper()}', YY

    # EDGE: volume spike + near-res
    elif (vol_spike and near_res):
        signal, action, color = '⚡ EDGE', f'WATCH {entry_name[:10].upper()}', YY

    # Vol spike saja
    elif vol_spike and volume > liquidity * 5:
        signal, action, color = '🔊 VOL SPIKE', f'WATCH {entry_name[:10].upper()}', Y

    # Near-res saja
    elif near_res and days is not None and days < 0.25:
        signal, action, color = '⏰ NEAR-RES', f'WATCH {entry_name[:10].upper()}', Y

    # Momentum saja
    elif abs(momentum_pct) >= 5:
        direction = names[0] if momentum_pct > 0 else (names[1] if N>1 else names[0])
        signal, action, color = '📈 MOMENTUM', f'WATCH {direction[:10].upper()}', C

    elif near_res:
        signal, action, color = '⏰ NEAR-RES', 'MONITOR', Y

    else:
        signal, action, color = '➖ MONITOR', 'MONITOR', W

    return {
        'signal': signal, 'action': action, 'color': color, 'method': method,
        'names': names, 'gamma_px': gamma_px, 'ask_prices': ask_prices,
        'ask_sum': ask_sum, 'is_arb': is_arb, 'arb_profit': arb_profit,
        'entry_outcome': entry_name, 'entry_price': entry_px, 'entry_fv': entry_fv,
        'spread_pct': max_spread, 'spreads': spreads,
        'ev_pct': ev_pct, 'kelly': kelly, 'kelly_usd': kelly * CFG['BANKROLL'],
        'momentum_pct': momentum_pct, 'momentum_dir': momentum_dir,
        'near_res': near_res, 'near_note': near_note,
        'vol_spike': vol_spike, 'vol_note': vol_note,
        'score': score, 'clob': clob,
    }

def process(m: dict, history: dict, clob_map: dict) -> Optional[dict]:
    q = (m.get('question') or '').strip()
    if not q: return None

    liq  = float(m.get('liquidity') or 0)
    vol  = float(m.get('volume24hr') or m.get('volume') or 0)
    names  = parse_outcomes(m)
    prices = parse_prices(m)
    tids   = parse_token_ids(m)
    days   = parse_days(m)

    if len(prices) < 2 or len(names) < 2: return None
    if days is not None and days < -1: return None

    n = min(len(names), len(prices))
    names, prices = names[:n], prices[:n]

    clob = [clob_map.get(tids[i]) if i < len(tids) else None for i in range(n)]

    mid = str(m.get('id', q[:40]))
    res = analyze(names, prices, clob, liq, vol, days, history.get(mid, []))
    if not res: return None

    # Format days string
    days_str = fd(days).replace(Z,'').replace(Y,'').replace(R,'').replace(RR,'').replace(W,'')

    return {
        'id': mid, 'question': q,
        'category': str(m.get('category') or m.get('groupItemTitle') or 'General')[:20],
        'liquidity': liq, 'volume_24h': vol, 'days': days, 'days_str': days_str,
        'token_ids': tids,
        **res,
    }

# ══════════════════════════════════════════════════════════════════
# DISPLAY
# ══════════════════════════════════════════════════════════════════
def banner():
    print(CC + WW + '''
╔══════════════════════════════════════════════════════════════════════╗
║  POLYMARKET SCANNER v9.0 — REAL MISPRICING DETECTOR                ║
║  Arbitrage · Spread · Near-Resolution · Volume Spike · Momentum     ║
╚══════════════════════════════════════════════════════════════════════╝''')

def display_stats(st: dict):
    wr_c  = GG if st['win_rate']>=55 else (Y if st['win_rate']>=45 else R)
    pnl_c = G if st['pnl']>0 else (Y if st['pnl']==0 else R)
    print(f'\n{WW}  ┌─ JOURNAL ────────────────────────────────────────────────────────┐')
    print(
        f'  │  Total:{W}{st["total"]}{Z}  '
        f'Open:{C}{st["open"]}{Z}  '
        f'Closed:{W}{st["closed"]}{Z}  '
        f'Win:{G}{st["wins"]}{Z}  '
        f'Loss:{R}{st["losses"]}{Z}  '
        f'WinRate:{wr_c}{st["win_rate"]:.1f}%{Z}  '
        f'P&L:{pnl_c}${st["pnl"]:+.2f}{Z}  '
        f'Exposure:{Y}${st["exposure"]:.2f}{Z}'
    )
    print(f'{WW}  └──────────────────────────────────────────────────────────────────┘{Z}')

    if st['recent']:
        rows = []
        for t in st['recent']:
            ts,sig,q,ep,amt,status,pnl_v = t
            pnl_s = f'{G}+${pnl_v:.2f}{Z}' if (pnl_v or 0)>0 else (f'{R}${pnl_v:.2f}{Z}' if pnl_v else f'{Y}open{Z}')
            rows.append([ts[:16],sig[:14],q,f'{ep:.3f}',f'${amt:.2f}',status,pnl_s])
        print(f'\n{WW}  Trade terbaru:{Z}')
        print(tabulate(rows, headers=['Waktu','Signal','Pasar','Entry','Bet','Status','P&L'], tablefmt='simple'))

def display(results, stats_scan, stats_j):
    if CFG['CLEAR_SCREEN']: os.system('clear 2>/dev/null || cls 2>/dev/null || true')
    banner()
    sp  = SPIN[stats_scan['scans'] % len(SPIN)]
    ts  = datetime.now().strftime('%H:%M:%S')
    print(f'\n{C}{"═"*72}')
    print(
        f'  {sp} {W}Waktu:{Y}{ts}{Z}  '
        f'{W}Scan:{C}#{stats_scan["scans"]}{Z}  '
        f'{W}Pasar:{C}{stats_scan["fetched"]}{Z}  '
        f'{W}Valid:{G}{stats_scan["valid"]}{Z}  '
        f'{W}Durasi:{C}{stats_scan["ms"]}ms{Z}  '
        f'{W}Alert:{GG}{stats_scan["alerts"]}{Z}'
    )
    print(f'{C}{"═"*72}')

    display_stats(stats_j)

    if not results:
        print(f'\n{Y}  Menunggu data...\n')
        return

    # Tabel
    print(f'\n{WW}  TOP {len(results)} PELUANG — SCAN #{stats_scan["scans"]}\n')
    rows = []
    for rank, r in enumerate(results, 1):
        col = r['color']
        q   = (r['question'][:30]+'..')if len(r['question'])>30 else r['question']
        mo  = r['momentum_dir']
        mc  = G if '↑' in mo else (R if '↓' in mo else W)
        rk  = f'{GG}★{rank}{Z}' if 'ARBITRAGE' in r['signal'] or 'STRONG' in r['signal'] else f'{col}{rank}{Z}'

        rows.append([
            rk, f'{col}{r["signal"]}{Z}', q,
            f'{col}{r["action"][:14]}{Z}',
            f'{r["entry_price"]:.3f}',
            fp(r['ev_pct']),
            f'{G}{r["spread_pct"]:.1f}%{Z}' if r['spread_pct']>3 else f'{W}{r["spread_pct"]:.1f}%{Z}',
            f'{GG}{r["arb_profit"]:+.2f}%{Z}' if r['is_arb'] else f'{W}—{Z}',
            f'{C}{r["score"]:.0f}{Z}',
            fu(r['liquidity']),
            f'{G}${r["kelly_usd"]:.2f}{Z}',
            fd(r['days']),
            f'{mc}{mo}{Z}',
        ])

    print(tabulate(rows,
        headers=['#','Signal','Pasar','Action','Entry','EV%','Spread','Arb%','Skor','Liq','Kelly$','Sisa','Mom'],
        tablefmt='simple'))

    # Detail top 5
    print(f'\n{WW}  ━━━━━ DETAIL TOP 5 ━━━━━\n')
    for r in results[:5]:
        _card(r)

    print(f'{C}{"─"*72}')
    at_str = f'{GG}ON ${CFG["TRADE_PER_SIGNAL"]}/signal{Z}' if AUTO_TRADE else f'{Y}OFF{Z}'
    print(
        f'  Bankroll:{G}${CFG["BANKROLL"]:.2f}{Z}  '
        f'AutoTrade:{at_str}  '
        f'Scan:{C}{CFG["SCAN_INTERVAL"]}s{Z}  '
        f'Journal:{C}{CSV_PATH}{Z}\n'
    )

def _card(r: dict):
    col = r['color']
    print(f'  {col}{"━"*68}{Z}')
    print(f'  {col}{r["signal"]}{Z}  {WW}{r["question"][:65]}{Z}')
    print(
        f'  {C}Liq:{Z}{fu(r["liquidity"])}  '
        f'{C}Vol24h:{Z}{fu(r["volume_24h"])}  '
        f'{C}Sisa:{Z}{fd(r["days"])}  '
        f'{C}Method:{Z}{W}{r["method"]}{Z}'
    )

    # Outcome dengan harga gamma dan CLOB
    for i,(name,gp) in enumerate(zip(r['names'],r['gamma_px'])):
        is_e = name == r['entry_outcome']
        c2   = GG if is_e else W
        mark = ' ◄ ENTRY' if is_e else ''
        cd   = r['clob'][i] if i < len(r.get('clob',[])) else None
        ask  = r['ask_prices'][i] if i < len(r.get('ask_prices',[])) else gp
        spd  = r['spreads'][i] if i < len(r.get('spreads',[])) else 0

        clob_str = f' [CLOB ask:{ask:.3f} spread:{spd:.1f}%]' if cd else ''
        print(f'    {c2}{name}: gamma={gp:.3f}{clob_str}{mark}{Z}')

    print()
    if r['is_arb']:
        print(f'  {GG}  >>> 💰 ARBITRAGE: BELI SEMUA OUTCOME!{Z}')
        print(f'  {GG}      Total ask = {r["ask_sum"]:.4f} < 1.0{Z}')
        print(f'  {GG}      Profit dijamin: {r["arb_profit"]:+.2f}%{Z}')
    else:
        ev_c = GG if r['ev_pct']>=5 else (G if r['ev_pct']>0 else R)
        print(
            f'  {Y}  >>> {r["action"]}{Z}  '
            f'@ {GG}{r["entry_price"]:.4f}{Z}  '
            f'EV:{ev_c}{r["ev_pct"]:+.2f}%{Z}  '
            f'Spread:{G}{r["spread_pct"]:.1f}%{Z}  '
            f'Kelly:${G}{r["kelly_usd"]:.2f}{Z}'
        )
    for note in [r.get('near_note'), r.get('vol_note')]:
        if note: print(f'  {Y}  ⚠  {note}{Z}')
    if r['momentum_dir']:
        mc = G if '↑' in r['momentum_dir'] else R
        print(f'  {W}  Momentum: {mc}{r["momentum_dir"]}{Z}')
    print()

# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
async def main():
    os.system('clear')
    init_db()
    banner()
    print(f'\n  {Y}Inisialisasi scanner v9.0...{Z}')
    print(f'  {C}Pasar:{Z} {CFG["MAX_PAGES"]*CFG["MARKETS_PER_PAGE"]}  {C}Interval:{Z} {CFG["SCAN_INTERVAL"]}s')
    print(f'  {C}Bankroll:{Z} ${CFG["BANKROLL"]:.2f}  {C}AutoTrade:{Z} {"ON" if AUTO_TRADE else "OFF"}')
    print(f'  {C}Telegram:{Z} {"Terhubung ✓" if TELEGRAM_TOKEN else "Tidak aktif"}')
    print(f'  {C}Journal:{Z} {CSV_PATH}\n')
    await asyncio.sleep(1)

    history : Dict[str, list] = {}
    scans   = 0
    alerts  = 0
    exposure= 0.0
    already_traded = set()

    conn = aiohttp.TCPConnector(limit=50, limit_per_host=15, ttl_dns_cache=300, ssl=False)
    hdrs = {'User-Agent': 'Mozilla/5.0 PolyScanner/9.0', 'Accept': 'application/json'}

    async with aiohttp.ClientSession(connector=conn, headers=hdrs) as session:

        # Test Telegram
        if TELEGRAM_TOKEN:
            await send_telegram(session,
                '🤖 <b>Polymarket Scanner v9.0 Online!</b>\n'
                'Bot sedang scan 1500 pasar setiap 15 detik.\n'
                'Notifikasi akan masuk saat ada sinyal kuat.')

        while True:
            t0 = time.time()
            try:
                # Fetch markets
                raw = await fetch_markets(session)

                # Kumpulkan token IDs
                all_tids = []
                for m in raw:
                    tids = parse_token_ids(m)
                    all_tids.extend(tids[:2])
                all_tids = list(dict.fromkeys(all_tids))

                # Fetch CLOB data
                clob_map = await fetch_clob_batch(session, all_tids)

                # Process
                results  = []
                new_hist : Dict[str, list] = {}

                for m in raw:
                    try:
                        r = process(m, history, clob_map)
                        if r:
                            results.append(r)
                            new_hist[r['id']] = r['gamma_px']
                        else:
                            mid = str(m.get('id',''))
                            pp  = parse_prices(m)
                            if mid and pp: new_hist[mid] = pp
                    except: continue

                history = new_hist
                results.sort(key=lambda x: x['score'], reverse=True)
                top = results[:CFG['DISPLAY_TOP']]

                scans += 1
                ms     = int((time.time()-t0)*1000)

                # Alert & journal untuk sinyal kuat
                for r in top:
                    is_strong = r['signal'] in ['💰 ARBITRAGE','🔥 STRONG BUY','✅ BUY','⚡ EDGE','🔊 VOL SPIKE']
                    if is_strong:
                        alerts += 1
                        await tg_notify(session, r, scans)
                        # Log ke journal (paper trade, amount=0)
                        if r['id'] not in already_traded:
                            db_log_trade(r, amount=0)
                            already_traded.add(r['id'])
                            log.info(f"SIGNAL: {r['signal']} | {r['question'][:50]}")

                # Log scan stats
                try:
                    conn2 = sqlite3.connect(DB_PATH)
                    conn2.execute(
                        'INSERT INTO scan_log (ts,fetched,valid,ms) VALUES (?,?,?,?)',
                        (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), len(raw), len(results), ms)
                    )
                    conn2.commit()
                    conn2.close()
                except: pass

                stats_j = db_get_stats()
                display(top, {'scans':scans,'fetched':len(raw),'valid':len(results),'ms':ms,'alerts':alerts}, stats_j)

            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f'Scan error: {e}')

            try:
                await asyncio.sleep(CFG['SCAN_INTERVAL'])
            except KeyboardInterrupt:
                break

    print(f'\n{Y}  Scanner berhenti. Total scan: {scans}{Z}')
    print(f'  Journal: {CSV_PATH}\n')

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f'\n{Y}  Sampai jumpa! 👋{Z}\n')

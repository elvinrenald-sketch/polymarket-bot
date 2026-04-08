#!/usr/bin/env python3
"""
POLYMARKET FULL AUTO BOT v11.2
================================
FIXES:
  - TERM env not set → Railway crash FIXED
  - CC not defined → FIXED (dihapus, pakai plain text)
  - os.system('clear') crash di non-TTY FIXED
  - Unicode encoding crash FIXED
  - already_opened reset saat restart FIXED (load dari DB)
  - Auto OPEN hanya STRONG BUY + ARBITRAGE
  - Auto CLOSE: TIME_EXIT <45m, FORCE_EXIT <3m
  - MAX EXPOSURE $5 → stop open posisi baru
  - fetch_price FIX: API call changed to /midpoint directly
  - fetch_clob_batch FIX: API call changed to use POST objects
  - Auto Close timezone FIX: fixed datetime diff calculation
"""

import os, sys

if not os.environ.get('TERM'):
    os.environ['TERM'] = 'dumb'

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import asyncio
import aiohttp
import json
import time
import csv
import sqlite3
import logging
import math
import re
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from pathlib import Path

try:
    from intelligence import TradingBrain
except ImportError:
    TradingBrain = None

# Colorama import aman — strip warna di Railway (no TTY)
try:
    from colorama import Fore, Style, init as colorama_init
    IS_TTY = sys.stdout.isatty()
    colorama_init(autoreset=True, strip=not IS_TTY)
    GG = Fore.GREEN + Style.BRIGHT
    G  = Fore.GREEN
    R  = Fore.RED
    RR = Fore.RED + Style.BRIGHT
    Y  = Fore.YELLOW
    YY = Fore.YELLOW + Style.BRIGHT
    C  = Fore.CYAN
    W  = Fore.WHITE
    WW = Fore.WHITE + Style.BRIGHT
    M  = Fore.MAGENTA
    Z  = Style.RESET_ALL
except Exception:
    GG = G = R = RR = Y = YY = C = W = WW = M = Z = ''

try:
    from tabulate import tabulate
except Exception:
    def tabulate(rows, headers=None, tablefmt=None):
        lines = []
        if headers:
            lines.append('  '.join(str(h) for h in headers))
        for r in rows:
            lines.append('  '.join(str(x) for x in r))
        return '\n'.join(lines)

SPIN = ['-', '\\', '|', '/']

# ══════════════════════════════════════════════════════════════════
# ENV VARIABLES
# ══════════════════════════════════════════════════════════════════
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '8340614437:AAG-0RQsA_tbKdScd9uNNHqbcab7k1NDhkw')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '6469687459')
PRIVATE_KEY      = os.environ.get('PRIVATE_KEY', '')
WALLET_ADDRESS   = os.environ.get('WALLET_ADDRESS', '')
AUTO_TRADE       = os.environ.get('AUTO_TRADE', 'false').lower() == 'true'

# ══════════════════════════════════════════════════════════════════
# KONFIGURASI
# ══════════════════════════════════════════════════════════════════
CFG = {
    # API
    'GAMMA_API'           : 'https://gamma-api.polymarket.com',
    'CLOB_API'            : 'https://clob.polymarket.com',

    # Scanner
    'SCAN_INTERVAL'       : 30,
    'MARKETS_PER_PAGE'    : 100,
    'MAX_PAGES'           : 15,
    'DISPLAY_TOP'         : 10,
    'CLEAR_SCREEN'        : False,   # Railway tidak punya terminal

    # Risk Management — aman untuk $10
    'BANKROLL'            : 10.00,
    'TRADE_PER_SIGNAL'    : 1.00,    # $1 per entry
    'MAX_POSITIONS'       : 5,       # Max 5 posisi terbuka
    'MAX_EXPOSURE'        : 5.00,    # STOP buka posisi baru jika exposure >= $5

    # Auto-Close rules
    'TAKE_PROFIT_PCT'     : 50.0,    # Close jika naik 50%
    'STOP_LOSS_PCT'       : 40.0,    # Close jika turun 40%
    'TIME_EXIT_MINUTES'   : 45,      # Close jika sisa < 45 menit
    'FORCE_EXIT_MINUTES'  : 3,       # FORCE close jika sisa < 3 menit
    'MAX_HOLD_HOURS'      : 72,      # Force close setelah 72 jam
    'MIN_ML_CONFIDENCE'   : 50.0,    # Minimal skor dari Brain (0-100)

    # Signal — hanya STRONG BUY & ARBITRAGE yang auto-open
    'AUTO_OPEN_SIGNALS'   : ['STRONG BUY', 'ARBITRAGE'],
    'MIN_MOMENTUM'        : 10.0,
    'MIN_LIQUIDITY'       : 500,
    'VOL_SPIKE_RATIO'     : 3.0,
    'NEAR_RES_HOURS'      : 6,
    'KELLY_FRACTION'      : 0.25,
}

# ══════════════════════════════════════════════════════════════════
# PATHS
# ══════════════════════════════════════════════════════════════════
JOURNAL_DIR = os.path.expanduser('~/polymarket-scanner/journal')
Path(JOURNAL_DIR).mkdir(parents=True, exist_ok=True)
DB_PATH    = os.path.join(JOURNAL_DIR, 'trades.db')
MODEL_PATH = os.path.join(JOURNAL_DIR, 'brain.joblib')
CSV_PATH = os.path.join(JOURNAL_DIR, 'trades.csv')
LOG_PATH = os.path.join(JOURNAL_DIR, 'scanner.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('poly')

# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════
def strip_ansi(s: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*m', '', str(s))

def fd(d) -> str:
    if d is None:    return '--'
    if d < 0:        return 'EXPIRED'
    if d < 0.002:    return '<3m'
    if d < 0.007:    return '<10m'
    if d < 0.031:    return '<45m'
    if d < 0.042:    return '<1h'
    if d < 0.25:     return '<6h'
    if d < 1:        return f'{d*24:.0f}h'
    return f'{d:.1f}d'

def fu(v) -> str:
    if v >= 1_000_000: return f'${v/1_000_000:.1f}M'
    if v >= 1_000:     return f'${v/1_000:.1f}K'
    return f'${v:.0f}'

def format_sisa(days) -> str:
    if days is None: return '--'
    if days < 0: return 'EXPIRED'
    total_seconds = int(days * 86400)
    if total_seconds < 60: return f'{total_seconds}s'
    hours, remainder = divmod(total_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours > 24: return f'{hours//24}d {hours%24}h'
    if hours > 0: return f'{hours}h {minutes}m'
    return f'{minutes}m'

def parse_iso_date(date_str: str) -> Optional[datetime]:
    if not date_str: return None
    try:
        # Handle Z or +00:00
        clean_str = str(date_str).replace('Z', '+00:00')
        # If it's just a date (YYYY-MM-DD), add end of day
        if len(clean_str) == 10:
            clean_str += "T23:59:59+00:00"
        return datetime.fromisoformat(clean_str)
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS positions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        open_ts       TEXT NOT NULL,
        close_ts      TEXT,
        market_id     TEXT,
        token_id      TEXT,
        question      TEXT,
        signal        TEXT,
        outcome       TEXT,
        entry_price   REAL,
        exit_price    REAL,
        amount_usd    REAL,
        shares        REAL,
        status        TEXT DEFAULT "OPEN",
        close_reason  TEXT,
        pnl_usd       REAL DEFAULT 0,
        pnl_pct       REAL DEFAULT 0,
        result        TEXT,
        end_date      TEXT,
        features_json TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS scan_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, fetched INTEGER, valid INTEGER,
        open_pos INTEGER, ms INTEGER
    )''')
    conn.commit()
    conn.close()
    log.info(f'DB OK: {DB_PATH}')

def db_open_position(r: dict, amount: float, shares: float) -> int:
    ts   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    features = json.dumps(r) # Save the whole result as features
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute('''INSERT INTO positions
        (open_ts,market_id,token_id,question,signal,outcome,
         entry_price,amount_usd,shares,status,end_date,features_json)
        VALUES (?,?,?,?,?,?,?,?,?,"OPEN",?,?)''', (
        ts,
        r.get('id', ''),
        r.get('entry_token_id', ''),
        r.get('question', '')[:200],
        r.get('signal', ''),
        r.get('entry_outcome', ''),
        r.get('entry_price', 0),
        amount, shares,
        r.get('end_date', ''),
        features
    ))
    pos_id = cur.lastrowid
    conn.commit(); conn.close()
    log.info(f"DB OPEN #{pos_id}: {r.get('signal')} | {r.get('entry_outcome')} "
             f"@ {r.get('entry_price', 0):.4f} | ${amount:.2f}")
    return pos_id

def db_close_position(pos_id: int, exit_price: float, reason: str) -> float:
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute('SELECT entry_price,amount_usd,shares,outcome FROM positions WHERE id=?',
                (pos_id,))
    row = cur.fetchone()
    if not row:
        conn.close(); return 0.0
    entry_price, amount_usd, shares, outcome = row
    proceeds = shares * exit_price if shares else 0
    pnl_usd  = proceeds - amount_usd
    pnl_pct  = (pnl_usd / amount_usd * 100) if amount_usd > 0 else 0
    result   = 'WIN' if pnl_usd > 0 else 'LOSS'
    ts       = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cur.execute('''UPDATE positions SET
        close_ts=?, exit_price=?, status="CLOSED",
        close_reason=?, pnl_usd=?, pnl_pct=?, result=?
        WHERE id=?''', (ts, exit_price, reason,
                        round(pnl_usd, 4), round(pnl_pct, 2),
                        result, pos_id))
    conn.commit(); conn.close()
    log.info(f"DB CLOSE #{pos_id}: {reason} @ {exit_price:.4f} | "
             f"P&L: ${pnl_usd:+.4f} ({pnl_pct:+.1f}%) [{result}]")
    return pnl_usd

def db_get_open_positions() -> List[dict]:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur  = conn.cursor()
        cur.execute('''SELECT id,open_ts,market_id,token_id,question,signal,
                              outcome,entry_price,amount_usd,shares,end_date
                       FROM positions WHERE status="OPEN" ORDER BY id''')
        rows = cur.fetchall(); conn.close()
        return [
            {'id': r[0], 'open_ts': r[1], 'market_id': r[2],
             'token_id': r[3], 'question': r[4], 'signal': r[5],
             'outcome': r[6], 'entry_price': r[7], 'amount_usd': r[8],
             'shares': r[9], 'end_date': r[10]}
            for r in rows
        ]
    except Exception as e:
        log.error(f'db_get_open_positions error: {e}')
        return []

def db_get_open_market_ids() -> set:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur  = conn.cursor()
        cur.execute("SELECT market_id FROM positions WHERE status='OPEN'")
        rows = cur.fetchall(); conn.close()
        return {r[0] for r in rows}
    except Exception:
        return set()

def db_get_stats() -> dict:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur  = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM positions')
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM positions WHERE status='CLOSED'")
        closed = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'")
        open_c = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND pnl_usd>0")
        wins = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM positions WHERE status='CLOSED'")
        pnl = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(amount_usd),0) FROM positions WHERE status='OPEN'")
        exposure = cur.fetchone()[0]
        cur.execute('''SELECT open_ts,signal,substr(question,1,30),
                              entry_price,amount_usd,status,pnl_usd,close_reason
                       FROM positions ORDER BY id DESC LIMIT 8''')
        recent = cur.fetchall(); conn.close()
        wr = (wins / closed * 100) if closed > 0 else 0
        return {
            'total': total, 'closed': closed, 'open': open_c,
            'wins': wins, 'losses': closed - wins, 'win_rate': wr,
            'pnl': pnl, 'exposure': exposure, 'recent': recent,
        }
    except Exception as e:
        log.error(f'db_get_stats error: {e}')
        return {'total': 0, 'closed': 0, 'open': 0, 'wins': 0,
                'losses': 0, 'win_rate': 0, 'pnl': 0, 'exposure': 0, 'recent': []}

# ══════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════
async def tg(session, text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    clean = strip_ansi(text)
    try:
        await session.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': clean, 'parse_mode': 'HTML'},
            timeout=aiohttp.ClientTimeout(total=5)
        )
    except Exception as e:
        log.warning(f'Telegram error: {e}')

async def tg_open(session, r: dict, pos_id: int, amount: float):
    mode = 'REAL TRADE' if AUTO_TRADE and PRIVATE_KEY else 'PAPER TRADE'
    text = (
        f"<b>OPEN POSISI #{pos_id}</b>\n"
        f"{'='*24}\n"
        f"<b>{r['question'][:80]}</b>\n\n"
        f"Signal  : <b>{r['signal']}</b>\n"
        f"Outcome : <b>{r.get('entry_outcome', '')}</b>\n"
        f"Entry   : <b>{r['entry_price']:.4f}</b>\n"
        f"Bet     : <b>${amount:.2f}</b>\n"
        f"Sisa    : {fd(r.get('days'))}\n"
        f"Liq     : {fu(r.get('liquidity', 0))}\n"
        f"Momentum: {r.get('momentum_pct', 0):+.1f}%\n"
        f"Mode    : {mode}\n"
        f"TP: +{CFG['TAKE_PROFIT_PCT']:.0f}% | "
        f"SL: -{CFG['STOP_LOSS_PCT']:.0f}% | "
        f"TimeExit: <{CFG['TIME_EXIT_MINUTES']}m | "
        f"Force: <{CFG['FORCE_EXIT_MINUTES']}m"
    )
    await tg(session, text)

async def tg_close(session, pos: dict, exit_price: float, pnl: float, reason: str):
    emoji = 'PROFIT' if pnl > 0 else 'RUGI'
    text = (
        f"<b>CLOSE #{pos['id']} [{emoji}]</b>\n"
        f"{'='*24}\n"
        f"{pos['question'][:60]}\n\n"
        f"Alasan : <b>{reason}</b>\n"
        f"Entry  : {pos['entry_price']:.4f} → Exit: <b>{exit_price:.4f}</b>\n"
        f"P&L    : <b>${pnl:+.4f}</b>\n"
    )
    await tg(session, text)

# ══════════════════════════════════════════════════════════════════
# API
# ══════════════════════════════════════════════════════════════════
async def api_get(session, url, params=None):
    try:
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status == 200:
                return await r.json(content_type=None)
    except Exception:
        pass
    return None

async def fetch_markets(session) -> List[dict]:
    tasks = [
        api_get(session, f"{CFG['GAMMA_API']}/markets", {
            'active': 'true', 'closed': 'false',
            'limit': CFG['MARKETS_PER_PAGE'],
            'offset': i * CFG['MARKETS_PER_PAGE'],
            'order': 'volume24hr', 'ascending': 'false',
        })
        for i in range(CFG['MAX_PAGES'])
    ]
    pages = await asyncio.gather(*tasks)
    out = []
    for p in pages:
        if isinstance(p, list) and p:
            out.extend(p)
    return out

async def fetch_price(session, token_id: str) -> Optional[float]:
    if not token_id:
        return None
    try:
        data = await api_get(session, f"{CFG['CLOB_API']}/midpoint",
                             [('token_id', token_id)])
        if isinstance(data, dict):
            v = float(data.get('mid', 0))
            return v if v > 0 else None
    except Exception:
        pass
    return None

async def fetch_clob_batch(session, token_ids: List[str]) -> Dict[str, dict]:
    if not token_ids:
        return {}
    result = {}
    batches = [token_ids[i:i+40] for i in range(0, min(len(token_ids), 200), 40)]
    
    async def post_batch(url, batch):
        try:
            async with session.post(url, json=[{"token_id": str(t)} for t in batch], 
                                    timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status == 200:
                    return await r.json()
        except Exception:
            pass
        return {}

    mid_tasks = [post_batch(f"{CFG['CLOB_API']}/midpoints", b) for b in batches]
    spd_tasks = [post_batch(f"{CFG['CLOB_API']}/spreads", b) for b in batches]
    
    mids_res, spds_res = await asyncio.gather(
        asyncio.gather(*mid_tasks, return_exceptions=True),
        asyncio.gather(*spd_tasks, return_exceptions=True)
    )
    
    mid_map, spd_map = {}, {}
    for batch_res in mids_res:
        if isinstance(batch_res, dict):
            for k, v in batch_res.items():
                if k != "error":
                    try: mid_map[k] = float(v)
                    except: pass
                    
    for batch_res in spds_res:
        if isinstance(batch_res, dict):
            for k, v in batch_res.items():
                if k != "error":
                    try: spd_map[k] = float(v)
                    except: pass

    for tid, mid in mid_map.items():
        if mid > 0:
            spd = spd_map.get(tid, 0.04)
            result[tid] = {
                'mid': mid,
                'bid': max(0.001, mid - spd / 2),
                'ask': min(0.999, mid + spd / 2),
                'spread': spd,
            }
    return result

# ══════════════════════════════════════════════════════════════════
# PARSER
# ══════════════════════════════════════════════════════════════════
def parse_list(raw) -> list:
    if isinstance(raw, list): return raw
    if isinstance(raw, str):
        try:   return json.loads(raw)
        except Exception: return []
    return []

def parse_prices(m: dict) -> List[float]:
    for field in ['outcomePrices', 'prices']:
        raw = parse_list(m.get(field, []))
        if raw:
            try:
                vals = [max(0.001, min(0.999, float(str(x)))) for x in raw]
                if len(vals) >= 2: return vals
            except Exception: pass
    return []

def parse_outcomes(m: dict) -> List[str]:
    return [str(x) for x in parse_list(m.get('outcomes', '[]'))]

def parse_token_ids(m: dict) -> List[str]:
    return [str(x) for x in parse_list(m.get('clobTokenIds', '[]'))]

def parse_days(m: dict) -> Optional[float]:
    for f in ['endDateIso', 'endDate']:
        val = m.get(f)
        if val:
            dt = parse_iso_date(val)
            if dt:
                return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
    return None

# ══════════════════════════════════════════════════════════════════
# ANALISIS SINYAL
# ══════════════════════════════════════════════════════════════════
def analyze(names, gamma_px, clob, liq, vol, days, prev_px) -> Optional[dict]:
    N = len(gamma_px)
    if N < 2:
        return None

    ask_prices = [
        clob[i]['ask'] if i < len(clob) and clob[i] else gamma_px[i]
        for i in range(N)
    ]
    ask_sum    = sum(ask_prices)
    is_arb     = ask_sum < 0.995
    arb_profit = max(0.0, (1.0 - ask_sum) * 100)

    spreads = []
    for i in range(N):
        if i < len(clob) and clob[i] and clob[i]['mid'] > 0:
            spreads.append(clob[i]['spread'] / clob[i]['mid'] * 100)
        else:
            spreads.append(0.0)
    max_spread = max(spreads) if spreads else 0.0

    mom_pct, mom_dir = 0.0, ''
    if prev_px and len(prev_px) == N and prev_px[0] > 0:
        chg     = (gamma_px[0] - prev_px[0]) / prev_px[0] * 100
        mom_pct = chg
        if   chg >= 5:  mom_dir = f'UP {chg:.1f}%'
        elif chg <= -5: mom_dir = f'DN {abs(chg):.1f}%'

    near_res, near_note, near_bonus = False, '', 0.0
    if days is not None and 0 <= days <= 1:
        for p in gamma_px:
            if 0.05 < p < 0.95:
                near_res = True; near_bonus = 25.0
                if   days < 0.007: near_note = '<10m!'
                elif days < 0.031: near_note = '<45m'
                elif days < 0.25:  near_note = '<6h'
                else:              near_note = '<24h'
                break

    vol_spike, vol_note, vol_bonus = False, '', 0.0
    if liq > 0 and vol > liq * CFG['VOL_SPIKE_RATIO']:
        vol_spike = True
        ratio     = vol / liq
        vol_note  = f'VOL {ratio:.0f}x'
        vol_bonus = min(25.0, ratio * 3)

    entry_name = names[0]
    entry_idx  = 0

    score = (
        (100 if is_arb else 0) +
        min(60, abs(mom_pct) * 4) +
        min(25, max_spread * 2.5) +
        min(15, math.log10(max(liq, 1)) * 4) +
        min(15, math.log10(max(vol, 1)) * 4) +
        near_bonus + vol_bonus
    )

    is_strong = False
    is_auto   = False

    if is_arb and arb_profit > 0.2:
        signal = 'ARBITRAGE'; action = 'BELI ALL'; color = GG
        is_strong = True; is_auto = True
        
    elif abs(mom_pct) >= 15 and near_res and vol_spike:
        d = names[0] if mom_pct > 0 else (names[1] if N > 1 else names[0])
        signal = 'STRONG BUY'; action = f'BUY {d[:12].upper()}'; color = GG
        entry_name = d; is_strong = True; is_auto = True

    elif abs(mom_pct) >= 10 and near_res:
        d = names[0] if mom_pct > 0 else (names[1] if N > 1 else names[0])
        signal = 'STRONG BUY'; action = f'BUY {d[:12].upper()}'; color = GG
        entry_name = d; is_strong = True; is_auto = True

    elif abs(mom_pct) >= 15:
        d = names[0] if mom_pct > 0 else (names[1] if N > 1 else names[0])
        signal = 'BUY'; action = f'BUY {d[:12].upper()}'; color = G
        entry_name = d; is_strong = True

    elif abs(mom_pct) >= 10 and vol_spike:
        d = names[0] if mom_pct > 0 else (names[1] if N > 1 else names[0])
        signal = 'BUY'; action = f'BUY {d[:12].upper()}'; color = G
        entry_name = d; is_strong = True

    elif vol_spike and near_res:
        signal = 'EDGE'; action = 'WATCH'; color = YY

    elif abs(mom_pct) >= 5 and near_res:
        d = names[0] if mom_pct > 0 else (names[1] if N > 1 else names[0])
        signal = 'EDGE'; action = f'BUY {d[:10].upper()}'; color = YY
        entry_name = d

    elif abs(mom_pct) >= 5:
        d = names[0] if mom_pct > 0 else (names[1] if N > 1 else names[0])
        signal = 'MOMENTUM'; action = f'WATCH {d[:10].upper()}'; color = C
        entry_name = d

    elif near_res and days is not None and days < 0.25:
        signal = 'NEAR-RES'; action = 'WATCH'; color = Y

    else:
        signal = 'MONITOR'; action = 'MONITOR'; color = W

    entry_idx = next((i for i, n in enumerate(names) if n == entry_name), 0)
    entry_px  = ask_prices[entry_idx] if entry_idx < len(ask_prices) else ask_prices[0]

    kelly = 0.0
    if 0.001 < entry_px < 0.999:
        fair_p = min(0.999, max(0.001, entry_px * 1.05))
        b = (1 / entry_px) - 1
        if b > 0:
            k = (b * fair_p - (1 - fair_p)) / b
            kelly = max(0.0, k * CFG['KELLY_FRACTION'])

    return {
        'signal': signal, 'action': action, 'color': color,
        'names': names, 'gamma_px': gamma_px, 'ask_prices': ask_prices,
        'is_arb': is_arb, 'arb_profit': arb_profit,
        'entry_outcome': entry_name, 'entry_price': entry_px,
        'entry_token_idx': entry_idx,
        'ev_pct': arb_profit if is_arb else 0.0,
        'kelly': kelly, 'kelly_usd': kelly * CFG['BANKROLL'],
        'spread_pct': max_spread,
        'momentum_pct': mom_pct, 'momentum_dir': mom_dir,
        'near_res': near_res, 'near_note': near_note,
        'vol_spike': vol_spike, 'vol_note': vol_note,
        'score': score, 'is_strong': is_strong, 'is_auto': is_auto,
        'clob': clob,
    }

def process(m: dict, history: dict, clob_map: dict) -> Optional[dict]:
    try:
        q = (m.get('question') or '').strip()
        if not q: return None
        liq    = float(m.get('liquidity') or 0)
        vol    = float(m.get('volume24hr') or m.get('volume') or 0)
        names  = parse_outcomes(m)
        prices = parse_prices(m)
        tids   = parse_token_ids(m)
        days   = parse_days(m)
        if len(prices) < 2 or len(names) < 2: return None
        if days is not None and days < -1: return None
        n = min(len(names), len(prices))
        names, prices = names[:n], prices[:n]
        clob = [clob_map.get(tids[i]) if i < len(tids) else None for i in range(n)]
        mid  = str(m.get('id', q[:40]))
        res  = analyze(names, prices, clob, liq, vol, days, history.get(mid, []))
        if not res: return None
        entry_idx = res.get('entry_token_idx', 0)
        entry_tid = tids[entry_idx] if entry_idx < len(tids) else ''
        end_date  = m.get('endDateIso') or m.get('endDate') or ''
        return {
            'id': mid, 'question': q, 'category': str(m.get('category') or 'General')[:20],
            'liquidity': liq, 'volume_24h': vol, 'days': days,
            'token_ids': tids, 'entry_token_id': entry_tid, 'end_date': end_date,
            **res,
        }
    except Exception as e:
        log.debug(f'process error: {e}')
        return None

# ══════════════════════════════════════════════════════════════════
# POSITION MANAGER
# ══════════════════════════════════════════════════════════════════
class PositionManager:
    def __init__(self):
        self.open_positions: List[dict] = []

    async def refresh(self):
        self.open_positions = db_get_open_positions()

    @property
    def count(self) -> int:
        return len(self.open_positions)

    @property
    def total_exposure(self) -> float:
        return sum(p['amount_usd'] for p in self.open_positions)

    def can_open(self) -> Tuple[bool, str]:
        if self.count >= CFG['MAX_POSITIONS']:
            return False, f"Max {CFG['MAX_POSITIONS']} posisi"
        if self.total_exposure + CFG['TRADE_PER_SIGNAL'] > CFG['MAX_EXPOSURE']:
            return False, f"Exposure penuh ${self.total_exposure:.2f}/${CFG['MAX_EXPOSURE']}"
        return True, 'OK'

    async def check_and_close(self, session, active_market_ids: Optional[set] = None) -> List[dict]:
        closed_list = []
        now = datetime.now(timezone.utc)

        for pos in self.open_positions:
            entry_price = pos['entry_price']
            token_id    = pos.get('token_id', '')
            market_id   = pos.get('market_id', '')
            end_date    = pos.get('end_date', '')

            dt = parse_iso_date(end_date)
            days_left = (dt - now).total_seconds() / 86400 if dt else None

            # hold_hours: use UTC for both
            hold_hours = 0
            try:
                open_str   = pos['open_ts']
                open_dt    = datetime.strptime(open_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                hold_hours = (now - open_dt).total_seconds() / 3600
            except Exception: pass

            current_price = await fetch_price(session, token_id)
            if current_price is None:
                current_price = entry_price

            price_change_pct = 0.0
            if entry_price > 0:
                price_change_pct = (current_price - entry_price) / entry_price * 100

            should_close = False
            close_reason = ''
            exit_price   = current_price

            # Check if market is still returned as active by Gamma API
            market_ghost = active_market_ids is not None and market_id not in active_market_ids

            if days_left is not None and 0 <= days_left < CFG['FORCE_EXIT_MINUTES'] / 1440:
                should_close = True; close_reason = f'FORCE_EXIT (<{CFG["FORCE_EXIT_MINUTES"]}m)'
            elif days_left is not None and days_left < 0:
                should_close = True; close_reason = 'EXPIRED'; exit_price = 0.0
            elif days_left is not None and 0 <= days_left < CFG['TIME_EXIT_MINUTES'] / 1440:
                should_close = True; close_reason = f'TIME_EXIT (<{CFG["TIME_EXIT_MINUTES"]}m)'
            elif price_change_pct >= CFG['TAKE_PROFIT_PCT']:
                should_close = True; close_reason = f'TAKE_PROFIT (+{price_change_pct:.1f}%)'
            elif price_change_pct <= -CFG['STOP_LOSS_PCT']:
                should_close = True; close_reason = f'STOP_LOSS ({price_change_pct:.1f}%)'
            elif hold_hours >= CFG['MAX_HOLD_HOURS']:
                should_close = True; close_reason = f'MAX_HOLD ({hold_hours:.0f}h)'
            elif market_ghost:
                # If market not in active scan AND it's been open more than 1 hour
                if hold_hours > 1:
                    should_close = True; close_reason = 'MARKET_RESOLVING (GHOST)'

            if should_close:
                pnl = db_close_position(pos['id'], exit_price, close_reason)
                await tg_close(session, pos, exit_price, pnl, close_reason)
                closed_list.append({
                    'pos': pos, 'exit_price': exit_price,
                    'pnl': pnl, 'reason': close_reason,
                    'price_change': price_change_pct,
                })

        if closed_list:
            await self.refresh()

        return closed_list

    async def open_position(self, session, r: dict) -> Optional[int]:
        can, why = self.can_open()
        if not can:
            log.info(f'SKIP OPEN: {why}')
            return None
        amount = CFG['TRADE_PER_SIGNAL']
        entry  = r['entry_price']
        shares = amount / entry if entry > 0 else 0
        pos_id = db_open_position(r, amount, shares)
        await tg_open(session, r, pos_id, amount)
        await self.refresh()
        return pos_id

# ══════════════════════════════════════════════════════════════════
# DISPLAY
# ══════════════════════════════════════════════════════════════════
def banner():
    mode = 'REAL TRADE' if AUTO_TRADE and PRIVATE_KEY else 'PAPER TRADE'
    print('=' * 70)
    print('  POLYMARKET AUTO BOT v11.2 (FIXED)')
    print(f'  Mode: {mode} | TPM: {CFG["TIME_EXIT_MINUTES"]}m | FEM: {CFG["FORCE_EXIT_MINUTES"]}m')
    print('=' * 70)

def display_stats(st: dict, pm: 'PositionManager'):
    print(f'\n  JOURNAL: Total={st["total"]} | Open={pm.count}/{CFG["MAX_POSITIONS"]} | '
          f'Closed={st["closed"]} | Win={st["wins"]} | Loss={st["losses"]} | '
          f'WR={st["win_rate"]:.1f}% | P&L=${st["pnl"]:+.2f}')
    print(f'  Exposure: ${pm.total_exposure:.2f}/${CFG["MAX_EXPOSURE"]:.0f} | '
          f'Slot: {CFG["MAX_POSITIONS"] - pm.count} tersisa')

    if pm.open_positions:
        print(f'\n  POSISI TERBUKA ({pm.count}):')
        now = datetime.now(timezone.utc)
        for p in pm.open_positions:
            dt = parse_iso_date(p.get('end_date'))
            days_left = (dt - now).total_seconds() / 86400 if dt else None
            sisa_str = format_sisa(days_left)
            print(f'    #{p["id"]} | {p["signal"][:10]} | {p["question"][:35]} | '
                  f'Entry:{p["entry_price"]:.3f} | Sisa:{sisa_str} | {p["open_ts"][11:16]}')

    if st['recent']:
        print('\n  RIWAYAT TERBARU:')
        rows = []
        for t in st['recent']:
            ts, sig, q, ep, amt, status, pnl_v, reason = t
            pnl_str = f'+${pnl_v:.3f}' if (pnl_v or 0) > 0 else f'${(pnl_v or 0):.3f}'
            rows.append([ts[11:16], sig[:12], q, f'{ep:.3f}', f'${amt:.2f}', status, pnl_str, (reason or '')[:15]])
        print(tabulate(rows, headers=['Jam', 'Signal', 'Pasar', 'Entry', 'Bet', 'Status', 'P&L', 'Alasan'], tablefmt='simple'))

def display(results, stats_scan, stats_j, pm, closed_this_scan):
    sp = SPIN[stats_scan['scans'] % 4]
    ts = datetime.now().strftime('%H:%M:%S')
    print('\n' + '=' * 70)
    print(f'  [{sp}] {ts} | Scan #{stats_scan["scans"]} | '
          f'Pasar: {stats_scan["fetched"]} | Valid: {stats_scan["valid"]} | '
          f'{stats_scan["ms"]}ms | Closed: {len(closed_this_scan)}')

    banner()
    display_stats(stats_j, pm)

    if closed_this_scan:
        print(f'\n  BARU DITUTUP ({len(closed_this_scan)}):')
        for c in closed_this_scan:
            pnl    = c['pnl']
            status = 'PROFIT' if pnl > 0 else 'LOSS'
            print(f'    [{status}] #{c["pos"]["id"]} | {c["reason"]} | P&L: ${pnl:+.3f} | {c["pos"]["question"][:40]}')

    if not results:
        print('\n  Menunggu data...')
        return

    can_open, reason = pm.can_open()
    slot_info = f'BISA OPEN ({CFG["MAX_POSITIONS"]-pm.count} slot)' if can_open else f'PENUH ({reason})'
    print(f'\n  TOP {len(results)} SINYAL | {slot_info}')

    rows = []
    for rank, r in enumerate(results, 1):
        q   = (r['question'][:28] + '..') if len(r['question']) > 28 else r['question']
        can = 'AUTO' if (r.get('is_auto') and can_open) else ('-' if not r.get('is_strong') else 'WATCH')
        bs  = f"{r.get('brain_score', 100):.0f}%"
        rows.append([rank, r['signal'], q, r['action'][:14], f'{r["entry_price"]:.3f}', bs, fd(r['days']), f'{r["score"]:.0f}', fu(r['liquidity']), can])
    print(tabulate(rows, headers=['#', 'Signal', 'Pasar', 'Action', 'Entry', 'Brain', 'Sisa', 'Skor', 'Liq', 'Auto?'], tablefmt='simple'))
    print()

# ══════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════
async def main():
    init_db()
    
    # Initialize Brain
    brain = None
    if TradingBrain:
        brain = TradingBrain(DB_PATH, MODEL_PATH)

    banner()
    log.info('=== POLYMARKET AUTO BOT v11.2 START ===')
    log.info(f'TIME_EXIT={CFG["TIME_EXIT_MINUTES"]}m | FORCE_EXIT={CFG["FORCE_EXIT_MINUTES"]}m | MAX_EXP=${CFG["MAX_EXPOSURE"]} | MAX_POS={CFG["MAX_POSITIONS"]}')

    history       : Dict[str, list] = {}
    scans         = 0
    pm            = PositionManager()
    await pm.refresh()
    already_opened = db_get_open_market_ids()

    connector = aiohttp.TCPConnector(limit=50, limit_per_host=15, ttl_dns_cache=300, ssl=False)
    hdrs = {'User-Agent': 'Mozilla/5.0 PolyBot/11.2', 'Accept': 'application/json'}

    async with aiohttp.ClientSession(connector=connector, headers=hdrs) as session:
        if TELEGRAM_TOKEN:
            mode = 'REAL TRADE' if AUTO_TRADE and PRIVATE_KEY else 'PAPER TRADE'
            await tg(session, f'<b>Polymarket Auto Bot v11.2 Online (FIXED)!</b>\nMode: <b>{mode}</b>')

        while True:
            t0               = time.time()
            closed_this_scan = []
            try:
                await pm.refresh()
                
                # Fetch markets FIRST so we know which are active
                raw = await fetch_markets(session)
                active_ids = {str(m.get('id', '')) for m in raw}
                
                if pm.open_positions:
                    closed_this_scan = await pm.check_and_close(session, active_market_ids=active_ids)
                    if closed_this_scan:
                        await pm.refresh()
                        for c in closed_this_scan:
                            already_opened.discard(c['pos']['market_id'])
                all_tids = []
                for m in raw:
                    tids = parse_token_ids(m)
                    all_tids.extend(tids[:2])
                all_tids = list(dict.fromkeys(all_tids))
                
                clob_map = await fetch_clob_batch(session, all_tids)

                results   = []
                new_hist  : Dict[str, list] = {}
                for m in raw:
                    try:
                        r = process(m, history, clob_map)
                        if r:
                            # ML Prediction
                            if brain:
                                r['brain_score'] = brain.predict_confidence(r)
                            else:
                                r['brain_score'] = 100.0

                            results.append(r)
                            new_hist[r['id']] = r['gamma_px']
                        else:
                            mid = str(m.get('id', ''))
                            pp  = parse_prices(m)
                            if mid and pp:
                                new_hist[mid] = pp
                    except Exception:
                        continue

                history = new_hist
                results.sort(key=lambda x: x['score'], reverse=True)
                top = results[:CFG['DISPLAY_TOP']]

                scans += 1
                ms     = int((time.time() - t0) * 1000)

                auto_candidates = [
                    r for r in results
                    if r.get('is_auto')
                    and r['id'] not in already_opened
                    and r['liquidity'] >= CFG['MIN_LIQUIDITY']
                    and r.get('brain_score', 100) >= CFG['MIN_ML_CONFIDENCE']
                    and (r['days'] is None or r['days'] >= (CFG['TIME_EXIT_MINUTES'] * 2) / 1440)
                ]

                # Run Brain Training every 10 scans
                if brain and scans % 10 == 0:
                    await asyncio.to_thread(brain.train)

                if auto_candidates:
                    can, _ = pm.can_open()
                    if can:
                        best = auto_candidates[0]
                        pos_id = await pm.open_position(session, best)
                        if pos_id:
                            already_opened.add(best['id'])

                try:
                    c2 = sqlite3.connect(DB_PATH)
                    c2.execute('INSERT INTO scan_log (ts,fetched,valid,open_pos,ms) VALUES (?,?,?,?,?)',
                               (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), len(raw), len(results), pm.count, ms))
                    c2.commit(); c2.close()
                except Exception: pass

                stats_j = db_get_stats()
                display(top, {'scans': scans, 'fetched': len(raw), 'valid': len(results), 'ms': ms}, stats_j, pm, closed_this_scan)

            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f'Main loop error: {e}')

            try:
                await asyncio.sleep(CFG['SCAN_INTERVAL'])
            except KeyboardInterrupt:
                break

    final = db_get_stats()
    log.info(f'Bot berhenti. Total={final["total"]} | P&L=${final["pnl"]:+.2f}')

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: print('\nBot berhenti.\n')

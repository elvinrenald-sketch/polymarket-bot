#!/usr/bin/env python3
"""
POLYMARKET FULL AUTO BOT v10.0
================================
FITUR UTAMA:
  ✅ Auto OPEN posisi saat sinyal kuat
  ✅ Auto CLOSE posisi (Take Profit / Stop Loss / Time Exit)
  ✅ Max 10 posisi terbuka, $1 per entry
  ✅ Tidak open baru jika sudah 10 posisi
  ✅ Journal lengkap dengan P&L
  ✅ Notifikasi Telegram setiap open dan close

CARA CLOSE OTOMATIS:
  - Take Profit: harga naik 50% dari entry → jual, ambil profit
  - Stop Loss: harga turun 40% dari entry → jual, potong rugi
  - Time Exit: sisa waktu < 30 menit → jual berapapun harganya
  - Resolved: market selesai → catat hasil (win/loss)

CATATAN PENTING:
  Bot ini adalah PAPER TRADING dulu.
  Untuk real trading perlu install: pip install py-clob-client eth-account
  Dan isi PRIVATE_KEY di Railway Variables.
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
from typing import Optional, List, Dict, Tuple
from colorama import Fore, Style, init
from tabulate import tabulate
from pathlib import Path

init(autoreset=True)

# ══════════════════════════════════════════════════════════════════
# ENVIRONMENT VARIABLES
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
    # API
    'GAMMA_API'         : 'https://gamma-api.polymarket.com',
    'CLOB_API'          : 'https://clob.polymarket.com',

    # Scanner
    'SCAN_INTERVAL'     : 15,
    'MARKETS_PER_PAGE'  : 100,
    'MAX_PAGES'         : 15,
    'DISPLAY_TOP'       : 10,
    'CLEAR_SCREEN'      : True,

    # ── RISK MANAGEMENT ──────────────────────────────────────────
    'BANKROLL'          : 10.00,    # Total modal $10
    'TRADE_PER_SIGNAL'  : 1.00,     # $1 per entry
    'MAX_POSITIONS'     : 10,       # Maksimal 10 posisi terbuka
    'MAX_EXPOSURE'      : 10.00,    # Max $10 total exposure

    # ── AUTO-CLOSE CONDITIONS ────────────────────────────────────
    'TAKE_PROFIT_PCT'   : 50.0,     # Jual jika harga naik 50% dari entry
    'STOP_LOSS_PCT'     : 40.0,     # Jual jika harga turun 40% dari entry
    'TIME_EXIT_MINUTES' : 30,       # Jual jika sisa waktu < 30 menit
    'FORCE_EXIT_MINUTES': 5,        # Force jual jika sisa < 5 menit

    # ── SIGNAL THRESHOLDS ────────────────────────────────────────
    'MIN_MOMENTUM'      : 10.0,     # Min momentum % untuk entry
    'MIN_LIQUIDITY'     : 500,      # Min likuiditas untuk entry
    'VOL_SPIKE_RATIO'   : 3.0,      # Volume > 3x liq = spike
    'NEAR_RES_HOURS'    : 6,        # Pasar tutup < 6 jam

    'KELLY_FRACTION'    : 0.25,
    'SOUND_ALERT'       : False,
}

# Journal
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
Y=Fore.YELLOW; YY=Fore.YELLOW+Style.BRIGHT; C=Fore.CYAN; W=Fore.WHITE
WW=Fore.WHITE+Style.BRIGHT; M=Fore.MAGENTA; Z=Style.RESET_ALL
SPIN=['⣾','⣽','⣻','⢿','⡿','⣟','⣯','⣷']

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
    if d is None: return '—'
    if d<0: return f'{R}EXP{Z}'
    if d<0.021: return f'{RR}<30m{Z}'
    if d<0.25: return f'{RR}<6h{Z}'
    if d<1: return f'{Y}{d*24:.0f}h{Z}'
    return f'{W}{d:.1f}d{Z}'

# ══════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS positions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        open_ts      TEXT NOT NULL,
        close_ts     TEXT,
        market_id    TEXT,
        condition_id TEXT,
        token_id     TEXT,
        question     TEXT,
        signal       TEXT,
        outcome      TEXT,
        entry_price  REAL,
        exit_price   REAL,
        amount_usd   REAL,
        shares       REAL,
        status       TEXT DEFAULT "OPEN",
        close_reason TEXT,
        pnl_usd      REAL,
        pnl_pct      REAL,
        result       TEXT,
        order_id_open  TEXT,
        order_id_close TEXT,
        end_date     TEXT,
        notes        TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS scan_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, fetched INTEGER, valid INTEGER,
        open_pos INTEGER, ms INTEGER
    )''')
    conn.commit()
    conn.close()

def db_open_position(r: dict, amount: float, shares: float,
                     order_id: str = '') -> int:
    ts   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute('''INSERT INTO positions
        (open_ts,market_id,condition_id,token_id,question,signal,outcome,
         entry_price,amount_usd,shares,status,order_id_open,end_date)
        VALUES (?,?,?,?,?,?,?,?,?,?,"OPEN",?,?)''', (
        ts,
        r.get('id',''),
        r.get('condition_id',''),
        r.get('entry_token_id',''),
        r.get('question','')[:200],
        r.get('signal',''),
        r.get('entry_outcome',''),
        r.get('entry_price',0),
        amount, shares, order_id,
        r.get('end_date',''),
    ))
    pos_id = cur.lastrowid
    conn.commit()
    conn.close()

    # CSV log
    _csv_append({
        'id': pos_id, 'ts': ts, 'type': 'OPEN',
        'signal': r.get('signal',''),
        'question': r.get('question','')[:80],
        'outcome': r.get('entry_outcome',''),
        'price': r.get('entry_price',0),
        'amount': amount, 'shares': shares,
        'status': 'OPEN', 'pnl': '',
    })
    log.info(f"OPEN #{pos_id}: {r.get('signal')} | {r.get('entry_outcome')} "
             f"@ {r.get('entry_price',0):.3f} | ${amount:.2f}")
    return pos_id

def db_close_position(pos_id: int, exit_price: float,
                      reason: str, order_id: str = ''):
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute('SELECT entry_price, amount_usd, shares, outcome FROM positions WHERE id=?',
                (pos_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return 0

    entry_price, amount_usd, shares, outcome = row

    # Hitung P&L
    # Di Polymarket: jual shares di exit_price
    proceeds = shares * exit_price
    pnl_usd  = proceeds - amount_usd
    pnl_pct  = (pnl_usd / amount_usd * 100) if amount_usd > 0 else 0
    result   = 'WIN' if pnl_usd > 0 else 'LOSS'

    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cur.execute('''UPDATE positions SET
        close_ts=?, exit_price=?, status="CLOSED",
        close_reason=?, pnl_usd=?, pnl_pct=?,
        result=?, order_id_close=?
        WHERE id=?''', (
        ts, exit_price, reason, round(pnl_usd,4),
        round(pnl_pct,2), result, order_id, pos_id
    ))
    conn.commit()
    conn.close()

    _csv_append({
        'id': pos_id, 'ts': ts, 'type': 'CLOSE',
        'signal': reason, 'question': '', 'outcome': outcome,
        'price': exit_price, 'amount': amount_usd,
        'shares': shares, 'status': 'CLOSED',
        'pnl': round(pnl_usd, 4),
    })
    log.info(f"CLOSE #{pos_id}: {reason} @ {exit_price:.3f} | "
             f"P&L: ${pnl_usd:+.3f} ({pnl_pct:+.1f}%) [{result}]")
    return pnl_usd

def db_get_open_positions() -> List[dict]:
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute('''SELECT id,open_ts,market_id,token_id,question,signal,
                          outcome,entry_price,amount_usd,shares,end_date
                   FROM positions WHERE status="OPEN" ORDER BY id''')
    rows = cur.fetchall()
    conn.close()
    positions = []
    for r in rows:
        positions.append({
            'id': r[0], 'open_ts': r[1], 'market_id': r[2],
            'token_id': r[3], 'question': r[4], 'signal': r[5],
            'outcome': r[6], 'entry_price': r[7], 'amount_usd': r[8],
            'shares': r[9], 'end_date': r[10],
        })
    return positions

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
        cur.execute('''SELECT open_ts,signal,substr(question,1,25),
                              entry_price,amount_usd,status,pnl_usd,close_reason
                       FROM positions ORDER BY id DESC LIMIT 8''')
        recent = cur.fetchall()
        conn.close()
        wr = (wins/closed*100) if closed>0 else 0
        return {
            'total':total,'closed':closed,'open':open_c,
            'wins':wins,'losses':closed-wins,'win_rate':wr,
            'pnl':pnl,'exposure':exposure,'recent':recent
        }
    except:
        return {'total':0,'closed':0,'open':0,'wins':0,'losses':0,
                'win_rate':0,'pnl':0,'exposure':0,'recent':[]}

def _csv_append(row: dict):
    exists = os.path.exists(CSV_PATH)
    with open(CSV_PATH, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if not exists: w.writeheader()
        w.writerow(row)

# ══════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════
async def tg(session, text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        await session.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
            timeout=aiohttp.ClientTimeout(total=5)
        )
    except: pass

async def tg_open(session, r: dict, pos_id: int, amount: float):
    text = (
        f"🟢 <b>OPEN POSISI #{pos_id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 {r['question'][:70]}\n\n"
        f"🎯 {r['signal']} → <b>{r['action']}</b>\n"
        f"💰 Entry: <b>{r['entry_price']:.4f}</b>\n"
        f"💵 Bet: <b>${amount:.2f}</b>\n"
        f"⏰ Sisa: {fd(r.get('days')).replace(chr(27)+'[0m','').replace(chr(27),'')}\n"
        f"📋 Mode: {'REAL TRADE' if AUTO_TRADE and PRIVATE_KEY else 'PAPER TRADE'}"
    )
    await tg(session, text)

async def tg_close(session, pos: dict, exit_price: float,
                   pnl: float, reason: str):
    emoji = '✅' if pnl > 0 else '❌'
    text  = (
        f"{emoji} <b>CLOSE POSISI #{pos['id']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 {pos['question'][:50]}\n\n"
        f"🎯 Outcome: <b>{pos['outcome']}</b>\n"
        f"📥 Entry: {pos['entry_price']:.4f}\n"
        f"📤 Exit: <b>{exit_price:.4f}</b>\n"
        f"💰 P&L: <b>${pnl:+.3f}</b>\n"
        f"📝 Alasan: {reason}\n"
        f"{'🎉 PROFIT!' if pnl > 0 else '⚠️ Loss - cut rugi tepat waktu'}"
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
        if isinstance(p, list) and p: out.extend(p)
    return out

async def fetch_price(session, token_id: str) -> Optional[float]:
    """Fetch harga terbaru untuk token yang kita pegang"""
    if not token_id: return None
    try:
        data = await api_get(session, f"{CFG['CLOB_API']}/midpoints",
                             [('token_id', token_id)])
        if isinstance(data, list) and data:
            return float(data[0].get('mid', 0)) or None
    except: pass
    # Fallback: coba dari Gamma
    return None

async def fetch_clob_batch(session, token_ids: List[str]) -> Dict[str, dict]:
    if not token_ids: return {}
    result = {}
    batches = [token_ids[i:i+40] for i in range(0, min(len(token_ids),200), 40)]
    mid_tasks = [api_get(session, f"{CFG['CLOB_API']}/midpoints",
                         [('token_id',t) for t in b]) for b in batches]
    spd_tasks = [api_get(session, f"{CFG['CLOB_API']}/spreads",
                         [('token_id',t) for t in b]) for b in batches]
    mids_all, spds_all = await asyncio.gather(
        asyncio.gather(*mid_tasks), asyncio.gather(*spd_tasks))
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
                'mid': mid, 'bid': max(0.001, mid-spd/2),
                'ask': min(0.999, mid+spd/2), 'spread': spd,
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
                if len(vals)>=2: return vals
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
# ANALISIS SINYAL
# ══════════════════════════════════════════════════════════════════
def analyze(names, gamma_px, clob, liq, vol, days, prev_px) -> Optional[dict]:
    N = len(gamma_px)
    if N < 2: return None

    # Ask prices dari CLOB (lebih akurat)
    ask_prices = [
        clob[i]['ask'] if i < len(clob) and clob[i] else gamma_px[i]
        for i in range(N)
    ]
    ask_sum = sum(ask_prices)

    # Arbitrage check
    is_arb     = ask_sum < 0.995
    arb_profit = max(0.0, (1.0 - ask_sum) * 100)

    # Spread
    spreads = []
    for i in range(N):
        if i < len(clob) and clob[i] and clob[i]['mid'] > 0:
            spreads.append(clob[i]['spread'] / clob[i]['mid'] * 100)
        else:
            spreads.append(0.0)
    max_spread = max(spreads) if spreads else 0.0

    # Momentum
    mom_pct, mom_dir = 0.0, ''
    if prev_px and len(prev_px)==N and prev_px[0]>0:
        chg = (gamma_px[0]-prev_px[0])/prev_px[0]*100
        mom_pct = chg
        if chg>=5: mom_dir = f'↑{chg:.1f}%'
        elif chg<=-5: mom_dir = f'↓{abs(chg):.1f}%'

    # Near-resolution
    near_res, near_note, near_bonus = False, '', 0.0
    if days is not None and 0<=days<=1:
        for p in gamma_px:
            if 0.05<p<0.95:
                near_res=True; near_bonus=25.0
                if days<0.021: near_note='⏰ <30 MENIT!'
                elif days<0.25: near_note='⏰ <6 JAM'
                elif days<0.5: near_note='⏰ <12 JAM'
                else: near_note='⏰ <24 JAM'
                break

    # Volume spike
    vol_spike, vol_note, vol_bonus = False, '', 0.0
    if liq>0 and vol>liq*CFG['VOL_SPIKE_RATIO']:
        vol_spike=True
        ratio=vol/liq
        vol_note=f'🔊 VOL {ratio:.0f}x'
        vol_bonus=min(25.0, ratio*3)

    # Entry selection
    if is_arb:
        entry_i=0; entry_px=ask_prices[0]; ev_pct=arb_profit; method='ARB'
    else:
        entry_i=0; entry_px=ask_prices[0]; ev_pct=0.0; method='SIGNAL'

    entry_name = names[entry_i] if entry_i<len(names) else names[0]

    # Kelly
    kelly = 0.0
    if ev_pct>0 and 0.001<entry_px<0.999:
        fair_p = min(0.999, entry_px*(1+ev_pct/100))
        b = (1/entry_px)-1
        if b>0:
            k = (b*fair_p-(1-fair_p))/b
            kelly = max(0.0, k*CFG['KELLY_FRACTION'])

    # Score
    score = (
        (100 if is_arb else 0) +
        min(60, abs(mom_pct)*4) +
        min(25, max_spread*2.5) +
        min(15, math.log10(max(liq,1))*4) +
        min(15, math.log10(max(vol,1))*4) +
        near_bonus + vol_bonus
    )

    # Signal logic
    is_strong_signal = False
    if is_arb and arb_profit > 0.2:
        signal='💰 ARBITRAGE'; action=f'BELI {"+".join(names[:2])}'; color=GG
        is_strong_signal=True
    elif abs(mom_pct)>=15 and near_res and vol_spike:
        d = names[0] if mom_pct>0 else (names[1] if N>1 else names[0])
        signal='🔥 STRONG BUY'; action=f'BUY {d[:12].upper()}'; color=GG
        entry_name=d; is_strong_signal=True
    elif abs(mom_pct)>=10 and near_res:
        d = names[0] if mom_pct>0 else (names[1] if N>1 else names[0])
        signal='🔥 STRONG BUY'; action=f'BUY {d[:12].upper()}'; color=GG
        entry_name=d; is_strong_signal=True
    elif abs(mom_pct)>=15:
        d = names[0] if mom_pct>0 else (names[1] if N>1 else names[0])
        signal='✅ BUY'; action=f'BUY {d[:12].upper()}'; color=G
        entry_name=d; is_strong_signal=True
    elif abs(mom_pct)>=10 and vol_spike:
        d = names[0] if mom_pct>0 else (names[1] if N>1 else names[0])
        signal='✅ BUY'; action=f'BUY {d[:12].upper()}'; color=G
        entry_name=d; is_strong_signal=True
    elif vol_spike and near_res:
        signal='⚡ EDGE'; action=f'WATCH {entry_name[:10].upper()}'; color=YY
        is_strong_signal=True
    elif abs(mom_pct)>=5 and near_res:
        d = names[0] if mom_pct>0 else (names[1] if N>1 else names[0])
        signal='⚡ EDGE'; action=f'BUY {d[:10].upper()}'; color=YY
        entry_name=d
    elif abs(mom_pct)>=5:
        d = names[0] if mom_pct>0 else (names[1] if N>1 else names[0])
        signal='📈 MOMENTUM'; action=f'WATCH {d[:10].upper()}'; color=C
        entry_name=d
    elif near_res and days is not None and days<0.25:
        signal='⏰ NEAR-RES'; action='WATCH'; color=Y
    else:
        signal='➖ MONITOR'; action='MONITOR'; color=W

    # Update entry price based on selected outcome
    entry_idx = next((i for i,n in enumerate(names) if n==entry_name), 0)
    entry_px  = ask_prices[entry_idx] if entry_idx<len(ask_prices) else ask_prices[0]

    return {
        'signal':signal,'action':action,'color':color,'method':method,
        'names':names,'gamma_px':gamma_px,'ask_prices':ask_prices,
        'is_arb':is_arb,'arb_profit':arb_profit,
        'entry_outcome':entry_name,'entry_price':entry_px,
        'entry_token_idx':entry_idx,
        'ev_pct':ev_pct,'kelly':kelly,'kelly_usd':kelly*CFG['BANKROLL'],
        'spread_pct':max_spread,'spreads':spreads,
        'momentum_pct':mom_pct,'momentum_dir':mom_dir,
        'near_res':near_res,'near_note':near_note,
        'vol_spike':vol_spike,'vol_note':vol_note,
        'score':score,'is_strong_signal':is_strong_signal,
        'clob':clob,
    }

def process(m: dict, history: dict, clob_map: dict) -> Optional[dict]:
    q = (m.get('question') or '').strip()
    if not q: return None
    liq    = float(m.get('liquidity') or 0)
    vol    = float(m.get('volume24hr') or m.get('volume') or 0)
    names  = parse_outcomes(m)
    prices = parse_prices(m)
    tids   = parse_token_ids(m)
    days   = parse_days(m)
    if len(prices)<2 or len(names)<2: return None
    if days is not None and days<-1: return None
    n = min(len(names),len(prices))
    names,prices = names[:n],prices[:n]
    clob = [clob_map.get(tids[i]) if i<len(tids) else None for i in range(n)]
    mid  = str(m.get('id',q[:40]))
    res  = analyze(names,prices,clob,liq,vol,days,history.get(mid,[]))
    if not res: return None
    entry_idx = res.get('entry_token_idx',0)
    entry_tid = tids[entry_idx] if entry_idx<len(tids) else ''
    return {
        'id':mid,'question':q,
        'condition_id':m.get('conditionId',''),
        'category':str(m.get('category') or m.get('groupItemTitle') or 'General')[:20],
        'liquidity':liq,'volume_24h':vol,'days':days,
        'token_ids':tids,'entry_token_id':entry_tid,
        'end_date':m.get('endDateIso',m.get('endDate','')),
        **res,
    }

# ══════════════════════════════════════════════════════════════════
# AUTO-CLOSE ENGINE — Ini yang paling penting!
# ══════════════════════════════════════════════════════════════════
class PositionManager:
    def __init__(self):
        self.open_positions: List[dict] = []

    async def refresh(self):
        """Load posisi open dari database"""
        self.open_positions = db_get_open_positions()

    @property
    def count(self) -> int:
        return len(self.open_positions)

    @property
    def total_exposure(self) -> float:
        return sum(p['amount_usd'] for p in self.open_positions)

    def can_open(self) -> Tuple[bool, str]:
        """Cek apakah boleh buka posisi baru"""
        if self.count >= CFG['MAX_POSITIONS']:
            return False, f"Max {CFG['MAX_POSITIONS']} posisi sudah tercapai"
        if self.total_exposure >= CFG['MAX_EXPOSURE']:
            return False, f"Max exposure ${CFG['MAX_EXPOSURE']} sudah tercapai"
        return True, "OK"

    async def check_and_close(self, session) -> List[dict]:
        """
        CEK SEMUA POSISI OPEN — Tutup yang memenuhi syarat:
        1. Take Profit: harga naik 50%
        2. Stop Loss: harga turun 40%
        3. Time Exit: sisa < 30 menit
        4. Force Exit: sisa < 5 menit
        """
        closed_list = []

        for pos in self.open_positions:
            entry_price = pos['entry_price']
            token_id    = pos.get('token_id', '')
            end_date    = pos.get('end_date', '')

            # Hitung sisa waktu
            days_left = None
            if end_date:
                try:
                    dt = datetime.fromisoformat(
                        str(end_date).replace('Z','+00:00'))
                    days_left = (dt - datetime.now(timezone.utc)
                                 ).total_seconds() / 86400
                except: pass

            # Fetch harga terbaru
            current_price = await fetch_price(session, token_id)
            if current_price is None:
                current_price = entry_price  # fallback

            # Hitung perubahan harga
            if entry_price > 0:
                price_change_pct = (current_price - entry_price) / entry_price * 100
            else:
                price_change_pct = 0

            # ── TENTUKAN APAKAH PERLU CLOSE ──────────────────────
            should_close = False
            close_reason = ''
            exit_price   = current_price

            # 1. FORCE EXIT — sisa < 5 menit (prioritas tertinggi)
            if (days_left is not None and
                    days_left < CFG['FORCE_EXIT_MINUTES'] / 1440):
                should_close = True
                close_reason = f'⏰ FORCE EXIT (<{CFG["FORCE_EXIT_MINUTES"]}m)'

            # 2. TIME EXIT — sisa < 30 menit
            elif (days_left is not None and
                  days_left < CFG['TIME_EXIT_MINUTES'] / 1440):
                should_close = True
                close_reason = f'⏰ TIME EXIT (<{CFG["TIME_EXIT_MINUTES"]}m)'

            # 3. TAKE PROFIT — harga naik 50%
            elif price_change_pct >= CFG['TAKE_PROFIT_PCT']:
                should_close = True
                close_reason = f'✅ TAKE PROFIT (+{price_change_pct:.1f}%)'

            # 4. STOP LOSS — harga turun 40%
            elif price_change_pct <= -CFG['STOP_LOSS_PCT']:
                should_close = True
                close_reason = f'❌ STOP LOSS ({price_change_pct:.1f}%)'

            if should_close:
                # Hitung P&L
                shares  = pos.get('shares', pos['amount_usd'] / entry_price)
                pnl_usd = db_close_position(
                    pos['id'], exit_price, close_reason)

                # Notif Telegram
                await tg_close(session, pos, exit_price, pnl_usd, close_reason)

                closed_list.append({
                    'pos': pos,
                    'exit_price': exit_price,
                    'pnl': pnl_usd,
                    'reason': close_reason,
                    'price_change': price_change_pct,
                })

                log.info(f"AUTO-CLOSE #{pos['id']}: {close_reason} | "
                         f"P&L ${pnl_usd:+.3f}")

        # Refresh list setelah close
        if closed_list:
            await self.refresh()

        return closed_list

    async def open_position(self, session, r: dict) -> Optional[int]:
        """Buka posisi baru"""
        can, reason = self.can_open()
        if not can:
            log.info(f"SKIP OPEN: {reason}")
            return None

        amount = CFG['TRADE_PER_SIGNAL']
        entry  = r['entry_price']
        shares = amount / entry if entry > 0 else 0

        pos_id = db_open_position(r, amount, shares, order_id='PAPER')
        await tg_open(session, r, pos_id, amount)
        await self.refresh()
        return pos_id

# ══════════════════════════════════════════════════════════════════
# DISPLAY
# ══════════════════════════════════════════════════════════════════
def banner():
    at = f'{GG}ON{Z}' if AUTO_TRADE and PRIVATE_KEY else f'{Y}PAPER{Z}'
    print(CC + WW + f'''
╔══════════════════════════════════════════════════════════════════════╗
║  POLYMARKET FULL AUTO BOT v10.0                                    ║
║  Auto Open · Auto Close · Max 10 Posisi · $1/entry · $10 Total    ║
╚══════════════════════════════════════════════════════════════════════╝
  Mode: {at}  TP:{G}+{CFG["TAKE_PROFIT_PCT"]:.0f}%{Z}  SL:{R}-{CFG["STOP_LOSS_PCT"]:.0f}%{Z}  TimeExit:{Y}<{CFG["TIME_EXIT_MINUTES"]}m{Z}''')

def display_positions(positions: List[dict]):
    """Tampilkan posisi yang sedang terbuka"""
    if not positions:
        print(f'\n  {W}Tidak ada posisi terbuka.{Z}')
        return

    print(f'\n{WW}  📂 POSISI TERBUKA ({len(positions)}/{CFG["MAX_POSITIONS"]})\n')
    rows = []
    for p in positions:
        entry = p['entry_price']
        rows.append([
            f'{C}#{p["id"]}{Z}',
            p['signal'][:14],
            p['question'][:25]+'...' if len(p['question'])>25 else p['question'],
            p['outcome'][:8],
            f'{entry:.3f}',
            p['open_ts'][11:16],
            fd(None),  # days not available here
        ])
    print(tabulate(rows,
        headers=['#','Signal','Pasar','Outcome','Entry','Jam','Sisa'],
        tablefmt='simple'))

def display_stats(st: dict, pm: 'PositionManager'):
    wr_c  = GG if st['win_rate']>=55 else (Y if st['win_rate']>=45 else R)
    pnl_c = G if st['pnl']>0 else (Y if st['pnl']==0 else R)
    exp_c = RR if pm.total_exposure>=CFG['MAX_EXPOSURE'] else (Y if pm.total_exposure>=5 else G)

    print(f'\n{WW}  ┌─ JOURNAL & P&L ──────────────────────────────────────────────────┐')
    print(
        f'  │  Total:{W}{st["total"]}{Z}  '
        f'Open:{C}{pm.count}{Z}/{CFG["MAX_POSITIONS"]}  '
        f'Closed:{W}{st["closed"]}{Z}  '
        f'Win:{G}{st["wins"]}{Z}  '
        f'Loss:{R}{st["losses"]}{Z}  '
        f'WR:{wr_c}{st["win_rate"]:.1f}%{Z}  '
        f'P&L:{pnl_c}${st["pnl"]:+.2f}{Z}'
    )
    print(
        f'  │  Exposure:{exp_c}${pm.total_exposure:.2f}{Z}/${CFG["MAX_EXPOSURE"]:.0f}  '
        f'Slot tersisa:{G}{CFG["MAX_POSITIONS"]-pm.count}{Z}  '
        f'TP:{G}+{CFG["TAKE_PROFIT_PCT"]:.0f}%{Z}  '
        f'SL:{R}-{CFG["STOP_LOSS_PCT"]:.0f}%{Z}  '
        f'TimeExit:{Y}<{CFG["TIME_EXIT_MINUTES"]}m{Z}'
    )
    print(f'{WW}  └──────────────────────────────────────────────────────────────────┘{Z}')

    if st['recent']:
        rows = []
        for t in st['recent']:
            ts,sig,q,ep,amt,status,pnl_v,reason = t
            if status=='OPEN':
                st_str = f'{C}OPEN{Z}'
                pnl_str = f'{Y}terbuka{Z}'
            else:
                st_str  = f'{G}WIN{Z}' if (pnl_v or 0)>0 else f'{R}LOSS{Z}'
                pnl_str = f'{G}+${pnl_v:.3f}{Z}' if (pnl_v or 0)>0 else f'{R}${pnl_v:.3f}{Z}'
            rows.append([
                ts[11:16], sig[:13], q,
                f'{ep:.3f}', f'${amt:.2f}',
                st_str, pnl_str,
                (reason or '')[:15]
            ])
        print(f'\n{WW}  Riwayat trade:{Z}')
        print(tabulate(rows,
            headers=['Jam','Signal','Pasar','Entry','Bet','Status','P&L','Alasan Close'],
            tablefmt='simple'))

def display(results, stats_scan, stats_j, pm: 'PositionManager', closed_this_scan):
    try:
        if CFG['CLEAR_SCREEN']: os.system('clear 2>/dev/null || true')
    except: pass
    banner()

    sp = SPIN[stats_scan['scans'] % len(SPIN)]
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'\n{C}{"═"*72}')
    print(
        f'  {sp} {W}Waktu:{Y}{ts}{Z}  '
        f'{W}Scan:{C}#{stats_scan["scans"]}{Z}  '
        f'{W}Pasar:{C}{stats_scan["fetched"]}{Z}  '
        f'{W}Valid:{G}{stats_scan["valid"]}{Z}  '
        f'{W}Durasi:{C}{stats_scan["ms"]}ms{Z}  '
        f'{W}Close scan ini:{GG}{len(closed_this_scan)}{Z}'
    )
    print(f'{C}{"═"*72}')

    display_stats(stats_j, pm)
    display_positions(pm.open_positions)

    if closed_this_scan:
        print(f'\n{WW}  🔔 BARU DITUTUP SCAN INI:\n')
        for c in closed_this_scan:
            pnl = c['pnl']
            col = G if pnl>0 else R
            print(f'  {col}#{c["pos"]["id"]} {c["reason"]} | '
                  f'P&L: ${pnl:+.3f} | '
                  f'{c["pos"]["question"][:40]}{Z}')

    if not results:
        print(f'\n{Y}  Menunggu data...\n')
        return

    # Tabel sinyal
    can_open, reason = pm.can_open()
    slot_color = G if can_open else R
    print(f'\n{WW}  TOP {len(results)} SINYAL — '
          f'Slot: {slot_color}{CFG["MAX_POSITIONS"]-pm.count} tersedia{Z}\n')

    rows = []
    for rank, r in enumerate(results,1):
        col = r['color']
        q   = (r['question'][:28]+'..')if len(r['question'])>28 else r['question']
        mo  = r['momentum_dir']
        mc  = G if '↑' in mo else (R if '↓' in mo else W)
        can = f'{G}✓{Z}' if (r.get('is_strong_signal') and can_open) else f'{W}—{Z}'
        rows.append([
            f'{col}{rank}{Z}',
            f'{col}{r["signal"]}{Z}',
            q,
            f'{col}{r["action"][:14]}{Z}',
            f'{r["entry_price"]:.3f}',
            fp(r['momentum_pct']),
            fd(r['days']),
            f'{C}{r["score"]:.0f}{Z}',
            fu(r['liquidity']),
            can,
        ])

    print(tabulate(rows,
        headers=['#','Signal','Pasar','Action','Entry','Mom','Sisa','Skor','Liq','Entry?'],
        tablefmt='simple'))

    print(f'\n{C}{"─"*72}')
    print(
        f'  TP:{G}+{CFG["TAKE_PROFIT_PCT"]:.0f}%{Z}  '
        f'SL:{R}-{CFG["STOP_LOSS_PCT"]:.0f}%{Z}  '
        f'TimeExit:{Y}<{CFG["TIME_EXIT_MINUTES"]}m{Z}  '
        f'ForceExit:{RR}<{CFG["FORCE_EXIT_MINUTES"]}m{Z}  '
        f'Journal:{C}{CSV_PATH}{Z}\n'
    )

# ══════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════
async def main():
    try: os.system('clear')
    except: pass
    init_db()
    banner()

    print(f'\n  {Y}Inisialisasi...{Z}')
    print(f'  {C}Max posisi:{Z} {CFG["MAX_POSITIONS"]} posisi / ${CFG["MAX_EXPOSURE"]:.0f} total')
    print(f'  {C}Per trade:{Z} ${CFG["TRADE_PER_SIGNAL"]:.2f}')
    print(f'  {C}Take Profit:{Z} +{CFG["TAKE_PROFIT_PCT"]:.0f}%')
    print(f'  {C}Stop Loss:{Z} -{CFG["STOP_LOSS_PCT"]:.0f}%')
    print(f'  {C}Time Exit:{Z} < {CFG["TIME_EXIT_MINUTES"]} menit tersisa')
    print(f'  {C}Force Exit:{Z} < {CFG["FORCE_EXIT_MINUTES"]} menit tersisa')
    print(f'  {C}Mode:{Z} {"REAL TRADE" if AUTO_TRADE and PRIVATE_KEY else "PAPER TRADE"}')
    print(f'  {C}Telegram:{Z} {"✓ Aktif" if TELEGRAM_TOKEN else "✗ Tidak aktif"}')
    print(f'  {C}Journal:{Z} {CSV_PATH}')
    await asyncio.sleep(1)

    history  : Dict[str, list] = {}
    scans    = 0
    pm       = PositionManager()
    await pm.refresh()
    already_opened = set()

    conn = aiohttp.TCPConnector(limit=50,limit_per_host=15,ttl_dns_cache=300,ssl=False)
    hdrs = {'User-Agent':'Mozilla/5.0 PolyBot/10.0','Accept':'application/json'}

    async with aiohttp.ClientSession(connector=conn, headers=hdrs) as session:
        if TELEGRAM_TOKEN:
            mode = 'REAL TRADE' if AUTO_TRADE and PRIVATE_KEY else 'PAPER TRADE'
            await tg(session,
                f'🤖 <b>Polymarket Auto Bot v10.0 Online!</b>\n'
                f'Mode: <b>{mode}</b>\n'
                f'Max: {CFG["MAX_POSITIONS"]} posisi / ${CFG["MAX_EXPOSURE"]:.0f}\n'
                f'TP: +{CFG["TAKE_PROFIT_PCT"]:.0f}% | '
                f'SL: -{CFG["STOP_LOSS_PCT"]:.0f}% | '
                f'TimeExit: <{CFG["TIME_EXIT_MINUTES"]}m')

        while True:
            t0 = time.time()
            closed_this_scan = []
            try:
                # ── 1. CHECK & CLOSE POSISI ──────────────────────
                await pm.refresh()
                if pm.open_positions:
                    closed_this_scan = await pm.check_and_close(session)
                    await pm.refresh()

                # ── 2. FETCH MARKETS ─────────────────────────────
                raw = await fetch_markets(session)

                all_tids = []
                for m in raw:
                    tids = parse_token_ids(m)
                    all_tids.extend(tids[:2])
                all_tids = list(dict.fromkeys(all_tids))

                clob_map = await fetch_clob_batch(session, all_tids)

                # ── 3. PROCESS MARKETS ───────────────────────────
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
                ms = int((time.time()-t0)*1000)

                # ── 4. AUTO-OPEN POSISI BARU ─────────────────────
                for r in top:
                    if not r.get('is_strong_signal'): continue
                    if r['id'] in already_opened: continue
                    if r['liquidity'] < CFG['MIN_LIQUIDITY']: continue

                    # Skip pasar yang sudah mau habis sangat cepat
                    if r['days'] is not None and r['days'] < 0.021:
                        continue  # < 30 menit, terlalu riskan

                    can, reason = pm.can_open()
                    if not can: break

                    pos_id = await pm.open_position(session, r)
                    if pos_id:
                        already_opened.add(r['id'])
                        log.info(f"OPENED #{pos_id}: {r['signal']} "
                                 f"{r['entry_outcome']} @ {r['entry_price']:.3f}")
                    break  # Buka 1 posisi per scan

                # ── 5. LOG SCAN ──────────────────────────────────
                try:
                    conn2 = sqlite3.connect(DB_PATH)
                    conn2.execute(
                        'INSERT INTO scan_log (ts,fetched,valid,open_pos,ms) VALUES (?,?,?,?,?)',
                        (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                         len(raw), len(results), pm.count, ms)
                    )
                    conn2.commit(); conn2.close()
                except: pass

                stats_j = db_get_stats()
                display(top, {
                    'scans':scans,'fetched':len(raw),
                    'valid':len(results),'ms':ms,
                }, stats_j, pm, closed_this_scan)

            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f'Error: {e}')

            try:
                await asyncio.sleep(CFG['SCAN_INTERVAL'])
            except KeyboardInterrupt:
                break

    final = db_get_stats()
    print(f'\n{Y}  Bot berhenti. Scan: {scans}{Z}')
    print(f'  Total trades: {final["total"]} | '
          f'Win: {final["wins"]} | Loss: {final["losses"]} | '
          f'P&L: ${final["pnl"]:+.2f}')
    print(f'  Journal: {CSV_PATH}\n')

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f'\n{Y}  Sampai jumpa! 👋{Z}\n')

#!/usr/bin/env python3
"""
POLYMARKET SCANNER v7.0
========================
Fitur:
  1. Trade Journal otomatis (simpan ke CSV + SQLite)
  2. Auto-entry order ($1 per sinyal kuat, max $10 total)
  3. Bisa jalan di background / server 24 jam
  4. Win rate tracker real-time
  5. P&L report harian

SETUP AUTO-TRADING:
  1. Buka https://polymarket.com
  2. Login → klik avatar → Settings → API Keys → Create Key
  3. Simpan: API_KEY, API_SECRET, API_PASSPHRASE, WALLET_ADDRESS, PRIVATE_KEY
  4. Isi di bagian CREDENTIALS di bawah
"""

import asyncio
import aiohttp
import os
import json
import time
import os
import math
import csv
import sqlite3
import hashlib
import hmac
import base64
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from colorama import Fore, Style, init
from tabulate import tabulate
from pathlib import Path

init(autoreset=True)

import os

# ─────────────────────────────────────────────────────────────────────
# TELEGRAM NOTIFICATION
# ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
_tg_last_sent    = {}   # anti-spam: market_id -> timestamp

async def send_telegram(session, message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        await session.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=aiohttp.ClientTimeout(total=5))
    except:
        pass

async def notify_signal(session, r: dict, scan_num: int):
    """Kirim notifikasi sinyal kuat ke Telegram - anti spam 30 menit"""
    if r["signal"] not in ["🔥 STRONG BUY", "✅ BUY", "💰 ARBITRAGE", "⚡ EDGE"]:
        return
    
    # Anti-spam: jangan kirim notif sama dalam 30 menit
    now = time.time()
    last = _tg_last_sent.get(r.get("id",""), 0)
    if now - last < 1800:
        return
    _tg_last_sent[r.get("id","")] = now
    
    days_str = f"{r['days']:.1f} hari" if r.get("days") is not None else "?"
    msg = (
        f"<b>{r['signal']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>{r['question'][:80]}</b>\n\n"
        f"🎯 Action: <b>{r['action']}</b>\n"
        f"💰 Entry: <b>{r['entry_price']:.3f}</b>\n"
        f"📈 Fair Value: {r['entry_fv']:.3f}\n"
        f"⚡ EV: {r['ev_pct']:+.2f}%\n"
        f"💧 Likuiditas: ${r['liquidity']:,.0f}\n"
        f"⏰ Sisa: {days_str}\n"
        f"🔍 Scan #{scan_num}"
    )
    await send_telegram(session, msg)


# ══════════════════════════════════════════════════════════════════
# KONFIGURASI — WAJIB DIISI UNTUK AUTO-TRADING
# ══════════════════════════════════════════════════════════════════
CREDENTIALS = {
    # Dapatkan dari: polymarket.com → Settings → API Keys
    'API_KEY'        : '',   # ← isi API key kamu
    'API_SECRET'     : '',   # ← isi API secret kamu
    'API_PASSPHRASE' : '',   # ← isi passphrase kamu
    'PRIVATE_KEY'    : os.environ.get('PRIVATE_KEY', ''),
    'WALLET_ADDRESS' : os.environ.get('WALLET_ADDRESS', ''),
}

CFG = {
    # API
    'GAMMA_API'         : 'https://gamma-api.polymarket.com',
    'CLOB_API'          : 'https://clob.polymarket.com',
    'DATA_API'          : 'https://data-api.polymarket.com',

    # Scanner
    'SCAN_INTERVAL'     : 10,
    'MARKETS_PER_PAGE'  : 100,
    'MAX_PAGES'         : 15,
    'DISPLAY_TOP'       : 15,
    'CLEAR_SCREEN'      : True,

    # ── AUTO-TRADING ──────────────────────────────────────────
    'AUTO_TRADE'        : os.environ.get('AUTO_TRADE','false').lower() == 'true',
    'TRADE_PER_SIGNAL'  : 1.00,      # $1 per entry
    'MAX_TOTAL_EXPOSURE': 10.00,     # max $10 total terbuka sekaligus
    'MIN_SIGNAL_TO_TRADE': ['🔥 STRONG BUY', '💰 ARBITRAGE'],  # sinyal yang di-auto-trade
    'MIN_EV_TO_TRADE'   : 5.0,       # minimum EV% untuk auto-trade
    'MIN_LIQUIDITY'     : 500,       # minimum likuiditas pasar untuk auto-trade

    # ── JOURNAL ───────────────────────────────────────────────
    'JOURNAL_DIR'       : os.path.expanduser('~/polymarket-scanner/journal'),
    'DB_PATH'           : os.path.expanduser('~/polymarket-scanner/journal/trades.db'),
    'CSV_PATH'          : os.path.expanduser('~/polymarket-scanner/journal/trades.csv'),
    'LOG_PATH'          : os.path.expanduser('~/polymarket-scanner/journal/scanner.log'),

    # ── RISK ──────────────────────────────────────────────────
    'KELLY_FRACTION'    : 0.25,
    'BANKROLL'          : 10.00,     # total modal yang dialokasikan

    # ── ALERT ─────────────────────────────────────────────────
    'SOUND_ALERT'       : False,
}

# ══════════════════════════════════════════════════════════════════
# LOGGING SETUP
# ══════════════════════════════════════════════════════════════════
Path(CFG['JOURNAL_DIR']).mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(CFG['LOG_PATH']),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger('polyscanner')

# ══════════════════════════════════════════════════════════════════
# WARNA
# ══════════════════════════════════════════════════════════════════
GG = Fore.GREEN  + Style.BRIGHT
G  = Fore.GREEN
R  = Fore.RED
RR = Fore.RED    + Style.BRIGHT
Y  = Fore.YELLOW
YY = Fore.YELLOW + Style.BRIGHT
C  = Fore.CYAN
CC = Fore.CYAN   + Style.BRIGHT
W  = Fore.WHITE
WW = Fore.WHITE  + Style.BRIGHT
M  = Fore.MAGENTA
Z  = Style.RESET_ALL

def fp(v, d=2):
    if   v >=  5: return f'{GG}{v:+.{d}f}%{Z}'
    elif v >   0: return f'{G}{v:+.{d}f}%{Z}'
    elif v == 0:  return f'{W}+0.00%{Z}'
    elif v >  -5: return f'{R}{v:+.{d}f}%{Z}'
    else:         return f'{RR}{v:+.{d}f}%{Z}'

def fu(v):
    if   v >= 1_000_000: return f'${v/1_000_000:.2f}M'
    elif v >= 1_000:     return f'${v/1_000:.1f}K'
    else:                return f'${v:.0f}'

def fd(d):
    if d is None: return f'{W}—{Z}'
    if d < 0:     return f'{R}EXP{Z}'
    if d < 0.042: return f'{RR}<1h{Z}'
    if d < 1:     return f'{RR}{d*24:.0f}h{Z}'
    if d < 7:     return f'{Y}{d:.1f}d{Z}'
    return        f'{W}{d:.0f}d{Z}'

SPIN = ['⣾','⣽','⣻','⢿','⡿','⣟','⣯','⣷']

# ══════════════════════════════════════════════════════════════════
# DATABASE JOURNAL
# ══════════════════════════════════════════════════════════════════
class TradeJournal:
    def __init__(self):
        self.db_path  = CFG['DB_PATH']
        self.csv_path = CFG['CSV_PATH']
        self._init_db()

    def _init_db(self):
        """Buat tabel database jika belum ada"""
        conn = sqlite3.connect(self.db_path)
        cur  = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    TEXT NOT NULL,
                market_id    TEXT,
                question     TEXT,
                category     TEXT,
                signal       TEXT,
                action       TEXT,
                outcome      TEXT,
                entry_price  REAL,
                fair_value   REAL,
                ev_pct       REAL,
                edge_pct     REAL,
                amount_usd   REAL,
                kelly_usd    REAL,
                is_auto      INTEGER DEFAULT 0,
                order_id     TEXT,
                status       TEXT DEFAULT 'OPEN',
                exit_price   REAL,
                pnl_usd      REAL,
                resolved_at  TEXT,
                outcome_result TEXT,
                notes        TEXT
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS scan_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                markets   INTEGER,
                valid     INTEGER,
                alerts    INTEGER,
                ms        INTEGER
            )
        ''')
        conn.commit()
        conn.close()

    def log_trade(self, r: dict, amount: float, is_auto: bool = False, order_id: str = '') -> int:
        """Simpan satu trade ke database dan CSV"""
        ts   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(self.db_path)
        cur  = conn.cursor()
        cur.execute('''
            INSERT INTO trades
            (timestamp, market_id, question, category, signal, action, outcome,
             entry_price, fair_value, ev_pct, edge_pct, amount_usd, kelly_usd,
             is_auto, order_id, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            ts,
            r.get('id', ''),
            r.get('question', '')[:200],
            r.get('category', ''),
            r.get('signal', ''),
            r.get('action', ''),
            r.get('entry_outcome', ''),
            r.get('entry_price', 0),
            r.get('entry_fv', 0),
            r.get('ev_pct', 0),
            r.get('edge_pct', 0),
            amount,
            r.get('kelly_usd', 0),
            1 if is_auto else 0,
            order_id,
            'OPEN',
        ))
        trade_id = cur.lastrowid
        conn.commit()
        conn.close()

        # Juga simpan ke CSV
        self._append_csv({
            'id'          : trade_id,
            'timestamp'   : ts,
            'market_id'   : r.get('id', ''),
            'question'    : r.get('question', '')[:100],
            'signal'      : r.get('signal', ''),
            'action'      : r.get('action', ''),
            'outcome'     : r.get('entry_outcome', ''),
            'entry_price' : r.get('entry_price', 0),
            'fair_value'  : r.get('entry_fv', 0),
            'ev_pct'      : r.get('ev_pct', 0),
            'amount_usd'  : amount,
            'is_auto'     : 'AUTO' if is_auto else 'MANUAL',
            'order_id'    : order_id,
            'status'      : 'OPEN',
        })

        log.info(f'TRADE LOGGED #{trade_id}: {r.get("signal")} | {r.get("entry_outcome")} @ {r.get("entry_price"):.3f} | ${amount:.2f}')
        return trade_id

    def _append_csv(self, row: dict):
        file_exists = os.path.exists(self.csv_path)
        with open(self.csv_path, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=row.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)

    def resolve_trade(self, trade_id: int, outcome_result: str, exit_price: float):
        """Update trade setelah market resolved"""
        conn = sqlite3.connect(self.db_path)
        cur  = conn.cursor()
        cur.execute('SELECT entry_price, amount_usd, outcome FROM trades WHERE id=?', (trade_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return

        entry_price, amount_usd, outcome = row

        # Hitung P&L
        if outcome_result == outcome:
            # WIN: payout = amount / entry_price
            pnl = (amount_usd / entry_price) - amount_usd
        else:
            # LOSS: kehilangan amount yang ditaruh
            pnl = -amount_usd

        cur.execute('''
            UPDATE trades
            SET status='CLOSED', exit_price=?, pnl_usd=?,
                resolved_at=?, outcome_result=?
            WHERE id=?
        ''', (exit_price, pnl, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), outcome_result, trade_id))
        conn.commit()
        conn.close()
        log.info(f'TRADE RESOLVED #{trade_id}: {outcome_result} | P&L = ${pnl:+.2f}')

    def get_stats(self) -> dict:
        """Hitung statistik win rate, P&L, dll"""
        conn   = sqlite3.connect(self.db_path)
        cur    = conn.cursor()

        # Semua trades
        cur.execute('SELECT COUNT(*) FROM trades')
        total = cur.fetchone()[0]

        # Closed trades
        cur.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED'")
        closed = cur.fetchone()[0]

        # Win rate
        cur.execute("""
            SELECT COUNT(*) FROM trades
            WHERE status='CLOSED' AND pnl_usd > 0
        """)
        wins = cur.fetchone()[0]

        # Total P&L
        cur.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM trades WHERE status='CLOSED'")
        total_pnl = cur.fetchone()[0]

        # Total invested
        cur.execute("SELECT COALESCE(SUM(amount_usd),0) FROM trades WHERE status='OPEN'")
        open_exposure = cur.fetchone()[0]

        # Best trade
        cur.execute("SELECT question, pnl_usd FROM trades WHERE status='CLOSED' ORDER BY pnl_usd DESC LIMIT 1")
        best = cur.fetchone()

        # Worst trade
        cur.execute("SELECT question, pnl_usd FROM trades WHERE status='CLOSED' ORDER BY pnl_usd ASC LIMIT 1")
        worst = cur.fetchone()

        # Today's P&L
        today = datetime.now().strftime('%Y-%m-%d')
        cur.execute("""
            SELECT COALESCE(SUM(pnl_usd),0) FROM trades
            WHERE status='CLOSED' AND resolved_at LIKE ?
        """, (f'{today}%',))
        today_pnl = cur.fetchone()[0]

        # Recent trades
        cur.execute("""
            SELECT timestamp, signal, outcome, entry_price, amount_usd, status, pnl_usd
            FROM trades ORDER BY id DESC LIMIT 10
        """)
        recent = cur.fetchall()

        conn.close()

        win_rate = (wins / closed * 100) if closed > 0 else 0.0
        roi      = (total_pnl / max(1, total * CFG['TRADE_PER_SIGNAL'])) * 100

        return {
            'total'         : total,
            'closed'        : closed,
            'open'          : total - closed,
            'wins'          : wins,
            'losses'        : closed - wins,
            'win_rate'      : win_rate,
            'total_pnl'     : total_pnl,
            'today_pnl'     : today_pnl,
            'open_exposure' : open_exposure,
            'roi'           : roi,
            'best'          : best,
            'worst'         : worst,
            'recent'        : recent,
        }

    def log_scan(self, markets: int, valid: int, alerts: int, ms: int):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'INSERT INTO scan_log (timestamp, markets, valid, alerts, ms) VALUES (?,?,?,?,?)',
            (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), markets, valid, alerts, ms)
        )
        conn.commit()
        conn.close()

# ══════════════════════════════════════════════════════════════════
# AUTO-TRADER (Polymarket CLOB API)
# ══════════════════════════════════════════════════════════════════
class AutoTrader:
    def __init__(self, journal: TradeJournal):
        self.journal      = journal
        self.total_open   = 0.0      # total exposure saat ini
        self.trades_today = 0
        self.enabled      = (
            CFG['AUTO_TRADE'] and
            bool(CREDENTIALS['API_KEY']) and
            bool(CREDENTIALS['PRIVATE_KEY'])
        )

    def _make_headers(self, method: str, path: str, body: str = '') -> dict:
        """Buat header autentikasi CLOB API"""
        ts  = str(int(time.time() * 1000))
        msg = ts + method.upper() + path + body
        sig = hmac.new(
            CREDENTIALS['API_SECRET'].encode(),
            msg.encode(),
            hashlib.sha256
        ).hexdigest()
        return {
            'POLY-API-KEY'     : CREDENTIALS['API_KEY'],
            'POLY-SIGNATURE'   : sig,
            'POLY-TIMESTAMP'   : ts,
            'POLY-PASSPHRASE'  : CREDENTIALS['API_PASSPHRASE'],
            'Content-Type'     : 'application/json',
        }

    async def place_order(
        self,
        session     : aiohttp.ClientSession,
        token_id    : str,
        side        : str,        # 'BUY' atau 'SELL'
        price       : float,
        amount_usdc : float,
    ) -> Optional[dict]:
        """Kirim limit order ke CLOB API"""
        if not self.enabled:
            return None

        size     = round(amount_usdc / price, 2)
        endpoint = '/order'
        body     = json.dumps({
            'tokenID'    : token_id,
            'price'      : round(price, 4),
            'size'       : size,
            'side'       : side,
            'type'       : 'GTC',        # Good Till Cancel
            'feeRateBps' : 0,
        })
        headers = self._make_headers('POST', endpoint, body)

        try:
            async with session.post(
                f"{CFG['CLOB_API']}{endpoint}",
                data=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 200 and data.get('orderID'):
                    log.info(f'ORDER PLACED: {side} {token_id[:20]} @ {price} x {size} = ${amount_usdc}')
                    return data
                else:
                    log.warning(f'ORDER FAILED: {data}')
        except Exception as e:
            log.error(f'ORDER ERROR: {e}')
        return None

    async def auto_trade(
        self,
        session : aiohttp.ClientSession,
        r       : dict,
        token_id: str,
    ) -> bool:
        """Eksekusi auto-trade jika memenuhi syarat"""
        if not self.enabled:
            return False

        # Cek apakah sinyal memenuhi kriteria
        if r['signal'] not in CFG['MIN_SIGNAL_TO_TRADE']:
            return False
        if r['ev_pct'] < CFG['MIN_EV_TO_TRADE']:
            return False
        if r['liquidity'] < CFG['MIN_LIQUIDITY']:
            return False
        if self.total_open >= CFG['MAX_TOTAL_EXPOSURE']:
            log.info(f'AUTO-TRADE SKIPPED: max exposure ${CFG["MAX_TOTAL_EXPOSURE"]} reached')
            return False

        amount = CFG['TRADE_PER_SIGNAL']

        # Jangan trade jika ini arbitrage (butuh 2 legs, lebih kompleks)
        if r['is_arb']:
            log.info('ARBITRAGE signal — skip auto-trade (manual recommended)')
            return False

        # Place order
        result = await self.place_order(
            session  = session,
            token_id = token_id,
            side     = 'BUY',
            price    = r['entry_price'],
            amount_usdc = amount,
        )

        if result:
            order_id = result.get('orderID', '')
            trade_id = self.journal.log_trade(r, amount, is_auto=True, order_id=order_id)
            self.total_open += amount
            self.trades_today += 1
            log.info(f'AUTO-TRADE SUCCESS: #{trade_id} order={order_id}')
            return True
        return False

# ══════════════════════════════════════════════════════════════════
# PARSER & API (sama seperti v6, dipertahankan)
# ══════════════════════════════════════════════════════════════════
def parse_list(raw) -> list:
    if isinstance(raw, list): return raw
    if isinstance(raw, str):
        try: return json.loads(raw)
        except: return []
    return []

def parse_prices(m: dict) -> List[float]:
    for field in ['outcomePrices','prices','outcome_prices']:
        raw = parse_list(m.get(field, []))
        if raw:
            try:
                vals = [max(0.001, min(0.999, float(str(x)))) for x in raw]
                if len(vals) >= 2: return vals
            except: continue
    return []

def parse_outcomes(m: dict) -> List[str]:
    return [str(x) for x in parse_list(m.get('outcomes', '[]'))]

def parse_token_ids(m: dict) -> List[str]:
    return [str(x) for x in parse_list(m.get('clobTokenIds', '[]'))]

def parse_days(m: dict) -> Optional[float]:
    for field in ['endDateIso','endDate','end_date_iso']:
        val = m.get(field)
        if val:
            try:
                dt = datetime.fromisoformat(str(val).replace('Z','+00:00'))
                return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
            except: continue
    return None

async def api_get(session, url, params=None):
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status == 200:
                return await r.json(content_type=None)
    except: pass
    return None

async def fetch_all_markets(session) -> List[dict]:
    tasks = [
        api_get(session, f"{CFG['GAMMA_API']}/markets", {
            'active':'true','closed':'false',
            'limit': CFG['MARKETS_PER_PAGE'],
            'offset': i * CFG['MARKETS_PER_PAGE'],
            'order':'volume24hr','ascending':'false',
        })
        for i in range(CFG['MAX_PAGES'])
    ]
    pages = await asyncio.gather(*tasks)
    out = []
    for p in pages:
        if isinstance(p, list) and p: out.extend(p)
    return out

async def fetch_clob(session, token_ids: List[str]) -> Dict[str, dict]:
    if not token_ids: return {}
    batches = [token_ids[i:i+40] for i in range(0, min(len(token_ids),200), 40)]
    mid_t = [api_get(session, f"{CFG['CLOB_API']}/midpoints",
                     [('token_id',t) for t in b]) for b in batches]
    spd_t = [api_get(session, f"{CFG['CLOB_API']}/spreads",
                     [('token_id',t) for t in b]) for b in batches]
    mids_pages, spds_pages = await asyncio.gather(
        asyncio.gather(*mid_t), asyncio.gather(*spd_t))
    mid_map, spd_map = {}, {}
    for page in mids_pages:
        if isinstance(page, list):
            for x in page:
                tid = str(x.get('token_id',''))
                try: mid_map[tid] = float(x.get('mid',0))
                except: pass
    for page in spds_pages:
        if isinstance(page, list):
            for x in page:
                tid = str(x.get('token_id',''))
                try: spd_map[tid] = float(x.get('spread',0))
                except: pass
    result = {}
    for tid, mid in mid_map.items():
        if mid > 0:
            spd = spd_map.get(tid, 0.02)
            result[tid] = {'mid':mid,'bid':max(0.001,mid-spd/2),'ask':min(0.999,mid+spd/2),'spread':spd}
    return result

# ══════════════════════════════════════════════════════════════════
# ANALISIS MISPRICING
# ══════════════════════════════════════════════════════════════════
def analyze(names, gamma_px, clob_data, liquidity, volume, days, prev_px, stale_count) -> Optional[dict]:
    N = len(gamma_px)
    if N < 2: return None

    price_sum     = sum(gamma_px)
    overround_pct = (price_sum - 1.0) * 100
    is_arb        = price_sum < 0.99
    arb_profit    = max(0.0, (1.0 - price_sum) * 100)

    # Normalized fair values
    norm = [p / price_sum for p in gamma_px]

    # CLOB gap: berapa % lebih murah beli di CLOB vs Gamma
    clob_gaps = []
    for i, cd in enumerate(clob_data):
        clob_gaps.append((gamma_px[i] - cd['ask']) * 100 if cd else 0.0)

    best_clob_i   = max(range(N), key=lambda i: clob_gaps[i])
    best_clob_gap = clob_gaps[best_clob_i]
    best_clob_ask = clob_data[best_clob_i]['ask'] if clob_data[best_clob_i] else gamma_px[best_clob_i]

    # Edge dari overround
    edges       = [(norm[i] - gamma_px[i]) * 100 for i in range(N)]
    best_over_i = max(range(N), key=lambda i: edges[i])
    best_edge   = edges[best_over_i]

    # Pilih metode entry terbaik
    if best_clob_gap > best_edge and best_clob_gap > 0.5:
        entry_i    = best_clob_i
        entry_px   = best_clob_ask
        ev_pct     = best_clob_gap
        method     = 'CLOB-GAP'
    else:
        entry_i    = best_over_i
        entry_px   = gamma_px[best_over_i]
        ev_pct     = best_edge
        method     = 'OVERROUND'

    entry_name = names[entry_i]
    entry_fv   = norm[entry_i]

    # Kelly
    kelly = 0.0
    if 0.001 < entry_px < 0.999 and entry_fv > 0:
        b = (1.0 / entry_px) - 1.0
        if b > 0:
            k = (b * entry_fv - (1 - entry_fv)) / b
            kelly = max(0.0, k * CFG['KELLY_FRACTION'])

    # Momentum
    momentum_pct, momentum_dir = 0.0, ''
    if prev_px and len(prev_px) == N and prev_px[0] > 0:
        chg = (gamma_px[0] - prev_px[0]) / prev_px[0] * 100
        momentum_pct = chg
        if   chg >=  5: momentum_dir = f'↑{chg:.1f}%'
        elif chg <= -5: momentum_dir = f'↓{abs(chg):.1f}%'

    # Near-resolution
    near_res, near_note, near_bonus = False, '', 0.0
    if days is not None and 0 <= days <= 3:
        mid_px = gamma_px[0]
        if 0.05 < mid_px < 0.95:
            near_res   = True
            near_bonus = 25.0
            if   days < 0.042: near_note = '⏰ CLOSES <1H!'
            elif days < 1:     near_note = '⏰ CLOSES <24H'
            else:              near_note = f'⏰ CLOSES {days:.1f}D'

    # Volume anomali
    vol_note = ''
    if liquidity > 0 and volume > liquidity * 3:
        vol_note = f'🔊 VOL SPIKE {volume/liquidity:.0f}x'

    # Composite score
    score = (
        (100 if is_arb else 0) +
        min(60, max(0, ev_pct  * 6)) +
        min(40, max(0, best_clob_gap * 4)) +
        min(15, math.log10(max(liquidity, 1)) * 4) +
        min(15, math.log10(max(volume, 1)) * 4) +
        min(20, abs(overround_pct) * 2) +
        min(20, abs(momentum_pct) * 2) +
        near_bonus
    )

    # Signal
    if is_arb and arb_profit > 0.3:
        signal, action, color = '💰 ARBITRAGE', 'BELI SEMUA OUTCOME', GG
    elif ev_pct >= 8:
        signal, action, color = '🔥 STRONG BUY', f'BUY {entry_name[:12].upper()}', GG
    elif ev_pct >= 3:
        signal, action, color = '✅ BUY', f'BUY {entry_name[:12].upper()}', G
    elif near_res and ev_pct > 0:
        signal, action, color = '⏰ NEAR-RES', f'BUY {entry_name[:12].upper()}', YY
    elif momentum_pct >= 5:
        signal, action, color = '📈 SELL PUMP', f'BUY {(names[1] if N>1 else "NO")[:12].upper()}', M
    elif momentum_pct <= -5:
        signal, action, color = '📉 DIP BUY', f'BUY {entry_name[:12].upper()}', C
    elif ev_pct >= 1:
        signal, action, color = '⚡ EDGE', f'BUY {entry_name[:12].upper()}', Y
    elif near_res:
        signal, action, color = '⏰ NEAR-RES', 'MONITOR', Y
    else:
        signal, action, color = '➖ MONITOR', 'MONITOR', W

    return {
        'signal': signal, 'action': action, 'color': color, 'method': method,
        'gamma_prices': gamma_px, 'price_sum': price_sum,
        'overround_pct': overround_pct, 'is_arb': is_arb, 'arb_profit': arb_profit,
        'entry_outcome': entry_name, 'entry_price': entry_px, 'entry_fv': entry_fv,
        'edge_pct': best_edge, 'ev_pct': ev_pct, 'clob_gap_pct': best_clob_gap,
        'kelly': kelly, 'kelly_usd': kelly * CFG['BANKROLL'],
        'momentum_pct': momentum_pct, 'momentum_dir': momentum_dir,
        'near_res': near_res, 'near_note': near_note,
        'vol_note': vol_note, 'score': score,
        'names': names, 'norm_fv': norm, 'edges': edges,
        'clob_data': clob_data,
    }

def process_market(m, history, stale_cnts, clob_map) -> Optional[dict]:
    q = (m.get('question') or '').strip()
    if not q: return None
    liq    = float(m.get('liquidity') or 0)
    vol    = float(m.get('volume24hr') or m.get('volume') or 0)
    names  = parse_outcomes(m)
    prices = parse_prices(m)
    tids   = parse_token_ids(m)
    days   = parse_days(m)
    if len(prices) < 2 or len(names) < 2: return None
    if days is not None and days < -7: return None
    n      = min(len(names), len(prices))
    names, prices = names[:n], prices[:n]
    clob_per = [clob_map.get(tids[i]) if i < len(tids) else None for i in range(n)]
    mid    = str(m.get('id', q[:40]))
    res    = analyze(names, prices, clob_per, liq, vol, days,
                     history.get(mid, []), stale_cnts.get(mid, 0))
    if not res: return None
    return {
        'id': mid, 'question': q,
        'category': str(m.get('category') or m.get('groupItemTitle') or 'General')[:20],
        'liquidity': liq, 'volume_24h': vol, 'days': days,
        'token_ids': tids,
        **res,
    }

# ══════════════════════════════════════════════════════════════════
# DISPLAY
# ══════════════════════════════════════════════════════════════════
def banner():
    auto_status = f'{GG}AUTO-TRADE ON{Z}' if CFG['AUTO_TRADE'] else f'{Y}AUTO-TRADE OFF{Z}'
    print(CC + WW + f'''
╔════════════════════════════════════════════════════════════════════╗
║  POLYMARKET SCANNER v7.0  ·  Journal + Auto-Trade + Win Rate     ║
╚════════════════════════════════════════════════════════════════════╝
  Status: {auto_status}  Bankroll: {G}${CFG["BANKROLL"]:.2f}{Z}  Per-Trade: {G}${CFG["TRADE_PER_SIGNAL"]:.2f}{Z}''')

def display_journal_panel(stats: dict):
    """Panel statistik win rate dan P&L"""
    wr    = stats['win_rate']
    pnl   = stats['total_pnl']
    wr_c  = GG if wr >= 55 else (Y if wr >= 45 else R)
    pnl_c = G  if pnl > 0  else (Y if pnl == 0 else R)

    print(f'\n{WW}  ┌─ JOURNAL & WIN RATE ──────────────────────────────────────────┐')
    print(
        f'  │  Trades: {W}{stats["total"]}{Z}  '
        f'Open: {C}{stats["open"]}{Z}  '
        f'Closed: {W}{stats["closed"]}{Z}  '
        f'Win: {G}{stats["wins"]}{Z}  '
        f'Loss: {R}{stats["losses"]}{Z}  '
        f'WinRate: {wr_c}{wr:.1f}%{Z}  '
        f'P&L: {pnl_c}${pnl:+.2f}{Z}  '
        f'Today: {pnl_c}${stats["today_pnl"]:+.2f}{Z}'
    )
    print(
        f'  │  Exposure: {Y}${stats["open_exposure"]:.2f}{Z} / ${CFG["MAX_TOTAL_EXPOSURE"]:.2f}  '
        f'ROI: {pnl_c}{stats["roi"]:+.1f}%{Z}  '
        f'Journal: {C}{CFG["CSV_PATH"]}{Z}'
    )
    if stats['best']:
        print(f'  │  Best: {G}+${stats["best"][1]:.2f}{Z} — {stats["best"][0][:50]}')
    if stats['worst'] and stats["worst"][1] < 0:
        print(f'  │  Worst: {R}${stats["worst"][1]:.2f}{Z} — {stats["worst"][0][:50]}')
    print(f'{WW}  └────────────────────────────────────────────────────────────────┘{Z}')

    # 5 trade terbaru
    if stats['recent']:
        print(f'\n{WW}  TRADE TERBARU:{Z}')
        rows = []
        for t in stats['recent'][:5]:
            ts, sig, out, ep, amt, status, pnl_v = t
            st_col = G if status == 'OPEN' else (GG if (pnl_v or 0) > 0 else R)
            pnl_str = f'{G}+${pnl_v:.2f}{Z}' if (pnl_v or 0) > 0 else (f'{R}${pnl_v:.2f}{Z}' if pnl_v else f'{Y}open{Z}')
            rows.append([
                ts[:16], sig[:15], out[:12],
                f'{ep:.3f}', f'${amt:.2f}',
                f'{st_col}{status}{Z}', pnl_str
            ])
        print(tabulate(rows,
            headers=['Waktu','Signal','Outcome','Harga','Bet','Status','P&L'],
            tablefmt='simple'))

def display(results, stats_scan, stats_journal):
    if CFG['CLEAR_SCREEN']: os.system('clear')
    banner()

    sp  = SPIN[stats_scan['scans'] % len(SPIN)]
    ts  = datetime.now().strftime('%H:%M:%S')
    print(f'\n{C}{"═"*70}')
    print(
        f'  {sp} {W}Waktu:{Y}{ts}{Z}  '
        f'{W}Scan:{C}#{stats_scan["scans"]}{Z}  '
        f'{W}Pasar:{C}{stats_scan["fetched"]}{Z}  '
        f'{W}Valid:{G}{stats_scan["parsed"]}{Z}  '
        f'{W}Durasi:{C}{stats_scan["ms"]}ms{Z}  '
        f'{W}Alert:{GG}{stats_scan["alerts"]}{Z}  '
        f'{W}AutoTrade:{GG if CFG["AUTO_TRADE"] else Y}{"ON" if CFG["AUTO_TRADE"] else "OFF"}{Z}'
    )
    print(f'{C}{"═"*70}')

    # Journal panel
    display_journal_panel(stats_journal)

    if not results:
        print(f'\n{Y}  Menunggu data...\n')
        return

    # Tabel peluang
    print(f'\n{WW}  TOP {len(results)} PELUANG — SCAN #{stats_scan["scans"]}\n')
    rows = []
    for rank, r in enumerate(results, 1):
        col = r['color']
        q   = (r['question'][:30]+'..') if len(r['question'])>30 else r['question']
        mo  = r['momentum_dir']
        mc  = G if '↑' in mo else (R if '↓' in mo else W)
        rk  = f'{GG}★{rank}{Z}' if 'ARBITRAGE' in r['signal'] or 'STRONG' in r['signal'] else f'{col}{rank}{Z}'
        rows.append([
            rk,
            f'{col}{r["signal"]}{Z}',
            q,
            f'{col}{r["action"][:14]}{Z}',
            f'{r["entry_price"]:.3f}',
            f'{M}{r["entry_fv"]:.3f}{Z}',
            fp(r['ev_pct']),
            fp(r['clob_gap_pct']),
            f'{C}{r["score"]:.0f}{Z}',
            fu(r['liquidity']),
            f'{G}${r["kelly_usd"]:.2f}{Z}',
            fd(r['days']),
            f'{mc}{mo}{Z}',
        ])
    print(tabulate(rows,
        headers=['#','Signal','Pasar','Action','Harga','FairVal',
                 'EV%','Gap%','Skor','Liq','Kelly$','Sisa','Mom'],
        tablefmt='simple'))

    # Detail top 3
    print(f'\n{WW}  ━━━━━ DETAIL ENTRY TOP 3 ━━━━━\n')
    for r in results[:3]:
        _card(r)

    # Footer
    print(f'{C}{"─"*70}')
    print(
        f'  {C}Journal:{Z} {CFG["CSV_PATH"]}  '
        f'{C}DB:{Z} {CFG["DB_PATH"]}\n'
        f'  {C}Auto-Trade:{Z} {"ON $"+str(CFG["TRADE_PER_SIGNAL"])+" per signal" if CFG["AUTO_TRADE"] else "OFF (edit CFG AUTO_TRADE=True untuk aktifkan)"}\n'
        f'  {R}Ctrl+C{Z}=stop  '
        f'{Y}Log:{Z} {CFG["LOG_PATH"]}\n'
    )

def _card(r):
    col = r['color']
    print(f'  {col}{"━"*66}{Z}')
    print(f'  {col}{r["signal"]}{Z}  {WW}{r["question"][:63]}{Z}')
    print(
        f'  {C}Liq:{Z}{fu(r["liquidity"])}  '
        f'{C}Vol24h:{Z}{fu(r["volume_24h"])}  '
        f'{C}Sisa:{Z}{fd(r["days"])}  '
        f'{C}Overround:{Z}{fp(r["overround_pct"])}  '
        f'{C}Sum:{Z}{r["price_sum"]:.4f}'
    )
    for i, (name, gp) in enumerate(zip(r['names'], r['gamma_prices'])):
        is_e = name == r['entry_outcome']
        c2   = GG if is_e else W
        cd   = r['clob_data'][i] if i < len(r['clob_data']) else None
        clob = f' [CLOB ask:{cd["ask"]:.3f} spread:{cd["spread"]:.3f}]' if cd else ''
        fv_v = r['norm_fv'][i] if i < len(r['norm_fv']) else gp
        ed_v = r['edges'][i]   if i < len(r['edges'])   else 0
        mk   = ' ◄ ENTRY DISINI' if is_e else ''
        print(f'    {c2}{name}: {gp:.3f} (fair:{fv_v:.3f} edge:{ed_v:+.1f}%){clob}{mk}{Z}')
    print()
    if r['is_arb']:
        print(f'  {GG}  >>> 💰 ARBITRAGE: BELI SEMUA OUTCOME — Profit: {fp(r["arb_profit"])}{Z}')
    else:
        ev_c = GG if r['ev_pct'] >= 5 else (G if r['ev_pct'] > 0 else R)
        print(
            f'  {Y}  >>> REKOMENDASI: {r["action"]}{Z}\n'
            f'       Entry:{GG}{r["entry_price"]:.4f}{Z}  '
            f'FairVal:{M}{r["entry_fv"]:.4f}{Z}  '
            f'EV:{ev_c}{r["ev_pct"]:+.2f}%{Z}  '
            f'Kelly:${G}{r["kelly_usd"]:.2f}{Z}'
        )
        if r['clob_gap_pct'] > 0.5:
            print(f'       {G}CLOB lebih murah {r["clob_gap_pct"]:+.2f}% dari Gamma — beli via CLOB!{Z}')
    for note in [r.get('near_note'), r.get('vol_note')]:
        if note: print(f'       {Y}⚠  {note}{Z}')
    if r['momentum_dir']:
        mc = G if '↑' in r['momentum_dir'] else R
        print(f'       Momentum: {mc}{r["momentum_dir"]}{Z}')
    print()

# ══════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════
async def main():
    os.system('clear')
    journal   = TradeJournal()
    trader    = AutoTrader(journal)

    banner()
    print(f'\n  {Y}Inisialisasi...{Z}')
    print(f'  {C}Pasar per scan:{Z} {CFG["MAX_PAGES"]*CFG["MARKETS_PER_PAGE"]}')
    print(f'  {C}Interval:{Z} {CFG["SCAN_INTERVAL"]}s')
    print(f'  {C}Journal:{Z} {CFG["CSV_PATH"]}')
    print(f'  {C}Database:{Z} {CFG["DB_PATH"]}')
    print(f'  {C}Log file:{Z} {CFG["LOG_PATH"]}')
    if CFG['AUTO_TRADE'] and not CREDENTIALS['API_KEY']:
        print(f'\n  {RR}⚠  AUTO_TRADE=True tapi CREDENTIALS belum diisi!{Z}')
        print(f'  {Y}  Edit file dan isi API_KEY, API_SECRET, PRIVATE_KEY, WALLET_ADDRESS{Z}\n')
    print(f'\n  {G}Menghubungkan ke Polymarket...{Z}\n')
    await asyncio.sleep(1)

    history   : Dict[str, List[float]] = {}
    stale_cnts: Dict[str, int]         = {}
    scans  = 0
    alerts = 0
    already_traded = set()    # market ID yang sudah di-trade sesi ini

    conn = aiohttp.TCPConnector(limit=50, limit_per_host=15, ttl_dns_cache=300, ssl=False)
    hdrs = {'User-Agent': 'Mozilla/5.0 PolyScanner/7.0', 'Accept': 'application/json'}

    async with aiohttp.ClientSession(connector=conn, headers=hdrs) as session:
        while True:
            t0 = time.time()
            try:
                raw = await fetch_all_markets(session)

                all_tids = []
                for m in raw:
                    tids = parse_token_ids(m)
                    all_tids.extend(tids[:2])
                all_tids = list(dict.fromkeys(all_tids))

                clob_map = await fetch_clob(session, all_tids)

                results   = []
                new_hist  : Dict[str, List[float]] = {}
                new_stale : Dict[str, int]         = {}

                for m in raw:
                    try:
                        r = process_market(m, history, stale_cnts, clob_map)
                        if r:
                            mid = r['id']
                            results.append(r)
                            new_hist[mid]  = r['gamma_prices']
                            new_stale[mid] = 0 if history.get(mid) != r['gamma_prices'] else stale_cnts.get(mid,0)+1
                        else:
                            mid = str(m.get('id',''))
                            pp  = parse_prices(m)
                            if mid and pp:
                                new_hist[mid]  = pp
                                new_stale[mid] = stale_cnts.get(mid,0)
                    except: continue

                history    = new_hist
                stale_cnts = new_stale

                results.sort(key=lambda x: x['score'], reverse=True)
                top = results[:CFG['DISPLAY_TOP']]

                scans += 1
                ms     = int((time.time()-t0)*1000)

                # Auto-trade & alert
                for r in top:
                    if r['signal'] in CFG['MIN_SIGNAL_TO_TRADE']:
                        alerts += 1
                        if CFG['SOUND_ALERT']:
                            os.system('afplay /System/Library/Sounds/Ping.aiff &')
                        # Auto-trade jika belum di-trade pasar ini
                        if CFG['AUTO_TRADE'] and r['id'] not in already_traded:
                            tids = r.get('token_ids', [])
                            tid  = tids[0] if tids else ''
                            ok   = await trader.auto_trade(session, r, tid)
                            if ok: already_traded.add(r['id'])

                # Simpan statistik scan
                journal.log_scan(len(raw), len(results), alerts, ms)

                # Journal manual untuk sinyal kuat (buat referensi)
                for r in top[:3]:
                    if r['signal'] in ['🔥 STRONG BUY', '💰 ARBITRAGE', '✅ BUY']:
                        if r['id'] not in already_traded:
                            journal.log_trade(r, amount=0, is_auto=False)

                stats_j = journal.get_stats()

                display(top, {
                    'scans':scans,'fetched':len(raw),'parsed':len(results),
                    'ms':ms,'alerts':alerts,
                }, stats_j)

            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f'Scan error: {e}')

            try:
                await asyncio.sleep(CFG['SCAN_INTERVAL'])
            except KeyboardInterrupt:
                break

    print(f'\n{Y}  Scanner berhenti. Total scan: {scans}{Z}')
    print(f'  {C}Journal tersimpan di:{Z} {CFG["CSV_PATH"]}')
    print(f'  {C}Database:{Z} {CFG["DB_PATH"]}\n')

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f'\n{Y}  Sampai jumpa! 👋{Z}\n')

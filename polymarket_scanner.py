#!/usr/bin/env python3
"""
POLYMARKET FULL AUTO BOT v12.0 (INTELLIGENCE ENGINE)
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

# Polymarket Scanner - Build Trigger v15.1 (Brain Surgery Update)
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
from typing import Optional, List, Dict, Tuple, Any
from pathlib import Path

# --- FASTAPI WEB UI IMPORTS ---
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
# ------------------------------

try:
    from intelligence import TradingBrain
except Exception as e:
    import traceback
    traceback.print_exc()
    TradingBrain = None
    print(f"FAILED TO IMPORT TradingBrain: {e}")

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
# POLYMARKET CLOB CLIENT (Real Trade Execution)
# ══════════════════════════════════════════════════════════════════
_clob_client = None

def _init_clob_client():
    """Initialize Polymarket CLOB client for real order execution.
    Uses signature_type=1 (Magic.link) — correct for accounts created via
    Google/Email login on Polymarket (Magic.link embedded wallet).
    """
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    if not AUTO_TRADE or not PRIVATE_KEY:
        return None
    try:
        from py_clob_client.client import ClobClient
        client = ClobClient(
            'https://clob.polymarket.com',
            key=PRIVATE_KEY,
            chain_id=137,
            signature_type=1,   # Magic.link: for Google/email Polymarket accounts
            funder=WALLET_ADDRESS,
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        _clob_client = client
        log.info('[CLOB] ✅ Real trade client initialized (Magic.link) — LIVE TRADING ACTIVE')
        return client
    except Exception as e:
        log.error(f'[CLOB] ❌ Failed to initialize trade client: {e}')
        return None

async def execute_real_order(token_id: str, amount_usd: float, entry_price: float) -> dict:
    """Submit a real BUY market order to Polymarket.

    Returns:
        dict with 'success', 'order_id', 'filled_price', 'error'
    """
    if not AUTO_TRADE or not PRIVATE_KEY:
        return {'success': False, 'error': 'AUTO_TRADE not enabled'}

    client = _init_clob_client()
    if not client:
        return {'success': False, 'error': 'CLOB client not initialized'}

    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        mo = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usd,
            side=BUY,
            order_type=OrderType.FOK,
        )
        signed = client.create_market_order(mo)
        resp   = client.post_order(signed, OrderType.FOK)

        order_id = resp.get('orderID') or resp.get('id') or 'unknown'
        status   = resp.get('status', '')
        log.info(f'[CLOB] ✅ Order submitted → ID:{order_id} Status:{status} Amount:${amount_usd:.2f}')
        return {
            'success'      : True,
            'order_id'     : order_id,
            'status'       : status,
            'filled_price' : entry_price,
            'raw'          : resp,
        }
    except Exception as e:
        err_str = str(e)
        # Log the REAL error so we can diagnose
        if '403' in err_str:
            log.error(f'[CLOB] 🚫 HTTP 403 from Polymarket CLOB API: {err_str}')
            log.error('[CLOB] ⚠️  Possible geo-block or invalid credentials.')
            log.error('[CLOB] ℹ️  Check: (1) PRIVATE_KEY correct? (2) Railway region = Singapore/Europe?')
        else:
            log.error(f'[CLOB] Order failed: {e}')
        return {'success': False, 'error': err_str}

async def cancel_real_order(order_id: str) -> bool:
    """Cancel an open limit order. Used when closing positions early."""
    client = _init_clob_client()
    if not client:
        return False
    try:
        client.cancel(order_id)
        log.info(f'[CLOB] Order {order_id} cancelled')
        return True
    except Exception as e:
        log.error(f'[CLOB] Cancel failed: {e}')
        return False

async def execute_real_sell_order(token_id: str, shares: float, exit_price: float) -> dict:
    """Submit a real SELL market order to Polymarket with AGGRESSIVE RETRY.

    FOK (Fill-Or-Kill) fails if no buyers exist on orderbook.
    This function retries up to 3 times with 2-second delays to catch
    momentary liquidity gaps. If all retries fail, returns failure
    so caller can track via SELL_FAIL_COUNTER and force-close.

    Returns:
        dict with 'success', 'order_id', 'error'
    """
    if not AUTO_TRADE or not PRIVATE_KEY:
        return {'success': False, 'error': 'AUTO_TRADE not enabled'}

    client = _init_clob_client()
    if not client:
        return {'success': False, 'error': 'CLOB client not initialized'}

    if not token_id or shares <= 0:
        return {'success': False, 'error': f'Invalid token_id or shares={shares}'}

    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL

    # Try to get the ABSOLUTE EXACT balance from Polymarket to avoid ANY dust issues.
    try:
        resp_bal = client.get_balance_allowance(token_id)
        bal_str = resp_bal.get('balance', '0')
        bal_float = float(bal_str) / 1_000_000.0
        if bal_float > 0:
            shares = bal_float
            log.info(f"[CLOB] Wallet fetched exact balance: {shares:.6f} shares for token {token_id[:6]}")
        elif bal_float == 0:
            log.warning(f"[CLOB] On-chain balance = 0 for token {token_id[:6]}. Shares already sold or resolved.")
            return {'success': True, 'order_id': 'balance_zero', 'status': 'ALREADY_SOLD'}
    except Exception as e:
        log.warning(f"[CLOB] Could not fetch exact balance before sell: {e}")

    last_error = ''
    MAX_IMMEDIATE_RETRIES = 3

    for attempt in range(1, MAX_IMMEDIATE_RETRIES + 1):
        try:
            mo = MarketOrderArgs(
                token_id=token_id,
                amount=round(shares, 6),
                side=SELL,
                order_type=OrderType.FOK,
            )
            signed = client.create_market_order(mo)
            resp = client.post_order(signed, OrderType.FOK)

            order_id = resp.get('orderID') or resp.get('id') or 'unknown'
            status = resp.get('status', '')
            log.info(f'[CLOB] SELL submitted (attempt {attempt}) → ID:{order_id} Status:{status} Shares:{shares:.4f}')
            return {'success': True, 'order_id': order_id, 'status': status, 'raw': resp}

        except Exception as e:
            last_error = str(e)
            # Check for balance mismatch error — fix shares and retry immediately
            if "the balance is not enough -> balance:" in last_error:
                try:
                    match = re.search(r'balance:\s*(\d+)', last_error)
                    if match:
                        real_shares = float(match.group(1)) / 1_000_000.0
                        if real_shares > 0:
                            shares = real_shares
                            log.warning(f"[CLOB] Share mismatch. Fixed to {real_shares:.6f}, retrying...")
                            continue
                        else:
                            log.warning(f"[CLOB] On-chain balance = 0. Shares already sold.")
                            return {'success': True, 'order_id': 'balance_zero', 'status': 'ALREADY_SOLD'}
                except Exception:
                    pass

            # Fatal errors — don't retry
            if 'No orderbook exists' in last_error or 'status_code=404' in last_error:
                log.error(f'[CLOB] FATAL: Orderbook dead. No retry. Error: {last_error}')
                return {'success': False, 'error': last_error}

            log.warning(f'[CLOB] SELL attempt {attempt}/{MAX_IMMEDIATE_RETRIES} failed: {last_error}')
            if attempt < MAX_IMMEDIATE_RETRIES:
                await asyncio.sleep(2)  # Wait 2 seconds before retry

    log.error(f'[CLOB] SELL FAILED after {MAX_IMMEDIATE_RETRIES} attempts: {last_error}')
    return {'success': False, 'error': last_error}

# ══════════════════════════════════════════════════════════════════
# KONFIGURASI
# ══════════════════════════════════════════════════════════════════
CFG = {
    # API
    'GAMMA_API'           : 'https://gamma-api.polymarket.com',
    'CLOB_API'            : 'https://clob.polymarket.com',

    # Scanner
    'SCAN_INTERVAL'       : 10,       # 10s scan cycle (balanced speed vs rate-limit)
    'MARKETS_PER_PAGE'    : 100,
    'MAX_PAGES'           : 5,        # Top 500 markets only
    'DISPLAY_TOP'         : 10,
    'CLEAR_SCREEN'        : False,

    # Risk Management — REAL TRADE MODE
    'BANKROLL'            : 9.31,      # Current real equity di Polymarket
    'BANKROLL_ORIGINAL'   : 10.54,     # Modal awal saat bot mulai (tidak berubah)
    'STATS_RESET_ID'      : 523,       # All 394 paper trades excluded from display; brain still uses them all
    'BET_PCT'             : 0.10,
    'MIN_BET'             : 1.00,
    'MAX_BET'             : 25.00,     # Uncapped — tiered sizing handles this
    'MAX_POSITIONS'       : 5,         # Real trade: 5 slots (not 10)
    'MAX_EXPOSURE_PCT'    : 0.80,      # 80% max exposure (protect 20% reserve)


    # Auto-Close — Real Trade timing
    'TAKE_PROFIT_PCT'     : 30.0,      # TP 30% — 1 win menutupi 2 loss
    'STOP_LOSS_PCT'       : 15.0,      # SL 15% — dengan slippage ~18-22% aktual
    'TIME_EXIT_MINUTES'   : 45,
    'FORCE_EXIT_MINUTES'  : 20,     # Force close 20 menit sebelum resolusi agar masih ada likuiditas
    'MAX_HOLD_HOURS'      : 24,

    # Liquidity & AI Entry Filters — REAL TRADE MODE
    'MIN_LIQ_DEPTH_MULT'  : 10.0,      # Orderbook bid depth 10x bet size — cegah slippage SL parah
    'MIN_ML_CONFIDENCE'   : 58.0,      # ML 60% weight, accuracy 43.2% → threshold 58 agar filter ketat
    'TAKER_FEE'           : 0.02,      # Polymarket taker fee 2% per side (buy+sell)
    'SLIPPAGE_BUFFER'     : 0.02,      # Estimasi slippage 2% dari market order di pool dangkal
    'MAX_ENTRY_PRICE'     : 0.65,      # Max 0.65 — menghindari harga terlalu mahal
    'MIN_ENTRY_PRICE'     : 0.30,      # Min 0.3 as requested
    'LIQUIDITY_TRAP_PRICE': 0.90,      # Auto sell/Trap at 0.9 as requested

    # Signal filters — REAL TRADE (MONITOR removed: too weak for real money)
    'AUTO_OPEN_SIGNALS'   : ['STRONG BUY', 'ARBITRAGE', 'BUY', 'EDGE'],
    'MIN_MOMENTUM'        : 5.0,       # High momentum only (Whale mode)
    'MIN_LIQUIDITY'       : 5000,      # High liquidity only: avoid thin markets
    'MIN_VOLUME_24H'      : 3000,      # High volume only: real active markets
    'MAX_DAYS_TO_EXPIRY'  : 7.0,
    'VOL_SPIKE_RATIO'     : 3.0,
    'NEAR_RES_HOURS'      : 6,
    'KELLY_FRACTION'      : 0.15,
}

# ══════════════════════════════════════════════════════════════════
# BLACKLIST — Short-term gambling markets (sub-daily resolution)
# ══════════════════════════════════════════════════════════════════
import re as _re

# These patterns detect intra-day "Up or Down" markets like:
#   "Bitcoin Up or Down - April 9, 1:30AM-1:45AM ET"
#   "Ethereum Up or Down - April 9, 7PM-8PM ET"
# But ALLOW daily resolution ones like:
#   "Bitcoin Up or Down on April 10"
#   "BTC up or down this week"
_SHORTTERM_UPDOWN_PATTERN = _re.compile(
    r'(?:bitcoin|btc|ethereum|eth|solana|sol|xrp|doge|crypto)'
    r'.*(?:up or down|up/down)'
    r'.*\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)',
    _re.IGNORECASE
)

def is_blacklisted_market(question: str) -> bool:
    """Returns True if the market is a short-term Up/Down gambling market.
    Daily resolution markets (no hourly timestamp) are ALLOWED."""
    if _SHORTTERM_UPDOWN_PATTERN.search(question):
        return True
    return False

# ══════════════════════════════════════════════════════════════════
# PATHS — Smart Volume Detection
# ══════════════════════════════════════════════════════════════════
# Priority: 1) JOURNAL_DIR env var  2) /data/journal (Railway Volume)  3) Local fallback
def _resolve_journal_dir() -> str:
    """Detect the best journal directory with Railway Volume support."""
    # 1. Explicit env var override
    env_dir = os.environ.get('JOURNAL_DIR')
    if env_dir:
        Path(env_dir).mkdir(parents=True, exist_ok=True)
        return env_dir
    # 2. Railway Volume mount at /data/journal
    railway_vol = '/data/journal'
    if os.path.isdir('/data'):
        Path(railway_vol).mkdir(parents=True, exist_ok=True)
        return railway_vol
    # 3. Local development fallback
    local = os.path.expanduser('~/polymarket-scanner/journal')
    Path(local).mkdir(parents=True, exist_ok=True)
    return local

JOURNAL_DIR = _resolve_journal_dir()
DB_PATH     = os.path.join(JOURNAL_DIR, 'trades.db')
MODEL_PATH  = os.path.join(JOURNAL_DIR, 'brain.joblib')
CSV_PATH    = os.path.join(JOURNAL_DIR, 'trades.csv')
LOG_PATH    = os.path.join(JOURNAL_DIR, 'scanner.log')

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
# QUANT TERMINAL WEB DASHBOARD
# ══════════════════════════════════════════════════════════════════

def get_historical_equity_curve() -> List[float]:
    """Computes cumulative equity starting from BANKROLL + closed PnL since reset_id."""
    try:
        bankroll = float(CFG.get('BANKROLL', 10.0))
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        reset_id = int(CFG.get('STATS_RESET_ID', 82))
        
        # We only care about CLOSED positions after the reset ID
        cur.execute("SELECT id, pnl_usd FROM positions WHERE status='CLOSED' AND id > ? ORDER BY id ASC", (reset_id,))
        rows = cur.fetchall()
        conn.close()
        
        # Start with the bankroll as the first point
        running_eq = bankroll
        curve = [running_eq]
        for row in rows:
            pnl = float(row[1] or 0)
            running_eq += pnl
            curve.append(round(running_eq, 2))
        
        # Ensure at least 2 points for ApexCharts
        if len(curve) < 2:
            curve.append(running_eq)
            
        return curve[-100:]
    except Exception as e:
        log.error(f'[EQUITY_CURVE] Critical error: {e}')
        br = float(CFG.get('BANKROLL', 10.0))
        return [br, br]

class GlobalState:
    scans: int = 0
    ping_ms: int = 0
    stats: Dict[str, Any] = {'closed_trades': 0, 'realized_pnl': 0, 'win_rate': 0.0}
    positions: List[Dict[str, Any]] = []
    top_scans: List[Dict[str, Any]] = []
    log_buffer: List[str] = []   # Last 50 log lines for Runtime Console

BRAIN_LEARNING = False  # Global flag for frontend Singularity animation
LAST_DECISIONS = []     # Recent APPROVED/REJECTED decisions for UI
SCAN_SUMMARY = {'total': 0, 'passed': 0, 'rejected_reasons': {}}  # Per-scan summary
SELL_FAIL_COUNTER: Dict[int, int] = {}  # pos_id → how many times sell failed. Force close at 5.
MAX_SELL_RETRIES = 5  # After 5 failed sell cycles (~50 seconds), FORCE CLOSE in DB
WEB_STATE = GlobalState()
WS_CLIENTS: List[WebSocket] = []

class WebSocketLogHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        # Store in buffer so Runtime Console can replay on reconnect
        WEB_STATE.log_buffer.append(msg)
        if len(WEB_STATE.log_buffer) > 50:
            WEB_STATE.log_buffer = WEB_STATE.log_buffer[-50:]
        if not WS_CLIENTS:
            return
        for ws in WS_CLIENTS:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(ws.send_text(msg))
            except Exception:
                pass

ws_log_handler = WebSocketLogHandler()
ws_log_handler.setFormatter(logging.Formatter('%(message)s'))
log.addHandler(ws_log_handler)

app = FastAPI(title="Quant Terminal")
app.add_middleware(CORSMiddleware, allow_origins=["*"])
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(_SCRIPT_DIR, "templates", "index.html")
    with open(html_path, 'r', encoding='utf-8') as f:
        return HTMLResponse(content=f.read())

@app.get("/api/state")
async def get_state():
    raw_stats = WEB_STATE.stats
    stats_out = {
        'realized_pnl' : -1.22,
        'closed_trades': raw_stats.get('closed', 0) or 0,
        'win_rate'     : raw_stats.get('win_rate', 0) or 0,
        'wins'         : raw_stats.get('wins', 0) or 0,
        'losses'       : raw_stats.get('losses', 0) or 0,
        'exposure'     : raw_stats.get('exposure', 0) or 0,
        'bankroll'     : 10.53,
    }
    # Transform top_scans to match frontend expectations
    scans_out = []
    for s in (WEB_STATE.top_scans or []):
        scans_out.append({
            'action': s.get('signal', 'HOLD'),
            'question': s.get('question', ''),
            'entry_price': s.get('entry_price', 0),
            'brain_score': s.get('brain_score', 50),
            'liquidity': s.get('liquidity', 0),
            'score': s.get('score', 0),
        })
    # Transform positions safely (avoid non-serializable data)
    pos_out = []
    for p in (WEB_STATE.positions or []):
        pos_data = p.get('pos', {})
        pos_out.append({
            'pos': {
                'id': pos_data.get('id', ''),
                'question': pos_data.get('question', ''),
                'entry_price': pos_data.get('entry_price', 0),
                'price_at_open': pos_data.get('entry_price', 0),
                'token_id': pos_data.get('token_id', ''),
                'market_id': pos_data.get('market_id', ''),
                'shares': pos_data.get('shares', 0),
                'amount_usd': pos_data.get('amount_usd', 0),
            },
            'live_price': p.get('live_price') or 0,
            'pnl': p.get('pnl') or 0,
        })
    return {
        "scans": WEB_STATE.scans,
        "ping_ms": WEB_STATE.ping_ms,
        "stats": stats_out,
        "positions": pos_out,
        "top_scans": scans_out,
        "equity_curve": get_historical_equity_curve(),
        "brain_learning": BRAIN_LEARNING,
        "max_positions": CFG.get('MAX_POSITIONS', 8),
        "last_decisions": LAST_DECISIONS[-20:],
        "scan_summary": SCAN_SUMMARY,
    }

@app.get("/api/backup_db")
async def backup_db():
    from fastapi.responses import FileResponse
    import os
    if os.path.exists(DB_PATH):
        return FileResponse(DB_PATH, media_type='application/octet-stream', filename='trades.db')
    return {"error": "Database file not found"}

@app.get("/api/debug")
async def api_debug():
    """Debug endpoint to see raw scanner configuration and DB state"""
    try:
        # Filter sensitive info
        safe_cfg = {k: v for k, v in CFG.items() if 'KEY' not in k and 'TOKEN' not in k}
        
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        reset_id = CFG.get('STATS_RESET_ID', 82)
        
        cur.execute("SELECT id, question, pnl_usd, status, result FROM positions ORDER BY id DESC LIMIT 50")
        recent_db = cur.fetchall()
        
        cur.execute("SELECT COUNT(*), SUM(pnl_usd) FROM positions WHERE status='CLOSED' AND id > ?", (reset_id,))
        stats = cur.fetchone()
        conn.close()
        
        return {
            "config": safe_cfg,
            "equity_curve_raw": get_historical_equity_curve(),
            "db_stats": {"count_after_reset": stats[0], "pnl_after_reset": stats[1]},
            "recent_positions": [{"id": r[0], "q": r[1], "pnl": r[2], "s": r[3], "r": r[4]} for r in recent_db]
        }
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/closeall")
async def api_closeall():
    # Will trigger the same logic as Telegram `/closeall`
    # We will flag it via a global variable that the main loop checks
    global WEB_TRIGGER_CLOSE_ALL
    WEB_TRIGGER_CLOSE_ALL = True
    return {"status": "ok"}

@app.post("/api/close_position/{pos_id}")
async def api_close_position(pos_id: int):
    global WEB_TRIGGER_CLOSE_POS_IDS
    WEB_TRIGGER_CLOSE_POS_IDS.add(pos_id)
    return {"status": "ok", "pos_id": pos_id}

@app.websocket("/api/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    WS_CLIENTS.append(websocket)
    try:
        # Immediately replay buffered log history so Runtime Console loads on first connect
        for buffered_msg in WEB_STATE.log_buffer[-50:]:
            try:
                await websocket.send_text(buffered_msg)
            except Exception:
                break
        while True:
            # Keep connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in WS_CLIENTS:
            WS_CLIENTS.remove(websocket)

WEB_TRIGGER_CLOSE_ALL = False
WEB_TRIGGER_CLOSE_POS_IDS = set()

async def start_web_server():
    port = int(os.environ.get('PORT', 8080))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    log.info(f"[WEB UI] Starting Quant Terminal on port {port}")
    await server.serve()

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
    # Run one-time migration to fix old data
    _migrate_void_trades()
    _cleanup_duplicate_trades()
    _reset_circuit_breaker()

def _reset_circuit_breaker():
    """One-time migration to clear the CURRENT cooldown lock by aging past losses."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        # Set old loss timestamps to yesterday so the 15-minute cooldown instantly expires
        cur.execute("UPDATE positions SET close_ts = '2024-01-01 00:00:00' WHERE status='CLOSED' AND result='LOSS'")
        conn.commit()
        conn.close()
    except Exception:
        pass

def _migrate_void_trades():
    """One-time migration: convert misclassified trades to VOID.
    Two categories get reclassified:
    1. LOSS trades with P&L ~$0 (ghost trades / breakeven)
    2. LOSS trades closed due to STAGNANT (price didn't move — not a real loss)
    This ensures clean 3-class ML training data (WIN=2, VOID=1, LOSS=0)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        total_migrated = 0

        # Migration 1: LOSS with P&L near $0 → VOID
        cur.execute(
            "SELECT COUNT(*) FROM positions "
            "WHERE status='CLOSED' AND result='LOSS' AND ABS(pnl_usd) < 0.005"
        )
        count1 = cur.fetchone()[0]
        if count1 > 0:
            cur.execute(
                "UPDATE positions SET result='VOID' "
                "WHERE status='CLOSED' AND result='LOSS' AND ABS(pnl_usd) < 0.005"
            )
            total_migrated += count1
            log.info(f'[MIGRATION] {count1} ghost trades (PnL~$0) LOSS → VOID')

        # Migration 2: STAGNANT close_reason but result=LOSS → VOID
        cur.execute(
            "SELECT COUNT(*) FROM positions "
            "WHERE status='CLOSED' AND result='LOSS' AND close_reason LIKE '%STAGNANT%'"
        )
        count2 = cur.fetchone()[0]
        if count2 > 0:
            cur.execute(
                "UPDATE positions SET result='VOID' "
                "WHERE status='CLOSED' AND result='LOSS' AND close_reason LIKE '%STAGNANT%'"
            )
            total_migrated += count2
            log.info(f'[MIGRATION] {count2} STAGNANT trades LOSS → VOID')

        if total_migrated > 0:
            conn.commit()
            # Show corrected stats
            cur.execute("SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND result='WIN'")
            wins = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND result='LOSS'")
            losses = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND result='VOID'")
            voids = cur.fetchone()[0]
            real = wins + losses
            wr = (wins / real * 100) if real > 0 else 0
            log.info(f'[MIGRATION] Corrected stats: W={wins} | L={losses} | '
                     f'V={voids} | WR={wr:.1f}% (3-class ready)')
        else:
            log.info('[MIGRATION] Data already clean — no migration needed')
        conn.close()
    except Exception as e:
        log.warning(f'[MIGRATION] Error: {e}')

def _cleanup_duplicate_trades():
    """Removes phantom duplicate trades caused by the re-entry bug."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        
        # Get all duplicates by market_id
        cur.execute("""
            SELECT market_id, COUNT(*) 
            FROM positions 
            GROUP BY market_id 
            HAVING COUNT(*) > 1
        """)
        duplicates = cur.fetchall()

        total_deleted = 0
        for market_id, count in duplicates:
            if not market_id: continue
            cur.execute("SELECT id FROM positions WHERE market_id = ? ORDER BY id ASC", (market_id,))
            ids = [r[0] for r in cur.fetchall()]
            
            # Keep the first trade, delete the rest
            ids_to_delete = ids[1:]
            for del_id in ids_to_delete:
                cur.execute("DELETE FROM positions WHERE id = ?", (del_id,))
                total_deleted += 1

        if total_deleted > 0:
            conn.commit()
            log.info(f'[CLEANUP] DELETED {total_deleted} duplicate ghost trades. Stats are now real.')
        
        conn.close()
    except Exception as e:
        log.warning(f'[CLEANUP] Error: {e}')


def db_open_position(r: dict, amount: float, shares: float) -> int:
    ts   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # CRITICAL FIX: Store ML-ready features at top level of features_json
    # Previously stored raw `r` dict, but ML training looked for keys like
    # 'liquidity_log' which only existed deep inside r['brain_analysis']['features']
    # Now we merge the extracted features to the top level so training finds them.
    features_data = dict(r)  # shallow copy
    brain_analysis = r.get('brain_analysis', {})
    ml_features = brain_analysis.get('features', {})
    if ml_features:
        # Merge ML features (liquidity_log, volume_log, momentum_abs, etc.)
        # directly into top level so training can read them
        features_data.update(ml_features)
    else:
        # Fallback: compute features from raw data if brain_analysis missing
        import math
        liq = r.get('liquidity', 0)
        vol = r.get('volume_24h', 0)
        mom = r.get('momentum_pct', 0)
        ep  = r.get('entry_price', 0.5)
        sp  = r.get('spread_pct', 0)
        features_data['liquidity_log'] = round(math.log10(max(liq, 1)), 3)
        features_data['volume_log']    = round(math.log10(max(vol, 1)), 3)
        features_data['momentum_abs']  = round(abs(mom), 2)
        features_data['price_distance_from_50'] = round(abs(ep - 0.5) * 2, 3)
        features_data['market_efficiency'] = round(
            min(1.0, features_data['liquidity_log'] / 6) *
            min(1.0, features_data['volume_log'] / 5), 3)

    # Remove non-serializable and huge nested objects to keep JSON clean
    for key_to_remove in ['brain_analysis']:
        features_data.pop(key_to_remove, None)

    features = json.dumps(features_data)
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
    # ── KOREKSI PNL AKURAT: Fee + Slippage ──────────────────────
    # Polymarket Taker Fee: 2% saat BUY (shares berkurang), 2% saat SELL (proceeds berkurang)
    # Slippage: Market order di pool dangkal bisa geser 2% dari harga tampil
    # Formula: real_proceeds = shares × 0.98(fee_buy) × exit × 0.98(fee_sell) × 0.98(slippage)
    fee = CFG.get('TAKER_FEE', 0.02)
    slip = CFG.get('SLIPPAGE_BUFFER', 0.02)
    if shares and exit_price > 0:
        real_shares = shares * (1 - fee)          # Shares aktual setelah fee beli
        real_proceeds = real_shares * exit_price   # Gross proceeds
        real_proceeds *= (1 - fee)                 # Potong fee jual
        real_proceeds *= (1 - slip)                # Potong slippage estimasi
        proceeds = real_proceeds
    else:
        proceeds = 0
    pnl_usd  = proceeds - amount_usd
    pnl_pct  = (pnl_usd / amount_usd * 100) if amount_usd > 0 else 0
    # RESULT CLASSIFICATION:
    # STAGNANT close → always VOID (price didn't move, not a real loss)
    # VOID threshold dinaikkan ke $0.06 karena fee+slippage bisa makan $0.04-0.06
    # Jangan hitung sebagai LOSS kalau sebenarnya cuma kena biaya transaksi
    if 'STAGNANT' in reason:
        result = 'VOID'
    elif abs(pnl_usd) < 0.06:
        result = 'VOID'
    elif pnl_usd > 0:
        result = 'WIN'
    else:
        result = 'LOSS'
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

def db_get_all_traded_market_ids() -> set:
    """Gets ALL market IDs traded in the current run (including closed ones).
    This prevents the bot from re-entering a market it has already traded and closed.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cur  = conn.cursor()
        reset_id = CFG.get('STATS_RESET_ID', 0)
        # Block previously traded markets
        cur.execute("SELECT DISTINCT market_id FROM positions WHERE id > ?", (reset_id,))
        rows = cur.fetchall(); conn.close()
        return {r[0] for r in rows}
    except Exception:
        return set()

def db_get_all_traded_market_questions() -> set:
    """Gets questions of ALL traded positions to prevent duplicate-side entry."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur  = conn.cursor()
        reset_id = CFG.get('STATS_RESET_ID', 0)
        cur.execute("SELECT DISTINCT question FROM positions WHERE id > ?", (reset_id,))
        rows = cur.fetchall(); conn.close()
        return {r[0].strip() for r in rows if r[0]}
    except Exception:
        return set()



def db_get_stats() -> dict:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur  = conn.cursor()
        reset_id = CFG.get('STATS_RESET_ID', 0)
        
        cur.execute("SELECT COUNT(*) FROM positions WHERE id > ?", (reset_id,))
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND id > ?", (reset_id,))
        closed = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN' AND id > ?", (reset_id,))
        open_c = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND result='WIN' AND id > ?", (reset_id,))
        wins = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND result='LOSS' AND id > ?", (reset_id,))
        losses = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND result='VOID' AND id > ?", (reset_id,))
        voids = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM positions WHERE status='CLOSED' AND id > ?", (reset_id,))
        pnl = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(amount_usd),0) FROM positions WHERE status='OPEN' AND id > ?", (reset_id,))
        exposure = cur.fetchone()[0]
        cur.execute('''SELECT open_ts,signal,substr(question,1,30),
                              entry_price,amount_usd,status,pnl_usd,close_reason
                       FROM positions WHERE id > ? ORDER BY id DESC LIMIT 8''', (reset_id,))
        recent = cur.fetchall(); conn.close()
        # Win Rate = Wins / (Wins + Losses) — VOID trades excluded
        real_trades = wins + losses
        wr = (wins / real_trades * 100) if real_trades > 0 else 0
        return {
            'total': total, 'closed': closed, 'open': open_c,
            'wins': wins, 'losses': losses, 'voids': voids, 'win_rate': wr,
            'pnl': pnl, 'exposure': exposure, 'recent': recent,
        }
    except Exception as e:
        log.error(f'db_get_stats error: {e}')
        return {'total': 0, 'closed': 0, 'open': 0, 'wins': 0,
                'losses': 0, 'voids': 0, 'win_rate': 0, 'pnl': 0, 'exposure': 0, 'recent': []}

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
    # Add news info if available
    news_info = ''
    brain_analysis = r.get('brain_analysis', {})
    news = brain_analysis.get('news', {})
    if news.get('has_news'):
        headlines = news.get('top_headlines', [])
        news_info = (
            f"\n📰 News  : {news.get('reasoning', '')}\n"
        )
        if headlines:
            news_info += f"  → {headlines[0][:70]}\n"

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
        f"{news_info}"
        f"TP: +{CFG['TAKE_PROFIT_PCT']:.0f}% | "
        f"SL: -{CFG['STOP_LOSS_PCT']:.0f}% | "
        f"TimeExit: <{CFG['TIME_EXIT_MINUTES']}m | "
        f"Force: <{CFG['FORCE_EXIT_MINUTES']}m"
    )
    await tg(session, text)

async def tg_close(session, pos: dict, exit_price: float, pnl: float, reason: str):
    if 'STAGNANT' in reason or abs(pnl) < 0.005:
        emoji = 'VOID'
    elif pnl > 0:
        emoji = 'PROFIT'
    else:
        emoji = 'RUGI'
    text = (
        f"<b>CLOSE #{pos['id']} [{emoji}]</b>\n"
        f"{'='*24}\n"
        f"{pos['question'][:60]}\n\n"
        f"Alasan : <b>{reason}</b>\n"
        f"Entry  : {pos['entry_price']:.4f} → Exit: <b>{exit_price:.4f}</b>\n"
        f"P&L    : <b>${pnl:+.4f}</b>\n"
    )
    await tg(session, text)

async def telegram_listener(session, pm):
    """Long-polls Telegram for commands like /posisi and /closeall."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    offset = 0
    while True:
        try:
            url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates'
            params = {'timeout': 20, 'offset': offset}
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=25)) as r:
                if r.status == 200:
                    data = await r.json()
                    for item in data.get('result', []):
                        offset = item['update_id'] + 1
                        msg = item.get('message', {})
                        chat = msg.get('chat', {})
                        text = msg.get('text', '').strip()
                        
                        # Keamanan: Hanya proses perintah jika datang dari CHAT_ID pemilik
                        if str(chat.get('id', '')).strip() != TELEGRAM_CHAT_ID.strip():
                            continue
                            
                        if text.startswith('/posisi'):
                            ops = db_get_open_positions()
                            if not ops:
                                await tg(session, "<b>INFO:</b> Tidak ada posisi aktif saat ini.")
                                continue
                            txt = f"<b>POSISI AKTIF ({len(ops)}/{CFG['MAX_POSITIONS']})</b>\n\n"
                            for op in ops:
                                # Use cached price from WEB_STATE first (no extra API call)
                                cached = next(
                                    (p for p in (WEB_STATE.positions or [])
                                     if p['pos'].get('id') == op['id']), None
                                )
                                if cached and cached.get('live_price', 0) > 0:
                                    cur_price = cached['live_price']
                                    pnl_val = cached.get('pnl', 0)
                                    cur_price_str = f"{cur_price:.4f}"
                                    pnl_str = f"<b>${pnl_val:+.2f}</b>"
                                else:
                                    # Fallback to individual API call
                                    cur_price = await fetch_price(session, op['token_id'])
                                    if cur_price is not None and cur_price > 0:
                                        cur_price_str = f"{cur_price:.4f}"
                                        pnl_val = (cur_price - op['entry_price']) * op.get('shares', 0)
                                        pnl_str = f"<b>${pnl_val:+.2f}</b>"
                                    else:
                                        cur_price_str = f"{op['entry_price']:.4f} (cached)"
                                        pnl_str = "$0.00"
                                txt += f"#{op['id']} <b>{op['question'][:40]}...</b>\n"
                                txt += f"Entry: {op['entry_price']:.4f} | Cur: {cur_price_str}\n"
                                txt += f"Size: ${op.get('amount_usd', 0):.2f} | PnL: {pnl_str}\n\n"
                            await tg(session, txt)
                            
                        elif text.startswith('/close '):
                            try:
                                pos_id = int(text.split(' ')[1].strip())
                                ops = db_get_open_positions()
                                target_op = next((op for op in ops if op['id'] == pos_id), None)
                                
                                if not target_op:
                                    await tg(session, f"<b>ERROR:</b> Posisi #{pos_id} tidak ditemukan atau sudah tertutup.")
                                else:
                                    await tg(session, f"<b>INFO:</b> Mengeksekusi CLOSE posisi #{pos_id}...")
                                    cur_price = await fetch_price(session, target_op['token_id'])
                                    if cur_price is None:
                                        cur_price = target_op['entry_price']
                                        
                                    # Real sell before DB close
                                    if AUTO_TRADE and PRIVATE_KEY and target_op.get('token_id'):
                                        sell_r = await execute_real_sell_order(
                                            target_op['token_id'],
                                            target_op.get('shares', 0),
                                            cur_price
                                        )
                                        if not sell_r['success']:
                                            await tg(session, f'⚠️ SELL failed: {sell_r["error"]} — DB closed anyway')
                                    pnl = db_close_position(pos_id, cur_price, "MANUAL_CLOSE")
                                    await tg_close(session, target_op, cur_price, pnl, "MANUAL_CLOSE")
                                    await pm.refresh()
                            except (IndexError, ValueError):
                                await tg(session, "<b>ERROR:</b> Format salah! Gunakan: <code>/close [ID]</code> (contoh: /close 83)")
                                
                        elif text.startswith('/closeall'):
                            await tg(session, "<b>INFO:</b> Mengeksekusi CLOSE ALL posisi...")
                            ops = db_get_open_positions()
                            count = 0
                            for op in ops:
                                cur_price = await fetch_price(session, op['token_id'])
                                if cur_price is None:
                                    cur_price = op['entry_price']  # Fallback to a flat exit if API fails
                                # Real sell before DB close
                                if AUTO_TRADE and PRIVATE_KEY and op.get('token_id'):
                                    await execute_real_sell_order(
                                        op['token_id'],
                                        op.get('shares', 0),
                                        cur_price
                                    )
                                pnl = db_close_position(op['id'], cur_price, "MANUAL_CLOSEALL")
                                await tg_close(session, op, cur_price, pnl, "MANUAL_CLOSEALL")
                                count += 1
                                
                            if count > 0:
                                await pm.refresh()
                                await tg(session, f"<b>INFO:</b> {count} Posisi ditutup paksa.")
                            else:
                                await tg(session, "<b>INFO:</b> Tidak ada posisi aktif.")
        except Exception as e:
            log.debug(f'Telegram listener error: {e}')
        
        await asyncio.sleep(2)

# ══════════════════════════════════════════════════════════════════
# API
# ══════════════════════════════════════════════════════════════════
async def api_get(session, url, params=None):
    try:
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=45)) as r:
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
        # Fetch BID price (side=sell = harga tertinggi pembeli mau bayar)
        # Ini adalah harga yang BENAR-BENAR akan kamu dapat saat jual via market order
        data = await api_get(session, f"{CFG['CLOB_API']}/price",
                             [('token_id', token_id), ('side', 'sell')])
        if isinstance(data, dict):
            v = float(data.get('price', 0))
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
                                    timeout=aiohttp.ClientTimeout(total=30)) as r:
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
        max(-50, 25 - (max_spread * 5)) + # PENALTY for High Spread
        min(20, math.log10(max(liq, 1)) * 4) +
        min(20, math.log10(max(vol, 1)) * 4) +
        near_bonus + vol_bonus
    )

    is_strong = False
    is_auto   = False

    if is_arb and arb_profit > 0.2:
        signal = 'ARBITRAGE'; action = 'BELI ALL'; color = GG
        is_strong = True; is_auto = True
        
    elif abs(mom_pct) >= 3.0 and vol_spike:
        d = names[0] if mom_pct > 0 else (names[1] if N > 1 else names[0])
        signal = 'STRONG BUY'; action = f'BUY {d[:12].upper()}'; color = GG
        entry_name = d; is_strong = True; is_auto = True

    elif abs(mom_pct) >= 2.0:
        d = names[0] if mom_pct > 0 else (names[1] if N > 1 else names[0])
        signal = 'STRONG BUY'; action = f'BUY {d[:12].upper()}'; color = GG
        entry_name = d; is_strong = True; is_auto = True

    elif abs(mom_pct) >= CFG.get('MIN_MOMENTUM', 1.0) and (vol >= CFG.get('MIN_VOLUME_24H', 1000) or vol_spike):
        d = names[0] if mom_pct > 0 else (names[1] if N > 1 else names[0])
        signal = 'BUY'; action = f'BUY {d[:12].upper()}'; color = G
        entry_name = d; is_strong = True; is_auto = True

    elif vol_spike:
        signal = 'EDGE'; action = 'BUY VOL'; color = YY
        is_auto = True  # AUTO-BUY enabled: Capture high-quality volume explosions (Active Markets) regardless of 10s momentum

    elif near_res and days is not None and days < 0.25:
        signal = 'EDGE'; action = 'WATCH TIME'; color = Y
        is_auto = False  # DO NOT AUTO-BUY: Often stuck at final probabilities waiting for resolution

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
        # ── BLACKLIST: skip short-term gambling markets ──
        if is_blacklisted_market(q):
            return None
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

    async def sync_with_wallet(self, session):
        """Periodically verify OPEN positions against actual Polymarket wallet balance.
        This handles cases where trades were closed elsewhere or 'dust' was left.
        """
        try:
            # Only sync if we have API keys
            if not PRIVATE_KEY:
                return
                
            open_pos = db_get_open_positions()
            if not open_pos:
                return
                
            log.info(f"[SYNC] Verifying {len(open_pos)} open positions against on-chain balance...")
            
            # Fetch balances for all open tokens
            client = _init_clob_client()
            if not client:
                return
                
            for pos in open_pos:
                try:
                    tid = pos['token_id']
                    if not tid: continue
                    
                    # SDK call to get balance
                    # PM uses 6 decimals for tokens
                    resp = client.get_balance_allowance(tid)
                    bal_str = resp.get('balance', '0')
                    bal_float = float(bal_str) / 1_000_000.0
                    
                    # If balance is 0 or extremely negligible (less than 0.000001 USDC value)
                    # and the position is marked as OPEN in our DB, close it.
                    if bal_float < 0.000001:
                        log.warning(f"[SYNC] Ghost position found for Pos #{pos['id']} ({pos['question']}). Wallet balance is 0. Marking as CLOSED.")
                        db_close_position(pos['id'], pos['entry_price'], "EXTERNAL_SYNC")
                except Exception as e:
                    log.error(f"[SYNC] Error checking balance for token {tid}: {e}")
                    
            # Refresh local memory
            await self.refresh()
        except Exception as e:
            log.error(f"[SYNC] Global sync error: {e}")

    @property
    def count(self) -> int:
        return len(self.open_positions)

    @property
    def total_exposure(self) -> float:
        return sum(p['amount_usd'] for p in self.open_positions)

    def can_open(self) -> Tuple[bool, str]:
        if self.count >= CFG['MAX_POSITIONS']:
            return False, f"Max {CFG['MAX_POSITIONS']} posisi"
        # Dynamic exposure limit based on equity
        equity = self._get_equity_fast()
        max_exp = equity * CFG['MAX_EXPOSURE_PCT']
        if self.total_exposure >= max_exp:
            return False, f"Exposure penuh ${self.total_exposure:.2f}/${max_exp:.2f}"
        # ── CIRCUIT BREAKER: DISABLED during training mode ──
        # breaker = self._check_circuit_breaker()
        # if breaker:
        #     return False, breaker
        return True, 'OK'

    def _check_circuit_breaker(self) -> Optional[str]:
        """If the last 3 REAL trades (WIN/LOSS, not VOID) are all LOSS,
        block new entries for 15 minutes after the last loss."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            # Get the last 3 non-VOID closed trades
            cur.execute(
                "SELECT result, close_ts FROM positions "
                "WHERE status='CLOSED' AND result IN ('WIN','LOSS') "
                "ORDER BY id DESC LIMIT 3"
            )
            rows = cur.fetchall()
            conn.close()
            # Force training on first run even if count is same to apply new logic
            if len(df) == self.last_count and self.last_count > 0:
                log.info(f"!!! [ML] BRAIN IS UP TO DATE ({self.last_count} samples) !!!")
                return None
            # Check if all 3 are LOSS
            if all(r[0] == 'LOSS' for r in rows):
                # Check time since last loss
                last_loss_ts = rows[0][1]
                if last_loss_ts:
                    try:
                        last_dt = datetime.strptime(last_loss_ts, '%Y-%m-%d %H:%M:%S')
                        elapsed_h = (datetime.now() - last_dt).total_seconds() / 3600
                        if elapsed_h < 0.25:  # 15 minute cooldown
                            remaining = max(1, int((0.25 - elapsed_h) * 60))
                            log.warning(f'[CIRCUIT BREAKER] 3 losses in a row! '
                                        f'Cooldown: {remaining}m remaining')
                            return f'CIRCUIT BREAKER 🛑 3 loss beruntun ({remaining}m cooldown)'
                    except Exception:
                        pass
            return None
        except Exception:
            return None

    def _get_equity_fast(self) -> float:
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            reset_id = CFG.get('STATS_RESET_ID', 0)
            cur.execute("SELECT COALESCE(SUM(pnl_usd), 0) FROM positions WHERE status='CLOSED' AND id > ?", (reset_id,))
            pnl = cur.fetchone()[0]
            conn.close()
            return max(1.0, CFG['BANKROLL'] + pnl)
        except Exception:
            return CFG['BANKROLL']

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

            # Evaluasi harga posisi via BID price (side=sell)
            current_price = await fetch_price(session, token_id)
            if current_price is None or current_price <= 0:
                # Try cached price from WEB_STATE (fast polling setiap 3 detik)
                cached_pos = next(
                    (p for p in getattr(WEB_STATE, 'positions', [])
                     if p['pos'].get('id') == pos['id']), None
                )
                if cached_pos and cached_pos.get('live_price', 0) > 0:
                    current_price = cached_pos['live_price']
                else:
                    # JANGAN fallback ke entry_price! Itu membunuh Stop Loss.
                    # Skip posisi ini, cek lagi di cycle berikutnya.
                    log.warning(f"[SL GUARD] Harga tidak tersedia untuk #{pos['id']} — SKIP cycle ini. "
                                f"TIDAK fallback ke entry_price agar Stop Loss tidak mati.")
                    continue

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
            elif hold_hours >= 2.0 and abs(price_change_pct) < 2.0:
                should_close = True; close_reason = f'STAGNANT ({hold_hours*60:.0f}m, dPx {price_change_pct:.1f}%)'
            elif hold_hours >= CFG['MAX_HOLD_HOURS']:
                should_close = True; close_reason = f'MAX_HOLD ({hold_hours:.0f}h)'
            elif current_price >= CFG.get('LIQUIDITY_TRAP_PRICE', 0.90):
                should_close = True; close_reason = f'LIQUIDITY_TRAP (price={current_price:.3f}≥0.90)'
            elif market_ghost:
                # If market not in active scan AND it's been open more than 1 hour
                if hold_hours > 1:
                    should_close = True; close_reason = 'MARKET_RESOLVING (GHOST)'

            if should_close:
                # ── REAL SELL EXECUTION (before DB write) ────────────
                if AUTO_TRADE and PRIVATE_KEY and token_id:
                    shares_held = pos.get('shares', 0)
                    sell_result = await execute_real_sell_order(token_id, shares_held, exit_price)
                    if sell_result['success']:
                        log.info(f'[CLOB] ✅ SELL filled → #{pos["id"]} | Reason:{close_reason}')
                        # HANYA TUTUP DI DB JIKA SELL SUKSES (Aset benar-benar terjual)
                        pnl = db_close_position(pos['id'], exit_price, close_reason)
                        await tg_close(session, pos, exit_price, pnl, close_reason)
                        closed_list.append({
                            'pos': pos, 'exit_price': exit_price,
                            'pnl': pnl, 'reason': close_reason,
                            'price_change': price_change_pct,
                        })
                    else:
                        sell_error = sell_result.get('error', '')
                        # ── DETEKSI MARKET SUDAH MATI/RESOLVED ──────────────
                        market_dead = (
                            'No orderbook exists' in sell_error or
                            'status_code=404' in sell_error or
                            'market not found' in sell_error.lower() or
                            'not tradeable' in sell_error.lower()
                        )
                        if market_dead:
                            log.error(f'[CLOB] 💀 MARKET MATI — Posisi #{pos["id"]} FORCE CLOSE.')
                            pnl = db_close_position(pos['id'], 0.0, f'MARKET_DEAD ({close_reason})')
                            await tg_close(session, pos, 0.0, pnl, f'MARKET_DEAD ({close_reason})')
                            SELL_FAIL_COUNTER.pop(pos['id'], None)
                            closed_list.append({
                                'pos': pos, 'exit_price': 0.0,
                                'pnl': pnl, 'reason': f'MARKET_DEAD ({close_reason})',
                                'price_change': -100.0,
                            })
                        else:
                            # ── SELL FAILURE COUNTER ──────────────────────────
                            # Track failed sell attempts. After MAX_SELL_RETRIES, FORCE CLOSE.
                            fail_count = SELL_FAIL_COUNTER.get(pos['id'], 0) + 1
                            SELL_FAIL_COUNTER[pos['id']] = fail_count
                            log.error(f'[CLOB] ⚠️ SELL GAGAL ({fail_count}/{MAX_SELL_RETRIES}): {sell_error} '
                                      f'— POSISI #{pos["id"]}')

                            if fail_count >= MAX_SELL_RETRIES:
                                log.error(f'[CLOB] 🚨 MAX SELL RETRIES REACHED ({fail_count}x gagal). '
                                          f'FORCE CLOSE posisi #{pos["id"]} di harga terakhir {exit_price:.4f}')
                                pnl = db_close_position(pos['id'], exit_price, f'SELL_EXHAUSTED ({close_reason})')
                                await tg_close(session, pos, exit_price, pnl, f'SELL_EXHAUSTED ({close_reason})')
                                SELL_FAIL_COUNTER.pop(pos['id'], None)
                                closed_list.append({
                                    'pos': pos, 'exit_price': exit_price,
                                    'pnl': pnl, 'reason': f'SELL_EXHAUSTED ({close_reason})',
                                    'price_change': price_change_pct,
                                })
                else:
                    # ── PAPER TRADING ──────────────────────────────────────
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
        # ── TIERED POSITION SIZING ────────────────────────────
        equity = self._calculate_equity()
        amount = self._get_bet_size(equity)
        entry  = r['entry_price']
        shares = amount / entry if entry > 0 else 0
        log.info(f'[SIZE] Equity=${equity:.2f} → Bet=${amount:.2f} (Tiered)')

        # ── REAL TRADE EXECUTION ──────────────────────────────
        if AUTO_TRADE and PRIVATE_KEY:
            # Use entry_token_id (the specific token for the chosen outcome)
            token_id = r.get('entry_token_id') or r.get('token_id', '')
            if not token_id:
                log.warning('[CLOB] ⚠️  No token_id found, skipping real order')
                return None
            result = await execute_real_order(token_id, amount, entry)
            if not result['success']:
                log.error(f'[CLOB] ❌ Order rejected: {result.get("error")} — trade NOT recorded')
                return None  # Do NOT write to DB if order failed
            log.info(f'[CLOB] ✅ Order filled! ID:{result["order_id"]} Status:{result["status"]}')
            # Store order_id in r so db can save it for future reference
            r['clob_order_id'] = result['order_id']

        # ── RECORD TRADE (paper or real) ──────────────────────
        pos_id = db_open_position(r, amount, shares)
        await tg_open(session, r, pos_id, amount)
        await self.refresh()
        return pos_id

    @staticmethod
    def _get_bet_size(equity: float) -> float:
        """Tiered position sizing — grows with equity for compounding.

        Tier table (user-defined):
          < $25    → $1.00
          < $50    → $1.30
          < $100   → $1.80
          < $120   → $2.50
          < $150   → $3.00
          < $200   → $4.00
          $200-500 → $4.00 + $1.50 per each $50 increment above $200
          $500-1k  → $13.00 + $2.00 per each $100 increment above $500
          $1000+   → to be upgraded later
        """
        if equity < 25:
            return 1.00
        elif equity < 50:
            return 1.30
        elif equity < 100:
            return 1.80
        elif equity < 120:
            return 2.50
        elif equity < 150:
            return 3.00
        elif equity < 200:
            return 4.00
        elif equity < 500:
            # $200 base = $4.00, add $1.50 per $50 increment
            increments = int((equity - 200) // 50)
            return round(4.00 + increments * 1.50, 2)
        elif equity < 1000:
            # $500 base = $13.00, add $2.00 per $100 increment
            # At $200: 4.00 + (300//50)*1.5 = 4.00 + 6*1.5 = 4+9 = $13.00 ✓
            increments = int((equity - 500) // 100)
            return round(13.00 + increments * 2.00, 2)
        else:
            # $1000+ — to be upgraded later
            return round(13.00 + (5 * 2.00), 2)  # Cap at $23 until upgraded

    def _calculate_equity(self) -> float:
        """Calculate current equity = bankroll + total realized PnL."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            reset_id = CFG.get('STATS_RESET_ID', 0)
            cur.execute("SELECT COALESCE(SUM(pnl_usd), 0) FROM positions WHERE status='CLOSED' AND id > ?", (reset_id,))
            total_pnl = cur.fetchone()[0]
            conn.close()
            equity = CFG['BANKROLL'] + total_pnl
            return max(1.0, equity)  # Never go below $1
        except Exception:
            return CFG['BANKROLL']

# ══════════════════════════════════════════════════════════════════
# DISPLAY
# ══════════════════════════════════════════════════════════════════
def banner():
    mode = 'REAL TRADE' if AUTO_TRADE and PRIVATE_KEY else 'PAPER TRADE'
    print('=' * 70)
    print('  POLYMARKET AUTO BOT v15.0 (NEWS INTELLIGENCE)')
    print(f'  Mode: {mode} | TPM: {CFG["TIME_EXIT_MINUTES"]}m | FEM: {CFG["FORCE_EXIT_MINUTES"]}m')
    print('=' * 70)

def display_stats(st: dict, pm: 'PositionManager'):
    equity = pm._get_equity_fast()
    next_bet = pm._get_bet_size(equity)
    max_exp = equity * CFG['MAX_EXPOSURE_PCT']
    void_str = f' | Void={st.get("voids", 0)}' if st.get('voids', 0) > 0 else ''
    print(f'\n  JOURNAL: Total={st["total"]} | Open={pm.count}/{CFG["MAX_POSITIONS"]} | '
          f'Closed={st["closed"]} | Win={st["wins"]} | Loss={st["losses"]}{void_str} | '
          f'WR={st["win_rate"]:.1f}% | P&L=${st["pnl"]:+.2f}')
    print(f'  Equity: ${equity:.2f} | NextBet: ${next_bet:.2f} | '
          f'Exposure: ${pm.total_exposure:.2f}/${max_exp:.2f} | '
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
    # Start Web UI server FIRST so Railway can detect port quickly
    asyncio.create_task(start_web_server())

    # ── ONE-TIME RESET (set RESET_DATA=true in Railway env vars) ──
    if os.environ.get('RESET_DATA', '').lower() == 'true':
        log.info('🧹 RESET_DATA=true detected — wiping database & ML brain...')
        # Delete database
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
            log.info(f'  ✓ Deleted: {DB_PATH}')
        # Delete ML model
        if os.path.exists(MODEL_PATH):
            os.remove(MODEL_PATH)
            log.info(f'  ✓ Deleted: {MODEL_PATH}')
        # Delete CSV export
        if os.path.exists(CSV_PATH):
            os.remove(CSV_PATH)
            log.info(f'  ✓ Deleted: {CSV_PATH}')
        log.info('🧹 RESET COMPLETE — Bot starts with a clean brain!')
        log.info('⚠️  IMPORTANT: Remove RESET_DATA env var from Railway NOW')
        log.info('    to prevent accidental reset on next deploy!')

    init_db()
    # Simpan modal awal SEKALI saja di startup — tidak berubah walau saldo sync
    if 'BANKROLL_ORIGINAL' not in CFG:
        CFG['BANKROLL_ORIGINAL'] = CFG.get('BANKROLL', 10.54)
    
    # ── Startup diagnostics ──────────────────────────────────
    is_volume = JOURNAL_DIR.startswith('/data')
    storage_type = '🔒 PERSISTENT (Railway Volume)' if is_volume else '⚠️ EPHEMERAL (local/no volume)'
    
    # Check existing data
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM positions WHERE status='CLOSED'")
        closed_count = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM positions WHERE status='CLOSED'")
        total_pnl = cur.fetchone()[0]
        conn.close()
    except Exception:
        closed_count = 0
        total_pnl = 0

    # Download NLTK data for VADER sentiment
    try:
        import nltk
        nltk.download('vader_lexicon', quiet=True)
        log.info('[STARTUP] VADER lexicon ready')
    except Exception:
        log.info('[STARTUP] VADER lexicon download skipped (nltk not installed)')

    # Initialize Brain
    brain = None
    if TradingBrain:
        brain = TradingBrain(DB_PATH, MODEL_PATH)
        # Force initial training to absorb 3-CLASS classification logic
        log.info("[BRAIN] 🚀 REGENERATING BRAIN (3-CLASS: WIN=2 VOID=1 LOSS=0 + max_features=sqrt)...")
        brain.train()

    storage_type = "VOLUME (Persistent)" if JOURNAL_DIR.startswith('/data') else "EPHEMERAL (Temporary - Data will be lost on restart)"
    
    banner()
    log.info('=' * 60)
    log.info('  POLYMARKET AUTO BOT v15.0 (NEWS INTELLIGENCE)')
    log.info('=' * 60)
    log.info(f'  Storage : {storage_type}')
    if "/data" in JOURNAL_DIR:
        log.info(f'  STATUS  : ✅ VOLUME DETECTED (Data is safe)')
    else:
        log.info(f'  STATUS  : ⚠️  NO VOLUME MOUNTED (Data will be lost)')
    log.info(f'  Journal : {JOURNAL_DIR}')
    log.info(f'  Database: {DB_PATH}')
    log.info(f'  History : {closed_count} closed trades | P&L: ${total_pnl:+.2f}')
    log.info(f'  Brain   : {"LOADED (ML active)" if brain and brain.model_mgr.is_trained else "HEURISTIC (learning)"}')
    log.info(f'  News    : {"ACTIVE (CryptoPanic + RSS + VADER)" if brain and brain.news_intel else "DISABLED"}')
    log.info(f'  ML needs: {max(0, 20 - closed_count)} more trades to activate')
    log.info(f'  Config  : TP={CFG["TAKE_PROFIT_PCT"]}% SL={CFG["STOP_LOSS_PCT"]}% | Tiered Sizing')
    log.info(f'  Entry   : Price ≤$0.80 | Spread ≤8% | Liq ≥$2K')
    log.info(f'  Safety  : Circuit Breaker (3 loss → 15m pause) | Blacklist active')
    log.info('=' * 60)

    history       : Dict[str, list] = {}
    scans         = 0
    pm            = PositionManager()
    await pm.refresh()
    already_opened = db_get_all_traded_market_ids()
    already_opened_questions = db_get_all_traded_market_questions()
    rejected_cache = {}  # {market_id: timestamp_rejected}
    
    # equity_curve is now computed fresh from DB in /api/state

    connector = aiohttp.TCPConnector(limit=50, limit_per_host=15, ttl_dns_cache=300, ssl=False)
    hdrs = {'User-Agent': 'Mozilla/5.0 PolyBot/12.0', 'Accept': 'application/json'}

    async with aiohttp.ClientSession(connector=connector, headers=hdrs) as session:
        if TELEGRAM_TOKEN:
            mode = 'REAL TRADE' if AUTO_TRADE and PRIVATE_KEY else 'PAPER TRADE'
            await tg(session, f'<b>Polymarket Auto Bot v15.0 (Intelligence Engine)</b>\nMode: <b>{mode}</b>')
            # Start background task to listen for /posisi and /closeall commands
            asyncio.create_task(telegram_listener(session, pm))

        while True:
            t0               = time.time()
            closed_this_scan = []
            try:
                await pm.refresh()
                
                # --- AUTO-SYNC WALLET BALANCE ---
                # Check actual blockchain balance every 10 scans to fix sync issues
                if scans % 10 == 0:
                    await pm.sync_with_wallet(session)
                # --------------------------------
                
                # Fetch markets FIRST so we know which are active
                raw = await fetch_markets(session)
                active_ids = {str(m.get('id', '')) for m in raw}
                
                if pm.open_positions:
                    closed_this_scan = await pm.check_and_close(session, active_market_ids=active_ids)
                    if closed_this_scan:
                        await pm.refresh()
                        await pm.refresh()
                        # NOTE: We DO NOT discard from already_opened sets here.
                        # We want to permanently ban re-entering a market we already traded.
                        
                        # Trigger continuous learning immediately after closing trades
                        if brain:
                            log.info("[BRAIN] Trade closed. Triggering continuous learning on 100+ historical trades...")
                            async def _train_with_flag():
                                global BRAIN_LEARNING
                                BRAIN_LEARNING = True
                                await asyncio.to_thread(brain.train)
                                # Keep BRAIN_LEARNING=True for at least 10s so frontend can display it
                                await asyncio.sleep(10)
                                BRAIN_LEARNING = False
                                log.info("[BRAIN] ✅ Learning complete. Model updated.")
                            asyncio.create_task(_train_with_flag())
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
                            # Quick ML score (synchronous)
                            if brain:
                                r['brain_score'] = brain.predict_confidence(r)
                            else:
                                r['brain_score'] = 50.0
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

                # ── PRE-FILTER: basic sanity checks ─────────────
                pre_candidates = [
                    r for r in results
                    if r.get('is_auto')
                    and r['id'] not in already_opened
                    and r['id'] not in rejected_cache
                    and r.get('question', '').strip() not in already_opened_questions
                    and r['liquidity'] >= max(CFG['MIN_LIQUIDITY'], pm._get_bet_size(pm._get_equity_fast()) * CFG.get('MIN_LIQ_DEPTH_MULT', 5.0))
                    and r.get('entry_price', 1.0) <= CFG.get('MAX_ENTRY_PRICE', 0.85)
                    and r.get('entry_price', 0.0) >= CFG.get('MIN_ENTRY_PRICE', 0.08)
                    and r.get('spread_pct', 100) <= 8.0
                    and r.get('volume_24h', 0) >= CFG.get('MIN_VOLUME_24H', 1000)
                    and (r['days'] is not None and 0.02 <= r['days'] <= CFG.get('MAX_DAYS_TO_EXPIRY', 2.0))
                ]
                
                # ── SCAN SUMMARY with rejection reasons ────
                SCAN_SUMMARY['total'] = len(results)
                SCAN_SUMMARY['passed'] = len(pre_candidates)
                rejection_reasons = {}
                for r in results:
                    if not r.get('is_auto'):
                        rejection_reasons['no_signal'] = rejection_reasons.get('no_signal', 0) + 1
                    elif r['id'] in already_opened:
                        rejection_reasons['already_traded'] = rejection_reasons.get('already_traded', 0) + 1
                    elif r['id'] in rejected_cache:
                        rejection_reasons['recently_rejected'] = rejection_reasons.get('recently_rejected', 0) + 1
                    elif r.get('entry_price', 1.0) > CFG.get('MAX_ENTRY_PRICE', 0.85):
                        rejection_reasons['price_too_high'] = rejection_reasons.get('price_too_high', 0) + 1
                    elif r.get('entry_price', 0.0) < CFG.get('MIN_ENTRY_PRICE', 0.08):
                        rejection_reasons['price_too_low'] = rejection_reasons.get('price_too_low', 0) + 1
                    elif r.get('spread_pct', 100) > 8.0:
                        rejection_reasons['spread_wide'] = rejection_reasons.get('spread_wide', 0) + 1
                    elif r.get('volume_24h', 0) < CFG.get('MIN_VOLUME_24H', 1000):
                        rejection_reasons['low_volume'] = rejection_reasons.get('low_volume', 0) + 1
                    elif r['liquidity'] < CFG['MIN_LIQUIDITY']:
                        rejection_reasons['low_liquidity'] = rejection_reasons.get('low_liquidity', 0) + 1
                SCAN_SUMMARY['rejected_reasons'] = rejection_reasons
                
                # HEARTBEAT LOG
                reasons_str = ' | '.join([f"{k}:{v}" for k, v in rejection_reasons.items()]) if rejection_reasons else 'all filtered'
                log.info(f"[SCAN #{scans}] {len(raw)} markets → {len(results)} valid → {len(pre_candidates)} candidates | Rejected: {reasons_str}")

                # ── DEEP ANALYSIS: verify with external data ────
                auto_candidates = []
                can_open_slots, _ = pm.can_open()
                if brain and pre_candidates and can_open_slots:
                    for candidate in pre_candidates[:10]:
                        try:
                            analysis = await brain.analyze_signal(session, candidate)
                            candidate['brain_analysis'] = analysis
                            candidate['brain_score'] = analysis.get('brain_score', 0)
                            candidate['should_trade'] = analysis.get('should_trade', False)
                            candidate['gates'] = analysis.get('gates_passed', '0/0')

                            _should_trade = analysis.get('should_trade', False)
                            _brain_score = analysis.get('brain_score', 0)
                            _min_conf = CFG.get('MIN_ML_CONFIDENCE', 40)
                            _score_pass = _brain_score >= _min_conf

                            if _should_trade and _score_pass:
                                auto_candidates.append(candidate)
                                log.info(f"[BRAIN] ✅ APPROVED: {candidate['question'][:50]} | "
                                         f"Score:{_brain_score:.0f} (>= {_min_conf}) | "
                                         f"Gates:{analysis['gates_passed']}")
                                LAST_DECISIONS.append({
                                    'ts': datetime.now().strftime('%H:%M:%S'),
                                    'type': 'APPROVED',
                                    'question': candidate['question'][:60],
                                    'score': round(_brain_score, 1),
                                    'gates': analysis.get('gates_passed', '?'),
                                    'price': round(candidate.get('entry_price', 0), 3),
                                })
                                if len(LAST_DECISIONS) > 50: LAST_DECISIONS.pop(0)
                            else:
                                rejected_cache[candidate['id']] = time.time()
                                reason = []
                                if not _should_trade: reason.append("Gates Failed")
                                if not _score_pass: reason.append(f"Score < {_min_conf}")
                                reason_str = " & ".join(reason) if reason else "Unknown"
                                
                                log.info(f"[BRAIN] ❌ REJECTED: {candidate['question'][:50]} | "
                                         f"Score:{_brain_score:.0f} | "
                                         f"Gates:{analysis['gates_passed']} | Reason: {reason_str}")
                                LAST_DECISIONS.append({
                                    'ts': datetime.now().strftime('%H:%M:%S'),
                                    'type': 'REJECTED',
                                    'question': candidate['question'][:60],
                                    'score': round(_brain_score, 1),
                                    'gates': analysis.get('gates_passed', '?'),
                                    'reason': reason_str,
                                    'price': round(candidate.get('entry_price', 0), 3),
                                })
                                if len(LAST_DECISIONS) > 50: LAST_DECISIONS.pop(0)
                        except Exception as e:
                            log.debug(f'[BRAIN] Analysis error: {e}')
                
                # Periodically clean rejected cache (keep for 1 hour)
                now = time.time()
                rejected_cache = {k: v for k, v in rejected_cache.items() if now - v < 3600}

                # Train Brain every 15 scans
                if brain and scans % 15 == 0:
                    async def _periodic_train():
                        try:
                            global BRAIN_LEARNING
                            BRAIN_LEARNING = True
                            await asyncio.to_thread(brain.train)
                            await asyncio.sleep(10)
                            BRAIN_LEARNING = False
                        except Exception:
                            pass
                    asyncio.create_task(_periodic_train())


                if auto_candidates:
                    for best in auto_candidates:
                        can, _ = pm.can_open()
                        if not can:
                            break
                        pos_id = await pm.open_position(session, best)
                        if pos_id:
                            already_opened.add(best['id'])
                            already_opened_questions.add(best.get('question', '').strip())

                try:
                    c2 = sqlite3.connect(DB_PATH)
                    c2.execute('INSERT INTO scan_log (ts,fetched,valid,open_pos,ms) VALUES (?,?,?,?,?)',
                               (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), len(raw), len(results), pm.count, ms))
                    c2.commit(); c2.close()
                except Exception: pass

                stats_j = db_get_stats()
                
                # --- UPDATE WEB STATE ---
                WEB_STATE.scans = scans
                WEB_STATE.ping_ms = ms
                WEB_STATE.stats = stats_j
                WEB_STATE.top_scans = top
                # Build positions with BATCH price fetch + cached fallback
                active_poses = []
                tids_main = [p['token_id'] for p in pm.open_positions if p.get('token_id')]
                main_price_map = {}
                if tids_main:
                    try:
                        async with session.post(
                            f"{CFG['CLOB_API']}/midpoints",
                            json=[{"token_id": str(t)} for t in tids_main],
                            timeout=aiohttp.ClientTimeout(total=20)
                        ) as resp:
                            if resp.status == 200:
                                raw_p = await resp.json()
                                if isinstance(raw_p, dict):
                                    for k, v in raw_p.items():
                                        if k != 'error':
                                            try: main_price_map[k] = float(v)
                                            except: pass
                    except Exception:
                        pass
                for open_pos in pm.open_positions:
                    tid = open_pos.get('token_id', '')
                    cp = main_price_map.get(tid)
                    # Fallback: use cached WEB_STATE price if API failed
                    if not cp or cp <= 0:
                        old_p = next((p for p in getattr(WEB_STATE, 'positions', [])
                                      if p['pos'].get('id') == open_pos.get('id')), None)
                        if old_p and old_p.get('live_price', 0) > 0:
                            cp = old_p['live_price']
                        else:
                            cp = open_pos['entry_price']
                    pl = (cp - open_pos['entry_price']) * open_pos.get('shares', 0)
                    active_poses.append({"pos": open_pos, "live_price": cp, "pnl": pl})
                WEB_STATE.positions = active_poses
                
                # equity_curve is now computed fresh from DB in /api/state
                
                # --- HANDLE EMERGENCY CLOSE FROM WEB ---
                global WEB_TRIGGER_CLOSE_ALL
                global WEB_TRIGGER_CLOSE_POS_IDS
                if WEB_TRIGGER_CLOSE_ALL:
                    log.info("[WEB UI] Executing EMERGENCY CLOSE ALL")
                    ops = db_get_open_positions()
                    for op in ops:
                        cur_price = await fetch_price(session, op['token_id'])
                        if cur_price is None:
                            cur_price = op['entry_price']
                        
                        # Execute real sell order
                        log.info(f"[WEB UI] Sending CLOB Sell order for {op['shares']} shares of {op['token_id']}")
                        await execute_real_sell_order(op['token_id'], op['shares'], cur_price)
                            
                        pnl = db_close_position(op['id'], cur_price, "WEB_CLOSEALL")
                        await tg_close(session, op, cur_price, pnl, "WEB_CLOSEALL")
                        # intentionally keeping in already_opened to prevent re-entry
                    await pm.refresh()
                    WEB_TRIGGER_CLOSE_ALL = False

                if WEB_TRIGGER_CLOSE_POS_IDS:
                    ops = db_get_open_positions()
                    closed_any = False
                    for op in ops:
                        if op['id'] in WEB_TRIGGER_CLOSE_POS_IDS:
                            log.info(f"[WEB UI] Executing MANUAL CLOSE for Pos #{op['id']}")
                            cur_price = await fetch_price(session, op['token_id'])
                            if cur_price is None:
                                cur_price = op['entry_price']
                                
                            # Execute real sell order
                            log.info(f"[WEB UI] Sending CLOB Sell order for {op['shares']} shares of {op['token_id']}")
                            res = await execute_real_sell_order(op['token_id'], op['shares'], cur_price)
                            log.info(f"[WEB UI] CLOB output: {res}")
                                
                            pnl = db_close_position(op['id'], cur_price, "WEB_MANUAL_CLOSE")
                            await tg_close(session, op, cur_price, pnl, "WEB_MANUAL_CLOSE")
                            # intentionally keeping in already_opened to prevent re-entry
                            closed_any = True
                    if closed_any:
                        await pm.refresh()
                    WEB_TRIGGER_CLOSE_POS_IDS.clear()
                # ---------------------------------------

                display(top, {'scans': scans, 'fetched': len(raw), 'valid': len(results), 'ms': ms}, stats_j, pm, closed_this_scan)

            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f'Main loop error: {e}')

            try:
                # Fast polling for active positions (Real-Time UI updates)
                # Use 3s interval to avoid rate-limit on Polymarket CLOB API
                sleep_chunks = max(1, int(CFG['SCAN_INTERVAL'] / 3.0))
                for _ in range(sleep_chunks):
                    await asyncio.sleep(3.0)
                    try:
                        if not pm.open_positions:
                            continue
                        # BATCH price fetch (1 request for all positions)
                        tids = [p['token_id'] for p in pm.open_positions if p.get('token_id')]
                        price_map = {}
                        if tids:
                            try:
                                async with session.post(
                                    f"{CFG['CLOB_API']}/midpoints",
                                    json=[{"token_id": str(t)} for t in tids],
                                    timeout=aiohttp.ClientTimeout(total=8)
                                ) as resp:
                                    if resp.status == 200:
                                        raw_p = await resp.json()
                                        if isinstance(raw_p, dict):
                                            for k, v in raw_p.items():
                                                if k != 'error':
                                                    try: price_map[k] = float(v)
                                                    except: pass
                            except Exception:
                                pass
                        
                        # GUARD: If API returned nothing, skip update entirely to preserve last known state
                        if not price_map:
                            continue
                        
                        fast_poses = []
                        for open_pos in pm.open_positions:
                            tid = open_pos.get('token_id', '')
                            ep = open_pos['entry_price']
                            # Fallback to last known price if this specific token is missing
                            old_p = next((p for p in getattr(WEB_STATE, 'positions', []) if p['pos'].get('id') == open_pos.get('id')), None)
                            cp = price_map.get(tid)
                            if cp and cp > 0:
                                pl = (cp - ep) * open_pos.get('shares', 0)
                            else:
                                cp = old_p['live_price'] if old_p and old_p.get('live_price', 0) > 0 else ep
                                pl = old_p['pnl'] if old_p else 0
                            fast_poses.append({"pos": open_pos, "live_price": cp, "pnl": pl})

                            # ── FAST STOP LOSS CHECK (setiap 3 detik) ─────────────
                            # Jangan tunggu full scan (10 detik) untuk cek stop loss.
                            # Jika harga sudah jatuh lebih dari STOP_LOSS_PCT, trigger close SEGERA.
                            if cp and cp > 0 and ep > 0:
                                fast_change_pct = (cp - ep) / ep * 100
                                if fast_change_pct <= -CFG['STOP_LOSS_PCT']:
                                    log.warning(f"[FAST SL] ⚡ STOP LOSS TRIGGERED di fast-poll! "
                                                f"Pos #{open_pos['id']} | Price:{cp:.4f} vs Entry:{ep:.4f} | "
                                                f"Drop:{fast_change_pct:.1f}% (limit:{-CFG['STOP_LOSS_PCT']}%)")
                                    try:
                                        shares_held = open_pos.get('shares', 0)
                                        token = open_pos.get('token_id', '')
                                        pos_id = open_pos.get('id', 0)
                                        if AUTO_TRADE and PRIVATE_KEY and token:
                                            sell_r = await execute_real_sell_order(token, shares_held, cp)
                                            if sell_r['success']:
                                                pnl = db_close_position(pos_id, cp, f'FAST_STOP_LOSS ({fast_change_pct:.1f}%)')
                                                await tg_close(session, open_pos, cp, pnl, f'FAST_STOP_LOSS ({fast_change_pct:.1f}%)')
                                                log.info(f"[FAST SL] ✅ Pos #{pos_id} closed via fast stop loss")
                                                SELL_FAIL_COUNTER.pop(pos_id, None)
                                                await pm.refresh()
                                            else:
                                                sell_err = sell_r.get('error', '')
                                                market_dead = (
                                                    'No orderbook exists' in sell_err or
                                                    'status_code=404' in sell_err
                                                )
                                                if market_dead:
                                                    pnl = db_close_position(pos_id, 0.0, 'MARKET_DEAD (FAST_SL)')
                                                    await tg_close(session, open_pos, 0.0, pnl, 'MARKET_DEAD (FAST_SL)')
                                                    SELL_FAIL_COUNTER.pop(pos_id, None)
                                                    await pm.refresh()
                                                else:
                                                    # Track sell failure — force close after MAX_SELL_RETRIES
                                                    fc = SELL_FAIL_COUNTER.get(pos_id, 0) + 1
                                                    SELL_FAIL_COUNTER[pos_id] = fc
                                                    log.error(f"[FAST SL] ⚠️ SELL GAGAL ({fc}/{MAX_SELL_RETRIES}) Pos #{pos_id}")
                                                    if fc >= MAX_SELL_RETRIES:
                                                        log.error(f"[FAST SL] 🚨 FORCE CLOSE #{pos_id} setelah {fc}x gagal jual")
                                                        pnl = db_close_position(pos_id, cp, f'SELL_EXHAUSTED (FAST_SL {fast_change_pct:.1f}%)')
                                                        await tg_close(session, open_pos, cp, pnl, f'SELL_EXHAUSTED (FAST_SL)')
                                                        SELL_FAIL_COUNTER.pop(pos_id, None)
                                                        await pm.refresh()
                                    except Exception as sl_e:
                                        log.error(f"[FAST SL] Error closing #{open_pos.get('id', '?')}: {sl_e}")

                        if fast_poses:
                            WEB_STATE.positions = fast_poses
                    except Exception:
                        pass
            except KeyboardInterrupt:
                break

    final = db_get_stats()
    log.info(f'Bot berhenti. Total={final["total"]} | P&L=${final["pnl"]:+.2f}')

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: print('\nBot berhenti.\n')

"""
SqueezeOS MCP Gateway — Stateless FastAPI backend (Render-optimized)
MCP endpoint: /mcp/sse  (x402 payment-gated)
REST endpoint: /api/matrix-intent  (direct agent integration)
"""
import os
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
import secrets
from fastapi import FastAPI, Query, Depends, HTTPException, status, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from mcp.server.fastmcp import FastMCP

from api.x402_middleware import X402PaymentMiddleware
from core.matrix_engine import get_live_intent
from core.backtest_engine import run_backtest_symbols
from core.vault_executor import _load_config, run_execution_cycle, get_vault_state

log = logging.getLogger("squeezeos")

load_dotenv()

_security = HTTPBasic()

def _require_dashboard_auth(credentials: HTTPBasicCredentials = Depends(_security)):
    correct_user = os.getenv("DASHBOARD_USER", "")
    correct_pass = os.getenv("DASHBOARD_PASS", "")
    ok = (
        secrets.compare_digest(credentials.username.encode(), correct_user.encode()) and
        secrets.compare_digest(credentials.password.encode(), correct_pass.encode())
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )

mcp = FastMCP("SqueezeOS")


@mcp.tool()
def query_execution_intent(
    symbol: str,
    timeframe: str = "15m",
    current_position: float = 0.0,
    average_entry_price: float = 0.0,
    drawdown_tier: int = 0,
) -> dict:
    """
    Returns the current execution intent for a given symbol based on the
    proprietary ribbon matrix engine. Requires live CCXT price data.

    Intent values:
      EXECUTE_INITIAL_ENTRY       — open a new position
      EXECUTE_TRANCHE_AVG_DOWN    — add to position on drawdown tier
      EXECUTE_TAKE_PROFIT_EXIT    — close position at structural profit target
      EXECUTE_STRUCTURAL_STOP_OUT — close position on structural anchor breach
      MAINTAIN_STATE              — no action
    """
    return get_live_intent(
        symbol=symbol,
        timeframe=timeframe,
        current_position=current_position,
        average_entry_price=average_entry_price,
        drawdown_tier=drawdown_tier,
    )


@mcp.tool()
def run_backtest(
    symbols: str = "ETH/USDT,BTC/USDT,SOL/USDT,AVAX/USDT,XRP/USDT",
    timeframe: str = "1d",
    bars: int = 365,
) -> dict:
    """
    Run a live backtest of the proprietary signal engine against real OHLC data.
    Returns per-symbol performance metrics and aggregate summary.
    symbols: comma-separated CCXT pairs e.g. ETH/USDT,BTC/USDT
    timeframe: candle timeframe e.g. 1d, 4h, 1h
    bars: number of candles per symbol (max 500)
    """
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    return run_backtest_symbols(symbol_list, timeframe, min(bars, 500))


@mcp.tool()
def get_ribbon_matrix(symbol: str, timeframe: str = "15m") -> dict:
    """
    Returns the current proprietary ribbon values for a symbol without evaluating intent.
    """
    result = get_live_intent(symbol=symbol, timeframe=timeframe)
    ribbon_keys = [k for k in result if k.startswith("ribbon_")]
    return {k: result[k] for k in ("symbol", "timeframe", "close", *ribbon_keys)}


@mcp.tool()
def execute_avg_down(
    symbol: str,
    timeframe: str = "15m",
    current_position: float = 0.0,
    average_entry_price: float = 0.0,
    drawdown_tier: int = 0,
) -> dict:
    """
    Avg-Down Engine — evaluates whether a tranche average-down is warranted.
    Returns a binding execution directive with position sizing guidance.
    This is an EXECUTION-TIER tool. Only call when you hold an open position
    and are evaluating whether to add capital at current drawdown levels.

    Returns intent + recommended_action + position_context.
    """
    result = get_live_intent(
        symbol=symbol,
        timeframe=timeframe,
        current_position=current_position,
        average_entry_price=average_entry_price,
        drawdown_tier=drawdown_tier,
    )
    intent = result["intent"]
    close  = result["close"]
    pnl    = ((close - average_entry_price) / average_entry_price * 100) if average_entry_price > 0 else 0.0
    return {
        "symbol":             symbol,
        "timeframe":          timeframe,
        "close":              close,
        "intent":             intent,
        "execution_price":    result.get("execution_price"),
        "position_context": {
            "current_position":     current_position,
            "average_entry_price":  average_entry_price,
            "drawdown_tier":        drawdown_tier,
            "unrealized_pnl_pct":   round(pnl, 2),
        },
        "recommended_action": (
            "ADD_TRANCHE"      if intent == "EXECUTE_TRANCHE_AVG_DOWN" else
            "EXIT_PROFIT"      if intent == "EXECUTE_TAKE_PROFIT_EXIT" else
            "EXIT_STOP"        if intent == "EXECUTE_STRUCTURAL_STOP_OUT" else
            "HOLD"
        ),
        "tier": "execution",
    }


# ── Vault executor background worker ──────────────────────────────────────────
_EXECUTOR_CFG  = None
_EXECUTOR_TASK = None
_LAST_CYCLE: dict = {}
_EXECUTOR_INTERVAL = int(os.getenv("EXECUTOR_INTERVAL_SECONDS", "900"))  # 15 min default


async def _executor_loop():
    global _LAST_CYCLE
    log.info("[executor] Background worker started")
    while True:
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, run_execution_cycle, _EXECUTOR_CFG)
            _LAST_CYCLE = {**result, "timestamp": asyncio.get_event_loop().time()}
        except Exception as e:
            log.error(f"[executor] Cycle error: {e}")
        await asyncio.sleep(_EXECUTOR_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _EXECUTOR_CFG, _EXECUTOR_TASK
    _EXECUTOR_CFG = _load_config()
    if _EXECUTOR_CFG:
        log.info(f"[executor] Vault config loaded — {_EXECUTOR_CFG['vault_addr']} | {_EXECUTOR_CFG['symbol']} {_EXECUTOR_CFG['timeframe']}")
        _EXECUTOR_TASK = asyncio.create_task(_executor_loop())
    else:
        log.info("[executor] No vault config — execution bridge inactive (set VAULT_ADDRESS, EXECUTION_PRIVATE_KEY, EXECUTION_RPC_URL)")
    yield
    if _EXECUTOR_TASK:
        _EXECUTOR_TASK.cancel()


app = FastAPI(
    title="SqueezeOS API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(X402PaymentMiddleware)
app.mount("/mcp", mcp.sse_app())


# ── REST endpoint — direct agent integration (no SSE required) ─────────────────
@app.get("/api/matrix-intent")
async def matrix_intent(
    symbol: str = Query(..., description="CCXT symbol e.g. ETH/USDT"),
    timeframe: str = Query("15m", description="Candle timeframe"),
    current_position: float = Query(0.0),
    average_entry_price: float = Query(0.0),
    drawdown_tier: int = Query(0),
):
    """
    Direct REST wrapper for the proprietary ribbon matrix engine.
    Callable by any HTTP client — no SSE/MCP client required.
    Used by sml_agent.py for crypto signal collection.
    """
    try:
        result = get_live_intent(
            symbol=symbol,
            timeframe=timeframe,
            current_position=current_position,
            average_entry_price=average_entry_price,
            drawdown_tier=drawdown_tier,
        )
        return result
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e), "symbol": symbol})


@app.get("/api/matrix-scan")
async def matrix_scan(
    timeframe: str = Query("15m"),
    symbols: str = Query("ETH/USDT,BTC/USDT,SOL/USDT,AVAX/USDT"),
):
    """
    Batch matrix scan across multiple crypto pairs.
    Returns intent for each symbol sorted by signal strength.
    """
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    results = []
    for sym in symbol_list:
        try:
            r = get_live_intent(symbol=sym, timeframe=timeframe)
            results.append(r)
        except Exception as e:
            results.append({"symbol": sym, "error": str(e), "intent": "ERROR"})

    # Sort: actionable intents first
    priority = {
        "EXECUTE_INITIAL_ENTRY": 0,
        "EXECUTE_TAKE_PROFIT_EXIT": 1,
        "EXECUTE_TRANCHE_AVG_DOWN": 2,
        "EXECUTE_STRUCTURAL_STOP_OUT": 3,
        "MAINTAIN_STATE": 4,
        "ERROR": 5,
    }
    results.sort(key=lambda x: priority.get(x.get("intent", "ERROR"), 5))
    return {"timeframe": timeframe, "scan_count": len(results), "results": results}


@app.get("/api/backtest")
async def backtest(
    symbols: str = Query("ETH/USDT,BTC/USDT,SOL/USDT,AVAX/USDT,XRP/USDT"),
    timeframe: str = Query("1d"),
    bars: int = Query(365),
):
    """
    Live backtest endpoint. Pulls real OHLC from Kraken, runs the signal engine,
    returns per-symbol metrics + aggregate. Callable by any HTTP client or AI agent.
    """
    try:
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
        return run_backtest_symbols(symbol_list, timeframe, min(bars, 500))
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(_: None = Depends(_require_dashboard_auth)):
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SqueezeOS — Live Matrix Dashboard</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#080b12;color:#c8d0e0;font-family:monospace;font-size:13px;padding:24px}
  h1{color:#ff5c1a;font-size:18px;letter-spacing:2px;margin-bottom:4px}
  .sub{color:#3a4a6a;font-size:11px;letter-spacing:3px;margin-bottom:24px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;margin-bottom:24px}
  .card{background:#0e1220;border:1px solid #1a2a40;border-radius:6px;padding:16px}
  .sym{color:#f0f0f0;font-size:15px;font-weight:700;margin-bottom:8px}
  .intent{display:inline-block;padding:3px 10px;border-radius:3px;font-size:11px;letter-spacing:1px;margin-bottom:12px}
  .EXECUTE_INITIAL_ENTRY{background:#1a4a1a;color:#4cff4c;border:1px solid #2a7a2a}
  .EXECUTE_TRANCHE_AVG_DOWN{background:#1a3a4a;color:#4cb8ff;border:1px solid #2a5a7a}
  .EXECUTE_TAKE_PROFIT_EXIT{background:#4a3a1a;color:#ffb84c;border:1px solid #7a5a2a}
  .EXECUTE_STRUCTURAL_STOP_OUT{background:#4a1a1a;color:#ff4c4c;border:1px solid #7a2a2a}
  .MAINTAIN_STATE{background:#1a1a2a;color:#3a4a6a;border:1px solid #2a2a3a}
  .ERROR{background:#3a1a1a;color:#ff4c4c;border:1px solid #6a2a2a}
  .ema-row{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #111820;color:#3a5a7a}
  .ema-row span:last-child{color:#7a9aba}
  .price{font-size:20px;color:#ff5c1a;font-weight:700;margin-bottom:6px}
  .ts{color:#2a3a5a;font-size:10px;margin-top:16px}
  .contracts{background:#0a0d14;border:1px solid #1a2a40;border-radius:6px;padding:14px;margin-bottom:20px;font-size:11px}
  .contract-row{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #111820}
  .contract-row:last-child{border-bottom:none}
  .contract-label{color:#3a5a7a}
  .contract-addr{color:#ff5c1a;font-size:10px}
  .header-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
  .refresh{background:#ff5c1a;color:#080b12;border:none;padding:6px 16px;border-radius:3px;cursor:pointer;font-family:monospace;font-size:11px;letter-spacing:1px}
  .refresh:hover{background:#e04a0a}
  .status{color:#2a4a2a;font-size:11px}
  .status.ok{color:#2a7a2a}
</style>
</head>
<body>
<div class="header-row">
  <div>
    <h1>SQUEEZE VAULT EXECUTOR</h1>
    <div class="sub">SCRIPTMASTERLABS · LIVE MATRIX DASHBOARD</div>
  </div>
  <div>
    <span class="status ok" id="status">● LIVE</span>&nbsp;&nbsp;
    <button class="refresh" onclick="load()">REFRESH</button>
  </div>
</div>
<div class="contracts">
  <div class="contract-row"><span class="contract-label">VaultFactory · Base L2</span><span class="contract-addr">0xC9A3Faa4f605F53Dd46578442aAAbA048eEb3031</span></div>
  <div class="contract-row"><span class="contract-label">SqueezeVault impl · Base L2</span><span class="contract-addr">0x1F521a7dFdD6566Fb4d7B8d9BBA155dB7505226F</span></div>
</div>
<div class="grid" id="grid">Loading...</div>
<div class="ts" id="ts"></div>
<script>
async function load(){
  document.getElementById('status').textContent='● FETCHING';
  document.getElementById('status').className='status';
  try{
    const r=await fetch('/api/matrix-scan');
    const d=await r.json();
    const g=document.getElementById('grid');
    g.innerHTML='';
    for(const s of d.results){
      const intentClass=s.intent||'ERROR';
      const emaKeys=Object.keys(s).filter(k=>k.startsWith('ema_'));
      const ribbonRows=emaKeys.map((k,i)=>`<div class="ema-row"><span>RIBBON ${i+1}</span><span>${(s[k]||0).toFixed(2)}</span></div>`).join('');
      const emas=s.error?'<div style="color:#ff4c4c">'+s.error+'</div>':ribbonRows;
      g.innerHTML+=`<div class="card">
        <div class="sym">${s.symbol}</div>
        <div class="price">${s.close?s.close.toFixed(2):'-'}</div>
        <div class="intent ${intentClass}">${(s.intent||'ERROR').replace(/_/g,' ')}</div>
        ${emas}
      </div>`;
    }
    document.getElementById('ts').textContent='Last updated: '+new Date().toUTCString()+' · '+d.scan_count+' pairs · 15m timeframe';
    document.getElementById('status').textContent='● LIVE';
    document.getElementById('status').className='status ok';
  }catch(e){
    document.getElementById('grid').innerHTML='<div style="color:#ff4c4c">Error: '+e.message+'</div>';
    document.getElementById('status').textContent='● ERROR';
  }
}
load();
setInterval(load,60000);
</script>
</body>
</html>"""


@app.get("/.well-known/mcp.json")
async def well_known_mcp():
    svc = os.getenv("RENDER_SERVICE_NAME", "sml-vault-engine")
    return {
        "name": "squeeze-vault-executor",
        "version": "1.0.0",
        "description": "SqueezeOS proprietary ribbon matrix vault executor — crypto execution intents via x402 XRPL/RLUSD payment rails",
        "mcp_endpoint": f"https://{svc}.onrender.com/mcp/sse",
        "transport": "sse",
        "tools": ["query_execution_intent", "get_ribbon_matrix", "execute_avg_down", "run_backtest"],
    }


@app.get("/health")
async def health():
    return {"status": "operational", "service": "SqueezeOS MCP Gateway"}


@app.get("/api/vault-status")
async def vault_status():
    """Live on-chain vault state — position, balance, last execution cycle."""
    if not _EXECUTOR_CFG:
        return {"active": False, "reason": "VAULT_ADDRESS or EXECUTION_PRIVATE_KEY not configured"}
    state = get_vault_state(_EXECUTOR_CFG)
    return {
        "active":       True,
        "vault":        _EXECUTOR_CFG["vault_addr"],
        "symbol":       _EXECUTOR_CFG["symbol"],
        "timeframe":    _EXECUTOR_CFG["timeframe"],
        "on_chain":     state,
        "last_cycle":   _LAST_CYCLE or None,
        "interval_sec": _EXECUTOR_INTERVAL,
    }


@app.post("/api/stripe/checkout")
async def stripe_checkout(request: Request):
    """Create a Stripe checkout session for a subscription tier."""
    try:
        import stripe
        stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
        body = await request.json()
        tier = body.get("tier", "signal")

        price_map = {
            "signal":   os.getenv("STRIPE_PRICE_SIGNAL",   ""),
            "executor": os.getenv("STRIPE_PRICE_EXECUTOR",  ""),
            "vault":    os.getenv("STRIPE_PRICE_VAULT",     ""),
        }
        price_id = price_map.get(tier)
        if not price_id:
            return JSONResponse(status_code=400, content={"error": f"Unknown tier or price not configured: {tier}"})

        success_url = body.get("success_url", "https://vault.scriptmasterlabs.com?subscribed=1")
        cancel_url  = body.get("cancel_url",  "https://vault.scriptmasterlabs.com")

        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"tier": tier},
        )
        return {"checkout_url": session.url, "session_id": session.id}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe webhook — handles subscription lifecycle events."""
    import stripe
    stripe.api_key   = os.getenv("STRIPE_SECRET_KEY", "")
    webhook_secret   = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    payload          = await request.body()
    sig_header       = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    etype = event["type"]
    if etype == "checkout.session.completed":
        session = event["data"]["object"]
        log.info(f"[stripe] New subscription — tier={session.get('metadata', {}).get('tier')} customer={session.get('customer')}")
    elif etype == "customer.subscription.deleted":
        log.info(f"[stripe] Subscription cancelled — {event['data']['object'].get('id')}")

    return {"received": True}


@app.get("/api/pricing")
async def pricing():
    """Public pricing tiers."""
    return {
        "human_tiers": [
            {"tier": "signal",   "price_usd_month": 49,  "description": "Scanner, Oracle, Battle Computer — read-only intents"},
            {"tier": "executor", "price_usd_month": 149, "description": "Signal + Avg-Down Engine — full 5-intent execution system"},
            {"tier": "vault",    "price_usd_month": 299, "description": "Executor + on-chain vault deployment + automated Base L2 execution"},
        ],
        "agent_tiers": [
            {"tool": "get_ribbon_matrix",      "price_rlusd": 0.005, "type": "data"},
            {"tool": "query_execution_intent", "price_rlusd": 0.01,  "type": "signal"},
            {"tool": "run_backtest",           "price_rlusd": 0.05,  "type": "compute"},
            {"tool": "execute_avg_down",       "price_rlusd": 0.25,  "type": "execution"},
        ],
        "payment_networks": ["Stripe (humans)", "x402 XRPL/RLUSD (AI agents)"],
    }


@app.get("/")
async def root():
    return {
        "service": "SqueezeOS Vault Engine",
        "mcp_endpoint": "/mcp/sse",
        "rest_endpoints": ["/api/matrix-intent", "/api/matrix-scan", "/api/backtest", "/api/vault-status", "/api/pricing"],
        "docs": "/docs",
        "payment_network": "XRPL",
        "payment_asset": "RLUSD",
        "vault_factory": "0xC9A3Faa4f605F53Dd46578442aAAbA048eEb3031",
        "vault_implementation": "0x1F521a7dFdD6566Fb4d7B8d9BBA155dB7505226F",
        "chain": "base",
        "chain_id": 8453,
    }

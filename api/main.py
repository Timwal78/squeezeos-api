"""
SqueezeOS MCP Gateway — Stateless FastAPI backend (Render-optimized)
MCP endpoint: /mcp/sse  (x402 payment-gated)
REST endpoint: /api/matrix-intent  (direct agent integration)
"""
import os
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, HTMLResponse
from mcp.server.fastmcp import FastMCP

from api.x402_middleware import X402PaymentMiddleware
from core.matrix_engine import get_live_intent
from core.backtest_engine import run_backtest_symbols

load_dotenv()

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
    5-EMA Fibonacci Ribbon Matrix. Requires live CCXT price data.

    Intent values:
      EXECUTE_INITIAL_ENTRY       — open a new position
      EXECUTE_TRANCHE_AVG_DOWN    — add to position on drawdown tier
      EXECUTE_TAKE_PROFIT_EXIT    — close position at +35% structural target
      EXECUTE_STRUCTURAL_STOP_OUT — close position on EMA_365 breach
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
def get_ema_matrix(symbol: str, timeframe: str = "15m") -> dict:
    """
    Returns the current 5-EMA ribbon values for a symbol without evaluating intent.
    """
    result = get_live_intent(symbol=symbol, timeframe=timeframe)
    return {k: result[k] for k in ("symbol", "timeframe", "close", "ema_55", "ema_89", "ema_144", "ema_233", "ema_365")}


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


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
    Direct REST wrapper for the 5-EMA matrix engine.
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
async def dashboard():
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
      const emas=s.error?'<div style="color:#ff4c4c">'+s.error+'</div>':`
        <div class="ema-row"><span>EMA 55</span><span>${(s.ema_55||0).toFixed(2)}</span></div>
        <div class="ema-row"><span>EMA 89</span><span>${(s.ema_89||0).toFixed(2)}</span></div>
        <div class="ema-row"><span>EMA 144</span><span>${(s.ema_144||0).toFixed(2)}</span></div>
        <div class="ema-row"><span>EMA 233</span><span>${(s.ema_233||0).toFixed(2)}</span></div>
        <div class="ema-row"><span>EMA 365</span><span>${(s.ema_365||0).toFixed(2)}</span></div>`;
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
    return {
        "name": "squeeze-vault-executor",
        "version": "1.0.0",
        "description": "SqueezeOS 5-EMA Fibonacci Ribbon vault executor — crypto execution intents via x402 XRPL/RLUSD payment rails",
        "mcp_endpoint": "https://squeezeos-api-1.onrender.com/mcp/sse",
        "transport": "sse",
        "tools": ["query_execution_intent", "get_ema_matrix"],
    }


@app.get("/health")
async def health():
    return {"status": "operational", "service": "SqueezeOS MCP Gateway"}


@app.get("/")
async def root():
    return {
        "service": "SqueezeOS",
        "mcp_endpoint": "/mcp/sse",
        "rest_endpoints": ["/api/matrix-intent", "/api/matrix-scan"],
        "docs": "/docs",
        "payment_network": "XRPL",
        "payment_asset": "RLUSD",
        "vault_factory": "0xC9A3Faa4f605F53Dd46578442aAAbA048eEb3031",
        "vault_implementation": "0x1F521a7dFdD6566Fb4d7B8d9BBA155dB7505226F",
        "chain": "base",
        "chain_id": 8453,
    }

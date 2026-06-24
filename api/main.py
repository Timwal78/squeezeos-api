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
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

from api.x402_middleware import X402PaymentMiddleware
from core.matrix_engine import get_live_intent

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
    }

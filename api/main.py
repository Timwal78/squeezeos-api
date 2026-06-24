"""
SqueezeOS MCP Gateway — Stateless FastAPI backend (Render-optimized)
MCP endpoint: /mcp  (x402 payment-gated)
"""
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
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
      EXECUTE_INITIAL_ENTRY     — open a new position
      EXECUTE_TRANCHE_AVG_DOWN  — add to position on drawdown tier
      EXECUTE_TAKE_PROFIT_EXIT  — close position at +35% structural target
      EXECUTE_STRUCTURAL_STOP_OUT — close position on EMA_365 breach
      MAINTAIN_STATE            — no action
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


@app.get("/health")
async def health():
    return {"status": "operational", "service": "SqueezeOS MCP Gateway"}


@app.get("/")
async def root():
    return {
        "service": "SqueezeOS",
        "mcp_endpoint": "/mcp",
        "payment_network": "XRPL",
        "payment_asset": "RLUSD",
    }

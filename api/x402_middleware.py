"""
SqueezeOS L402 / x402 Payment Middleware
Authenticates AI-agent MCP requests via XRPL/RLUSD micropayment preimages.
"""
import os
import hashlib
import hmac
import httpx
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

XRPL_NODE        = os.getenv("XRPL_NODE", "https://xrplcluster.com")
PAYMENT_AMOUNT   = os.getenv("X402_AMOUNT_DROPS", "1000")   # 1000 drops RLUSD equivalent
MASTER_WALLET    = os.getenv("MASTER_WALLET_ADDRESS", "")
X402_SECRET      = os.getenv("X402_HMAC_SECRET", "").encode()

MCP_PATHS = {"/mcp", "/tools/query_execution_intent"}


def _verify_preimage(preimage: str, payment_hash: str) -> bool:
    """SHA-256(preimage) must match the declared payment_hash."""
    computed = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    return hmac.compare_digest(computed, payment_hash)


async def _verify_xrpl_payment(tx_hash: str) -> bool:
    """Confirm tx exists on XRPL, destination=MASTER_WALLET, amount>=PAYMENT_AMOUNT."""
    if not MASTER_WALLET:
        return False
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(
            XRPL_NODE,
            json={
                "method": "tx",
                "params": [{"transaction": tx_hash, "binary": False}],
            },
        )
    if resp.status_code != 200:
        return False
    data = resp.json()
    tx = data.get("result", {})
    if tx.get("status") != "success":
        return False
    meta = tx.get("meta", {})
    if meta.get("TransactionResult") != "tesSUCCESS":
        return False
    dest = tx.get("Destination", "")
    amount = int(tx.get("Amount", 0))
    return dest == MASTER_WALLET and amount >= int(PAYMENT_AMOUNT)


class X402PaymentMiddleware(BaseHTTPMiddleware):
    """
    Intercepts MCP endpoint requests.
    Accepts either:
      - X-Payment-Preimage + X-Payment-Hash  (L402-style hash reveal)
      - X-XRPL-TxHash                        (on-chain XRPL payment proof)
    Returns 402 with payment instructions if neither is present/valid.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path not in MCP_PATHS:
            return await call_next(request)

        preimage    = request.headers.get("X-Payment-Preimage")
        pay_hash    = request.headers.get("X-Payment-Hash")
        xrpl_tx     = request.headers.get("X-XRPL-TxHash")

        authorized = False

        if preimage and pay_hash:
            authorized = _verify_preimage(preimage, pay_hash)

        elif xrpl_tx:
            authorized = await _verify_xrpl_payment(xrpl_tx)

        if authorized:
            return await call_next(request)

        return JSONResponse(
            status_code=402,
            headers={
                "X-Payment-Required": "true",
                "X-Payment-Amount-Drops": PAYMENT_AMOUNT,
                "X-Payment-Destination": MASTER_WALLET,
                "X-Payment-Network": "XRPL",
                "X-Payment-Asset": "RLUSD",
            },
            content={
                "error": "Payment Required",
                "message": (
                    "Send XRPL/RLUSD micropayment to access the SqueezeOS MCP gateway. "
                    "Include X-XRPL-TxHash header with confirmed transaction hash, "
                    "or X-Payment-Preimage + X-Payment-Hash for L402 hash-reveal flow."
                ),
                "amount_drops": PAYMENT_AMOUNT,
                "destination": MASTER_WALLET,
                "network": "XRPL",
            },
        )

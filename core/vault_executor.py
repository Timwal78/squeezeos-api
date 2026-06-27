"""
Vault Executor — on-chain execution bridge.
Reads live intents from the ribbon matrix engine and fires SqueezeVault
transactions on Base mainnet via web3.py.

Required env vars:
  EXECUTION_RPC_URL        — Base mainnet RPC (e.g. https://mainnet.base.org)
  EXECUTION_PRIVATE_KEY    — Private key of vault owner wallet (hex, no 0x prefix)
  VAULT_ADDRESS            — Deployed SqueezeVault clone address
  VAULT_SYMBOL             — CCXT symbol to trade (default ETH/USDT)
  VAULT_TIMEFRAME          — Signal timeframe (default 15m)
  VAULT_USDT_AMOUNT        — USDT per entry tranche in human units (default 5.0)
  VAULT_SLIPPAGE_BPS       — Max slippage in bps (default 150 = 1.5%)
  VAULT_POOL_FEE           — Uniswap V3 pool fee tier (default 500 = 0.05%)
"""
from __future__ import annotations
import os
import logging
import time
from typing import Optional

log = logging.getLogger("vault_executor")

# ── Token addresses on Base mainnet ───────────────────────────────────────────
USDT_BASE = "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2"
WETH_BASE = "0x4200000000000000000000000000000000000006"

# ── Minimal ABI — only functions needed at runtime ────────────────────────────
VAULT_ABI = [
    {
        "name": "executeEntry",
        "type": "function",
        "inputs": [
            {"name": "tokenIn",  "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "minOut",   "type": "uint256"},
            {"name": "poolFee",  "type": "uint24"},
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "name": "executeTrancheAvgDown",
        "type": "function",
        "inputs": [
            {"name": "tokenIn",  "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "minOut",   "type": "uint256"},
            {"name": "poolFee",  "type": "uint24"},
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "name": "executeTakeProfitExit",
        "type": "function",
        "inputs": [
            {"name": "tokenIn",  "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "minOut",   "type": "uint256"},
            {"name": "poolFee",  "type": "uint24"},
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "name": "executeStructuralStopOut",
        "type": "function",
        "inputs": [
            {"name": "tokenIn",  "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "minOut",   "type": "uint256"},
            {"name": "poolFee",  "type": "uint24"},
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "name": "averageEntryPrice",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "positionSize",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "drawdownTier",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
    },
]

ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "decimals",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
    },
]


def _load_config() -> dict | None:
    rpc  = os.getenv("EXECUTION_RPC_URL", "")
    key  = os.getenv("EXECUTION_PRIVATE_KEY", "")
    addr = os.getenv("VAULT_ADDRESS", "")
    if not (rpc and key and addr):
        return None
    return {
        "rpc":          rpc,
        "private_key":  key if key.startswith("0x") else f"0x{key}",
        "vault_addr":   addr,
        "symbol":       os.getenv("VAULT_SYMBOL", "ETH/USDT"),
        "timeframe":    os.getenv("VAULT_TIMEFRAME", "15m"),
        "usdt_amount":  float(os.getenv("VAULT_USDT_AMOUNT", "5.0")),
        "slippage_bps": int(os.getenv("VAULT_SLIPPAGE_BPS", "150")),
        "pool_fee":     int(os.getenv("VAULT_POOL_FEE", "500")),
    }


def _get_web3(rpc: str):
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        raise RuntimeError(f"Cannot connect to RPC: {rpc}")
    return w3


def _minout(close_price: float, usdt_amount: float, slippage_bps: int) -> int:
    """
    Compute minimum ETH out for a given USDT in + slippage.
    Returns raw wei (18 decimals).
    """
    eth_expected = usdt_amount / close_price
    min_eth = eth_expected * (1 - slippage_bps / 10_000)
    return int(min_eth * 1e18)


def get_vault_state(cfg: dict) -> dict:
    """Read on-chain position state from the vault."""
    try:
        w3 = _get_web3(cfg["rpc"])
        from web3 import Web3
        vault = w3.eth.contract(
            address=Web3.to_checksum_address(cfg["vault_addr"]),
            abi=VAULT_ABI,
        )
        pos_size   = vault.functions.positionSize().call()
        avg_entry  = vault.functions.averageEntryPrice().call()
        dd_tier    = vault.functions.drawdownTier().call()

        usdt = w3.eth.contract(
            address=Web3.to_checksum_address(USDT_BASE),
            abi=ERC20_ABI,
        )
        usdt_bal = usdt.functions.balanceOf(
            Web3.to_checksum_address(cfg["vault_addr"])
        ).call()

        return {
            "position_size":        pos_size / 1e18,
            "average_entry_price":  avg_entry / 1e18 if avg_entry > 0 else 0.0,
            "drawdown_tier":        dd_tier,
            "usdt_balance":         usdt_bal / 1e6,
            "has_position":         pos_size > 0,
        }
    except Exception as e:
        log.error(f"[vault_state] {e}")
        return {
            "position_size": 0.0, "average_entry_price": 0.0,
            "drawdown_tier": 0, "usdt_balance": 0.0, "has_position": False,
        }


def execute_intent(cfg: dict, intent: str, close: float) -> Optional[str]:
    """
    Fire the appropriate vault function based on the intent signal.
    Returns transaction hash on success, None on skip or failure.
    """
    from web3 import Web3
    from eth_account import Account

    if intent == "MAINTAIN_STATE":
        return None

    try:
        w3 = _get_web3(cfg["rpc"])
        account = Account.from_key(cfg["private_key"])
        vault = w3.eth.contract(
            address=Web3.to_checksum_address(cfg["vault_addr"]),
            abi=VAULT_ABI,
        )

        usdt_contract = w3.eth.contract(
            address=Web3.to_checksum_address(USDT_BASE),
            abi=ERC20_ABI,
        )
        usdt_bal = usdt_contract.functions.balanceOf(
            Web3.to_checksum_address(cfg["vault_addr"])
        ).call()
        usdt_human = usdt_bal / 1e6

        if intent in ("EXECUTE_INITIAL_ENTRY", "EXECUTE_TRANCHE_AVG_DOWN"):
            if usdt_human < 1.0:
                log.warning(f"[executor] Skipping {intent} — vault USDT balance too low ({usdt_human:.2f})")
                return None
            amount_in = int(min(cfg["usdt_amount"], usdt_human * 0.95) * 1e6)
            min_out   = _minout(close, amount_in / 1e6, cfg["slippage_bps"])
        else:
            vault_state = get_vault_state(cfg)
            amount_in   = int(vault_state["position_size"] * 1e18)
            min_out     = int((amount_in / 1e18) * close * (1 - cfg["slippage_bps"] / 10_000) * 1e6)

        fn_map = {
            "EXECUTE_INITIAL_ENTRY":    vault.functions.executeEntry,
            "EXECUTE_TRANCHE_AVG_DOWN": vault.functions.executeTrancheAvgDown,
            "EXECUTE_TAKE_PROFIT_EXIT": vault.functions.executeTakeProfitExit,
            "EXECUTE_STRUCTURAL_STOP_OUT": vault.functions.executeStructuralStopOut,
        }
        fn = fn_map[intent]

        token_in  = USDT_BASE if "ENTRY" in intent or "AVG_DOWN" in intent else WETH_BASE
        token_out = WETH_BASE if "ENTRY" in intent or "AVG_DOWN" in intent else USDT_BASE

        nonce = w3.eth.get_transaction_count(account.address)
        tx = fn(
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            amount_in,
            min_out,
            cfg["pool_fee"],
        ).build_transaction({
            "from":     account.address,
            "nonce":    nonce,
            "gas":      400_000,
            "maxFeePerGas":         w3.to_wei("0.1", "gwei"),
            "maxPriorityFeePerGas": w3.to_wei("0.01", "gwei"),
        })

        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status == 1:
            log.info(f"[executor] ✅ {intent} confirmed — tx: {tx_hash.hex()}")
            return tx_hash.hex()
        else:
            log.error(f"[executor] ❌ {intent} reverted — tx: {tx_hash.hex()}")
            return None

    except Exception as e:
        log.error(f"[executor] {intent} failed: {e}")
        return None


def run_execution_cycle(cfg: dict) -> dict:
    """
    One full signal → execute cycle.
    Called by the background worker on each tick.
    """
    from core.matrix_engine import get_live_intent

    state = get_vault_state(cfg)
    result = get_live_intent(
        symbol=cfg["symbol"],
        timeframe=cfg["timeframe"],
        current_position=state["position_size"],
        average_entry_price=state["average_entry_price"],
        drawdown_tier=state["drawdown_tier"],
    )

    intent = result["intent"]
    close  = result["close"]

    log.info(f"[executor] {cfg['symbol']} → {intent} @ {close:.4f} | pos={state['position_size']:.4f} | USDT={state['usdt_balance']:.2f}")

    tx_hash = execute_intent(cfg, intent, close)

    return {
        "symbol":       cfg["symbol"],
        "intent":       intent,
        "close":        close,
        "vault_state":  state,
        "tx_hash":      tx_hash,
        "executed":     tx_hash is not None,
    }

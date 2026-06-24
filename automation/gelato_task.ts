/**
 * SqueezeOS Gelato Web3 Function
 * Off-chain trigger: polls the MCP gateway → routes execution intent to the user's EIP-1167 vault.
 *
 * Deploy via: gelato-web3-functions deploy
 * Runtime cost: $0.00 (Gelato free tier covers event-driven execution)
 */
import { Web3Function, Web3FunctionContext } from "@gelatonetwork/web3-functions-sdk";
import { Contract, ethers } from "ethers";
import axios from "axios";

// Minimal ABI fragments for the proxy vault
const VAULT_ABI = [
  "function executeEntry(address,address,uint256,uint24,uint256) returns (uint256)",
  "function executeTrancheAvgDown(address,address,uint256,uint24,uint24,uint256) returns (uint256)",
  "function executeTakeProfitExit(address,address,uint24,uint256,uint256) returns (uint256)",
  "function executeStructuralStopOut(address,address,uint24,uint256) returns (uint256)",
  "function positionSize() view returns (uint256)",
  "function averageEntryPrice() view returns (uint256)",
  "function drawdownTier() view returns (uint8)",
  "function owner() view returns (address)",
];

Web3Function.onRun(async (context: Web3FunctionContext) => {
  const { userArgs, secrets, multiChainProvider } = context;

  const symbol: string       = (userArgs.symbol as string) || "ETH/USDT";
  const timeframe: string    = (userArgs.timeframe as string) || "15m";
  const vaultAddress: string = userArgs.vaultAddress as string;
  const tokenIn: string      = userArgs.tokenIn as string;
  const tokenOut: string     = userArgs.tokenOut as string;
  const poolFee: number      = Number(userArgs.poolFee ?? 3000);
  const amountIn: string     = (userArgs.amountIn as string) || "0";
  const minOut: string       = (userArgs.minOut as string) || "0";

  const mcpUrl: string       = await secrets.get("MCP_URL") ?? "https://squeezeos-api.onrender.com/mcp";
  const xrplTxHash: string   = await secrets.get("XRPL_TX_HASH") ?? "";

  // ── Query SqueezeOS MCP Gateway ────────────────────────────────────────────
  let intentData: Record<string, unknown>;
  try {
    const resp = await axios.post(
      `${mcpUrl}/tools/query_execution_intent`,
      { symbol, timeframe },
      {
        headers: {
          "Content-Type":  "application/json",
          "X-XRPL-TxHash": xrplTxHash,
        },
        timeout: 10_000,
      }
    );
    intentData = resp.data as Record<string, unknown>;
  } catch (err: unknown) {
    return { canExec: false, message: `MCP gateway error: ${String(err)}` };
  }

  const intent = intentData.intent as string;

  if (intent === "MAINTAIN_STATE") {
    return { canExec: false, message: "MAINTAIN_STATE — no action required" };
  }

  // ── Connect to vault ───────────────────────────────────────────────────────
  const provider = multiChainProvider.default();
  const vault    = new Contract(vaultAddress, VAULT_ABI, provider);

  const positionSize     = await vault.positionSize() as bigint;
  const avgEntry         = await vault.averageEntryPrice() as bigint;
  const drawdownTier     = await vault.drawdownTier() as bigint;
  const currentPrice18   = BigInt(Math.floor((intentData.close as number) * 1e18));

  // ── Route intent to the correct vault function ────────────────────────────
  let callData: string;

  switch (intent) {
    case "EXECUTE_INITIAL_ENTRY":
      callData = vault.interface.encodeFunctionData("executeEntry", [
        tokenIn, tokenOut, amountIn, poolFee, minOut,
      ]);
      break;

    case "EXECUTE_TRANCHE_AVG_DOWN":
      callData = vault.interface.encodeFunctionData("executeTrancheAvgDown", [
        tokenIn, tokenOut, amountIn, poolFee, minOut,
      ]);
      break;

    case "EXECUTE_TAKE_PROFIT_EXIT":
      callData = vault.interface.encodeFunctionData("executeTakeProfitExit", [
        tokenIn, tokenOut, poolFee, minOut, currentPrice18,
      ]);
      break;

    case "EXECUTE_STRUCTURAL_STOP_OUT":
      callData = vault.interface.encodeFunctionData("executeStructuralStopOut", [
        tokenIn, tokenOut, poolFee, minOut,
      ]);
      break;

    default:
      return { canExec: false, message: `Unknown intent: ${intent}` };
  }

  return {
    canExec: true,
    callData: [{ to: vaultAddress, data: callData }],
  };
});

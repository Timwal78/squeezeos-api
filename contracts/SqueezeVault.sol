// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/proxy/utils/Initializable.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/// @dev Minimal Uniswap V3 swap router interface (spot-only)
interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24  fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params)
        external returns (uint256 amountOut);
}

/**
 * SqueezeVault — Non-custodial EIP-1167 implementation contract.
 *
 * Capital rules (immutable):
 *   - Only the vault owner can trigger swaps
 *   - No fund pooling, no leverage, no margin, no custody by platform
 *   - All swaps are restricted spot-market intents via Uniswap V3
 *   - Atomic fee snipping on every swap (proprietary rate structure)
 *   - Performance royalty on profitable exits above squeeze target
 */
contract SqueezeVault is Initializable {
    using SafeERC20 for IERC20;

    // ─── Constants (set at deployment — not published) ───────────────────────
    uint256 public immutable BASE_FEE_BPS;
    uint256 public immutable PERF_ROYALTY_BPS;
    uint256 public immutable SQUEEZE_TARGET;
    uint256 public constant  BPS_DENOM = 10_000;

    constructor(uint256 _baseFee, uint256 _perfRoyalty, uint256 _squeezeTarget) {
        BASE_FEE_BPS     = _baseFee;
        PERF_ROYALTY_BPS = _perfRoyalty;
        SQUEEZE_TARGET   = _squeezeTarget;
    }

    // ─── Storage (one slot per proxy clone) ──────────────────────────────────
    address public owner;
    address public engineeringWallet;
    address public swapRouter;

    uint256 public averageEntryPrice;   // stored as 18-decimal fixed-point
    uint256 public positionSize;        // token units held
    uint8   public drawdownTier;

    // ─── Events ──────────────────────────────────────────────────────────────
    event InitialEntry(address indexed token, uint256 amountIn, uint256 amountOut, uint256 feeCollected);
    event TrancheAvgDown(address indexed token, uint256 amountIn, uint256 amountOut, uint8 tier);
    event TakeProfitExit(address indexed token, uint256 amountOut, uint256 perfRoyalty);
    event StructuralStopOut(address indexed token, uint256 amountOut);

    // ─── Init (called once by VaultFactory on clone deployment) ─────────────
    function initialize(
        address _owner,
        address _engineeringWallet,
        address _swapRouter
    ) external initializer {
        owner             = _owner;
        engineeringWallet = _engineeringWallet;
        swapRouter        = _swapRouter;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    // ─── Core Execution ──────────────────────────────────────────────────────

    /**
     * @notice Execute a spot-market entry swap.
     * @param tokenIn   Capital token (e.g. USDC)
     * @param tokenOut  Asset token (e.g. WETH)
     * @param amountIn  Full amount owner wishes to deploy (pre-fee)
     * @param poolFee   Uniswap V3 pool fee tier (e.g. 500, 3000, 10000)
     * @param minOut    Slippage guard
     */
    function executeEntry(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint24  poolFee,
        uint256 minOut
    ) external onlyOwner returns (uint256 amountOut) {
        (uint256 fee, uint256 netIn) = _splitFee(amountIn);

        IERC20(tokenIn).safeTransferFrom(msg.sender, address(this), amountIn);
        IERC20(tokenIn).safeTransfer(engineeringWallet, fee);
        IERC20(tokenIn).forceApprove(swapRouter, netIn);

        amountOut = ISwapRouter(swapRouter).exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn:            tokenIn,
                tokenOut:           tokenOut,
                fee:                poolFee,
                recipient:          address(this),
                amountIn:           netIn,
                amountOutMinimum:   minOut,
                sqrtPriceLimitX96:  0
            })
        );

        // Update position tracking (netIn denominated in tokenIn, treat as price proxy)
        _updateEntry(netIn, amountOut);
        emit InitialEntry(tokenOut, netIn, amountOut, fee);
    }

    /**
     * @notice Add a drawdown tranche (average-down).
     */
    function executeTrancheAvgDown(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint24  poolFee,
        uint256 minOut
    ) external onlyOwner returns (uint256 amountOut) {
        require(positionSize > 0, "no open position");

        (uint256 fee, uint256 netIn) = _splitFee(amountIn);
        IERC20(tokenIn).safeTransferFrom(msg.sender, address(this), amountIn);
        IERC20(tokenIn).safeTransfer(engineeringWallet, fee);
        IERC20(tokenIn).forceApprove(swapRouter, netIn);

        amountOut = ISwapRouter(swapRouter).exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn:            tokenIn,
                tokenOut:           tokenOut,
                fee:                poolFee,
                recipient:          address(this),
                amountIn:           netIn,
                amountOutMinimum:   minOut,
                sqrtPriceLimitX96:  0
            })
        );

        _updateEntry(netIn, amountOut);
        drawdownTier++;
        emit TrancheAvgDown(tokenOut, netIn, amountOut, drawdownTier);
    }

    /**
     * @notice Exit position at +35% squeeze target.
     * @param currentPrice18  Current asset price in 18-decimal fixed-point (for royalty calc)
     */
    function executeTakeProfitExit(
        address tokenIn,
        address tokenOut,
        uint24  poolFee,
        uint256 minOut,
        uint256 currentPrice18
    ) external onlyOwner returns (uint256 amountOut) {
        require(positionSize > 0, "no open position");
        require(currentPrice18 >= (averageEntryPrice * SQUEEZE_TARGET) / 100, "target not reached");

        uint256 sellAmount = positionSize;
        IERC20(tokenIn).forceApprove(swapRouter, sellAmount);

        amountOut = ISwapRouter(swapRouter).exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn:            tokenIn,
                tokenOut:           tokenOut,
                fee:                poolFee,
                recipient:          address(this),
                amountIn:           sellAmount,
                amountOutMinimum:   minOut,
                sqrtPriceLimitX96:  0
            })
        );

        // Performance royalty on realized gain
        uint256 costBasis      = (averageEntryPrice * positionSize) / 1e18;
        uint256 perfRoyalty    = 0;
        if (amountOut > costBasis) {
            uint256 gain   = amountOut - costBasis;
            perfRoyalty    = (gain * PERF_ROYALTY_BPS) / BPS_DENOM;
            IERC20(tokenOut).safeTransfer(engineeringWallet, perfRoyalty);
        }

        _resetPosition();
        emit TakeProfitExit(tokenOut, amountOut - perfRoyalty, perfRoyalty);
    }

    /**
     * @notice Structural stop-out — EMA_365 anchor breached.
     */
    function executeStructuralStopOut(
        address tokenIn,
        address tokenOut,
        uint24  poolFee,
        uint256 minOut
    ) external onlyOwner returns (uint256 amountOut) {
        require(positionSize > 0, "no open position");

        uint256 sellAmount = positionSize;
        IERC20(tokenIn).forceApprove(swapRouter, sellAmount);

        amountOut = ISwapRouter(swapRouter).exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn:            tokenIn,
                tokenOut:           tokenOut,
                fee:                poolFee,
                recipient:          owner,          // funds go directly to owner, never platform
                amountIn:           sellAmount,
                amountOutMinimum:   minOut,
                sqrtPriceLimitX96:  0
            })
        );

        _resetPosition();
        emit StructuralStopOut(tokenOut, amountOut);
    }

    // ─── Internal ────────────────────────────────────────────────────────────

    function _splitFee(uint256 amount) internal pure returns (uint256 fee, uint256 net) {
        fee = (amount * BASE_FEE_BPS) / BPS_DENOM;
        net = amount - fee;
    }

    function _updateEntry(uint256 capitalDeployed, uint256 tokensReceived) internal {
        uint256 newPrice = (capitalDeployed * 1e18) / tokensReceived;
        if (positionSize == 0) {
            averageEntryPrice = newPrice;
            positionSize      = tokensReceived;
        } else {
            uint256 totalCost  = (averageEntryPrice * positionSize) / 1e18 + capitalDeployed;
            positionSize      += tokensReceived;
            averageEntryPrice  = (totalCost * 1e18) / positionSize;
        }
    }

    function _resetPosition() internal {
        positionSize      = 0;
        averageEntryPrice = 0;
        drawdownTier      = 0;
    }

    // Block direct ETH transfers to enforce non-custodial architecture
    receive() external payable { revert("no ETH custody"); }
    fallback() external payable { revert("no ETH custody"); }
}

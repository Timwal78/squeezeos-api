// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/proxy/utils/Initializable.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

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

contract SqueezeVault is Initializable {
    using SafeERC20 for IERC20;

    uint256 public immutable BASE_FEE_BPS;
    uint256 public immutable PERF_ROYALTY_BPS;
    uint256 public immutable SQUEEZE_TARGET;
    uint256 public constant BPS_DENOM = 10_000;

    address public owner;
    address public engineeringWallet;
    address public swapRouter;

    uint256 public averageEntryPrice;
    uint256 public positionSize;
    uint8   public drawdownTier;

    event InitialEntry(address indexed token, uint256 amountIn, uint256 amountOut, uint256 feeCollected);
    event TrancheAvgDown(address indexed token, uint256 amountIn, uint256 amountOut, uint8 tier);
    event TakeProfitExit(address indexed token, uint256 amountOut, uint256 perfRoyalty);
    event StructuralStopOut(address indexed token, uint256 amountOut);

    constructor(uint256 _baseFee, uint256 _perfRoyalty, uint256 _squeezeTarget) {
        BASE_FEE_BPS     = _baseFee;
        PERF_ROYALTY_BPS = _perfRoyalty;
        SQUEEZE_TARGET   = _squeezeTarget;
    }

    function initialize(address _owner, address _engineeringWallet, address _swapRouter) external initializer {
        owner             = _owner;
        engineeringWallet = _engineeringWallet;
        swapRouter        = _swapRouter;
    }

    event Deposit(address indexed token, uint256 amount);
    event Withdraw(address indexed token, uint256 amount);

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    function deposit(address token, uint256 amount) external onlyOwner {
        IERC20(token).safeTransferFrom(msg.sender, address(this), amount);
        emit Deposit(token, amount);
    }

    function withdraw(address token, uint256 amount) external onlyOwner {
        IERC20(token).safeTransfer(msg.sender, amount);
        emit Withdraw(token, amount);
    }

    function balance(address token) external view returns (uint256) {
        return IERC20(token).balanceOf(address(this));
    }

    function _splitFee(address token, uint256 amountIn) internal returns (uint256 netAmount) {
        uint256 fee = (amountIn * BASE_FEE_BPS) / BPS_DENOM;
        netAmount   = amountIn - fee;
        if (fee > 0) IERC20(token).safeTransfer(engineeringWallet, fee);
    }

    function executeEntry(address tokenIn, address tokenOut, uint256 amountIn, uint256 minOut, uint24 poolFee)
        external onlyOwner returns (uint256 amountOut)
    {
        uint256 net = _splitFee(tokenIn, amountIn);
        IERC20(tokenIn).forceApprove(swapRouter, net);
        amountOut = ISwapRouter(swapRouter).exactInputSingle(
            ISwapRouter.ExactInputSingleParams(tokenIn, tokenOut, poolFee, address(this), net, minOut, 0)
        );
        positionSize      = amountOut;
        averageEntryPrice = (amountIn * 1e18) / amountOut;
        drawdownTier      = 0;
        emit InitialEntry(tokenOut, amountIn, amountOut, amountIn - net);
    }

    function executeTrancheAvgDown(address tokenIn, address tokenOut, uint256 amountIn, uint256 minOut, uint24 poolFee)
        external onlyOwner returns (uint256 amountOut)
    {
        uint256 net = _splitFee(tokenIn, amountIn);
        IERC20(tokenIn).forceApprove(swapRouter, net);
        amountOut = ISwapRouter(swapRouter).exactInputSingle(
            ISwapRouter.ExactInputSingleParams(tokenIn, tokenOut, poolFee, address(this), net, minOut, 0)
        );
        uint256 totalCost = (averageEntryPrice * positionSize + amountIn * 1e18) / (positionSize + amountOut);
        averageEntryPrice = totalCost;
        positionSize     += amountOut;
        drawdownTier++;
        emit TrancheAvgDown(tokenOut, amountIn, amountOut, drawdownTier);
    }

    function executeTakeProfitExit(address tokenIn, address tokenOut, uint256 amountIn, uint256 minOut, uint24 poolFee)
        external onlyOwner returns (uint256 amountOut)
    {
        IERC20(tokenIn).forceApprove(swapRouter, amountIn);
        amountOut = ISwapRouter(swapRouter).exactInputSingle(
            ISwapRouter.ExactInputSingleParams(tokenIn, tokenOut, poolFee, address(this), amountIn, minOut, 0)
        );
        uint256 entryValue = (averageEntryPrice * positionSize) / 1e18;
        if (amountOut * SQUEEZE_TARGET > entryValue * 100) {
            uint256 gain   = amountOut - entryValue;
            uint256 royalty = (gain * PERF_ROYALTY_BPS) / BPS_DENOM;
            if (royalty > 0) IERC20(tokenOut).safeTransfer(engineeringWallet, royalty);
            emit TakeProfitExit(tokenOut, amountOut, royalty);
        } else {
            emit TakeProfitExit(tokenOut, amountOut, 0);
        }
        positionSize      = 0;
        averageEntryPrice = 0;
        drawdownTier      = 0;
    }

    function executeStructuralStopOut(address tokenIn, address tokenOut, uint256 amountIn, uint256 minOut, uint24 poolFee)
        external onlyOwner returns (uint256 amountOut)
    {
        IERC20(tokenIn).forceApprove(swapRouter, amountIn);
        amountOut = ISwapRouter(swapRouter).exactInputSingle(
            ISwapRouter.ExactInputSingleParams(tokenIn, tokenOut, poolFee, address(this), amountIn, minOut, 0)
        );
        positionSize      = 0;
        averageEntryPrice = 0;
        drawdownTier      = 0;
        emit StructuralStopOut(tokenOut, amountOut);
    }

    receive()  external payable { revert("no ETH custody"); }
    fallback()  external payable { revert("no ETH custody"); }
}

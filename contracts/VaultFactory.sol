// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/proxy/Clones.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

interface ISqueezeVault {
    function initialize(address owner, address engineeringWallet, address swapRouter) external;
}

/**
 * VaultFactory — EIP-1167 Minimal Proxy Factory for SqueezeVault.
 *
 * Deployment cost target: < $0.50 on Base / Arbitrum L2.
 * Each clone is ~45 bytes of bytecode pointing at the master implementation.
 */
contract VaultFactory is Ownable {
    address public immutable implementation;
    address public immutable engineeringWallet;
    address public immutable swapRouter;

    mapping(address => address) public vaultOf;   // user => vault clone
    address[] public allVaults;

    event VaultDeployed(address indexed user, address indexed vault);

    constructor(
        address _implementation,
        address _engineeringWallet,
        address _swapRouter
    ) Ownable(msg.sender) {
        implementation    = _implementation;
        engineeringWallet = _engineeringWallet;
        swapRouter        = _swapRouter;
    }

    /**
     * @notice Deploy a minimal proxy vault for the caller.
     *         One vault per address — reverts if already deployed.
     */
    function deployVault() external returns (address vault) {
        require(vaultOf[msg.sender] == address(0), "vault exists");

        vault = Clones.clone(implementation);
        ISqueezeVault(vault).initialize(msg.sender, engineeringWallet, swapRouter);

        vaultOf[msg.sender] = vault;
        allVaults.push(vault);

        emit VaultDeployed(msg.sender, vault);
    }

    function totalVaults() external view returns (uint256) {
        return allVaults.length;
    }
}

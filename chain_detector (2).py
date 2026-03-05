"""
chain_detector.py — Auto-detect which blockchain a contract address belongs to.
"""

import re
import aiohttp
import asyncio
import os

HELIUS_RPC   = f"https://mainnet.helius-rpc.com/?api-key={os.environ.get('HELIUS_API_KEY','')}"
ETHERSCAN_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
BASESCAN_KEY  = os.environ.get("BASESCAN_API_KEY", "")

# EVM address: 0x + 40 hex chars
EVM_PATTERN  = re.compile(r'^0x[0-9a-fA-F]{40}$')
# Solana address: base58, 32-44 chars
SOL_PATTERN  = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')


def is_evm_address(ca: str) -> bool:
    return bool(EVM_PATTERN.match(ca))

def is_solana_address(ca: str) -> bool:
    return bool(SOL_PATTERN.match(ca)) and not ca.startswith("0x")


async def detect_chain(ca: str) -> str:
    """
    Returns: 'solana', 'base', 'ethereum', or 'unknown'
    For EVM addresses, checks Base first (faster/cheaper), then Ethereum.
    """
    if is_solana_address(ca):
        return "solana"

    if not is_evm_address(ca):
        return "unknown"

    # Check Base and Ethereum in parallel
    async with aiohttp.ClientSession() as session:
        base_task = asyncio.create_task(_check_evm_chain(session, ca, "base"))
        eth_task  = asyncio.create_task(_check_evm_chain(session, ca, "ethereum"))
        base_found, eth_found = await asyncio.gather(base_task, eth_task)

    if base_found:
        return "base"
    if eth_found:
        return "ethereum"
    # Default for EVM if we can't confirm — try Base first as it's likely
    return "base"


async def _check_evm_chain(session: aiohttp.ClientSession, ca: str, chain: str) -> bool:
    """Check if a contract exists on the given chain."""
    try:
        if chain == "base":
            url = f"https://api.basescan.org/api?module=contract&action=getsourcecode&address={ca}&apikey={BASESCAN_KEY}"
        else:
            url = f"https://api.etherscan.io/api?module=contract&action=getsourcecode&address={ca}&apikey={ETHERSCAN_KEY}"

        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json()
            result = data.get("result", [])
            if isinstance(result, list) and result:
                return bool(result[0].get("ContractName") or result[0].get("ABI"))
    except Exception:
        pass
    return False


def chain_emoji(chain: str) -> str:
    return {"solana": "◎", "base": "🔵", "ethereum": "⟠"}.get(chain, "🔗")

def chain_name(chain: str) -> str:
    return {"solana": "Solana", "base": "Base", "ethereum": "Ethereum"}.get(chain, chain.title())

def get_explorer_url(ca: str, chain: str) -> str:
    if chain == "solana":   return f"https://solscan.io/token/{ca}"
    if chain == "base":     return f"https://basescan.org/token/{ca}"
    if chain == "ethereum": return f"https://etherscan.io/token/{ca}"
    return ""

def get_dex_url(ca: str, chain: str) -> str:
    if chain == "solana":   return f"https://pump.fun/coin/{ca}"
    if chain == "base":     return f"https://app.uniswap.org/#/swap?outputCurrency={ca}&chain=base"
    if chain == "ethereum": return f"https://app.uniswap.org/#/swap?outputCurrency={ca}"
    return ""

"""
evm_scanner.py — Risk scanner for ETH and Base chain tokens.
Mirrors scanner.py functionality using Etherscan/Basescan + DexScreener + Alchemy.
"""

import os
import asyncio
import aiohttp
import logging
import time

logger = logging.getLogger(__name__)

ETHERSCAN_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
BASESCAN_KEY  = os.environ.get("BASESCAN_API_KEY", "")
_ALCHEMY_KEY  = os.environ.get("ALCHEMY_API_KEY", "")

def _get_rpc(chain: str) -> str:
    env_var = os.environ.get(f"RPC_HTTP_{chain.upper()}")
    if env_var:
        return env_var
    if _ALCHEMY_KEY:
        if chain == "BASE":
            return f"https://base-mainnet.g.alchemy.com/v2/{_ALCHEMY_KEY}"
        return f"https://eth-mainnet.g.alchemy.com/v2/{_ALCHEMY_KEY}"
    # Ankr free public endpoints — no signup needed
    if chain == "BASE":
        return "https://rpc.ankr.com/base"
    return "https://rpc.ankr.com/eth"

ALCHEMY_ETH   = _get_rpc("ETH")
ALCHEMY_BASE  = _get_rpc("BASE")

CHAIN_CONFIG = {
    "ethereum": {
        "api":      "https://api.etherscan.io/api",
        "key":      ETHERSCAN_KEY,
        "rpc":      ALCHEMY_ETH,
        "explorer": "https://etherscan.io",
    },
    "base": {
        "api":      "https://api.basescan.org/api",
        "key":      BASESCAN_KEY,
        "rpc":      ALCHEMY_BASE,
        "explorer": "https://basescan.org",
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY
# ═══════════════════════════════════════════════════════════════════════════════

async def scan_evm_token(ca: str, chain: str) -> dict:
    async with aiohttp.ClientSession() as session:
        meta_task    = asyncio.create_task(get_evm_token_meta(session, ca, chain))
        supply_task  = asyncio.create_task(get_evm_supply(session, ca, chain))
        lp_task      = asyncio.create_task(get_evm_lp(session, ca, chain))
        holders_task = asyncio.create_task(get_evm_holders(session, ca, chain))
        dev_task     = asyncio.create_task(get_evm_dev_history(session, ca, chain))

        meta, supply_data, lp_data, holder_data, dev_data = await asyncio.gather(
            meta_task, supply_task, lp_task, holders_task, dev_task
        )

    combined = {**supply_data, **lp_data, **holder_data}
    combined["token_name"]   = meta.get("name", "Unknown")
    combined["token_symbol"] = meta.get("symbol", "???")
    combined["token_image"]  = meta.get("image", "")
    combined["chain"]        = chain
    combined["dev"]          = dev_data

    # Score each module
    ls = score_lp(lp_data)
    ss = score_supply(supply_data)
    hs = score_holders(holder_data)

    combined["risk_score"]    = int(ls * 0.35 + ss * 0.35 + hs * 0.30)
    combined["lp_risk"]       = risk_label(ls)
    combined["supply_risk"]   = risk_label(ss)
    combined["holder_risk"]   = risk_label(hs)
    combined["wallet_risk"]   = risk_label(hs)
    combined["mev_risk"]      = "N/A"
    combined["ai_summary"]    = generate_evm_summary(combined, ls, ss, hs)

    return combined


# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN METADATA
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_token_meta(session: aiohttp.ClientSession, ca: str, chain: str) -> dict:
    meta = {"name": "Unknown", "symbol": "???", "image": ""}

    # Source 1: DexScreener
    try:
        chain_id = "base" if chain == "base" else "ethereum"
        url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == chain_id]
                if pairs:
                    token = pairs[0].get("baseToken", {})
                    meta["name"]   = token.get("name", "Unknown")
                    meta["symbol"] = token.get("symbol", "???")
                    if meta["name"] != "Unknown":
                        return meta
    except Exception:
        pass

    # Source 2: Etherscan/Basescan token info
    try:
        cfg = CHAIN_CONFIG[chain]
        url = f"{cfg['api']}?module=token&action=tokeninfo&contractaddress={ca}&apikey={cfg['key']}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                data = await resp.json()
                result = data.get("result", [])
                if isinstance(result, list) and result:
                    meta["name"]   = result[0].get("tokenName", "Unknown")
                    meta["symbol"] = result[0].get("symbol", "???")
    except Exception:
        pass

    return meta


# ═══════════════════════════════════════════════════════════════════════════════
# SUPPLY CONCENTRATION
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_supply(session: aiohttp.ClientSession, ca: str, chain: str) -> dict:
    """
    Get supply concentration using Alchemy JSON-RPC + Moralis public API.
    No Etherscan/Basescan required.
    """
    try:
        cfg = CHAIN_CONFIG[chain]
        rpc = cfg["rpc"]

        # Step 1: get total supply via eth_call to totalSupply()
        # totalSupply() selector = 0x18160ddd
        supply_payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "eth_call",
            "params": [{"to": ca, "data": "0x18160ddd"}, "latest"]
        }
        async with session.post(rpc, json=supply_payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            supply_data = await resp.json()
        raw_supply = supply_data.get("result", "0x0")
        total_supply_raw = int(raw_supply, 16) if raw_supply and raw_supply != "0x" else 0

        # Step 2: get decimals via decimals() selector = 0x313ce567
        dec_payload = {
            "jsonrpc": "2.0", "id": 2,
            "method": "eth_call",
            "params": [{"to": ca, "data": "0x313ce567"}, "latest"]
        }
        async with session.post(rpc, json=dec_payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            dec_data = await resp.json()
        raw_dec = dec_data.get("result", "0x12")
        decimals = int(raw_dec, 16) if raw_dec and raw_dec != "0x" else 18
        total_supply = total_supply_raw / (10 ** decimals) if decimals else total_supply_raw

        if total_supply == 0:
            return _supply_defaults()

        # Step 3: get top holders via Moralis free API (no key needed for basic)
        moralis_chain = "base" if chain == "base" else "eth"
        holders_url = f"https://deep-index.moralis.io/api/v2.2/erc20/{ca}/owners?chain={moralis_chain}&limit=20&order=DESC"
        moralis_key = os.environ.get("MORALIS_API_KEY", "")

        amounts = []
        holder_count = "N/A"

        if moralis_key:
            async with session.get(
                holders_url,
                headers={"X-API-Key": moralis_key},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for h in data.get("result", []):
                        bal = float(h.get("balance_formatted") or h.get("balance", 0))
                        if bal > 0:
                            amounts.append(bal)
                    holder_count = data.get("cursor") and len(amounts) or len(amounts)

        # Fallback: use Transfer events to estimate top holders
        if not amounts:
            amounts, holder_count = await _get_holders_from_transfers(session, ca, chain, total_supply)

        if not amounts:
            return {**_supply_defaults(), "total_supply": total_supply}

        amounts = sorted(amounts, reverse=True)
        shares  = [a / total_supply for a in amounts if a > 0]

        if not shares:
            return _supply_defaults()

        top10 = round(sum(shares[:10]) * 100, 1)
        top1  = round(shares[0] * 100, 2)
        gini  = round(_gini(shares), 2)

        return {
            "top10_pct":    top10,
            "top1_pct":     top1,
            "gini":         gini,
            "holder_count": holder_count,
        }
    except Exception as e:
        logger.warning(f"[EVM] supply error: {e}", exc_info=True)
        return _supply_defaults()


async def _get_holders_from_transfers(session, ca, chain, total_supply):
    """Estimate holders by scanning Transfer events via Alchemy."""
    try:
        cfg = CHAIN_CONFIG[chain]
        rpc = cfg["rpc"]

        # Get latest block
        block_payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
        async with session.post(rpc, json=block_payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            block_data = await resp.json()
        latest_block = int(block_data.get("result", "0x0"), 16)
        from_block   = hex(max(0, latest_block - 5000))  # last ~5000 blocks

        # Transfer event topic
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        log_payload = {
            "jsonrpc": "2.0", "id": 2,
            "method": "eth_getLogs",
            "params": [{
                "address": ca,
                "topics": [transfer_topic],
                "fromBlock": from_block,
                "toBlock": "latest"
            }]
        }
        async with session.post(rpc, json=log_payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            log_data = await resp.json()

        logs = log_data.get("result", [])
        if not logs:
            return [], "N/A"

        # Count unique recipients
        from collections import Counter
        recipient_counts = Counter()
        for log in logs:
            topics = log.get("topics", [])
            if len(topics) >= 3:
                to_addr = "0x" + topics[2][-40:]
                data_hex = log.get("data", "0x0")
                try:
                    amount = int(data_hex, 16)
                    recipient_counts[to_addr.lower()] += amount
                except Exception:
                    pass

        if not recipient_counts:
            return [], "N/A"

        # Convert raw amounts with estimated decimals
        amounts = sorted([v for v in recipient_counts.values() if v > 0], reverse=True)
        # Normalize — just use relative shares
        total = sum(amounts)
        normalized = [a / total * total_supply for a in amounts[:20]]

        return normalized, len(recipient_counts)

    except Exception as e:
        logger.warning(f"[EVM] transfer scan error: {e}")
        return [], "N/A"

def _supply_defaults():
    return {"top10_pct": "N/A", "top1_pct": "N/A", "gini": "N/A", "holder_count": "N/A"}

def _gini(shares):
    if not shares: return 0
    n = len(shares)
    s = sorted(shares)
    cumsum = sum((2*(i+1) - n - 1) * x for i, x in enumerate(s))
    total = sum(s)
    return cumsum / (n * total) if total > 0 else 0


# ═══════════════════════════════════════════════════════════════════════════════
# LIQUIDITY / LP
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_lp(session: aiohttp.ClientSession, ca: str, chain: str) -> dict:
    try:
        chain_id = "base" if chain == "base" else "ethereum"
        url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return _lp_defaults()
            data = await resp.json()

        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == chain_id]
        if not pairs:
            return _lp_defaults()

        # Use the pair with highest liquidity
        pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd") or 0), reverse=True)
        best = pairs[0]

        liq     = float(best.get("liquidity", {}).get("usd") or 0)
        vol_24h = float(best.get("volume", {}).get("h24") or 0)
        mc      = float(best.get("fdv") or best.get("marketCap") or 0)
        price   = best.get("priceUsd", "0")
        dex     = best.get("dexId", "unknown")

        if liq == 0:      lp_status = "⚠ No liquidity found"
        elif liq < 1000:  lp_status = f"🔴 Very low — ${liq:,.0f} (rug risk)"
        elif liq < 10000: lp_status = f"🟡 Low — ${liq:,.0f} on {dex}"
        elif liq < 50000: lp_status = f"🟢 Moderate — ${liq:,.0f} on {dex}"
        else:             lp_status = f"🟢 Strong — ${liq:,.0f} on {dex}"

        return {
            "lp_locked":       lp_status,
            "lp_lock_duration": "Verify on DexTools/DexScreener",
            "lp_liquidity_usd": liq,
            "volume_24h":       vol_24h,
            "market_cap":       mc,
            "price":            price,
            "dex":              dex,
        }
    except Exception as e:
        logger.warning(f"[EVM] LP error: {e}")
        return _lp_defaults()

def _lp_defaults():
    return {"lp_locked": "N/A", "lp_lock_duration": "N/A", "lp_liquidity_usd": 0,
            "volume_24h": 0, "market_cap": 0, "price": "0", "dex": "N/A"}


# ═══════════════════════════════════════════════════════════════════════════════
# HOLDER / WALLET ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_holders(session: aiohttp.ClientSession, ca: str, chain: str) -> dict:
    """Get wallet activity using Alchemy eth_getLogs — no Etherscan needed."""
    try:
        cfg  = CHAIN_CONFIG[chain]
        rpc  = cfg["rpc"]
        zero = "0x0000000000000000000000000000000000000000"

        # Get latest block
        block_payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
        async with session.post(rpc, json=block_payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            block_data = await resp.json()
        latest_block = int(block_data.get("result", "0x0"), 16)

        # Scan last 10000 blocks (~8 hours on Base, ~1.3 hours on ETH)
        from_block = hex(max(0, latest_block - 10000))
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

        log_payload = {
            "jsonrpc": "2.0", "id": 2,
            "method": "eth_getLogs",
            "params": [{
                "address": ca,
                "topics": [transfer_topic],
                "fromBlock": from_block,
                "toBlock": "latest"
            }]
        }
        async with session.post(rpc, json=log_payload, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            log_data = await resp.json()

        logs = log_data.get("result", [])
        if not logs:
            return _holder_defaults()

        # Get block timestamps for freshness check
        cutoff_blocks = latest_block - 7200  # ~24h on Base (2s blocks), ~1h on ETH

        wallets      = set()
        fresh_wallets = set()
        zero_lower   = zero.lower()
        from collections import Counter
        sender_counts = Counter()

        for log in logs:
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue

            from_addr = ("0x" + topics[1][-40:]).lower()
            to_addr   = ("0x" + topics[2][-40:]).lower()
            blk_hex   = log.get("blockNumber", "0x0")
            blk_num   = int(blk_hex, 16)

            is_fresh = blk_num >= cutoff_blocks

            if to_addr and to_addr != zero_lower:
                wallets.add(to_addr)
                if is_fresh:
                    fresh_wallets.add(to_addr)

            if from_addr and from_addr != zero_lower:
                wallets.add(from_addr)
                sender_counts[from_addr] += 1

        wallet_count = len(wallets)
        if wallet_count == 0:
            return _holder_defaults()

        fresh_pct   = round(len(fresh_wallets) / wallet_count * 100, 1)
        clustered   = sum(1 for c in sender_counts.values() if c >= 3)
        cluster_pct = round(min(clustered / max(wallet_count, 1) * 100, 100), 1)

        return {
            "wallet_count":     wallet_count,
            "fresh_wallet_pct": fresh_pct,
            "cluster_pct":      cluster_pct,
        }
    except Exception as e:
        logger.warning(f"[EVM] holders error: {e}", exc_info=True)
        return _holder_defaults()

def _holder_defaults():
    return {"wallet_count": "N/A", "fresh_wallet_pct": "N/A", "cluster_pct": "N/A"}


# ═══════════════════════════════════════════════════════════════════════════════
# DEV HISTORY
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_dev_history(session: aiohttp.ClientSession, ca: str, chain: str) -> dict:
    """
    Find deployer and their history using Alchemy + DexScreener only.
    No Etherscan/Basescan required.
    """
    try:
        cfg      = CHAIN_CONFIG[chain]
        rpc      = cfg["rpc"]
        chain_id = "base" if chain == "base" else "ethereum"

        # Step 1: find deployer from transaction receipt (contractCreator)
        # Use eth_getTransactionReceipt on the token's creation tx
        # First get the code to confirm it's a contract
        code_payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "eth_getCode",
            "params": [ca, "latest"]
        }
        async with session.post(rpc, json=code_payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            code_data = await resp.json()
        if code_data.get("result", "0x") in ("0x", "0x0", None):
            return {"deployer": None, "token_count": 0, "tokens": [], "risk": "N/A", "risk_note": "Not a contract."}

        # Use Alchemy alchemy_getAssetTransfers to find the contract creation
        transfers_payload = {
            "jsonrpc": "2.0", "id": 2,
            "method": "alchemy_getAssetTransfers",
            "params": [{
                "toAddress": ca,
                "category": ["external"],
                "maxCount": "0x1",
                "order": "asc",
                "withMetadata": True,
            }]
        }
        deployer = None
        async with session.post(rpc, json=transfers_payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                tr_data = await resp.json()
                transfers = tr_data.get("result", {}).get("transfers", [])
                if transfers:
                    deployer = transfers[0].get("from", "")

        # Fallback: use DexScreener pair info which often has deployer
        if not deployer:
            try:
                dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
                async with session.get(dex_url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    if resp.status == 200:
                        dex = await resp.json()
                        pairs = [p for p in (dex.get("pairs") or []) if p.get("chainId") == chain_id]
                        if pairs:
                            deployer = pairs[0].get("info", {}).get("deployer", "") or ""
            except Exception:
                pass

        if not deployer:
            return {
                "deployer": None, "token_count": 0, "tokens": [],
                "risk": "⚠️ Unknown", "risk_note": "Could not identify deployer.",
                "summary": "Deployer wallet could not be identified.",
                "biggest_mc": 0, "dead_count": 0, "token_lines": [],
            }

        # Step 2: find other tokens by this deployer via DexScreener search
        deployed_tokens = []
        try:
            search_url = f"https://api.dexscreener.com/latest/dex/search?q={deployer}"
            async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    search_data = await resp.json()
                    for pair in search_data.get("pairs", []) or []:
                        if pair.get("chainId") != chain_id:
                            continue
                        pair_ca = pair.get("baseToken", {}).get("address", "").lower()
                        if pair_ca == ca.lower():
                            continue  # skip current token
                        mc   = float(pair.get("fdv") or pair.get("marketCap") or 0)
                        name = pair.get("baseToken", {}).get("name", "Unknown")
                        sym  = pair.get("baseToken", {}).get("symbol", "???")
                        deployed_tokens.append({"name": name, "symbol": sym, "mc": mc})
        except Exception as e:
            logger.warning(f"[EVM] DexScreener dev search error: {e}")

        total      = len(deployed_tokens)
        biggest_mc = max((t["mc"] for t in deployed_tokens), default=0)
        biggest    = next((t for t in deployed_tokens if t["mc"] == biggest_mc), {})
        biggest_name = biggest.get("name", "")
        dead       = sum(1 for t in deployed_tokens if t["mc"] == 0)

        def fmt_mc(mc):
            if mc >= 1_000_000: return f"${mc/1_000_000:.2f}M"
            if mc >= 1_000:     return f"${mc/1_000:.1f}K"
            return f"${mc:.0f}"

        if biggest_mc >= 500_000:
            risk = "🟢 Proven dev"
            risk_note = f"Previous launch hit {fmt_mc(biggest_mc)}"
        elif biggest_mc >= 100_000:
            risk = "🟡 Some track record"
            risk_note = f"Best launch: {fmt_mc(biggest_mc)}"
        elif total == 0:
            risk = "🆕 First deployment"
            risk_note = "No other tokens found for this deployer"
        elif dead > total * 0.7 and total > 2:
            risk = "🔴 Serial deployer"
            risk_note = f"{dead}/{total} previous tokens are dead"
        else:
            risk = "🟠 Low track record"
            risk_note = "No significant launches found"

        tokens_sorted = sorted(deployed_tokens, key=lambda x: x["mc"], reverse=True)
        token_lines = [
            f"{i}. {t['name']} (${t['symbol']}) — {fmt_mc(t['mc'])}"
            for i, t in enumerate(tokens_sorted[:5], 1)
        ]

        summary = f"Deployer has {total} other token(s) on {chain_id.title()}."
        if biggest_name:
            summary += f" Best launch: {biggest_name} @ {fmt_mc(biggest_mc)}."

        return {
            "deployer":     deployer,
            "token_count":  total,
            "dead_count":   dead,
            "tokens":       deployed_tokens,
            "token_lines":  token_lines,
            "biggest_mc":   biggest_mc,
            "biggest_name": biggest_name,
            "risk":         risk,
            "risk_note":    risk_note,
            "summary":      summary,
        }

    except Exception as e:
        logger.error(f"[EVM] dev history error: {e}", exc_info=True)
        return {"deployer": None, "token_count": 0, "tokens": [], "risk": "N/A",
                "risk_note": str(e), "biggest_mc": 0, "dead_count": 0, "token_lines": []}


# ═══════════════════════════════════════════════════════════════════════════════
# SCORING
# ═══════════════════════════════════════════════════════════════════════════════

def score_lp(d):
    liq = d.get("lp_liquidity_usd", 0)
    try:
        liq = float(liq)
        if liq == 0:    return 80
        if liq < 1000:  return 90
        if liq < 5000:  return 70
        if liq < 20000: return 40
        return 20
    except Exception:
        return 50

def score_supply(d):
    score = 0
    top10 = d.get("top10_pct", 0)
    gini  = d.get("gini", 0)
    if isinstance(top10, (int, float)):
        if top10 > 80:   score += 60
        elif top10 > 50: score += 40
        elif top10 > 30: score += 20
    if isinstance(gini, (int, float)):
        score += int(gini * 40)
    return min(score, 100)

def score_holders(d):
    score = 0
    fp = d.get("fresh_wallet_pct", 0)
    cp = d.get("cluster_pct", 0)
    if isinstance(fp, (int, float)): score += min(fp * 0.5, 50)
    if isinstance(cp, (int, float)): score += min(cp * 0.8, 50)
    return int(min(score, 100))

def risk_label(score):
    if score <= 30: return "🟢 Low"
    if score <= 60: return "🟡 Medium"
    if score <= 80: return "🟠 High"
    return "🔴 Critical"


# ═══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════

def generate_evm_summary(d, ls, ss, hs) -> str:
    flags, advice = [], []

    liq = d.get("lp_liquidity_usd", 0)
    try:
        liq = float(liq)
        if liq < 1000:
            flags.append("critically low liquidity")
            advice.append("Avoid entry until liquidity improves.")
        elif liq < 5000:
            flags.append("low liquidity")
            advice.append("Use small position sizes.")
    except Exception:
        pass

    top10 = d.get("top10_pct", 0)
    top1  = d.get("top1_pct", 0)
    if isinstance(top1, (int, float)) and top1 > 20:
        flags.append(f"single wallet holds {top1}% of supply")
        advice.append("Single-wallet dump risk is very high.")
    if isinstance(top10, (int, float)) and top10 > 50:
        flags.append(f"top 10 wallets hold {top10}% of supply")

    fp = d.get("fresh_wallet_pct", 0)
    if isinstance(fp, (int, float)) and fp > 50:
        flags.append(f"high fresh wallet activity ({fp}%)")

    overall = d.get("risk_score", 0)
    if overall <= 30:   opener = "✅ Relatively low risk."
    elif overall <= 60: opener = "⚠️ Moderate risk — proceed with caution."
    elif overall <= 80: opener = "🚨 High risk — significant red flags."
    else:               opener = "🔴 Critical risk — multiple severe red flags."

    flag_str = ("Key concerns: " + "; ".join(flags) + ".") if flags else "No major red flags detected."
    action   = " ".join(advice) if advice else "Always verify LP lock status before trading."
    return f"{opener} {flag_str} {action}"

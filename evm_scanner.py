"""
evm_scanner.py — Risk scanner for ETH and Base chain tokens.
Uses Etherscan/Basescan APIs + DexScreener. No Alchemy needed.
"""

import os
import asyncio
import aiohttp
import logging
import time
from collections import Counter

logger = logging.getLogger(__name__)

ETHERSCAN_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
BASESCAN_KEY  = os.environ.get("BASESCAN_API_KEY", "")

# QuickNode or Ankr fallback for RPC calls
_QN_ETH  = os.environ.get("RPC_HTTP_ETH",  "https://rpc.ankr.com/eth")
_QN_BASE = os.environ.get("RPC_HTTP_BASE", "https://rpc.ankr.com/base")

CHAIN_CONFIG = {
    "ethereum": {
        "api":      "https://api.etherscan.io/api",
        "key":      ETHERSCAN_KEY,
        "rpc":      _QN_ETH,
        "explorer": "https://etherscan.io",
        "chain_id": "ethereum",
    },
    "base": {
        "api":      "https://api.basescan.org/api",
        "key":      BASESCAN_KEY,
        "rpc":      _QN_BASE,
        "explorer": "https://basescan.org",
        "chain_id": "base",
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY
# ═══════════════════════════════════════════════════════════════════════════════

async def scan_evm_token(ca: str, chain: str) -> dict:
    async with aiohttp.ClientSession() as session:
        meta, supply_data, lp_data, holder_data, dev_data = await asyncio.gather(
            get_evm_token_meta(session, ca, chain),
            get_evm_supply(session, ca, chain),
            get_evm_lp(session, ca, chain),
            get_evm_holders(session, ca, chain),
            get_evm_dev_history(session, ca, chain),
        )

    combined = {**supply_data, **lp_data, **holder_data}
    combined["token_name"]   = meta.get("name", "Unknown")
    combined["token_symbol"] = meta.get("symbol", "???")
    combined["token_image"]  = meta.get("image", "")
    combined["chain"]        = chain
    combined["dev"]          = dev_data

    ls = score_lp(lp_data)
    ss = score_supply(supply_data)
    hs = score_holders(holder_data)

    combined["risk_score"]  = int(ls * 0.35 + ss * 0.35 + hs * 0.30)
    combined["lp_risk"]     = risk_label(ls)
    combined["supply_risk"] = risk_label(ss)
    combined["holder_risk"] = risk_label(hs)
    combined["wallet_risk"] = risk_label(hs)
    combined["mev_risk"]    = "N/A"
    combined["ai_summary"]  = generate_evm_summary(combined, ls, ss, hs)

    return combined


# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN METADATA
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_token_meta(session: aiohttp.ClientSession, ca: str, chain: str) -> dict:
    meta = {"name": "Unknown", "symbol": "???", "image": ""}

    # Source 1: DexScreener
    try:
        chain_id = CHAIN_CONFIG[chain]["chain_id"]
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
# SUPPLY CONCENTRATION — uses Etherscan/Basescan tokenholderlist
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_supply(session: aiohttp.ClientSession, ca: str, chain: str) -> dict:
    try:
        cfg = CHAIN_CONFIG[chain]
        key = cfg["key"]

        if not key:
            logger.warning(f"[EVM] No API key for {chain} — supply N/A")
            return _supply_defaults()

        # Get top 20 holders
        url = f"{cfg['api']}?module=token&action=tokenholderlist&contractaddress={ca}&page=1&offset=20&apikey={key}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            data = await resp.json()

        logger.info(f"[EVM] tokenholderlist status={data.get('status')} msg={data.get('message')} count={len(data.get('result', []) or [])}")

        holders = data.get("result", [])
        if not isinstance(holders, list) or not holders:
            # Fallback: get supply from RPC and holders from transfers
            return await _supply_from_rpc(session, ca, chain)

        # Get total supply
        supply_url = f"{cfg['api']}?module=stats&action=tokensupply&contractaddress={ca}&apikey={key}"
        async with session.get(supply_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            supply_data = await resp.json()
        total_supply = float(supply_data.get("result", 0) or 0)

        if total_supply == 0:
            total_supply = sum(float(h.get("TokenHolderQuantity", 0)) for h in holders)

        if total_supply == 0:
            return _supply_defaults()

        amounts = sorted([float(h.get("TokenHolderQuantity", 0)) for h in holders], reverse=True)
        shares  = [a / total_supply for a in amounts if a > 0]
        top10   = round(sum(shares[:10]) * 100, 1)
        top1    = round(shares[0] * 100, 2) if shares else 0
        gini    = round(_gini(shares), 2)

        return {"top10_pct": top10, "top1_pct": top1, "gini": gini, "holder_count": len(holders)}

    except Exception as e:
        logger.warning(f"[EVM] supply error: {e}", exc_info=True)
        return _supply_defaults()


async def _supply_from_rpc(session, ca, chain):
    """Fallback supply estimation using Transfer event logs via RPC."""
    try:
        rpc = CHAIN_CONFIG[chain]["rpc"]

        # Get total supply
        supply_payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                          "params": [{"to": ca, "data": "0x18160ddd"}, "latest"]}
        async with session.post(rpc, json=supply_payload, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            s = await resp.json()
        raw = s.get("result", "0x0")
        total_raw = int(raw, 16) if raw and raw not in ("0x", "0x0") else 0

        dec_payload = {"jsonrpc": "2.0", "id": 2, "method": "eth_call",
                       "params": [{"to": ca, "data": "0x313ce567"}, "latest"]}
        async with session.post(rpc, json=dec_payload, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            d = await resp.json()
        raw_dec = d.get("result", "0x12")
        decimals = int(raw_dec, 16) if raw_dec and raw_dec not in ("0x", "0x0") else 18
        total_supply = total_raw / (10 ** decimals) if total_raw > 0 else 0

        if total_supply == 0:
            return _supply_defaults()

        # Get latest block
        blk_payload = {"jsonrpc": "2.0", "id": 3, "method": "eth_blockNumber", "params": []}
        async with session.post(rpc, json=blk_payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            blk = await resp.json()
        latest = int(blk.get("result", "0x0"), 16)
        from_block = hex(max(0, latest - 50000))

        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        logs_payload = {"jsonrpc": "2.0", "id": 4, "method": "eth_getLogs",
                        "params": [{"address": ca, "topics": [transfer_topic],
                                    "fromBlock": from_block, "toBlock": "latest"}]}
        async with session.post(rpc, json=logs_payload, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            logs_data = await resp.json()

        logs = logs_data.get("result", [])
        logger.info(f"[EVM] RPC supply logs: {len(logs)}")

        if not logs:
            return {**_supply_defaults(), "holder_count": "< data range"}

        balances = Counter()
        zero = "0" * 64
        for log in logs:
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue
            from_addr = topics[1][-40:]
            to_addr   = topics[2][-40:]
            try:
                amount = int(log.get("data", "0x0"), 16) / (10 ** decimals)
            except Exception:
                continue
            if to_addr != zero[-40:]:
                balances[to_addr] += amount
            if from_addr != zero[-40:]:
                balances[from_addr] -= amount

        positive = sorted([v for v in balances.values() if v > 0], reverse=True)
        if not positive:
            return _supply_defaults()

        total = sum(positive)
        shares = [a / total for a in positive]
        return {
            "top10_pct":    round(sum(shares[:10]) * 100, 1),
            "top1_pct":     round(shares[0] * 100, 2),
            "gini":         round(_gini(shares), 2),
            "holder_count": len(positive),
        }
    except Exception as e:
        logger.warning(f"[EVM] RPC supply fallback error: {e}")
        return _supply_defaults()


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
# LIQUIDITY / LP — DexScreener
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_lp(session: aiohttp.ClientSession, ca: str, chain: str) -> dict:
    try:
        chain_id = CHAIN_CONFIG[chain]["chain_id"]
        url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return _lp_defaults()
            data = await resp.json()

        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == chain_id]
        if not pairs:
            return _lp_defaults()

        pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd") or 0), reverse=True)
        best = pairs[0]

        liq   = float(best.get("liquidity", {}).get("usd") or 0)
        vol   = float(best.get("volume", {}).get("h24") or 0)
        mc    = float(best.get("fdv") or best.get("marketCap") or 0)
        price = best.get("priceUsd", "0")
        dex   = best.get("dexId", "unknown")

        if liq == 0:      status = "⚠ No liquidity found"
        elif liq < 1000:  status = f"🔴 Very low — ${liq:,.0f} (rug risk)"
        elif liq < 10000: status = f"🟡 Low — ${liq:,.0f} on {dex}"
        elif liq < 50000: status = f"🟢 Moderate — ${liq:,.0f} on {dex}"
        else:             status = f"🟢 Strong — ${liq:,.0f} on {dex}"

        return {"lp_locked": status, "lp_lock_duration": "Verify on DexTools",
                "lp_liquidity_usd": liq, "volume_24h": vol,
                "market_cap": mc, "price": price, "dex": dex}
    except Exception as e:
        logger.warning(f"[EVM] LP error: {e}")
        return _lp_defaults()

def _lp_defaults():
    return {"lp_locked": "N/A", "lp_lock_duration": "N/A", "lp_liquidity_usd": 0,
            "volume_24h": 0, "market_cap": 0, "price": "0", "dex": "N/A"}


# ═══════════════════════════════════════════════════════════════════════════════
# WALLET ACTIVITY — Etherscan tokentx + RPC fallback
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_holders(session: aiohttp.ClientSession, ca: str, chain: str) -> dict:
    try:
        cfg = CHAIN_CONFIG[chain]
        key = cfg["key"]

        if key:
            # Use Etherscan/Basescan token transfers
            url = f"{cfg['api']}?module=account&action=tokentx&contractaddress={ca}&page=1&offset=100&sort=desc&apikey={key}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                data = await resp.json()

            txs = data.get("result", [])
            logger.info(f"[EVM] tokentx count={len(txs) if isinstance(txs, list) else txs}")

            if isinstance(txs, list) and txs:
                cutoff = int(time.time()) - 86400
                wallets, fresh = set(), set()
                zero = "0x0000000000000000000000000000000000000000"
                sender_counts = Counter()

                for tx in txs:
                    to_addr   = tx.get("to", "").lower()
                    from_addr = tx.get("from", "").lower()
                    ts        = int(tx.get("timeStamp", 0))
                    if to_addr and to_addr != zero:
                        wallets.add(to_addr)
                        if ts > cutoff: fresh.add(to_addr)
                    if from_addr and from_addr != zero:
                        wallets.add(from_addr)
                        sender_counts[from_addr] += 1

                wc = len(wallets)
                clustered = sum(1 for c in sender_counts.values() if c >= 3)
                return {
                    "wallet_count":     wc,
                    "fresh_wallet_pct": round(len(fresh) / max(wc, 1) * 100, 1),
                    "cluster_pct":      round(min(clustered / max(wc, 1) * 100, 100), 1),
                }

        # RPC fallback via eth_getLogs
        return await _holders_from_rpc(session, ca, chain)

    except Exception as e:
        logger.warning(f"[EVM] holders error: {e}", exc_info=True)
        return _holder_defaults()


async def _holders_from_rpc(session, ca, chain):
    try:
        rpc = CHAIN_CONFIG[chain]["rpc"]
        blk_payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
        async with session.post(rpc, json=blk_payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            blk = await resp.json()
        latest = int(blk.get("result", "0x0"), 16)
        from_block = hex(max(0, latest - 50000))
        cutoff_block = latest - 43200

        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        logs_payload = {"jsonrpc": "2.0", "id": 2, "method": "eth_getLogs",
                        "params": [{"address": ca, "topics": [transfer_topic],
                                    "fromBlock": from_block, "toBlock": "latest"}]}
        async with session.post(rpc, json=logs_payload, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            logs_data = await resp.json()

        logs = logs_data.get("result", [])
        logger.info(f"[EVM] RPC holder logs: {len(logs)}")
        if not logs:
            return _holder_defaults()

        wallets, fresh = set(), set()
        zero = "0" * 40
        sender_counts = Counter()

        for log in logs:
            topics = log.get("topics", [])
            if len(topics) < 3: continue
            from_addr = topics[1][-40:]
            to_addr   = topics[2][-40:]
            blk_num   = int(log.get("blockNumber", "0x0"), 16)
            if to_addr != zero:
                wallets.add(to_addr)
                if blk_num >= cutoff_block: fresh.add(to_addr)
            if from_addr != zero:
                wallets.add(from_addr)
                sender_counts[from_addr] += 1

        wc = len(wallets)
        clustered = sum(1 for c in sender_counts.values() if c >= 3)
        return {
            "wallet_count":     wc,
            "fresh_wallet_pct": round(len(fresh) / max(wc, 1) * 100, 1),
            "cluster_pct":      round(min(clustered / max(wc, 1) * 100, 100), 1),
        }
    except Exception as e:
        logger.warning(f"[EVM] RPC holders error: {e}")
        return _holder_defaults()

def _holder_defaults():
    return {"wallet_count": "N/A", "fresh_wallet_pct": "N/A", "cluster_pct": "N/A"}


# ═══════════════════════════════════════════════════════════════════════════════
# DEV HISTORY — Etherscan + DexScreener
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_dev_history(session: aiohttp.ClientSession, ca: str, chain: str) -> dict:
    empty = {"deployer": None, "token_count": 0, "tokens": [], "risk": "N/A",
             "risk_note": "", "biggest_mc": 0, "dead_count": 0,
             "token_lines": [], "summary": "Could not identify deployer."}
    try:
        cfg      = CHAIN_CONFIG[chain]
        key      = cfg["key"]
        chain_id = cfg["chain_id"]

        deployer = None

        # Method 1: Etherscan getcontractcreation
        if key:
            url = f"{cfg['api']}?module=contract&action=getcontractcreation&contractaddresses={ca}&apikey={key}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
            result = data.get("result", [])
            logger.info(f"[EVM] contract creation result: {result}")
            if isinstance(result, list) and result:
                deployer = result[0].get("contractCreator", "")

        # Method 2: first mint tx via RPC
        if not deployer:
            deployer = await _find_deployer_rpc(session, ca, chain)

        if not deployer:
            return {**empty, "risk": "⚠️ Unknown", "risk_note": "Could not identify deployer."}

        # Find other tokens by this deployer
        deployed_tokens = []

        # Via Etherscan tx list
        if key:
            cutoff_60d = int(time.time()) - (60 * 86400)
            tx_url = f"{cfg['api']}?module=account&action=txlist&address={deployer}&page=1&offset=100&sort=desc&apikey={key}"
            async with session.get(tx_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                tx_data = await resp.json()
            txs = tx_data.get("result", [])
            if isinstance(txs, list):
                contracts = [
                    tx.get("contractAddress", "")
                    for tx in txs
                    if tx.get("to", "") == ""
                    and int(tx.get("timeStamp", 0)) >= cutoff_60d
                    and tx.get("contractAddress")
                ]
                logger.info(f"[EVM] deployer contracts found: {len(contracts)}")

                if contracts:
                    # Enrich with DexScreener
                    for i in range(0, len(contracts[:29]), 29):
                        chunk = contracts[i:i+29]
                        try:
                            dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
                            async with session.get(dex_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                                dex = await resp.json()
                            for pair in dex.get("pairs", []) or []:
                                if pair.get("chainId") != chain_id: continue
                                mc   = float(pair.get("fdv") or pair.get("marketCap") or 0)
                                name = pair.get("baseToken", {}).get("name", "Unknown")
                                sym  = pair.get("baseToken", {}).get("symbol", "???")
                                addr = pair.get("baseToken", {}).get("address", "").lower()
                                if addr != ca.lower():
                                    deployed_tokens.append({"name": name, "symbol": sym, "mc": mc})
                        except Exception:
                            pass

        # Also search DexScreener by deployer address
        if not deployed_tokens:
            try:
                search_url = f"https://api.dexscreener.com/latest/dex/search?q={deployer}"
                async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        sd = await resp.json()
                        for pair in sd.get("pairs", []) or []:
                            if pair.get("chainId") != chain_id: continue
                            addr = pair.get("baseToken", {}).get("address", "").lower()
                            if addr == ca.lower(): continue
                            mc   = float(pair.get("fdv") or pair.get("marketCap") or 0)
                            name = pair.get("baseToken", {}).get("name", "Unknown")
                            sym  = pair.get("baseToken", {}).get("symbol", "???")
                            deployed_tokens.append({"name": name, "symbol": sym, "mc": mc})
            except Exception:
                pass

        def fmt_mc(mc):
            if mc >= 1_000_000: return f"${mc/1_000_000:.2f}M"
            if mc >= 1_000:     return f"${mc/1_000:.1f}K"
            return f"${mc:.0f}"

        total      = len(deployed_tokens)
        biggest_mc = max((t["mc"] for t in deployed_tokens), default=0)
        biggest    = next((t for t in deployed_tokens if t["mc"] == biggest_mc), {})
        dead       = sum(1 for t in deployed_tokens if t["mc"] == 0)

        if biggest_mc >= 500_000:   risk = "🟢 Proven dev"
        elif biggest_mc >= 100_000: risk = "🟡 Some track record"
        elif total == 0:            risk = "🆕 First deployment"
        elif dead > total * 0.7:    risk = "🔴 Serial deployer"
        else:                       risk = "🟠 Low track record"

        tokens_sorted = sorted(deployed_tokens, key=lambda x: x["mc"], reverse=True)
        token_lines   = [f"{i}. {t['name']} (${t['symbol']}) — {fmt_mc(t['mc'])}"
                         for i, t in enumerate(tokens_sorted[:5], 1)]
        summary = f"Deployer has {total} other token(s). Best: {biggest.get('name','N/A')} @ {fmt_mc(biggest_mc)}." if total else f"No other tokens found for this deployer on {chain_id.title()}."

        return {
            "deployer": deployer, "token_count": total, "dead_count": dead,
            "tokens": deployed_tokens, "token_lines": token_lines,
            "biggest_mc": biggest_mc, "biggest_name": biggest.get("name", ""),
            "risk": risk, "risk_note": "", "summary": summary,
        }

    except Exception as e:
        logger.error(f"[EVM] dev history error: {e}", exc_info=True)
        return empty


async def _find_deployer_rpc(session, ca, chain):
    """Find deployer via first mint Transfer event."""
    try:
        rpc = CHAIN_CONFIG[chain]["rpc"]
        blk_payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
        async with session.post(rpc, json=blk_payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            blk = await resp.json()
        latest = int(blk.get("result", "0x0"), 16)

        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        zero_topic     = "0x0000000000000000000000000000000000000000000000000000000000000000"
        from_block = hex(max(0, latest - 500000))

        logs_payload = {"jsonrpc": "2.0", "id": 2, "method": "eth_getLogs",
                        "params": [{"address": ca, "topics": [transfer_topic, zero_topic],
                                    "fromBlock": from_block, "toBlock": "latest"}]}
        async with session.post(rpc, json=logs_payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            logs_data = await resp.json()

        logs = logs_data.get("result", [])
        if not logs:
            return None

        creation_tx = logs[0].get("transactionHash", "")
        if not creation_tx:
            return None

        tx_payload = {"jsonrpc": "2.0", "id": 3, "method": "eth_getTransactionByHash",
                      "params": [creation_tx]}
        async with session.post(rpc, json=tx_payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            tx_data = await resp.json()
        tx = tx_data.get("result", {})
        return tx.get("from", "") if tx else None

    except Exception as e:
        logger.warning(f"[EVM] RPC deployer error: {e}")
        return None


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
    except: return 50

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

def generate_evm_summary(d, ls, ss, hs) -> str:
    flags, advice = [], []
    liq = d.get("lp_liquidity_usd", 0)
    try:
        liq = float(liq)
        if liq < 1000:  flags.append("critically low liquidity"); advice.append("Avoid until liquidity improves.")
        elif liq < 5000: flags.append("low liquidity"); advice.append("Use small position sizes.")
    except: pass
    top1 = d.get("top1_pct", 0)
    top10 = d.get("top10_pct", 0)
    if isinstance(top1, (int, float)) and top1 > 20:
        flags.append(f"single wallet holds {top1}% of supply"); advice.append("Single-wallet dump risk is very high.")
    if isinstance(top10, (int, float)) and top10 > 50:
        flags.append(f"top 10 hold {top10}% of supply")
    overall = d.get("risk_score", 0)
    if overall <= 30:   opener = "✅ Relatively low risk."
    elif overall <= 60: opener = "⚠️ Moderate risk — proceed with caution."
    elif overall <= 80: opener = "🚨 High risk — significant red flags."
    else:               opener = "🔴 Critical risk — multiple severe red flags."
    flag_str = ("Key concerns: " + "; ".join(flags) + ".") if flags else "No major red flags detected."
    action   = " ".join(advice) if advice else "Always verify LP lock status before trading."
    return f"{opener} {flag_str} {action}"

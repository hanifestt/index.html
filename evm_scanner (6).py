"""
evm_scanner.py — ETH/Base scanner using QuickNode RPC + DexScreener.
No Etherscan Pro required. All data from standard JSON-RPC + free APIs.
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

_QN_ETH  = os.environ.get("RPC_HTTP_ETH",  "https://rpc.ankr.com/eth")
_QN_BASE = os.environ.get("RPC_HTTP_BASE", "https://rpc.ankr.com/base")

CHAIN_CONFIG = {
    "ethereum": {
        "api":      "https://api.etherscan.io/api",
        "key":      ETHERSCAN_KEY,
        "rpc":      _QN_ETH,
        "explorer": "https://etherscan.io",
        "chain_id": "ethereum",
        "weth":     "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    },
    "base": {
        "api":      "https://api.basescan.org/api",
        "key":      BASESCAN_KEY,
        "rpc":      _QN_BASE,
        "explorer": "https://basescan.org",
        "chain_id": "base",
        "weth":     "0x4200000000000000000000000000000000000006",
    },
}

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_TOPIC     = "0x0000000000000000000000000000000000000000000000000000000000000000"
ZERO_ADDR      = "0x0000000000000000000000000000000000000000"


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
# RPC HELPER
# ═══════════════════════════════════════════════════════════════════════════════

async def rpc_call(session, chain, method, params, timeout=10):
    rpc = CHAIN_CONFIG[chain]["rpc"]
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(rpc, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        data = await resp.json()
    return data.get("result")


# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN METADATA
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_token_meta(session, ca, chain):
    meta = {"name": "Unknown", "symbol": "???"}
    chain_id = CHAIN_CONFIG[chain]["chain_id"]

    # DexScreener first — fastest
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == chain_id]
                if pairs:
                    t = pairs[0].get("baseToken", {})
                    if t.get("name"):
                        return {"name": t["name"], "symbol": t.get("symbol", "???")}
    except Exception:
        pass

    # Fallback: read name() and symbol() directly from contract via eth_call
    # name() = 0x06fdde03, symbol() = 0x95d89b41
    try:
        name_hex   = await rpc_call(session, chain, "eth_call", [{"to": ca, "data": "0x06fdde03"}, "latest"])
        symbol_hex = await rpc_call(session, chain, "eth_call", [{"to": ca, "data": "0x95d89b41"}, "latest"])

        def decode_string(hex_val):
            if not hex_val or hex_val in ("0x", "0x0"): return None
            try:
                raw = bytes.fromhex(hex_val[2:])
                # ABI string: offset(32) + length(32) + data
                if len(raw) >= 64:
                    length = int.from_bytes(raw[32:64], "big")
                    return raw[64:64+length].decode("utf-8", errors="ignore").strip("\x00")
            except Exception:
                pass
            return None

        name   = decode_string(name_hex)
        symbol = decode_string(symbol_hex)
        if name:   meta["name"]   = name
        if symbol: meta["symbol"] = symbol
    except Exception as e:
        logger.warning(f"[EVM] meta RPC error: {e}")

    return meta


# ═══════════════════════════════════════════════════════════════════════════════
# SUPPLY CONCENTRATION — via Transfer event logs from RPC
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_supply(session, ca, chain):
    try:
        rpc = CHAIN_CONFIG[chain]["rpc"]

        # Get decimals
        dec_hex = await rpc_call(session, chain, "eth_call", [{"to": ca, "data": "0x313ce567"}, "latest"])
        decimals = int(dec_hex, 16) if dec_hex and dec_hex not in ("0x", "0x0") else 18

        # Get total supply
        ts_hex = await rpc_call(session, chain, "eth_call", [{"to": ca, "data": "0x18160ddd"}, "latest"])
        total_raw = int(ts_hex, 16) if ts_hex and ts_hex not in ("0x", "0x0") else 0
        total_supply = total_raw / (10 ** decimals)

        if total_supply == 0:
            return _supply_defaults()

        # Get latest block
        latest_hex = await rpc_call(session, chain, "eth_blockNumber", [])
        latest = int(latest_hex, 16)

        # Scan Transfer logs — wider range for older tokens
        from_block = hex(max(0, latest - 100000))

        result = await rpc_call(session, chain, "eth_getLogs", [{
            "address": ca,
            "topics": [TRANSFER_TOPIC],
            "fromBlock": from_block,
            "toBlock": "latest"
        }], timeout=15)

        logs = result or []
        logger.info(f"[EVM] supply logs: {len(logs)} from block range {from_block}-latest")

        if not logs:
            return {**_supply_defaults(), "holder_count": 0}

        # Build balance map
        balances = Counter()
        zero40 = "0" * 40
        for log in logs:
            topics = log.get("topics", [])
            if len(topics) < 3: continue
            from_addr = topics[1][-40:].lower()
            to_addr   = topics[2][-40:].lower()
            try:
                amount = int(log.get("data", "0x0"), 16) / (10 ** decimals)
            except Exception:
                continue
            if from_addr != zero40: balances[from_addr] -= amount
            if to_addr   != zero40: balances[to_addr]   += amount

        positive = sorted([v for v in balances.values() if v > 0.0001], reverse=True)
        logger.info(f"[EVM] supply: {len(positive)} positive holders, total_supply={total_supply:.0f}")

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
        logger.error(f"[EVM] supply error: {e}", exc_info=True)
        return _supply_defaults()

def _supply_defaults():
    return {"top10_pct": "N/A", "top1_pct": "N/A", "gini": "N/A", "holder_count": "N/A"}

def _gini(shares):
    if not shares: return 0
    n, s = len(shares), sorted(shares)
    cumsum = sum((2*(i+1) - n - 1) * x for i, x in enumerate(s))
    total  = sum(s)
    return cumsum / (n * total) if total > 0 else 0


# ═══════════════════════════════════════════════════════════════════════════════
# LIQUIDITY — DexScreener
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_lp(session, ca, chain):
    try:
        chain_id = CHAIN_CONFIG[chain]["chain_id"]
        url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200: return _lp_defaults()
            data = await resp.json()

        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == chain_id]
        if not pairs: return _lp_defaults()

        pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd") or 0), reverse=True)
        best = pairs[0]
        liq  = float(best.get("liquidity", {}).get("usd") or 0)
        dex  = best.get("dexId", "unknown")

        if liq == 0:       status = "⚠ No liquidity found"
        elif liq < 1000:   status = f"🔴 Very low — ${liq:,.0f} (rug risk)"
        elif liq < 10000:  status = f"🟡 Low — ${liq:,.0f} on {dex}"
        elif liq < 50000:  status = f"🟢 Moderate — ${liq:,.0f} on {dex}"
        else:              status = f"🟢 Strong — ${liq:,.0f} on {dex}"

        return {
            "lp_locked":        status,
            "lp_lock_duration": "Verify on DexTools",
            "lp_liquidity_usd": liq,
            "volume_24h":       float(best.get("volume", {}).get("h24") or 0),
            "market_cap":       float(best.get("fdv") or best.get("marketCap") or 0),
            "price":            best.get("priceUsd", "0"),
            "dex":              dex,
        }
    except Exception as e:
        logger.warning(f"[EVM] LP error: {e}")
        return _lp_defaults()

def _lp_defaults():
    return {"lp_locked": "N/A", "lp_lock_duration": "N/A", "lp_liquidity_usd": 0,
            "volume_24h": 0, "market_cap": 0, "price": "0", "dex": "N/A"}


# ═══════════════════════════════════════════════════════════════════════════════
# WALLET ACTIVITY — via Transfer logs from RPC
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_holders(session, ca, chain):
    try:
        latest_hex = await rpc_call(session, chain, "eth_blockNumber", [])
        latest     = int(latest_hex, 16)
        from_block = hex(max(0, latest - 100000))

        # Base = ~2s/block so 100000 blocks ≈ 55 hours
        # ETH  = ~12s/block so 100000 blocks ≈ 14 days
        cutoff_block = latest - 43200  # ~24h on Base

        result = await rpc_call(session, chain, "eth_getLogs", [{
            "address": ca,
            "topics": [TRANSFER_TOPIC],
            "fromBlock": from_block,
            "toBlock": "latest"
        }], timeout=15)

        logs = result or []
        logger.info(f"[EVM] holder logs: {len(logs)}")

        if not logs:
            return _holder_defaults()

        wallets, fresh = set(), set()
        sender_counts  = Counter()
        zero40 = "0" * 40

        for log in logs:
            topics = log.get("topics", [])
            if len(topics) < 3: continue
            from_addr = topics[1][-40:].lower()
            to_addr   = topics[2][-40:].lower()
            blk_num   = int(log.get("blockNumber", "0x0"), 16)

            if to_addr != zero40:
                wallets.add(to_addr)
                if blk_num >= cutoff_block: fresh.add(to_addr)
            if from_addr != zero40:
                wallets.add(from_addr)
                sender_counts[from_addr] += 1

        wc        = len(wallets)
        clustered = sum(1 for c in sender_counts.values() if c >= 3)

        return {
            "wallet_count":     wc,
            "fresh_wallet_pct": round(len(fresh) / max(wc, 1) * 100, 1),
            "cluster_pct":      round(min(clustered / max(wc, 1) * 100, 100), 1),
        }

    except Exception as e:
        logger.error(f"[EVM] holders error: {e}", exc_info=True)
        return _holder_defaults()

def _holder_defaults():
    return {"wallet_count": "N/A", "fresh_wallet_pct": "N/A", "cluster_pct": "N/A"}


# ═══════════════════════════════════════════════════════════════════════════════
# DEV HISTORY — deployer via first mint + DexScreener search
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_dev_history(session, ca, chain):
    empty = {"deployer": None, "token_count": 0, "tokens": [], "risk": "⚠️ Unknown",
             "risk_note": "", "biggest_mc": 0, "dead_count": 0, "token_lines": [],
             "summary": "Could not identify deployer.", "biggest_name": ""}
    try:
        cfg      = CHAIN_CONFIG[chain]
        chain_id = cfg["chain_id"]
        deployer = None

        # Method 1: Etherscan/Basescan getcontractcreation (works on free tier)
        if cfg["key"]:
            try:
                url = f"{cfg['api']}?module=contract&action=getcontractcreation&contractaddresses={ca}&apikey={cfg['key']}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    data = await resp.json()
                result = data.get("result", [])
                logger.info(f"[EVM] contractcreation: status={data.get('status')} result={result}")
                if isinstance(result, list) and result:
                    deployer = result[0].get("contractCreator", "")
            except Exception as e:
                logger.warning(f"[EVM] etherscan deployer error: {e}")

        # Method 2: First Transfer from zero address (mint tx) via RPC
        if not deployer:
            try:
                latest_hex = await rpc_call(session, chain, "eth_blockNumber", [])
                latest     = int(latest_hex, 16)
                from_block = hex(max(0, latest - 500000))

                mint_logs = await rpc_call(session, chain, "eth_getLogs", [{
                    "address": ca,
                    "topics": [TRANSFER_TOPIC, ZERO_TOPIC],
                    "fromBlock": from_block,
                    "toBlock": "latest"
                }], timeout=12)

                if mint_logs:
                    tx_hash = mint_logs[0].get("transactionHash")
                    if tx_hash:
                        tx = await rpc_call(session, chain, "eth_getTransactionByHash", [tx_hash])
                        if tx:
                            deployer = tx.get("from", "")
                            logger.info(f"[EVM] deployer from mint tx: {deployer}")
            except Exception as e:
                logger.warning(f"[EVM] RPC deployer error: {e}")

        if not deployer:
            return empty

        # Find other tokens via DexScreener search by deployer
        deployed_tokens = []
        try:
            url = f"https://api.dexscreener.com/latest/dex/search?q={deployer}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
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
            logger.info(f"[EVM] deployer prev tokens via DexScreener: {len(deployed_tokens)}")
        except Exception as e:
            logger.warning(f"[EVM] DexScreener search error: {e}")

        # Also try Etherscan tx list for deployed contracts
        if cfg["key"] and not deployed_tokens:
            try:
                cutoff = int(time.time()) - (60 * 86400)
                url = f"{cfg['api']}?module=account&action=txlist&address={deployer}&page=1&offset=100&sort=desc&apikey={cfg['key']}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    tx_data = await resp.json()
                txs = tx_data.get("result", [])
                contracts = [
                    tx.get("contractAddress", "")
                    for tx in (txs if isinstance(txs, list) else [])
                    if tx.get("to", "") == "" and int(tx.get("timeStamp", 0)) >= cutoff
                    and tx.get("contractAddress") and tx.get("contractAddress").lower() != ca.lower()
                ]
                logger.info(f"[EVM] etherscan contracts: {len(contracts)}")
                if contracts:
                    chunks = [contracts[i:i+29] for i in range(0, min(len(contracts), 29), 29)]
                    for chunk in chunks:
                        url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                            dex = await resp.json()
                        for pair in dex.get("pairs", []) or []:
                            if pair.get("chainId") != chain_id: continue
                            mc   = float(pair.get("fdv") or pair.get("marketCap") or 0)
                            name = pair.get("baseToken", {}).get("name", "Unknown")
                            sym  = pair.get("baseToken", {}).get("symbol", "???")
                            deployed_tokens.append({"name": name, "symbol": sym, "mc": mc})
            except Exception as e:
                logger.warning(f"[EVM] etherscan tx list error: {e}")

        def fmt(mc):
            if mc >= 1_000_000: return f"${mc/1_000_000:.2f}M"
            if mc >= 1_000:     return f"${mc/1_000:.1f}K"
            return f"${mc:.0f}"

        total      = len(deployed_tokens)
        biggest_mc = max((t["mc"] for t in deployed_tokens), default=0)
        biggest    = next((t for t in deployed_tokens if t["mc"] == biggest_mc), {})
        dead       = sum(1 for t in deployed_tokens if t["mc"] == 0)

        if biggest_mc >= 500_000:        risk = "🟢 Proven dev"
        elif biggest_mc >= 100_000:      risk = "🟡 Some track record"
        elif total == 0:                 risk = "🆕 First deployment"
        elif dead > total * 0.7 and total > 2: risk = "🔴 Serial deployer"
        else:                            risk = "🟠 Low track record"

        tokens_sorted = sorted(deployed_tokens, key=lambda x: x["mc"], reverse=True)
        token_lines   = [f"{i}. {t['name']} (${t['symbol']}) — {fmt(t['mc'])}"
                         for i, t in enumerate(tokens_sorted[:5], 1)]
        summary = (f"Deployer has {total} other token(s). Best: {biggest.get('name','?')} @ {fmt(biggest_mc)}."
                   if total else f"No other tokens found for this deployer on {chain_id.title()}.")

        return {
            "deployer": deployer, "token_count": total, "dead_count": dead,
            "tokens": deployed_tokens, "token_lines": token_lines,
            "biggest_mc": biggest_mc, "biggest_name": biggest.get("name", ""),
            "risk": risk, "risk_note": "", "summary": summary,
        }

    except Exception as e:
        logger.error(f"[EVM] dev history error: {e}", exc_info=True)
        return empty


# ═══════════════════════════════════════════════════════════════════════════════
# SCORING + SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════

def score_lp(d):
    try:
        liq = float(d.get("lp_liquidity_usd", 0))
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
        score += 60 if top10 > 80 else 40 if top10 > 50 else 20 if top10 > 30 else 0
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

def risk_label(s):
    if s <= 30: return "🟢 Low"
    if s <= 60: return "🟡 Medium"
    if s <= 80: return "🟠 High"
    return "🔴 Critical"

def generate_evm_summary(d, ls, ss, hs):
    flags, advice = [], []
    try:
        liq = float(d.get("lp_liquidity_usd", 0))
        if liq < 1000:  flags.append("critically low liquidity"); advice.append("Avoid until liquidity improves.")
        elif liq < 5000: flags.append("low liquidity"); advice.append("Use small position sizes.")
    except: pass
    top1  = d.get("top1_pct", 0)
    top10 = d.get("top10_pct", 0)
    if isinstance(top1, (int, float)) and top1 > 20:
        flags.append(f"single wallet holds {top1}% of supply"); advice.append("Dump risk is very high.")
    if isinstance(top10, (int, float)) and top10 > 50:
        flags.append(f"top 10 hold {top10}% of supply")
    overall = d.get("risk_score", 0)
    opener  = ("✅ Relatively low risk." if overall <= 30 else
               "⚠️ Moderate risk — proceed with caution." if overall <= 60 else
               "🚨 High risk — significant red flags." if overall <= 80 else
               "🔴 Critical risk — multiple severe red flags.")
    flag_str = ("Key concerns: " + "; ".join(flags) + ".") if flags else "No major red flags detected."
    action   = " ".join(advice) if advice else "Always verify LP lock status before trading."
    return f"{opener} {flag_str} {action}"

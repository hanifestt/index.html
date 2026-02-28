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
ALCHEMY_ETH   = os.environ.get("ALCHEMY_ETH_RPC", f"https://eth-mainnet.g.alchemy.com/v2/{os.environ.get('ALCHEMY_API_KEY','')}")
ALCHEMY_BASE  = os.environ.get("ALCHEMY_BASE_RPC", f"https://base-mainnet.g.alchemy.com/v2/{os.environ.get('ALCHEMY_API_KEY','')}")

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
    try:
        cfg = CHAIN_CONFIG[chain]

        # Get top token holders
        url = f"{cfg['api']}?module=token&action=tokenholderlist&contractaddress={ca}&page=1&offset=20&apikey={cfg['key']}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return _supply_defaults()
            data = await resp.json()

        holders = data.get("result", [])
        if not isinstance(holders, list) or not holders:
            return _supply_defaults()

        # Get total supply
        supply_url = f"{cfg['api']}?module=stats&action=tokensupply&contractaddress={ca}&apikey={cfg['key']}"
        async with session.get(supply_url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            supply_data = await resp.json()
        total_supply = float(supply_data.get("result", 0) or 0)

        if total_supply == 0:
            # Fallback: sum all holders
            total_supply = sum(float(h.get("TokenHolderQuantity", 0)) for h in holders)

        if total_supply == 0:
            return _supply_defaults()

        amounts = sorted(
            [float(h.get("TokenHolderQuantity", 0)) for h in holders],
            reverse=True
        )
        shares   = [a / total_supply for a in amounts if a > 0]
        top10    = round(sum(shares[:10]) * 100, 1)
        top1     = round(shares[0] * 100, 2) if shares else 0
        gini     = round(_gini(shares), 2)

        return {
            "top10_pct":    top10,
            "top1_pct":     top1,
            "gini":         gini,
            "holder_count": len(holders),
        }
    except Exception as e:
        logger.warning(f"[EVM] supply error: {e}")
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
    try:
        cfg = CHAIN_CONFIG[chain]
        # Get recent transfers to find unique wallets and fresh activity
        url = f"{cfg['api']}?module=account&action=tokentx&contractaddress={ca}&page=1&offset=100&sort=desc&apikey={cfg['key']}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return _holder_defaults()
            data = await resp.json()

        txs = data.get("result", [])
        if not isinstance(txs, list) or not txs:
            return _holder_defaults()

        cutoff = int(time.time()) - 86400
        wallets = set()
        fresh_wallets = set()
        zero_addr = "0x0000000000000000000000000000000000000000"

        for tx in txs:
            to_addr = tx.get("to", "").lower()
            from_addr = tx.get("from", "").lower()
            ts = int(tx.get("timeStamp", 0))

            if to_addr and to_addr != zero_addr:
                wallets.add(to_addr)
                if ts > cutoff:
                    fresh_wallets.add(to_addr)
            if from_addr and from_addr != zero_addr:
                wallets.add(from_addr)

        wallet_count = len(wallets)
        fresh_pct = round(len(fresh_wallets) / max(wallet_count, 1) * 100, 1)

        # Cluster detection: wallets appearing multiple times in short window
        from collections import Counter
        sender_counts = Counter(tx.get("from", "").lower() for tx in txs)
        clustered = sum(1 for c in sender_counts.values() if c >= 3)
        cluster_pct = round(min(clustered / max(wallet_count, 1) * 100, 100), 1)

        return {
            "wallet_count":    wallet_count,
            "fresh_wallet_pct": fresh_pct,
            "cluster_pct":     cluster_pct,
        }
    except Exception as e:
        logger.warning(f"[EVM] holders error: {e}")
        return _holder_defaults()

def _holder_defaults():
    return {"wallet_count": "N/A", "fresh_wallet_pct": "N/A", "cluster_pct": "N/A"}


# ═══════════════════════════════════════════════════════════════════════════════
# DEV HISTORY
# ═══════════════════════════════════════════════════════════════════════════════

async def get_evm_dev_history(session: aiohttp.ClientSession, ca: str, chain: str) -> dict:
    try:
        cfg = CHAIN_CONFIG[chain]

        # Step 1: find deployer from contract creation tx
        url = f"{cfg['api']}?module=contract&action=getcontractcreation&contractaddresses={ca}&apikey={cfg['key']}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            data = await resp.json()

        result = data.get("result", [])
        deployer = None
        if isinstance(result, list) and result:
            deployer = result[0].get("contractCreator", "")

        if not deployer:
            return {"deployer": None, "token_count": 0, "tokens": [], "risk": "N/A", "risk_note": "Could not identify deployer."}

        # Step 2: find all contracts deployed by this wallet
        deploy_url = f"{cfg['api']}?module=account&action=txlist&address={deployer}&page=1&offset=50&sort=desc&apikey={cfg['key']}"
        async with session.get(deploy_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            deploy_data = await resp.json()

        txs = deploy_data.get("result", [])
        if not isinstance(txs, list):
            txs = []

        # Filter to contract creation txs (to field is empty)
        cutoff_60d = int(time.time()) - (60 * 86400)
        deployed_contracts = []
        for tx in txs:
            if tx.get("to", "") == "" and int(tx.get("timeStamp", 0)) >= cutoff_60d:
                contract_addr = tx.get("contractAddress", "")
                if contract_addr:
                    deployed_contracts.append(contract_addr)

        if not deployed_contracts:
            return {
                "deployer": deployer,
                "token_count": 0,
                "tokens": [],
                "risk": "🆕 New deployer",
                "risk_note": "No previous contract deployments found.",
                "summary": f"Deployer `{deployer[:8]}...{deployer[-4:]}` has no previous deployments in the last 60 days.",
            }

        # Step 3: cross-reference with DexScreener
        chain_id = "base" if chain == "base" else "ethereum"
        chunks = [deployed_contracts[i:i+29] for i in range(0, len(deployed_contracts[:29]), 29)]
        biggest_mc = 0
        biggest_name = ""
        tokens = []

        for chunk in chunks:
            try:
                dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
                async with session.get(dex_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        dex_data = await resp.json()
                        for pair in dex_data.get("pairs", []) or []:
                            if pair.get("chainId") != chain_id:
                                continue
                            mc   = float(pair.get("fdv") or pair.get("marketCap") or 0)
                            name = pair.get("baseToken", {}).get("name", "Unknown")
                            sym  = pair.get("baseToken", {}).get("symbol", "???")
                            addr = pair.get("baseToken", {}).get("address", "")
                            tokens.append({"name": name, "symbol": sym, "mc": mc, "address": addr})
                            if mc > biggest_mc:
                                biggest_mc   = mc
                                biggest_name = name
            except Exception:
                pass

        total = len(deployed_contracts)
        dead  = total - len(tokens)

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
        elif dead > total * 0.7 and total > 2:
            risk = "🔴 Serial deployer"
            risk_note = f"{dead}/{total} previous contracts are dead"
        else:
            risk = "🟠 Low track record"
            risk_note = "No significant launches found"

        token_lines = [
            f"{i}. {t['name']} (${t['symbol']}) — {fmt_mc(t['mc'])}"
            for i, t in enumerate(sorted(tokens, key=lambda x: x["mc"], reverse=True)[:5], 1)
        ]

        summary = f"This dev deployed {total} contract(s) in 60 days."
        if biggest_name:
            summary += f" Best launch: {biggest_name} @ {fmt_mc(biggest_mc)}."

        return {
            "deployer":     deployer,
            "token_count":  total,
            "dead_count":   dead,
            "tokens":       tokens,
            "token_lines":  token_lines,
            "biggest_mc":   biggest_mc,
            "biggest_name": biggest_name,
            "risk":         risk,
            "risk_note":    risk_note,
            "summary":      summary,
        }

    except Exception as e:
        logger.error(f"[EVM] dev history error: {e}")
        return {"deployer": None, "token_count": 0, "tokens": [], "risk": "N/A", "risk_note": str(e)}


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

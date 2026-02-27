"""
scanner.py — On-chain risk scanner for Chain Sentinel
Uses Helius RPC for wallet/MEV data and Birdeye for supply/LP data.
"""

import os
import asyncio
import aiohttp
import time

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
BIRDEYE_API_KEY = os.environ.get("BIRDEYE_API_KEY", "")

HELIUS_RPC  = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API  = "https://api.helius.xyz/v0"
BIRDEYE_API = "https://public-api.birdeye.so"


# ── Main entry ────────────────────────────────────────────────────────────────
async def scan_token(ca: str) -> dict:
    async with aiohttp.ClientSession() as session:
        wallet_data, lp_data, supply_data, mev_data = await asyncio.gather(
            scan_wallets(session, ca),
            scan_lp(session, ca),
            scan_supply(session, ca),
            scan_mev(session, ca),
        )

    combined = {**wallet_data, **lp_data, **supply_data, **mev_data}

    ws = score_wallets(wallet_data)
    ls = score_lp(lp_data)
    ss = score_supply(supply_data)
    ms = score_mev(mev_data)

    combined["risk_score"]   = int(ws*0.30 + ls*0.25 + ss*0.25 + ms*0.20)
    combined["wallet_risk"]  = risk_label(ws)
    combined["lp_risk"]      = risk_label(ls)
    combined["supply_risk"]  = risk_label(ss)
    combined["mev_risk"]     = risk_label(ms)
    combined["ai_summary"]   = generate_summary(combined, ws, ls, ss, ms)
    return combined


# ── Wallet scan — uses Helius getTokenAccounts RPC ───────────────────────────
async def scan_wallets(session: aiohttp.ClientSession, ca: str) -> dict:
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccounts",
            "params": {
                "mint": ca,
                "limit": 100,
                "displayOptions": {"showZeroBalance": False}
            }
        }
        async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return _wallet_defaults()
            data = await resp.json()

        accounts = data.get("result", {})
        if isinstance(accounts, dict):
            accounts = accounts.get("token_accounts", [])
        if not accounts:
            return _wallet_defaults()

        wallet_count = len(accounts)

        # Check how many wallets were recently active via transaction history
        tx_url = f"{HELIUS_API}/addresses/{ca}/transactions?api-key={HELIUS_API_KEY}&limit=100&type=TRANSFER"
        async with session.get(tx_url, timeout=aiohttp.ClientTimeout(total=12)) as resp2:
            txs = await resp2.json() if resp2.status == 200 else []

        cutoff = time.time() - 86400
        recent_wallets = set()
        all_wallets = set()
        slot_map: dict = {}

        for tx in txs if isinstance(txs, list) else []:
            fp = tx.get("feePayer", "")
            ts = tx.get("timestamp", 0)
            slot = tx.get("slot", 0)
            if fp:
                all_wallets.add(fp)
                slot_map.setdefault(slot, []).append(fp)
                if ts > cutoff:
                    recent_wallets.add(fp)

        fresh_pct = round(len(recent_wallets) / max(len(all_wallets), 1) * 100, 1)

        # Cluster detection: multiple wallets active in same slot
        clustered = sum(1 for ws in slot_map.values() if len(set(ws)) > 2)
        cluster_pct = round(min(clustered / max(len(slot_map), 1) * 100, 100), 1)

        return {
            "wallet_count": wallet_count,
            "cluster_pct": cluster_pct,
            "fresh_wallet_pct": fresh_pct,
        }
    except Exception as e:
        return _wallet_defaults()

def _wallet_defaults():
    return {"wallet_count": "N/A", "cluster_pct": "N/A", "fresh_wallet_pct": "N/A"}


# ── LP scan — Birdeye token overview ─────────────────────────────────────────
async def scan_lp(session: aiohttp.ClientSession, ca: str) -> dict:
    try:
        headers = {"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}
        url = f"{BIRDEYE_API}/defi/token_overview?address={ca}"
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return _lp_defaults()
            data = (await resp.json()).get("data", {})

        liq = float(data.get("liquidity") or 0)
        volume_24h = float(data.get("v24hUSD") or 0)
        price = float(data.get("price") or 0)
        mc = float(data.get("mc") or 0)

        if liq == 0:
            lp_status = "⚠ No liquidity data found"
        elif liq < 1000:
            lp_status = f"🔴 Very low — ${liq:,.0f} (high rug risk)"
        elif liq < 10000:
            lp_status = f"🟡 Low — ${liq:,.0f}"
        elif liq < 50000:
            lp_status = f"🟢 Moderate — ${liq:,.0f}"
        else:
            lp_status = f"🟢 Strong — ${liq:,.0f}"

        return {
            "lp_locked": lp_status,
            "lp_lock_duration": "Verify lock on Raydium/Unicrypt",
            "lp_liquidity_usd": liq,
            "volume_24h": volume_24h,
            "price": price,
            "market_cap": mc,
        }
    except Exception:
        return _lp_defaults()

def _lp_defaults():
    return {"lp_locked": "N/A", "lp_lock_duration": "N/A", "lp_liquidity_usd": 0, "volume_24h": 0, "price": 0, "market_cap": 0}


# ── Supply concentration — Helius RPC getLargestAccounts ─────────────────────
async def scan_supply(session: aiohttp.ClientSession, ca: str) -> dict:
    import logging
    logger = logging.getLogger(__name__)
    try:
        # Step 1: get total supply
        total_supply = await get_total_supply(session, ca)
        logger.info(f"[SUPPLY] total_supply={total_supply}")
        if not total_supply or total_supply == 0:
            logger.warning("[SUPPLY] total_supply is 0 or None")
            return _supply_defaults()

        # Step 2: use getTokenLargestAccounts — simpler, always works on free Helius
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [ca]
        }
        async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            status = resp.status
            raw = await resp.json()
            logger.info(f"[SUPPLY] getTokenLargestAccounts status={status} raw={str(raw)[:300]}")

        if status != 200:
            logger.warning(f"[SUPPLY] bad status {status}")
            return _supply_defaults()

        error = raw.get("error")
        if error:
            logger.warning(f"[SUPPLY] RPC error: {error}")
            return _supply_defaults()

        value = raw.get("result", {}).get("value", [])
        logger.info(f"[SUPPLY] got {len(value)} largest accounts")

        if not value:
            return _supply_defaults()

        # value is list of {address, amount, decimals, uiAmount, uiAmountString}
        amounts = []
        for item in value:
            ui = item.get("uiAmount")
            if ui is None:
                # fallback: raw amount / 10^decimals
                raw_amt = int(item.get("amount", 0))
                dec = int(item.get("decimals", 0))
                ui = raw_amt / (10 ** dec) if dec else raw_amt
            amounts.append(float(ui))

        amounts = sorted([a for a in amounts if a > 0], reverse=True)
        logger.info(f"[SUPPLY] amounts (top5): {amounts[:5]}")

        if not amounts:
            return _supply_defaults()

        shares = [a / total_supply for a in amounts]
        top10_pct = round(sum(shares[:10]) * 100, 1)
        top1_pct  = round(shares[0] * 100, 2)
        gini      = round(compute_gini(shares), 2)

        logger.info(f"[SUPPLY] top10={top10_pct}% top1={top1_pct}% gini={gini}")

        return {
            "top10_pct": top10_pct,
            "top1_pct":  top1_pct,
            "gini":      gini,
            "holder_count": len(amounts),
        }
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"[SUPPLY] exception: {e}", exc_info=True)
        return _supply_defaults()

async def get_total_supply(session: aiohttp.ClientSession, ca: str) -> float:
    import logging
    logger = logging.getLogger(__name__)
    try:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [ca]}
        async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            data = await resp.json()
            logger.info(f"[SUPPLY] getTokenSupply raw: {str(data)[:200]}")
            val = data.get("result", {}).get("value", {})
            ui = val.get("uiAmount")
            if ui is None:
                raw_amt = int(val.get("amount", 0))
                dec = int(val.get("decimals", 0))
                ui = raw_amt / (10 ** dec) if dec else raw_amt
            return float(ui or 0)
    except Exception as e:
        logging.getLogger(__name__).error(f"[SUPPLY] getTokenSupply error: {e}")
        return 0.0

def compute_gini(shares):
    if not shares: return 0
    n = len(shares)
    s = sorted(shares)
    cumsum = sum((2*(i+1) - n - 1) * x for i, x in enumerate(s))
    total = sum(s)
    return cumsum / (n * total) if total > 0 else 0

def _supply_defaults():
    return {"top10_pct": "N/A", "top1_pct": "N/A", "gini": "N/A", "holder_count": "N/A"}


# ── MEV scan — Helius transaction history ─────────────────────────────────────
async def scan_mev(session: aiohttp.ClientSession, ca: str) -> dict:
    try:
        url = f"{HELIUS_API}/addresses/{ca}/transactions?api-key={HELIUS_API_KEY}&limit=100"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return _mev_defaults()
            txs = await resp.json()

        if not isinstance(txs, list):
            return _mev_defaults()

        slot_map: dict = {}
        for tx in txs:
            slot = tx.get("slot", 0)
            fp   = tx.get("feePayer", "")
            slot_map.setdefault(slot, []).append(fp)

        # Sandwich = same wallet appears as first AND last in a slot with 3+ txs
        sandwich_count = 0
        for wallets in slot_map.values():
            if len(wallets) >= 3 and wallets[0] == wallets[-1] and wallets[0] != "":
                sandwich_count += 1

        # Count repeated fee payers (bot behavior)
        from collections import Counter
        payer_counts = Counter(tx.get("feePayer", "") for tx in txs)
        bot_wallets = sum(1 for cnt in payer_counts.values() if cnt >= 5)

        return {"mev_bots": bot_wallets, "sandwich_count": sandwich_count}
    except Exception:
        return _mev_defaults()

def _mev_defaults():
    return {"mev_bots": "N/A", "sandwich_count": "N/A"}


# ── Scoring ───────────────────────────────────────────────────────────────────
def score_wallets(d):
    score = 0
    cp = d.get("cluster_pct", 0)
    fp = d.get("fresh_wallet_pct", 0)
    if isinstance(cp, (int, float)): score += min(cp * 0.8, 50)
    if isinstance(fp, (int, float)): score += min(fp * 0.5, 50)
    return int(score)

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

def score_mev(d):
    sc   = d.get("sandwich_count", 0)
    bots = d.get("mev_bots", 0)
    score = 0
    if isinstance(sc,   (int, float)): score += min(sc * 5, 60)
    if isinstance(bots, (int, float)): score += min(bots * 20, 40)
    return min(score, 100)

def risk_label(score):
    if score <= 30: return "🟢 Low"
    if score <= 60: return "🟡 Medium"
    if score <= 80: return "🟠 High"
    return "🔴 Critical"


# ── Rule-based summary ────────────────────────────────────────────────────────
def generate_summary(d, ws, ls, ss, ms) -> str:
    flags, advice = [], []

    fresh = d.get("fresh_wallet_pct", 0)
    cluster = d.get("cluster_pct", 0)
    if isinstance(fresh, (int, float)) and fresh > 40:
        flags.append(f"high fresh wallet activity ({fresh}%)")
    if isinstance(cluster, (int, float)) and cluster > 30:
        flags.append(f"coordinated buying patterns detected ({cluster}%)")

    liq = d.get("lp_liquidity_usd", 0)
    try:
        liq = float(liq)
        if liq < 1000:
            flags.append("critically low liquidity — rug risk very high")
            advice.append("Avoid entry until liquidity improves.")
        elif liq < 5000:
            flags.append("low liquidity")
            advice.append("Use small position sizes.")
    except Exception:
        pass

    top10 = d.get("top10_pct", 0)
    top1  = d.get("top1_pct", 0)
    if isinstance(top1,  (int, float)) and top1 > 20:
        flags.append(f"single wallet holds {top1}% of supply")
        advice.append("Single-wallet dump risk is very high.")
    if isinstance(top10, (int, float)) and top10 > 50:
        flags.append(f"top 10 wallets hold {top10}% of supply")
        advice.append("Watch top holder movements closely.")

    sc = d.get("sandwich_count", 0)
    bots = d.get("mev_bots", 0)
    if isinstance(sc,   (int, float)) and sc > 2:
        flags.append(f"{sc} sandwich attack patterns detected")
        advice.append("Use MEV-protected RPC or higher slippage.")
    if isinstance(bots, (int, float)) and bots > 0:
        flags.append(f"{bots} suspected bot wallet(s) active")

    overall = d.get("risk_score", 0)
    if overall <= 30:   opener = "✅ Relatively low risk signals."
    elif overall <= 60: opener = "⚠️ Moderate risk — proceed with caution."
    elif overall <= 80: opener = "🚨 High risk — significant red flags."
    else:               opener = "🔴 Critical risk — multiple severe red flags."

    flag_str = ("Key concerns: " + "; ".join(flags) + ".") if flags else "No major red flags detected."
    action   = " ".join(advice) if advice else "Always verify LP lock status before trading."

    return f"{opener} {flag_str} {action}"

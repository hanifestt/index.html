"""
sniper.py — Runner Sniper for Chain Sentinel
Monitors new launches on Solana + Base, runs legitimacy checks,
alerts within 5-10 seconds if a token passes all filters.
"""

import os
import asyncio
import aiohttp
import json
import logging
import time

logger = logging.getLogger(__name__)

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
HELIUS_RPC     = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_WSS     = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API     = f"https://api.helius.xyz/v0"
_QN_BASE       = os.environ.get("RPC_HTTP_BASE", "https://rpc.ankr.com/base")
_QN_BASE_WSS   = os.environ.get("RPC_WSS_BASE",  "wss://rpc.ankr.com/base/ws")
PUMP_PROGRAM   = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_API       = "https://frontend-api.pump.fun"

STORAGE_FILE   = "sniper_data.json"

# Uniswap V2/V3 factories on Base
BASE_UNI_V2    = "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"
BASE_UNI_V3    = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
PAIR_CREATED   = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
POOL_CREATED   = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
WETH_BASE      = "0x4200000000000000000000000000000000000006"

_seen: set = set()
_sniper_users: set = set()


# ═══════════════════════════════════════════════════════════════════════════════
# STORAGE — user filter settings
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_FILTERS = {
    "min_lp_usd":        1000,    # minimum liquidity in USD
    "max_top10_pct":     60,      # max % held by top 10 wallets
    "min_dev_mc":        50000,   # dev's previous best launch MC
    "require_smart_money": False, # require smart money holders
    "max_cabal_pct":     30,      # max % of supply in cabal clusters
    "chains":            ["solana", "base"],
}

def _load() -> dict:
    try:
        with open(STORAGE_FILE) as f: return json.load(f)
    except Exception:
        return {"users": {}}

def _save(d):
    with open(STORAGE_FILE, "w") as f: json.dump(d, f, indent=2)

def enable_sniper(user_id: int):
    d = _load()
    uid = str(user_id)
    if uid not in d["users"]:
        d["users"][uid] = {"filters": DEFAULT_FILTERS.copy(), "active": True}
    else:
        d["users"][uid]["active"] = True
    _save(d)
    _sniper_users.add(user_id)

def disable_sniper(user_id: int):
    d = _load()
    uid = str(user_id)
    if uid in d["users"]:
        d["users"][uid]["active"] = False
    _save(d)
    _sniper_users.discard(user_id)

def get_filters(user_id: int) -> dict:
    d = _load()
    return d["users"].get(str(user_id), {}).get("filters", DEFAULT_FILTERS.copy())

def update_filter(user_id: int, key: str, value):
    d = _load()
    uid = str(user_id)
    if uid not in d["users"]:
        d["users"][uid] = {"filters": DEFAULT_FILTERS.copy(), "active": False}
    d["users"][uid]["filters"][key] = value
    _save(d)

def get_active_users() -> list:
    d = _load()
    return [
        (int(uid), info["filters"])
        for uid, info in d["users"].items()
        if info.get("active")
    ]

def is_sniping(user_id: int) -> bool:
    d = _load()
    return d["users"].get(str(user_id), {}).get("active", False)


# ═══════════════════════════════════════════════════════════════════════════════
# LEGITIMACY CHECKS (run in parallel for speed)
# ═══════════════════════════════════════════════════════════════════════════════

async def run_legitimacy_checks(session: aiohttp.ClientSession, ca: str, chain: str) -> dict:
    """
    Run all checks in parallel. Returns results dict with pass/fail per check.
    Target: complete within 5-8 seconds.
    """
    start = time.time()

    meta_task    = asyncio.create_task(check_metadata(session, ca, chain))
    lp_task      = asyncio.create_task(check_liquidity(session, ca, chain))
    supply_task  = asyncio.create_task(check_supply(session, ca, chain))
    dev_task     = asyncio.create_task(check_dev(session, ca, chain))
    cabal_task   = asyncio.create_task(check_cabal(session, ca, chain))
    sm_task      = asyncio.create_task(check_smart_money(session, ca, chain))

    meta, lp, supply, dev, cabal, sm = await asyncio.gather(
        meta_task, lp_task, supply_task, dev_task, cabal_task, sm_task,
        return_exceptions=True
    )

    # Replace exceptions with empty dicts
    def safe(r): return r if isinstance(r, dict) else {}
    meta, lp, supply, dev, cabal, sm = safe(meta), safe(lp), safe(supply), safe(dev), safe(cabal), safe(sm)

    elapsed = round((time.time() - start) * 1000)
    logger.info(f"[SNIPER] checks done in {elapsed}ms for {ca[:8]}")

    return {
        "ca":        ca,
        "chain":     chain,
        "meta":      meta,
        "lp":        lp,
        "supply":    supply,
        "dev":       dev,
        "cabal":     cabal,
        "sm":        sm,
        "elapsed_ms": elapsed,
    }


async def check_metadata(session, ca, chain) -> dict:
    """Get token name, symbol, socials."""
    try:
        if chain == "solana":
            url = f"{PUMP_API}/coins/{ca}"
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                   timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    d = await resp.json()
                    return {
                        "name":     d.get("name", "Unknown"),
                        "symbol":   d.get("symbol", "???"),
                        "twitter":  d.get("twitter", ""),
                        "telegram": d.get("telegram", ""),
                        "website":  d.get("website", ""),
                        "image":    d.get("image_uri", ""),
                        "desc":     (d.get("description", "") or "")[:100],
                    }
        # EVM — DexScreener
        chain_id = "base" if chain == "base" else "ethereum"
        url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data  = await resp.json()
                pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == chain_id]
                if pairs:
                    t    = pairs[0].get("baseToken", {})
                    info = pairs[0].get("info", {})
                    return {
                        "name":     t.get("name", "Unknown"),
                        "symbol":   t.get("symbol", "???"),
                        "twitter":  next((s["url"] for s in info.get("socials", []) if s.get("type") == "twitter"), ""),
                        "telegram": next((s["url"] for s in info.get("socials", []) if s.get("type") == "telegram"), ""),
                        "website":  info.get("websites", [{}])[0].get("url", "") if info.get("websites") else "",
                        "image":    info.get("imageUrl", ""),
                        "desc":     "",
                    }
    except Exception as e:
        logger.warning(f"[SNIPER] metadata error: {e}")
    return {"name": "Unknown", "symbol": "???", "twitter": "", "telegram": "", "website": "", "image": "", "desc": ""}


async def check_liquidity(session, ca, chain) -> dict:
    """Check LP size via DexScreener."""
    try:
        chain_id = {"solana": "solana", "base": "base", "ethereum": "ethereum"}[chain]
        url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return {"lp_usd": 0, "mc": 0, "volume_5m": 0, "price_change_5m": 0, "dex": "N/A"}
            data  = await resp.json()
        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == chain_id]
        if not pairs:
            return {"lp_usd": 0, "mc": 0, "volume_5m": 0, "dex": "N/A"}
        pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd") or 0), reverse=True)
        best = pairs[0]
        return {
            "lp_usd":          float(best.get("liquidity", {}).get("usd") or 0),
            "mc":              float(best.get("fdv") or best.get("marketCap") or 0),
            "volume_5m":       float(best.get("volume", {}).get("m5") or 0),
            "price_change_5m": float(best.get("priceChange", {}).get("m5") or 0),
            "dex":             best.get("dexId", "N/A"),
            "pair_url":        best.get("url", ""),
        }
    except Exception as e:
        logger.warning(f"[SNIPER] LP check error: {e}")
    return {"lp_usd": 0, "mc": 0, "volume_5m": 0, "dex": "N/A"}


async def check_supply(session, ca, chain) -> dict:
    """Check supply concentration via top holders."""
    try:
        if chain == "solana":
            payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [ca]}
            async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                data = await resp.json()
            accounts = data.get("result", {}).get("value", [])
            if not accounts:
                return {"top10_pct": 0, "top1_pct": 0, "safe": True}
            supply_payload = {"jsonrpc": "2.0", "id": 2, "method": "getTokenSupply", "params": [ca]}
            async with session.post(HELIUS_RPC, json=supply_payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                s = await resp.json()
            total = float(s.get("result", {}).get("value", {}).get("uiAmount") or 1)
            amounts = sorted([float(a.get("uiAmount") or 0) for a in accounts], reverse=True)
            shares  = [a / total for a in amounts if a > 0]
            top10   = round(sum(shares[:10]) * 100, 1)
            top1    = round(shares[0] * 100, 2) if shares else 0
            return {"top10_pct": top10, "top1_pct": top1, "safe": top10 < 60 and top1 < 20}

        else:
            # EVM: use Transfer logs
            rpc        = _QN_BASE if chain == "base" else os.environ.get("RPC_HTTP_ETH", "https://rpc.ankr.com/eth")
            TRANSFER_T = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
            blk_payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
            async with session.post(rpc, json=blk_payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                blk = await resp.json()
            latest     = int(blk.get("result", "0x0"), 16)
            from_block = hex(max(0, latest - 5000))
            logs_payload = {"jsonrpc": "2.0", "id": 2, "method": "eth_getLogs",
                            "params": [{"address": ca, "topics": [TRANSFER_T],
                                        "fromBlock": from_block, "toBlock": "latest"}]}
            async with session.post(rpc, json=logs_payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                logs_data = await resp.json()
            logs = logs_data.get("result", []) or []
            from collections import Counter
            balances = Counter()
            zero40 = "0" * 40
            for log in logs:
                topics = log.get("topics", [])
                if len(topics) < 3: continue
                fa = topics[1][-40:].lower()
                ta = topics[2][-40:].lower()
                try:
                    amt = int(log.get("data", "0x0"), 16)
                except: continue
                if fa != zero40: balances[fa] -= amt
                if ta != zero40: balances[ta] += amt
            positive = sorted([v for v in balances.values() if v > 0], reverse=True)
            if not positive:
                return {"top10_pct": 0, "top1_pct": 0, "safe": True}
            total  = sum(positive)
            shares = [a / total for a in positive]
            top10  = round(sum(shares[:10]) * 100, 1)
            top1   = round(shares[0] * 100, 2) if shares else 0
            return {"top10_pct": top10, "top1_pct": top1, "safe": top10 < 60 and top1 < 20}

    except Exception as e:
        logger.warning(f"[SNIPER] supply check error: {e}")
    return {"top10_pct": 0, "top1_pct": 0, "safe": True}


async def check_dev(session, ca, chain) -> dict:
    """Quick dev history check."""
    try:
        if chain == "solana":
            # Find deployer
            payload = {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                       "params": [ca, {"encoding": "jsonParsed"}]}
            async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
            parsed     = data.get("result", {}).get("value", {}).get("data", {}).get("parsed", {})
            deployer   = (parsed.get("info", {}) or {}).get("mintAuthority") or ""
            if not deployer:
                sigs_p = {"jsonrpc": "2.0", "id": 2, "method": "getSignaturesForAddress",
                          "params": [ca, {"limit": 100}]}
                async with session.post(HELIUS_RPC, json=sigs_p, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    sigs_d = await resp.json()
                sigs = sigs_d.get("result", [])
                if sigs:
                    oldest = sigs[-1].get("signature", "")
                    tx_p   = {"jsonrpc": "2.0", "id": 3, "method": "getTransaction",
                              "params": [oldest, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]}
                    async with session.post(HELIUS_RPC, json=tx_p, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                        tx_d = await resp.json()
                    keys = tx_d.get("result", {}).get("transaction", {}).get("message", {}).get("accountKeys", [])
                    for k in keys:
                        addr = k.get("pubkey") if isinstance(k, dict) else k
                        sig  = k.get("signer", False) if isinstance(k, dict) else False
                        if addr and addr != ca and sig:
                            deployer = addr; break

            if not deployer:
                return {"deployer": None, "biggest_mc": 0, "launches": 0, "bullish": False}

            # Check pump.fun history
            url = f"{PUMP_API}/coins/user-created-coins/{deployer}?offset=0&limit=20&includeNsfw=true"
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                   timeout=aiohttp.ClientTimeout(total=5)) as resp:
                coins = await resp.json() if resp.status == 200 else []

            biggest_mc = 0
            if isinstance(coins, list) and coins:
                mints = [c.get("mint", "") for c in coins[:10] if c.get("mint")]
                if mints:
                    dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(mints[:29])}"
                    async with session.get(dex_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            dex = await resp.json()
                            for pair in dex.get("pairs", []) or []:
                                if pair.get("chainId") != "solana": continue
                                mc = float(pair.get("fdv") or pair.get("marketCap") or 0)
                                if mc > biggest_mc: biggest_mc = mc

            return {
                "deployer":  deployer,
                "biggest_mc": biggest_mc,
                "launches":  len(coins) if isinstance(coins, list) else 0,
                "bullish":   biggest_mc >= 50000,
            }

        else:
            # EVM dev check
            cfg = {"base": {"api": "https://api.basescan.org/api", "key": os.environ.get("BASESCAN_API_KEY",""), "chain_id": "base"},
                   "ethereum": {"api": "https://api.etherscan.io/api", "key": os.environ.get("ETHERSCAN_API_KEY",""), "chain_id": "ethereum"}}[chain]
            deployer = None
            if cfg["key"]:
                url = f"{cfg['api']}?module=contract&action=getcontractcreation&contractaddresses={ca}&apikey={cfg['key']}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    d = await resp.json()
                r = d.get("result", [])
                if isinstance(r, list) and r:
                    deployer = r[0].get("contractCreator", "")

            if not deployer:
                return {"deployer": None, "biggest_mc": 0, "launches": 0, "bullish": False}

            biggest_mc = 0
            url = f"https://api.dexscreener.com/latest/dex/search?q={deployer}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    sd = await resp.json()
                    for pair in sd.get("pairs", []) or []:
                        if pair.get("chainId") != cfg["chain_id"]: continue
                        if pair.get("baseToken", {}).get("address", "").lower() == ca.lower(): continue
                        mc = float(pair.get("fdv") or pair.get("marketCap") or 0)
                        if mc > biggest_mc: biggest_mc = mc

            return {"deployer": deployer, "biggest_mc": biggest_mc, "launches": 0, "bullish": biggest_mc >= 50000}

    except Exception as e:
        logger.warning(f"[SNIPER] dev check error: {e}")
    return {"deployer": None, "biggest_mc": 0, "launches": 0, "bullish": False}


async def check_cabal(session, ca, chain) -> dict:
    """Quick cabal check — look for wallets funded by same source."""
    try:
        if chain == "solana":
            payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [ca]}
            async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
            accounts = data.get("result", {}).get("value", [])[:10]
            if not accounts:
                return {"cabal_pct": 0, "cluster_count": 0, "safe": True}

            # Resolve token accounts to owners
            token_addrs = [a["address"] for a in accounts if a.get("address")]
            mp_payload  = {"jsonrpc": "2.0", "id": 2, "method": "getMultipleAccounts",
                           "params": [token_addrs, {"encoding": "jsonParsed"}]}
            async with session.post(HELIUS_RPC, json=mp_payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                mp = await resp.json()

            owners = []
            for acc in mp.get("result", {}).get("value", []) or []:
                if acc:
                    owner = acc.get("data", {}).get("parsed", {}).get("info", {}).get("owner", "")
                    if owner: owners.append(owner)

            # Quick funding check — get first tx for each owner in parallel
            async def get_funder(owner):
                try:
                    sp = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                          "params": [owner, {"limit": 100}]}
                    async with session.post(HELIUS_RPC, json=sp, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        sd = await r.json()
                    sigs = sd.get("result", [])
                    if not sigs: return owner, None
                    oldest = sigs[-1].get("signature", "")
                    tp = {"jsonrpc": "2.0", "id": 2, "method": "getTransaction",
                          "params": [oldest, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]}
                    async with session.post(HELIUS_RPC, json=tp, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        td = await r.json()
                    keys = td.get("result", {}).get("transaction", {}).get("message", {}).get("accountKeys", [])
                    for k in keys:
                        addr = k.get("pubkey") if isinstance(k, dict) else k
                        sig  = k.get("signer", False) if isinstance(k, dict) else False
                        writ = k.get("writable", False) if isinstance(k, dict) else False
                        if addr and addr != owner and sig and writ:
                            return owner, addr
                except Exception:
                    pass
                return owner, None

            results  = await asyncio.gather(*[get_funder(o) for o in owners[:8]])
            from collections import Counter
            funders  = [f for _, f in results if f]
            funder_c = Counter(funders)
            clusters = sum(1 for c in funder_c.values() if c >= 2)

            # Estimate supply % in clusters
            cabal_holders = set()
            for funder, count in funder_c.items():
                if count >= 2:
                    for owner, f in results:
                        if f == funder: cabal_holders.add(owner)

            total_pct = 0
            supply_p  = {"jsonrpc": "2.0", "id": 3, "method": "getTokenSupply", "params": [ca]}
            async with session.post(HELIUS_RPC, json=supply_p, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                sp_d = await resp.json()
            total_supply = float(sp_d.get("result", {}).get("value", {}).get("uiAmount") or 1)

            for acc, token_acc in zip(accounts, token_addrs):
                mp2 = {"jsonrpc": "2.0", "id": 4, "method": "getMultipleAccounts",
                       "params": [[token_acc], {"encoding": "jsonParsed"}]}
                pass  # already have amounts from accounts

            for acc in accounts:
                owner_idx = token_addrs.index(acc["address"]) if acc["address"] in token_addrs else -1
                if owner_idx >= 0 and owner_idx < len(owners) and owners[owner_idx] in cabal_holders:
                    total_pct += float(acc.get("uiAmount") or 0) / total_supply * 100

            return {
                "cabal_pct":     round(total_pct, 1),
                "cluster_count": clusters,
                "safe":          clusters == 0 or total_pct < 20,
            }
    except Exception as e:
        logger.warning(f"[SNIPER] cabal check error: {e}")
    return {"cabal_pct": 0, "cluster_count": 0, "safe": True}


async def check_smart_money(session, ca, chain) -> dict:
    """Check if any known smart money wallets hold this token."""
    try:
        from mirror import get_all_tracked_wallets
        tracked = get_all_tracked_wallets()
        if not tracked:
            return {"sm_holders": [], "sm_count": 0}

        sm_holders = []
        chain_wallets = [w for w, subs in tracked.items() if any(s[1] == chain for s in subs)]

        if chain == "solana":
            payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [ca]}
            async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
            token_accs = data.get("result", {}).get("value", [])
            token_addrs = [a["address"] for a in token_accs if a.get("address")]

            if token_addrs:
                mp_payload = {"jsonrpc": "2.0", "id": 2, "method": "getMultipleAccounts",
                              "params": [token_addrs, {"encoding": "jsonParsed"}]}
                async with session.post(HELIUS_RPC, json=mp_payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    mp = await resp.json()
                for acc in mp.get("result", {}).get("value", []) or []:
                    if acc:
                        owner = acc.get("data", {}).get("parsed", {}).get("info", {}).get("owner", "")
                        if owner and owner in chain_wallets:
                            label = tracked[owner][0][2] if tracked[owner] else owner[:8]
                            sm_holders.append(label)

        return {"sm_holders": sm_holders, "sm_count": len(sm_holders)}
    except Exception as e:
        logger.warning(f"[SNIPER] smart money check error: {e}")
    return {"sm_holders": [], "sm_count": 0}


# ═══════════════════════════════════════════════════════════════════════════════
# FILTER ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def evaluate_filters(checks: dict, filters: dict) -> tuple[bool, list, list]:
    """
    Returns (passes, passed_checks, failed_checks)
    """
    passed = []
    failed = []

    lp      = checks.get("lp", {})
    supply  = checks.get("supply", {})
    dev     = checks.get("dev", {})
    cabal   = checks.get("cabal", {})
    sm      = checks.get("sm", {})

    # LP check
    lp_usd = lp.get("lp_usd", 0)
    min_lp  = filters.get("min_lp_usd", 1000)
    if lp_usd >= min_lp:
        passed.append(f"✅ LP: ${lp_usd:,.0f} (min ${min_lp:,.0f})")
    else:
        failed.append(f"❌ LP too low: ${lp_usd:,.0f} (need ${min_lp:,.0f})")

    # Supply check
    top10    = supply.get("top10_pct", 0)
    max_top10 = filters.get("max_top10_pct", 60)
    if isinstance(top10, (int, float)) and top10 <= max_top10:
        passed.append(f"✅ Supply: top 10 hold {top10}% (max {max_top10}%)")
    elif isinstance(top10, (int, float)):
        failed.append(f"❌ Supply concentrated: top 10 hold {top10}% (max {max_top10}%)")

    # Dev check
    biggest_mc = dev.get("biggest_mc", 0)
    min_dev_mc  = filters.get("min_dev_mc", 50000)
    def fmt_mc(mc):
        if mc >= 1_000_000: return f"${mc/1_000_000:.1f}M"
        if mc >= 1_000:     return f"${mc/1_000:.0f}K"
        return f"${mc:.0f}"
    if biggest_mc >= min_dev_mc:
        passed.append(f"✅ Dev: prev best {fmt_mc(biggest_mc)} (min {fmt_mc(min_dev_mc)})")
    else:
        failed.append(f"❌ Dev unproven: best launch {fmt_mc(biggest_mc)} (need {fmt_mc(min_dev_mc)})")

    # Cabal check
    cabal_pct = cabal.get("cabal_pct", 0)
    max_cabal  = filters.get("max_cabal_pct", 30)
    if cabal_pct <= max_cabal:
        passed.append(f"✅ Cabal: {cabal_pct}% in clusters (max {max_cabal}%)")
    else:
        failed.append(f"❌ Cabal detected: {cabal_pct}% supply in clusters")

    # Smart money (optional)
    if filters.get("require_smart_money"):
        sm_count = sm.get("sm_count", 0)
        if sm_count > 0:
            passed.append(f"✅ Smart money: {sm_count} tracked wallet(s) holding")
        else:
            failed.append(f"❌ No smart money holders detected")

    passes = len(failed) == 0
    return passes, passed, failed


# ═══════════════════════════════════════════════════════════════════════════════
# ALERT FORMATTER
# ═══════════════════════════════════════════════════════════════════════════════

def format_sniper_alert(checks: dict, passed: list, failed: list, filters: dict) -> str:
    ca      = checks["ca"]
    chain   = checks["chain"]
    meta    = checks.get("meta", {})
    lp      = checks.get("lp", {})
    dev     = checks.get("dev", {})
    sm      = checks.get("sm", {})
    elapsed = checks.get("elapsed_ms", 0)

    chain_emoji = {"solana": "◎", "base": "🔵", "ethereum": "⟠"}.get(chain, "🔗")
    name    = meta.get("name", "Unknown")
    symbol  = meta.get("symbol", "???")
    twitter = meta.get("twitter", "")
    telegram= meta.get("telegram", "")

    def fmt_mc(mc):
        if not mc: return "$0"
        if mc >= 1_000_000: return f"${mc/1_000_000:.2f}M"
        if mc >= 1_000:     return f"${mc/1_000:.1f}K"
        return f"${mc:.0f}"

    deployer   = dev.get("deployer", "")
    biggest_mc = dev.get("biggest_mc", 0)
    lp_usd     = lp.get("lp_usd", 0)
    mc         = lp.get("mc", 0)
    vol_5m     = lp.get("volume_5m", 0)
    pc_5m      = lp.get("price_change_5m", 0)
    sm_holders = sm.get("sm_holders", [])

    lines = [
        f"🎯 *RUNNER DETECTED* {chain_emoji}",
        f"",
        f"*{name}* (${symbol})",
        f"`{ca}`",
        f"",
        f"━━━ 📊 SNAPSHOT ━━━",
        f"• MC: `{fmt_mc(mc)}` | LP: `${lp_usd:,.0f}`",
        f"• Vol 5m: `${vol_5m:,.0f}` | Price Δ5m: `{pc_5m:+.1f}%`",
    ]

    if deployer:
        lines.append(f"• Dev prev best: `{fmt_mc(biggest_mc)}`")

    if sm_holders:
        lines.append(f"• 🧠 Smart money: {', '.join(sm_holders[:3])}")

    lines += ["", "━━━ ✅ PASSED CHECKS ━━━"]
    for p in passed:
        lines.append(f"  {p}")

    if failed:
        lines += ["", "━━━ ⚠️ WARNINGS ━━━"]
        for f in failed:
            lines.append(f"  {f}")

    # Socials
    social_btns = []
    if twitter:  social_btns.append(f"[🐦 Twitter]({twitter if twitter.startswith('http') else 'https://twitter.com/' + twitter.lstrip('@')})")
    if telegram: social_btns.append(f"[✈️ TG]({telegram if telegram.startswith('http') else 'https://t.me/' + telegram.lstrip('@')})")
    if social_btns:
        lines += ["", " • ".join(social_btns)]

    # Buy links
    if chain == "solana":
        buy_url  = f"https://pump.fun/{ca}"
        buy_url2 = f"https://raydium.io/swap/?inputCurrency=SOL&outputCurrency={ca}"
    elif chain == "base":
        buy_url  = f"https://app.uniswap.org/#/swap?outputCurrency={ca}&chain=base"
        buy_url2 = f"https://basescan.org/token/{ca}"
    else:
        buy_url  = f"https://app.uniswap.org/#/swap?outputCurrency={ca}"
        buy_url2 = f"https://etherscan.io/token/{ca}"

    lines += [
        f"",
        f"[🛒 Buy Now]({buy_url}) • [📊 Chart]({lp.get('pair_url', buy_url2)}) • [🔍 Full Scan](https://t.me/chainsentinel_bot?start=scan_{ca})",
        f"",
        f"⚡ Checked in `{elapsed}ms`",
        f"_Powered by Chain Sentinel • $CS_",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN MONITOR — integrates with existing pump.fun + Uniswap monitors
# ═══════════════════════════════════════════════════════════════════════════════

async def evaluate_and_alert(bot, ca: str, chain: str, sig: str = ""):
    """Called by monitor.py and evm_monitor.py for every new launch."""
    if ca in _seen:
        return
    _seen.add(ca)

    active_users = get_active_users()
    if not active_users:
        logger.debug(f"[SNIPER] Token {ca[:8]} detected but no active sniper users")
        return

    logger.info(f"[SNIPER] ⚡ Evaluating {ca[:8]}... on {chain} for {len(active_users)} user(s)")

    async with aiohttp.ClientSession() as session:
        checks = await run_legitimacy_checks(session, ca, chain)

    for user_id, filters in active_users:
        # Check if this chain is enabled for user
        if chain not in filters.get("chains", ["solana", "base"]):
            continue

        passes, passed, failed = evaluate_filters(checks, filters)

        # Send alert if passes all hard filters (LP + supply + dev + cabal)
        # Smart money is optional
        hard_failed = [f for f in failed if "Smart money" not in f]

        if not hard_failed:
            try:
                msg = format_sniper_alert(checks, passed, failed, filters)
                if len(msg) > 4000:
                    msg = msg[:3900] + "\n\n_truncated_"
                await bot.send_message(
                    chat_id=user_id,
                    text=msg,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
                logger.info(f"[SNIPER] Alert sent to {user_id} for {ca[:8]}")
            except Exception as e:
                logger.warning(f"[SNIPER] send error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# STANDALONE POLLING MONITOR — works even when Helius WebSocket is rate-limited
# Polls pump.fun + DexScreener directly every 30s for new launches
# ═══════════════════════════════════════════════════════════════════════════════

_last_pump_token = None

async def run_sniper_poller(bot):
    """
    Independent polling loop — checks pump.fun for new tokens every 30s.
    Does NOT depend on Helius WebSocket. Runs as a separate background task.
    """
    global _last_pump_token
    logger.info("[SNIPER] Standalone poller started.")
    await asyncio.sleep(15)  # stagger start

    while True:
        try:
            active_users = get_active_users()
            if active_users:
                logger.info(f"[SNIPER] Polling for {len(active_users)} active user(s)...")
                await _poll_new_launches(bot)
            else:
                logger.debug("[SNIPER] No active sniper users — use /snipe on")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[SNIPER] poller error: {e}", exc_info=True)
        await asyncio.sleep(30)


async def _poll_new_launches(bot):
    """Poll pump.fun and Base for new launches."""
    global _last_pump_token

    async with aiohttp.ClientSession() as session:
        # ── Solana: poll pump.fun latest coins ──
        try:
            url = f"{PUMP_API}/coins?offset=0&limit=20&sort=created_timestamp&order=DESC&includeNsfw=true"
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                   timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    coins = await resp.json()
                    if isinstance(coins, list) and coins:
                        latest_mint = coins[0].get("mint", "")

                        if _last_pump_token is None:
                            _last_pump_token = latest_mint
                            logger.info(f"[SNIPER] Poller initialized at {latest_mint[:8]}...")
                        elif latest_mint != _last_pump_token:
                            # Find all new tokens since last check
                            new_coins = []
                            for coin in coins:
                                mint = coin.get("mint", "")
                                if mint == _last_pump_token:
                                    break
                                if mint and mint not in _seen:
                                    new_coins.append(coin)

                            _last_pump_token = latest_mint
                            logger.info(f"[SNIPER] Poller found {len(new_coins)} new Solana token(s)")

                            for coin in new_coins[:5]:  # max 5 per poll cycle
                                mint = coin.get("mint", "")
                                if mint:
                                    asyncio.ensure_future(evaluate_and_alert(bot, mint, "solana"))
        except Exception as e:
            logger.warning(f"[SNIPER] pump.fun poll error: {e}")

        # ── Base: poll DexScreener for new pairs ──
        active_users = get_active_users()
        base_users = [u for u in active_users if "base" in u[1].get("chains", [])]
        if base_users:
            try:
                url = "https://api.dexscreener.com/token-profiles/latest/v1"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        profiles = await resp.json()
                        if isinstance(profiles, list):
                            for p in profiles[:10]:
                                if p.get("chainId") != "base":
                                    continue
                                ca = p.get("tokenAddress", "")
                                if ca and ca not in _seen:
                                    asyncio.ensure_future(evaluate_and_alert(bot, ca, "base"))
            except Exception as e:
                logger.warning(f"[SNIPER] Base poll error: {e}")

"""
clusters.py — Wallet Cluster & Cabal Detection for Chain Sentinel
Finds coordinated wallets sharing the same funding source.
Uses networkx to model relationships.
"""

import os
import asyncio
import aiohttp
import logging
import json
from collections import defaultdict

try:
    import networkx as nx
    HAS_NETWORKX = True
except ImportError:
    HAS_NETWORKX = False

logger = logging.getLogger(__name__)

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
HELIUS_RPC     = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API     = "https://api.helius.xyz/v0"

SYSTEM_PROGRAM  = "11111111111111111111111111111111"
EXCLUDED_FUNDRS = {
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bfE",
    "So11111111111111111111111111111111111111112",
    "ComputeBudget111111111111111111111111111111",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", # Raydium
}


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY
# ═══════════════════════════════════════════════════════════════════════════════

async def find_wallet_clusters(mint_address: str) -> dict:
    """
    Full cabal cluster detection for a token mint.
    Returns clusters, cabal probability score, and networkx graph data.
    """
    async with aiohttp.ClientSession() as session:

        # Step 1: Get top 20 holders
        logger.info(f"[CLUSTER] Fetching top holders for {mint_address[:8]}...")
        holders = await get_top_holders(session, mint_address)
        logger.info(f"[CLUSTER] Got {len(holders)} holders")

        if not holders:
            return {"error": "Could not fetch holders for this token."}

        # Step 2: Find funding wallet for each holder (in parallel)
        logger.info(f"[CLUSTER] Finding funding wallets...")
        funding_tasks = [get_funding_wallet(session, h["owner"]) for h in holders]
        funding_results = await asyncio.gather(*funding_tasks)

        # Step 3: Build holder → funder map
        holder_funder_map = {}
        for holder, funder in zip(holders, funding_results):
            owner = holder["owner"]
            if funder and funder not in EXCLUDED_FUNDRS:
                holder_funder_map[owner] = {
                    "funder": funder,
                    "balance_pct": holder.get("pct", 0),
                    "ui_amount": holder.get("ui_amount", 0),
                }

        logger.info(f"[CLUSTER] Mapped {len(holder_funder_map)} holders to funders")

        # Step 4: Group by funder — find cabals
        funder_to_holders = defaultdict(list)
        for holder, data in holder_funder_map.items():
            funder_to_holders[data["funder"]].append({
                "wallet": holder,
                "balance_pct": data["balance_pct"],
                "ui_amount": data["ui_amount"],
            })

        # A cabal = same funder funded 3+ holders
        clusters = []
        for funder, funded_holders in funder_to_holders.items():
            if len(funded_holders) >= 3:
                total_pct = sum(h["balance_pct"] for h in funded_holders)
                clusters.append({
                    "funder": funder,
                    "holders": funded_holders,
                    "holder_count": len(funded_holders),
                    "combined_supply_pct": round(total_pct, 2),
                })

        # Sort clusters by combined supply %
        clusters.sort(key=lambda x: x["combined_supply_pct"], reverse=True)

        # Step 5: Calculate cabal probability score
        score = calculate_cabal_score(clusters, holders, holder_funder_map)

        # Step 6: Build networkx graph
        graph_data = build_graph(holder_funder_map, clusters, mint_address)

        return {
            "mint": mint_address,
            "total_holders_scanned": len(holders),
            "holders_with_known_funder": len(holder_funder_map),
            "clusters": clusters,
            "cluster_count": len(clusters),
            "cabal_probability": score,
            "holder_funder_map": holder_funder_map,
            "graph": graph_data,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — GET TOP HOLDERS
# ═══════════════════════════════════════════════════════════════════════════════

async def get_top_holders(session: aiohttp.ClientSession, mint: str) -> list:
    try:
        # Get total supply for % calculation
        supply_payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenSupply",
            "params": [mint]
        }
        async with session.post(HELIUS_RPC, json=supply_payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            supply_data = await resp.json()
        total_supply = float(
            supply_data.get("result", {}).get("value", {}).get("uiAmount") or 1
        )

        # Get top 20 token accounts
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [mint]
        }
        async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()

        accounts = data.get("result", {}).get("value", [])
        if not accounts:
            return []

        token_addrs = [a["address"] for a in accounts if a.get("address")]

        # Resolve token accounts → owner wallets
        mp_payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getMultipleAccounts",
            "params": [token_addrs, {"encoding": "jsonParsed"}]
        }
        async with session.post(HELIUS_RPC, json=mp_payload, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            mp_data = await resp.json()

        holders = []
        for acc_info, token_acc in zip(mp_data.get("result", {}).get("value", []) or [], accounts):
            if not acc_info:
                continue
            owner = acc_info.get("data", {}).get("parsed", {}).get("info", {}).get("owner", "")
            ui_amt = float(token_acc.get("uiAmount") or 0)
            if owner and owner not in EXCLUDED_FUNDRS:
                holders.append({
                    "owner": owner,
                    "ui_amount": ui_amt,
                    "pct": round(ui_amt / total_supply * 100, 3) if total_supply > 0 else 0,
                })

        return holders

    except Exception as e:
        logger.error(f"[CLUSTER] get_top_holders error: {e}", exc_info=True)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — FIND FUNDING WALLET
# ═══════════════════════════════════════════════════════════════════════════════

async def get_funding_wallet(session: aiohttp.ClientSession, wallet: str) -> str:
    """
    Find the wallet that first funded this wallet with SOL.
    Looks at the oldest transaction and finds the SOL sender.
    """
    try:
        # Get all signatures — we want the oldest (last in list)
        sigs_payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [wallet, {"limit": 1000, "commitment": "finalized"}]
        }
        async with session.post(HELIUS_RPC, json=sigs_payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            sigs_data = await resp.json()

        sigs = sigs_data.get("result", [])
        if not sigs:
            return None

        # Oldest = last signature
        oldest_sig = sigs[-1].get("signature")
        if not oldest_sig:
            return None

        # Get the full transaction
        tx_payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTransaction",
            "params": [oldest_sig, {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0
            }]
        }
        async with session.post(HELIUS_RPC, json=tx_payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            tx_data = await resp.json()

        result = tx_data.get("result")
        if not result:
            return None

        # Look for SOL transfer TO our wallet in this tx
        message = result.get("transaction", {}).get("message", {})
        account_keys = message.get("accountKeys", [])
        instructions = message.get("instructions", [])

        # Get pre/post balances to find who sent SOL to our wallet
        pre_bals  = result.get("meta", {}).get("preBalances", [])
        post_bals = result.get("meta", {}).get("postBalances", [])

        # Find index of our wallet in account keys
        wallet_idx = None
        for i, key in enumerate(account_keys):
            addr = key.get("pubkey") if isinstance(key, dict) else key
            if addr == wallet:
                wallet_idx = i
                break

        if wallet_idx is not None and wallet_idx < len(pre_bals):
            our_pre  = pre_bals[wallet_idx]
            our_post = post_bals[wallet_idx]
            # Our wallet received SOL — find who sent it
            if our_post > our_pre:
                for i, (pre, post) in enumerate(zip(pre_bals, post_bals)):
                    if post < pre and i != wallet_idx:
                        sender_key = account_keys[i] if i < len(account_keys) else None
                        if sender_key:
                            sender = sender_key.get("pubkey") if isinstance(sender_key, dict) else sender_key
                            if sender and sender not in EXCLUDED_FUNDRS:
                                return sender

        # Fallback: fee payer of creation tx is usually the funder
        for key in account_keys:
            addr = key.get("pubkey") if isinstance(key, dict) else key
            signer = key.get("signer", False) if isinstance(key, dict) else False
            writable = key.get("writable", False) if isinstance(key, dict) else False
            if addr and addr != wallet and signer and writable:
                if addr not in EXCLUDED_FUNDRS:
                    return addr

        return None

    except Exception as e:
        logger.warning(f"[CLUSTER] get_funding_wallet error for {wallet[:8]}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 5 — CABAL PROBABILITY SCORE
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_cabal_score(clusters: list, holders: list, holder_funder_map: dict) -> int:
    """
    Score 0–100 based on:
    - How many clusters exist
    - How much supply they control
    - What % of holders are in clusters
    """
    if not clusters:
        return 0

    score = 0

    # Factor 1: cluster count (max 30pts)
    cluster_count = len(clusters)
    score += min(cluster_count * 10, 30)

    # Factor 2: combined supply controlled by cabals (max 40pts)
    total_cabal_pct = sum(c["combined_supply_pct"] for c in clusters)
    score += min(int(total_cabal_pct * 1.5), 40)

    # Factor 3: % of scanned holders that are in a cabal (max 30pts)
    cabal_holders = set()
    for c in clusters:
        for h in c["holders"]:
            cabal_holders.add(h["wallet"])

    if holder_funder_map:
        cabal_ratio = len(cabal_holders) / len(holder_funder_map)
        score += min(int(cabal_ratio * 30), 30)

    return min(score, 100)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 6 — NETWORKX GRAPH
# ═══════════════════════════════════════════════════════════════════════════════

def build_graph(holder_funder_map: dict, clusters: list, mint: str) -> dict:
    """
    Build a directed graph: funder → holder wallets.
    Returns serializable graph data + networkx Graph object if available.
    """
    nodes = []
    edges = []
    cabal_funders = {c["funder"] for c in clusters}
    cabal_holders = set()
    for c in clusters:
        for h in c["holders"]:
            cabal_holders.add(h["wallet"])

    # Add mint as root node
    nodes.append({"id": mint, "type": "mint", "label": f"TOKEN\n{mint[:8]}..."})

    seen_nodes = {mint}

    for holder, data in holder_funder_map.items():
        funder = data["funder"]
        pct = data["balance_pct"]

        # Funder node
        if funder not in seen_nodes:
            node_type = "cabal_funder" if funder in cabal_funders else "funder"
            nodes.append({
                "id": funder,
                "type": node_type,
                "label": f"{funder[:8]}...",
                "is_cabal": funder in cabal_funders,
            })
            seen_nodes.add(funder)

        # Holder node
        if holder not in seen_nodes:
            node_type = "cabal_holder" if holder in cabal_holders else "holder"
            nodes.append({
                "id": holder,
                "type": node_type,
                "label": f"{holder[:8]}...\n{pct:.1f}%",
                "balance_pct": pct,
                "is_cabal": holder in cabal_holders,
            })
            seen_nodes.add(holder)

        # Funder → holder edge (SOL funding)
        edges.append({
            "from": funder,
            "to": holder,
            "type": "funded",
            "weight": pct,
        })

        # Holder → mint edge (token holding)
        edges.append({
            "from": holder,
            "to": mint,
            "type": "holds",
            "weight": pct,
        })

    graph_data = {"nodes": nodes, "edges": edges}

    # Build networkx graph if available
    if HAS_NETWORKX:
        G = nx.DiGraph()
        for node in nodes:
            G.add_node(node["id"], **{k: v for k, v in node.items() if k != "id"})
        for edge in edges:
            G.add_edge(edge["from"], edge["to"], **{k: v for k, v in edge.items() if k not in ("from", "to")})

        graph_data["networkx"] = G
        graph_data["stats"] = {
            "nodes": G.number_of_nodes(),
            "edges": G.number_of_edges(),
            "density": round(nx.density(G), 4),
        }

    return graph_data


# ═══════════════════════════════════════════════════════════════════════════════
# REPORT FORMATTER
# ═══════════════════════════════════════════════════════════════════════════════

def format_cluster_report(result: dict) -> str:
    if result.get("error"):
        return f"❌ {result['error']}"

    mint           = result.get("mint", "")
    scanned        = result.get("total_holders_scanned", 0)
    mapped         = result.get("holders_with_known_funder", 0)
    clusters       = result.get("clusters", [])
    cluster_count  = result.get("cluster_count", 0)
    score          = result.get("cabal_probability", 0)
    graph_stats    = result.get("graph", {}).get("stats", {})

    if score >= 75:   score_label = "🔴 VERY HIGH"
    elif score >= 50: score_label = "🟠 HIGH"
    elif score >= 25: score_label = "🟡 MODERATE"
    else:             score_label = "🟢 LOW"

    lines = [
        f"🕸 *WALLET CLUSTER REPORT*",
        f"`{mint}`", f"",
        f"*Cabal Probability: {score}/100 — {score_label}*", f"",
        f"━━━ 📊 SCAN SUMMARY ━━━",
        f"• Holders scanned: `{scanned}`",
        f"• Funders identified: `{mapped}`",
        f"• Cabal clusters found: `{cluster_count}`",
    ]

    if graph_stats:
        lines.append(f"• Graph nodes/edges: `{graph_stats.get('nodes', 0)}/{graph_stats.get('edges', 0)}`")

    lines.append(f"")

    if not clusters:
        lines.append("✅ No cabal clusters detected. Holder funding appears organic.")
    else:
        lines.append(f"━━━ 🚨 CABAL CLUSTERS ━━━")
        for i, cluster in enumerate(clusters[:5], 1):
            funder = cluster["funder"]
            count  = cluster["holder_count"]
            pct    = cluster["combined_supply_pct"]
            lines += [
                f"",
                f"*Cluster #{i}*",
                f"• Funder: `{funder[:8]}...{funder[-4:]}`",
                f"  [Solscan](https://solscan.io/account/{funder})",
                f"• Funded wallets: `{count}`",
                f"• Combined supply: `{pct}%`",
                f"• Wallets:",
            ]
            for h in cluster["holders"][:5]:
                w = h["wallet"]
                p = h["balance_pct"]
                lines.append(f"  └ `{w[:8]}...{w[-4:]}` — `{p}%`")
            if len(cluster["holders"]) > 5:
                lines.append(f"  └ _...and {len(cluster['holders'])-5} more_")

    lines += [
        f"",
        f"━━━ 💡 WHAT THIS MEANS ━━━",
    ]

    if score >= 75:
        lines.append("🔴 Strong evidence of coordinated wallet activity. These wallets were likely set up by the same entity and may dump together.")
    elif score >= 50:
        lines.append("🟠 Significant clustering detected. Multiple holders share funding sources — possible team/insider coordination.")
    elif score >= 25:
        lines.append("🟡 Some clustering present. Could be exchange wallets, bots, or mild coordination. Monitor closely.")
    else:
        lines.append("🟢 Holder distribution looks organic. No significant cabal activity detected.")

    lines += [f"", f"_Powered by Chain Sentinel • $CS_"]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# STANDALONE SCRIPT
# ═══════════════════════════════════════════════════════════════════════════════

async def main_standalone(mint: str):
    logging.basicConfig(level=logging.INFO)
    print(f"\n🕸 Chain Sentinel — Wallet Cluster Detector")
    print(f"Scanning: {mint}\n")

    result = await find_wallet_clusters(mint)
    print(format_cluster_report(result))

    # Save full JSON (excluding networkx object)
    save_result = {k: v for k, v in result.items() if k != "graph"}
    save_result["graph_nodes"] = result.get("graph", {}).get("nodes", [])
    save_result["graph_edges"] = result.get("graph", {}).get("edges", [])

    with open("cluster_report.json", "w") as f:
        json.dump(save_result, f, indent=2, default=str)
    print("\n✅ Full report saved to cluster_report.json")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python clusters.py <mint_address>")
        sys.exit(1)
    asyncio.run(main_standalone(sys.argv[1]))

"""
mirror.py — Whale Mirror Trading System for Chain Sentinel
Tracks smart money wallets and alerts when they buy, with position sizing advice.
"""

import os
import asyncio
import aiohttp
import json
import logging
import time
from collections import defaultdict

logger = logging.getLogger(__name__)

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
HELIUS_RPC     = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_WSS     = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API     = f"https://api.helius.xyz/v0"

_QN_BASE = os.environ.get("RPC_HTTP_BASE", "https://rpc.ankr.com/base")
_QN_ETH  = os.environ.get("RPC_HTTP_ETH",  "https://rpc.ankr.com/eth")

STORAGE_FILE = "mirror_data.json"

# ═══════════════════════════════════════════════════════════════════════════════
# STORAGE
# ═══════════════════════════════════════════════════════════════════════════════

def _load() -> dict:
    try:
        with open(STORAGE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"wallets": {}, "budgets": {}, "active": {}}

def _save(data: dict):
    with open(STORAGE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def add_mirror_wallet(user_id: int, wallet: str, chain: str, label: str = "") -> bool:
    """Add a wallet to mirror for a user."""
    data = _load()
    uid  = str(user_id)
    if uid not in data["wallets"]:
        data["wallets"][uid] = {}
    wallet = wallet.lower() if chain != "solana" else wallet
    data["wallets"][uid][wallet] = {
        "chain":    chain,
        "label":    label or f"{wallet[:8]}...{wallet[-4:]}",
        "added_at": int(time.time()),
        "trades":   0,
    }
    _save(data)
    return True

def remove_mirror_wallet(user_id: int, wallet: str) -> bool:
    data = _load()
    uid  = str(user_id)
    wallet_lower = wallet.lower()
    if uid in data["wallets"]:
        # Match case-insensitively
        to_remove = next((k for k in data["wallets"][uid] if k.lower() == wallet_lower), None)
        if to_remove:
            del data["wallets"][uid][to_remove]
            _save(data)
            return True
    return False

def set_budget(user_id: int, amount_usd: float):
    data = _load()
    data["budgets"][str(user_id)] = amount_usd
    _save(data)

def get_budget(user_id: int) -> float:
    data = _load()
    return float(data["budgets"].get(str(user_id), 0))

def get_user_wallets(user_id: int) -> dict:
    data = _load()
    return data["wallets"].get(str(user_id), {})

def get_all_tracked_wallets() -> dict:
    """Returns {wallet: [(user_id, chain, label), ...]}"""
    data  = _load()
    index = defaultdict(list)
    for uid, wallets in data["wallets"].items():
        for wallet, info in wallets.items():
            index[wallet].append((int(uid), info["chain"], info["label"]))
    return dict(index)

def increment_trade_count(user_id: int, wallet: str):
    data = _load()
    uid  = str(user_id)
    if uid in data["wallets"] and wallet in data["wallets"][uid]:
        data["wallets"][uid][wallet]["trades"] = data["wallets"][uid][wallet].get("trades", 0) + 1
        _save(data)


# ═══════════════════════════════════════════════════════════════════════════════
# BALANCE FETCHING
# ═══════════════════════════════════════════════════════════════════════════════

async def get_wallet_balance_usd(session: aiohttp.ClientSession, wallet: str, chain: str) -> float:
    """Get total USD balance (native token) for a wallet."""
    try:
        if chain == "solana":
            return await _sol_balance_usd(session, wallet)
        elif chain == "base":
            return await _evm_balance_usd(session, wallet, _QN_BASE, "base")
        elif chain == "ethereum":
            return await _evm_balance_usd(session, wallet, _QN_ETH, "ethereum")
    except Exception as e:
        logger.warning(f"[MIRROR] balance error for {wallet[:8]}: {e}")
    return 0.0


async def _sol_balance_usd(session, wallet) -> float:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
    async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
        data = await resp.json()
    lamports = data.get("result", {}).get("value", 0)
    sol = lamports / 1e9

    # Get SOL price from DexScreener
    sol_price = await _get_sol_price(session)
    return sol * sol_price


async def _get_sol_price(session) -> float:
    try:
        url = "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json()
        pairs = data.get("pairs", []) or []
        if pairs:
            return float(pairs[0].get("priceUsd", 150))
    except Exception:
        pass
    return 150.0  # fallback


async def _evm_balance_usd(session, wallet, rpc, chain) -> float:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
               "params": [wallet, "latest"]}
    async with session.post(rpc, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
        data = await resp.json()
    wei = int(data.get("result", "0x0"), 16)
    eth = wei / 1e18

    price = await _get_eth_price(session)
    return eth * price


async def _get_eth_price(session) -> float:
    try:
        url = "https://api.dexscreener.com/latest/dex/tokens/0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json()
        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "ethereum"]
        if pairs:
            return float(pairs[0].get("priceUsd", 2000))
    except Exception:
        pass
    return 2000.0


# ═══════════════════════════════════════════════════════════════════════════════
# TRADE DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

async def get_recent_trades(session: aiohttp.ClientSession, wallet: str, chain: str, limit: int = 20) -> list:
    """Get recent buy trades for a wallet."""
    try:
        if chain == "solana":
            return await _sol_recent_trades(session, wallet, limit)
        else:
            return await _evm_recent_trades(session, wallet, chain, limit)
    except Exception as e:
        logger.warning(f"[MIRROR] trades error for {wallet[:8]}: {e}")
    return []


async def _sol_recent_trades(session, wallet, limit) -> list:
    url = f"{HELIUS_API}/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit={limit}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
        if resp.status != 200:
            return []
        txs = await resp.json()

    trades = []
    if not isinstance(txs, list):
        return []

    for tx in txs:
        ts      = tx.get("timestamp", 0)
        tx_type = tx.get("type", "")

        # Find SOL spent and token received
        sol_change = 0.0
        for change in tx.get("accountData", []):
            if change.get("account") == wallet:
                sol_change = change.get("nativeBalanceChange", 0) / 1e9

        # Buy = spent SOL, received token
        for transfer in tx.get("tokenTransfers", []):
            mint      = transfer.get("mint", "")
            to_addr   = transfer.get("toUserAccount", "")
            from_addr = transfer.get("fromUserAccount", "")
            amount    = float(transfer.get("tokenAmount", 0))

            if to_addr == wallet and sol_change < -0.001:
                trades.append({
                    "type":       "BUY",
                    "token":      mint,
                    "chain":      "solana",
                    "amount":     amount,
                    "spent_native": abs(sol_change),
                    "timestamp":  ts,
                    "sig":        tx.get("signature", ""),
                })

    return trades


async def _evm_recent_trades(session, wallet, chain, limit) -> list:
    rpc = _QN_BASE if chain == "base" else _QN_ETH

    # Get latest block
    blk = await _rpc(session, rpc, "eth_blockNumber", [])
    latest = int(blk, 16)
    from_block = hex(max(0, latest - 50000))

    # Get Transfer events TO this wallet (receiving tokens = possible buy)
    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    wallet_topic   = "0x" + "0" * 24 + wallet[2:].lower()

    logs = await _rpc(session, rpc, "eth_getLogs", [{
        "topics": [TRANSFER_TOPIC, None, wallet_topic],
        "fromBlock": from_block,
        "toBlock": "latest"
    }], timeout=12)

    if not logs:
        return []

    trades = []
    seen_tx = set()

    for log in (logs or [])[:limit]:
        tx_hash = log.get("transactionHash", "")
        if tx_hash in seen_tx:
            continue
        seen_tx.add(tx_hash)

        token_ca = log.get("address", "")
        blk_num  = int(log.get("blockNumber", "0x0"), 16)

        # Get tx to check ETH spent
        tx = await _rpc(session, rpc, "eth_getTransactionByHash", [tx_hash])
        if not tx or tx.get("from", "").lower() != wallet.lower():
            continue

        eth_value = int(tx.get("value", "0x0"), 16) / 1e18
        if eth_value < 0.001:
            continue  # skip tiny/zero value txs

        # Get block timestamp
        blk_data  = await _rpc(session, rpc, "eth_getBlockByNumber", [hex(blk_num), False])
        timestamp = int(blk_data.get("timestamp", "0x0"), 16) if blk_data else 0

        trades.append({
            "type":         "BUY",
            "token":        token_ca,
            "chain":        chain,
            "amount":       int(log.get("data", "0x0"), 16),
            "spent_native": eth_value,
            "timestamp":    timestamp,
            "tx_hash":      tx_hash,
        })

    return trades


async def _rpc(session, rpc_url, method, params, timeout=10):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        data = await resp.json()
    return data.get("result")


# ═══════════════════════════════════════════════════════════════════════════════
# MIRROR ALERT BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

async def build_mirror_alert(
    wallet: str, label: str, chain: str,
    trade: dict, user_budget_usd: float
) -> dict:
    """Build mirror alert data for a single trade."""
    async with aiohttp.ClientSession() as session:
        # Get wallet's total native balance in USD
        wallet_balance_usd = await get_wallet_balance_usd(session, wallet, chain)

        # Get native asset price
        if chain == "solana":
            native_price = await _get_sol_price(session)
            native_sym   = "SOL"
        else:
            native_price = await _get_eth_price(session)
            native_sym   = "ETH"

        # How much they spent in USD
        spent_native = trade.get("spent_native", 0)
        spent_usd    = spent_native * native_price

        # What % of their bag did they put in
        conviction_pct = (spent_usd / wallet_balance_usd * 100) if wallet_balance_usd > 0 else 0

        # Mirror sizing: same % of user's budget
        mirror_usd    = user_budget_usd * (conviction_pct / 100)
        mirror_native = mirror_usd / native_price if native_price > 0 else 0

        # Get token name
        token_ca   = trade.get("token", "")
        token_name = await _get_token_name(session, token_ca, chain)

    return {
        "wallet":           wallet,
        "label":            label,
        "chain":            chain,
        "token_ca":         token_ca,
        "token_name":       token_name,
        "spent_native":     round(spent_native, 4),
        "spent_usd":        round(spent_usd, 2),
        "wallet_bal_usd":   round(wallet_balance_usd, 2),
        "conviction_pct":   round(conviction_pct, 2),
        "user_budget_usd":  user_budget_usd,
        "mirror_usd":       round(mirror_usd, 2),
        "mirror_native":    round(mirror_native, 4),
        "native_sym":       native_sym,
        "native_price":     round(native_price, 2),
    }


async def _get_token_name(session, ca, chain) -> str:
    try:
        if chain == "solana":
            url = f"https://frontend-api.pump.fun/coins/{ca}"
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                   timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    d = await resp.json()
                    if d.get("name"):
                        return f"{d['name']} (${d.get('symbol','???')})"

        chain_id = {"base": "base", "ethereum": "ethereum"}.get(chain, chain)
        url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == chain_id]
                if pairs:
                    t = pairs[0].get("baseToken", {})
                    return f"{t.get('name','?')} (${t.get('symbol','???')})"
    except Exception:
        pass
    return f"`{ca[:8]}...{ca[-4:]}`"


# ═══════════════════════════════════════════════════════════════════════════════
# POLLING MONITOR — checks tracked wallets every 60s
# ═══════════════════════════════════════════════════════════════════════════════

_last_seen: dict = {}  # wallet -> last tx signature/hash

async def run_mirror_monitor(bot):
    """Poll tracked wallets every 60 seconds for new buys."""
    logger.info("[MIRROR] Mirror monitor started.")
    await asyncio.sleep(10)  # let bot fully start

    while True:
        try:
            await _poll_all_wallets(bot)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[MIRROR] poll error: {e}", exc_info=True)
        await asyncio.sleep(60)  # poll every 60 seconds


async def _poll_all_wallets(bot):
    all_wallets = get_all_tracked_wallets()
    if not all_wallets:
        return

    logger.info(f"[MIRROR] Polling {len(all_wallets)} wallets...")

    async with aiohttp.ClientSession() as session:
        for wallet, subscribers in all_wallets.items():
            try:
                chain = subscribers[0][1]  # all subscribers share same chain
                trades = await get_recent_trades(session, wallet, chain, limit=5)

                if not trades:
                    continue

                # Check for new trades since last poll
                latest_id = trades[0].get("sig") or trades[0].get("tx_hash", "")
                last_id   = _last_seen.get(wallet)

                if last_id is None:
                    # First poll — just record, don't alert
                    _last_seen[wallet] = latest_id
                    continue

                if latest_id == last_id:
                    continue  # nothing new

                # Find new trades
                new_trades = []
                for t in trades:
                    tid = t.get("sig") or t.get("tx_hash", "")
                    if tid == last_id:
                        break
                    new_trades.append(t)

                _last_seen[wallet] = latest_id

                # Alert each subscriber for each new buy
                for trade in new_trades:
                    if trade.get("type") != "BUY":
                        continue
                    for (user_id, _, label) in subscribers:
                        budget = get_budget(user_id)
                        if budget <= 0:
                            continue
                        try:
                            alert = await build_mirror_alert(wallet, label, chain, trade, budget)
                            msg   = format_mirror_alert(alert)
                            await bot.send_message(
                                chat_id=user_id,
                                text=msg,
                                parse_mode="Markdown",
                                disable_web_page_preview=True
                            )
                            increment_trade_count(user_id, wallet)
                            logger.info(f"[MIRROR] Alert sent to {user_id} for {wallet[:8]}")
                        except Exception as e:
                            logger.warning(f"[MIRROR] alert send error: {e}")

            except Exception as e:
                logger.warning(f"[MIRROR] wallet poll error {wallet[:8]}: {e}")
            await asyncio.sleep(1)  # small delay between wallets


# ═══════════════════════════════════════════════════════════════════════════════
# ALERT FORMATTER
# ═══════════════════════════════════════════════════════════════════════════════

def format_mirror_alert(a: dict) -> str:
    chain_emoji = {"solana": "◎", "base": "🔵", "ethereum": "⟠"}.get(a["chain"], "🔗")
    chain_name  = a["chain"].title()

    # Conviction level label
    pct = a["conviction_pct"]
    if pct >= 10:   conv_label = "🔥 VERY HIGH conviction"
    elif pct >= 5:  conv_label = "💪 HIGH conviction"
    elif pct >= 2:  conv_label = "✅ Moderate conviction"
    else:           conv_label = "👀 Small position"

    def fmt_usd(v):
        return f"${v:,.2f}"

    lines = [
        f"🪞 *MIRROR ALERT* {chain_emoji}",
        f"",
        f"*{a['label']}* just bought",
        f"*{a['token_name']}*",
        f"`{a['token_ca']}`",
        f"",
        f"━━━ 💰 THEIR TRADE ━━━",
        f"• Spent: `{a['spent_native']} {a['native_sym']}` ({fmt_usd(a['spent_usd'])})",
        f"• Wallet balance: `{fmt_usd(a['wallet_bal_usd'])}`",
        f"• Conviction: `{pct}%` of their bag — {conv_label}",
        f"",
        f"━━━ 🎯 YOUR MIRROR TRADE ━━━",
        f"• Your budget: `{fmt_usd(a['user_budget_usd'])}`",
        f"• To match conviction: `{a['mirror_native']} {a['native_sym']}` ({fmt_usd(a['mirror_usd'])})",
        f"",
    ]

    # Buy button URL
    if a["chain"] == "solana":
        buy_url = f"https://pump.fun/{a['token_ca']}"
    elif a["chain"] == "base":
        buy_url = f"https://app.uniswap.org/#/swap?outputCurrency={a['token_ca']}&chain=base"
    else:
        buy_url = f"https://app.uniswap.org/#/swap?outputCurrency={a['token_ca']}"

    lines += [
        f"[🛒 Buy {a['mirror_native']} {a['native_sym']} now]({buy_url})",
        f"",
        f"_Powered by Chain Sentinel • $CS_",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# MIRROR LIST FORMATTER
# ═══════════════════════════════════════════════════════════════════════════════

def format_mirror_list(user_id: int) -> str:
    wallets = get_user_wallets(user_id)
    budget  = get_budget(user_id)

    if not wallets:
        return (
            "🪞 *Mirror Tracker*\n\n"
            "No wallets tracked yet.\n\n"
            "Run `/smartmoney <CA1> <CA2>` to find smart money wallets, "
            "then use `/mirror add <wallet>` to track them.\n\n"
            f"Your budget: `{'$' + str(budget) if budget else 'Not set — use /mirror budget <amount>'}`"
        )

    lines = [
        f"🪞 *Mirror Tracker*",
        f"Your budget: `${budget:,.2f}`",
        f"Tracking `{len(wallets)}` wallet(s):",
        f"",
    ]

    for wallet, info in wallets.items():
        chain_emoji = {"solana": "◎", "base": "🔵", "ethereum": "⟠"}.get(info["chain"], "🔗")
        lines.append(
            f"{chain_emoji} `{wallet[:8]}...{wallet[-4:]}` — "
            f"{info['label']} | {info['trades']} alerts | {info['chain'].title()}"
        )

    lines += ["", "_Alerts fire within 60 seconds of a buy._"]
    return "\n".join(lines)

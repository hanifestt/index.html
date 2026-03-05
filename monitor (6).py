"""
monitor.py — Real-time Pump.fun Launch Monitor for Chain Sentinel
Listens to Solana via WebSocket, filters by socials + dev alpha, alerts via Telegram.
"""

import os
import asyncio
import aiohttp
import json
import logging
import time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot

logger = logging.getLogger(__name__)

HELIUS_API_KEY  = os.environ.get("HELIUS_API_KEY", "")
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
ADMIN_ID        = int(os.environ.get("ADMIN_ID", "0"))

# Helius WebSocket endpoint
HELIUS_WSS      = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_RPC      = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API      = f"https://api.helius.xyz/v0"

PUMP_PROGRAM    = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_API        = "https://frontend-api.pump.fun"

BULLISH_DEV_MC_THRESHOLD = 500_000  # $500k

# Track seen mints to avoid duplicate alerts
_seen_mints: set = set()
# Store alert chat IDs (users who enabled monitoring)
_monitor_users: set = set()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN MONITOR LOOP — runs as background task
# ═══════════════════════════════════════════════════════════════════════════════

async def run_monitor(bot: Bot):
    """
    Poll pump.fun REST API every 10 seconds for new launches.
    No WebSocket needed — works on free Helius plan.
    """
    run_monitor._retry_count = 0
    logger.info("[MONITOR] Starting pump.fun monitor (HTTP polling)...")
    last_mint = None
    await asyncio.sleep(5)

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{PUMP_API}/coins?offset=0&limit=20&sort=created_timestamp&order=DESC&includeNsfw=true"
                async with session.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 429:
                        logger.warning("[MONITOR] pump.fun rate limited, waiting 30s...")
                        await asyncio.sleep(30)
                        continue
                    if resp.status != 200:
                        await asyncio.sleep(15)
                        continue
                    coins = await resp.json()

            if not isinstance(coins, list) or not coins:
                await asyncio.sleep(10)
                continue

            latest_mint = coins[0].get("mint", "")

            if last_mint is None:
                last_mint = latest_mint
                logger.info(f"[MONITOR] Initialized at {latest_mint[:8]}...")
            elif latest_mint != last_mint:
                # Find new coins since last check
                new_coins = []
                for coin in coins:
                    mint = coin.get("mint", "")
                    if mint == last_mint:
                        break
                    if mint:
                        new_coins.append(coin)

                last_mint = latest_mint
                logger.info(f"[MONITOR] {len(new_coins)} new launch(es) detected")

                for coin in new_coins[:5]:
                    mint = coin.get("mint", "")
                    if not mint:
                        continue
                    start_time = time.time()
                    asyncio.ensure_future(
                        _process_launch_from_coin(coin, bot, start_time)
                    )

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[MONITOR] poll error: {e}")

        await asyncio.sleep(10)


async def _process_launch_from_coin(coin: dict, bot: Bot, start_time: float):
    """Process a new launch detected from pump.fun API polling."""
    mint = coin.get("mint", "")
    if not mint:
        return
    try:
        async with aiohttp.ClientSession() as local_session:
            # Build meta directly from coin data
            meta = {
                "name":     coin.get("name", "Unknown"),
                "symbol":   coin.get("symbol", "???"),
                "twitter":  coin.get("twitter", ""),
                "telegram": coin.get("telegram", ""),
                "website":  coin.get("website", ""),
                "image":    coin.get("image_uri", ""),
            }

            # Get dev alpha in parallel
            dev_data = await get_dev_alpha_for_monitor(local_session, coin.get("creator", ""))

            has_twitter  = bool(meta.get("twitter"))
            has_telegram = bool(meta.get("telegram"))

            if not (has_twitter or has_telegram):
                logger.info(f"[MONITOR] {mint[:8]}... skipped — no socials")
                # Still pass to sniper for evaluation
                try:
                    from sniper import evaluate_and_alert
                    asyncio.ensure_future(evaluate_and_alert(bot, mint, "solana"))
                except Exception:
                    pass
                return

            elapsed = round((time.time() - start_time) * 1000)
            await send_launch_alert(bot, mint, meta, dev_data, coin.get("creator", ""),
                                    "", elapsed, has_twitter, has_telegram)

            # Pass to sniper
            try:
                from sniper import evaluate_and_alert
                asyncio.ensure_future(evaluate_and_alert(bot, mint, "solana"))
            except Exception as e:
                logger.warning(f"[MONITOR] sniper hook error: {e}")

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"[MONITOR] _process_launch_from_coin error: {e}", exc_info=True)


async def _process_launch(sig: str, session: aiohttp.ClientSession, bot: Bot, start_time: float):
    # Use a fresh session per launch to avoid closed session errors
    try:
        async with aiohttp.ClientSession() as local_session:
            mint, metadata_uri, creator = await get_launch_details(local_session, sig)
            if not mint:
                return

            if mint in _seen_mints:
                return
            _seen_mints.add(mint)

            logger.info(f"[MONITOR] New token: {mint[:8]}... by {str(creator)[:8]}...")

            try:
                meta, dev_data = await asyncio.wait_for(
                    asyncio.gather(
                        fetch_metadata(local_session, mint, metadata_uri),
                        get_dev_alpha_for_monitor(local_session, creator),
                    ),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                logger.warning(f"[MONITOR] Timeout enriching {mint[:8]}...")
                return

            has_twitter  = bool(meta.get("twitter"))
            has_telegram = bool(meta.get("telegram"))

            if not (has_twitter or has_telegram):
                logger.info(f"[MONITOR] {mint[:8]}... skipped — no socials")
                return

            elapsed = round((time.time() - start_time) * 1000)
            await send_launch_alert(bot, mint, meta, dev_data, creator, sig, elapsed, has_twitter, has_telegram)

            # Pass to sniper for legitimacy scoring
            try:
                from sniper import evaluate_and_alert
                asyncio.ensure_future(evaluate_and_alert(bot, mint, "solana", sig))
            except Exception as e:
                logger.warning(f"[MONITOR] sniper hook error: {e}")

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"[MONITOR] _process_launch error: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — GET LAUNCH DETAILS FROM TRANSACTION
# ═══════════════════════════════════════════════════════════════════════════════

async def get_launch_details(session: aiohttp.ClientSession, sig: str):
    """Extract mint address, metadata URI and creator from the create transaction."""
    try:
        # First try pump.fun API — fastest
        pump_url = f"{PUMP_API}/trades/latest?limit=1"
        async with session.get(pump_url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
            pass  # just warm up

        # Get transaction via Helius
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTransaction",
            "params": [sig, {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0
            }]
        }
        async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            data = await resp.json()

        result = data.get("result")
        if not result:
            return None, None, None

        # Find mint from account keys — pump.fun create puts mint at index 0 or 1
        account_keys = result.get("transaction", {}).get("message", {}).get("accountKeys", [])
        inner_instructions = result.get("meta", {}).get("innerInstructions", [])
        log_messages = result.get("meta", {}).get("logMessages", [])

        # Extract creator (fee payer = index 0)
        creator = None
        for key in account_keys:
            addr = key.get("pubkey") if isinstance(key, dict) else key
            signer = key.get("signer", False) if isinstance(key, dict) else False
            if addr and signer:
                creator = addr
                break

        # Find mint address — look for InitializeMint in inner instructions
        mint = None
        for inner in inner_instructions:
            for ix in inner.get("instructions", []):
                parsed = ix.get("parsed", {})
                if isinstance(parsed, dict):
                    ix_type = parsed.get("type", "")
                    if "initializeMint" in ix_type:
                        mint = parsed.get("info", {}).get("mint")
                        if mint:
                            break
            if mint:
                break

        # Fallback: scan account keys for likely mint (32-44 char base58, not a known program)
        if not mint:
            known_programs = {PUMP_PROGRAM, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                              "11111111111111111111111111111111", "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bfE"}
            for key in account_keys[1:6]:
                addr = key.get("pubkey") if isinstance(key, dict) else key
                if addr and addr not in known_programs and len(addr) >= 32:
                    writable = key.get("writable", False) if isinstance(key, dict) else False
                    if writable:
                        mint = addr
                        break

        # Try to find metadata URI from logs
        metadata_uri = None
        for log in log_messages:
            if "uri" in log.lower() and "http" in log:
                import re
                urls = re.findall(r'https?://[^\s"\']+', log)
                if urls:
                    metadata_uri = urls[0]
                    break

        return mint, metadata_uri, creator

    except Exception as e:
        logger.warning(f"[MONITOR] get_launch_details error: {e}")
        return None, None, None


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — FETCH METADATA & SOCIALS
# ═══════════════════════════════════════════════════════════════════════════════

async def fetch_metadata(session: aiohttp.ClientSession, mint: str, metadata_uri: str = None) -> dict:
    """Fetch token name, symbol, description, twitter, telegram."""
    meta = {
        "name": "Unknown", "symbol": "???",
        "description": "", "image": "",
        "twitter": "", "telegram": "", "website": "",
    }

    # Source 1: pump.fun coin API — most reliable for new tokens
    try:
        url = f"{PUMP_API}/coins/{mint}"
        headers = {"User-Agent": "Mozilla/5.0"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, dict):
                    meta["name"]        = data.get("name", "Unknown")
                    meta["symbol"]      = data.get("symbol", "???")
                    meta["description"] = data.get("description", "")
                    meta["image"]       = data.get("image_uri", "")
                    meta["twitter"]     = data.get("twitter", "")
                    meta["telegram"]    = data.get("telegram", "")
                    meta["website"]     = data.get("website", "")
                    if meta["name"] != "Unknown":
                        return meta
    except Exception:
        pass

    # Source 2: metadata URI (IPFS or Arweave)
    if metadata_uri:
        try:
            async with session.get(metadata_uri, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    meta["name"]    = data.get("name", meta["name"])
                    meta["symbol"]  = data.get("symbol", meta["symbol"])
                    meta["image"]   = data.get("image", meta["image"])
                    ext = data.get("extensions", {})
                    meta["twitter"]  = ext.get("twitter", "") or data.get("twitter", "")
                    meta["telegram"] = ext.get("telegram", "") or data.get("telegram", "")
                    meta["website"]  = ext.get("website", "") or data.get("website", "")
        except Exception:
            pass

    return meta


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — DEV ALPHA (fast version)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_dev_alpha_for_monitor(session: aiohttp.ClientSession, creator: str) -> dict:
    """Lightweight dev history check for real-time monitoring."""
    if not creator:
        return {}
    try:
        # Check pump.fun for creator's previous tokens
        url = f"{PUMP_API}/coins/user-created-coins/{creator}?offset=0&limit=20&includeNsfw=true"
        headers = {"User-Agent": "Mozilla/5.0"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status != 200:
                return {}
            coins = await resp.json()

        if not isinstance(coins, list):
            return {}

        total = len(coins)
        if total == 0:
            return {"total_launches": 0, "biggest_mc": 0, "is_bullish": False}

        # Get market caps from DexScreener for previous tokens
        mints = [c.get("mint", "") for c in coins[:10] if c.get("mint")]
        biggest_mc = 0
        biggest_name = ""

        if mints:
            try:
                dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(mints[:29])}"
                async with session.get(dex_url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status == 200:
                        dex_data = await resp.json()
                        for pair in dex_data.get("pairs", []) or []:
                            if pair.get("chainId") != "solana":
                                continue
                            mc = float(pair.get("fdv") or pair.get("marketCap") or 0)
                            if mc > biggest_mc:
                                biggest_mc = mc
                                biggest_name = pair.get("baseToken", {}).get("name", "")
            except Exception:
                pass

        is_bullish = biggest_mc >= BULLISH_DEV_MC_THRESHOLD

        return {
            "total_launches": total,
            "biggest_mc": biggest_mc,
            "biggest_name": biggest_name,
            "is_bullish": is_bullish,
        }
    except Exception as e:
        logger.warning(f"[MONITOR] dev alpha error: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4 — SEND TELEGRAM ALERT
# ═══════════════════════════════════════════════════════════════════════════════

async def send_launch_alert(
    bot: Bot, mint: str, meta: dict, dev: dict,
    creator: str, sig: str, elapsed_ms: int,
    has_twitter: bool, has_telegram: bool
):
    def fmt_mc(mc):
        if mc >= 1_000_000: return f"${mc/1_000_000:.2f}M"
        if mc >= 1_000:     return f"${mc/1_000:.1f}K"
        return f"${mc:.0f}"

    name     = meta.get("name", "Unknown")
    symbol   = meta.get("symbol", "???")
    desc     = meta.get("description", "")[:120]
    twitter  = meta.get("twitter", "")
    telegram = meta.get("telegram", "")
    website  = meta.get("website", "")

    total_launches = dev.get("total_launches", 0)
    biggest_mc     = dev.get("biggest_mc", 0)
    biggest_name   = dev.get("biggest_name", "")
    is_bullish     = dev.get("is_bullish", False)

    # Social badges
    socials = []
    if has_twitter:  socials.append("🐦 Twitter")
    if has_telegram: socials.append("✈️ Telegram")
    if website:      socials.append("🌐 Website")
    social_str = " • ".join(socials)

    # Dev badge
    if is_bullish:
        dev_badge = f"🔥 *BULLISH DEV* — Best launch: {biggest_name} @ {fmt_mc(biggest_mc)}"
    elif total_launches > 3:
        dev_badge = f"⚠️ Serial launcher — {total_launches} previous tokens"
    elif total_launches > 0:
        dev_badge = f"📊 {total_launches} prev launch(es) — Best: {fmt_mc(biggest_mc)}"
    else:
        dev_badge = "🆕 First launch"

    lines = [
        f"🚀 *NEW TOKEN LAUNCH*",
        f"",
        f"*{name}* (${symbol})",
        f"`{mint}`",
        f"",
        f"📣 {social_str}",
        f"",
    ]

    if desc:
        lines += [f"_{desc}_", f""]

    lines += [
        f"👨‍💻 *Dev:* `{str(creator)[:8]}...{str(creator)[-4:]}`",
        f"{dev_badge}",
        f"",
        f"⚡ Detected in `{elapsed_ms}ms`",
        f"[View TX](https://solscan.io/tx/{sig})",
    ]

    text = "\n".join(lines)

    # Buttons
    buttons = [
        [
            InlineKeyboardButton("🛒 Buy 0.5 SOL", url=f"https://pump.fun/{mint}"),
            InlineKeyboardButton("📊 Pump.fun", url=f"https://pump.fun/coin/{mint}"),
        ],
        [
            InlineKeyboardButton("🔍 Full Scan", callback_data=f"scan:{mint}"),
            InlineKeyboardButton("🕸 Cluster Scan", callback_data=f"cluster:{mint}"),
        ]
    ]
    if twitter:
        buttons.append([InlineKeyboardButton("🐦 Twitter", url=twitter if twitter.startswith("http") else f"https://twitter.com/{twitter.lstrip('@')}")])
    if telegram:
        buttons.append([InlineKeyboardButton("✈️ Telegram", url=telegram if telegram.startswith("http") else f"https://t.me/{telegram.lstrip('@')}")])

    markup = InlineKeyboardMarkup(buttons)

    # Send to all monitoring users + admin
    recipients = _monitor_users | {ADMIN_ID} if ADMIN_ID else _monitor_users
    for chat_id in recipients:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            logger.info(f"[MONITOR] Alert sent to {chat_id} for {mint[:8]}...")
        except Exception as e:
            logger.warning(f"[MONITOR] Failed to send to {chat_id}: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# USER MANAGEMENT — who receives live alerts
# ═══════════════════════════════════════════════════════════════════════════════

def add_monitor_user(user_id: int):
    _monitor_users.add(user_id)

def remove_monitor_user(user_id: int):
    _monitor_users.discard(user_id)

def is_monitoring(user_id: int) -> bool:
    return user_id in _monitor_users

def get_monitor_count() -> int:
    return len(_monitor_users)

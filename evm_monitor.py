"""
evm_monitor.py — Real-time monitor for Base (pump.fun) and Ethereum (Uniswap) new launches.
"""

import os
import asyncio
import aiohttp
import json
import logging
import time
import websockets
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot

logger = logging.getLogger(__name__)

ALCHEMY_API_KEY = os.environ.get("ALCHEMY_API_KEY", "")
ALCHEMY_BASE_WS = f"wss://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
ALCHEMY_ETH_WS  = f"wss://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
ADMIN_ID        = int(os.environ.get("ADMIN_ID", "0"))

# Base pump.fun program (Zora/Base memecoin launchpad)
BASE_PUMP_CONTRACTS = [
    "0x0000000000000000000000000000000000000000",  # placeholder — Base pump.fun CA
    "0x420DD381b31aEf6683db6B902084cB0FFECe40Da", # Clanker (Base memecoin launcher)
    "0x3FC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD", # Uniswap Universal Router Base
]

# Uniswap V2/V3 factory addresses
UNISWAP_V2_FACTORY_ETH  = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
UNISWAP_V3_FACTORY_ETH  = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
UNISWAP_V2_FACTORY_BASE = "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"
UNISWAP_V3_FACTORY_BASE = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"

# PairCreated event topic
PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
# PoolCreated V3 topic
POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

BULLISH_DEV_MC = 500_000

_evm_monitor_users: set = set()
_seen_evm: set = set()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN — runs both Base and ETH monitors in parallel
# ═══════════════════════════════════════════════════════════════════════════════

async def run_evm_monitor(bot: Bot):
    logger.info("[EVM_MON] Starting EVM monitor...")
    await asyncio.gather(
        _monitor_chain(bot, "base"),
        _monitor_chain(bot, "ethereum"),
    )


async def _monitor_chain(bot: Bot, chain: str):
    ws_url = ALCHEMY_BASE_WS if chain == "base" else ALCHEMY_ETH_WS
    v2_factory = UNISWAP_V2_FACTORY_BASE if chain == "base" else UNISWAP_V2_FACTORY_ETH
    v3_factory = UNISWAP_V3_FACTORY_BASE if chain == "base" else UNISWAP_V3_FACTORY_ETH

    while True:
        try:
            async with websockets.connect(
                ws_url, ping_interval=20, ping_timeout=30
            ) as ws:
                logger.info(f"[EVM_MON] Connected to {chain}")

                # Subscribe to Uniswap V2 PairCreated
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "eth_subscribe",
                    "params": ["logs", {
                        "address": [v2_factory, v3_factory],
                        "topics": [[PAIR_CREATED_TOPIC, POOL_CREATED_TOPIC]]
                    }]
                }))
                await ws.recv()  # subscription confirmation

                async with aiohttp.ClientSession() as session:
                    async for raw in ws:
                        try:
                            await _handle_evm_event(raw, session, bot, chain)
                        except Exception as e:
                            logger.warning(f"[EVM_MON] Event error: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[EVM_MON] {chain} connection error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════════════════════
# EVENT HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

async def _handle_evm_event(raw: str, session: aiohttp.ClientSession, bot: Bot, chain: str):
    msg = json.loads(raw)
    if "result" not in msg.get("params", {}):
        return

    log = msg["params"]["result"]
    topics = log.get("topics", [])
    if not topics:
        return

    topic0 = topics[0].lower()
    start_time = time.time()

    # Decode token address from log data
    token_ca = None

    if topic0 == PAIR_CREATED_TOPIC.lower():
        # V2 PairCreated: token0 = topics[1], token1 = topics[2]
        # One of them is WETH — the other is the new token
        WETH_ETH  = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
        WETH_BASE = "0x4200000000000000000000000000000000000006"
        weth = WETH_BASE if chain == "base" else WETH_ETH

        if len(topics) >= 3:
            t0 = "0x" + topics[1][-40:]
            t1 = "0x" + topics[2][-40:]
            token_ca = t1 if t0.lower() == weth.lower() else t0

    elif topic0 == POOL_CREATED_TOPIC.lower():
        # V3 PoolCreated: token0 = topics[1], token1 = topics[2]
        WETH_ETH  = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
        WETH_BASE = "0x4200000000000000000000000000000000000006"
        weth = WETH_BASE if chain == "base" else WETH_ETH

        if len(topics) >= 3:
            t0 = "0x" + topics[1][-40:]
            t1 = "0x" + topics[2][-40:]
            token_ca = t1 if t0.lower() == weth.lower() else t0

    if not token_ca:
        return

    token_ca = token_ca.lower()
    if token_ca in _seen_evm:
        return
    _seen_evm.add(token_ca)

    tx_hash = log.get("transactionHash", "")
    logger.info(f"[EVM_MON] New {chain} token: {token_ca[:10]}... tx={tx_hash[:10]}...")

    asyncio.create_task(
        _process_evm_launch(token_ca, tx_hash, chain, session, bot, start_time)
    )


# ═══════════════════════════════════════════════════════════════════════════════
# LAUNCH PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

async def _process_evm_launch(ca: str, tx_hash: str, chain: str, session: aiohttp.ClientSession, bot: Bot, start_time: float):
    try:
        from evm_scanner import get_evm_token_meta, get_evm_dev_history

        # Fetch meta + dev in parallel
        meta, dev = await asyncio.gather(
            get_evm_token_meta(session, ca, chain),
            get_evm_dev_history(session, ca, chain),
        )

        name   = meta.get("name", "Unknown")
        symbol = meta.get("symbol", "???")

        # Filter: skip if no name (likely not a memecoin)
        if name == "Unknown" and symbol == "???":
            return

        elapsed = round((time.time() - start_time) * 1000)
        await _send_evm_alert(bot, ca, chain, meta, dev, tx_hash, elapsed)

    except Exception as e:
        logger.error(f"[EVM_MON] process error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# ALERT
# ═══════════════════════════════════════════════════════════════════════════════

async def _send_evm_alert(bot: Bot, ca: str, chain: str, meta: dict, dev: dict, tx_hash: str, elapsed_ms: int):
    chain_emoji = "🔵" if chain == "base" else "⟠"
    chain_name  = "Base" if chain == "base" else "Ethereum"
    explorer    = "basescan.org" if chain == "base" else "etherscan.io"

    name   = meta.get("name", "Unknown")
    symbol = meta.get("symbol", "???")

    def fmt_mc(mc):
        if mc >= 1_000_000: return f"${mc/1_000_000:.2f}M"
        if mc >= 1_000:     return f"${mc/1_000:.1f}K"
        return f"${mc:.0f}"

    deployer       = dev.get("deployer", "N/A")
    total_launches = dev.get("token_count", 0)
    biggest_mc     = dev.get("biggest_mc", 0)
    biggest_name   = dev.get("biggest_name", "")
    dev_risk       = dev.get("risk", "🆕 New deployer")

    is_bullish = biggest_mc >= BULLISH_DEV_MC

    if is_bullish:
        dev_badge = f"🔥 *BULLISH DEV* — Best launch: {biggest_name} @ {fmt_mc(biggest_mc)}"
    elif total_launches > 3:
        dev_badge = f"⚠️ Serial deployer — {total_launches} prev contracts"
    elif total_launches > 0:
        dev_badge = f"📊 {total_launches} prev deployment(s) — Best: {fmt_mc(biggest_mc)}"
    else:
        dev_badge = "🆕 First deployment"

    lines = [
        f"{chain_emoji} *NEW {chain_name.upper()} TOKEN*",
        f"",
        f"*{name}* (${symbol})",
        f"`{ca}`",
        f"",
        f"👨‍💻 *Dev:* `{str(deployer)[:10]}...{str(deployer)[-4:] if deployer and len(str(deployer)) > 10 else ''}`",
        f"{dev_badge}",
        f"",
        f"⚡ Detected in `{elapsed_ms}ms`",
        f"[View TX](https://{explorer}/tx/{tx_hash})",
    ]

    text = "\n".join(lines)

    dex_url = (
        f"https://app.uniswap.org/#/swap?outputCurrency={ca}&chain=base"
        if chain == "base"
        else f"https://app.uniswap.org/#/swap?outputCurrency={ca}"
    )

    buttons = [
        [
            InlineKeyboardButton("🛒 Buy on Uniswap", url=dex_url),
            InlineKeyboardButton(f"🔍 {chain_name}scan", url=f"https://{explorer}/token/{ca}"),
        ],
        [
            InlineKeyboardButton("📊 Full Scan", callback_data=f"scan:{ca}"),
            InlineKeyboardButton("🕸 Cluster Scan", callback_data=f"cluster:{ca}"),
        ],
    ]

    markup = InlineKeyboardMarkup(buttons)
    recipients = _evm_monitor_users | ({ADMIN_ID} if ADMIN_ID else set())

    for chat_id in recipients:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(f"[EVM_MON] send error to {chat_id}: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# USER MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def add_evm_monitor_user(user_id: int):    _evm_monitor_users.add(user_id)
def remove_evm_monitor_user(user_id: int): _evm_monitor_users.discard(user_id)
def is_evm_monitoring(user_id: int) -> bool: return user_id in _evm_monitor_users

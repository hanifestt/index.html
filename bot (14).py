import os
import asyncio
import json
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from scanner import scan_token, get_dev_alpha
from smartmoney import find_smart_money, format_smart_money_report
from clusters import find_wallet_clusters, format_cluster_report
from monitor import run_monitor, add_monitor_user, remove_monitor_user, is_monitoring, get_monitor_count
from evm_scanner import scan_evm_token
from evm_monitor import run_evm_monitor, add_evm_monitor_user, remove_evm_monitor_user, is_evm_monitoring
from chain_detector import detect_chain, chain_emoji, chain_name, get_explorer_url, get_dex_url, is_evm_address
from mirror import (add_mirror_wallet, remove_mirror_wallet, set_budget, get_budget,
    get_user_wallets, run_mirror_monitor, format_mirror_list, format_mirror_alert)
from sniper import (enable_sniper, disable_sniper, get_filters, update_filter,
    is_sniping, evaluate_and_alert, format_sniper_alert)
from watchlist import add_to_watchlist, remove_from_watchlist, get_watchlist, check_watchlist_alerts
from invites import generate_invite, use_invite, is_authorized, authorize_user, list_invites

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
BOT_USERNAME = os.environ.get("BOT_USERNAME", "chainsentinel_bot").lstrip("@")


def require_auth(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id == ADMIN_ID or is_authorized(user_id):
            return await func(update, context)
        else:
            await update.message.reply_text(
                "🔒 *Access Denied*\n\nChain Sentinel is invite-only.\nYou need a valid invite link to access this bot.",
                parse_mode="Markdown"
            )
    return wrapper


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "trader"

    if context.args:
        token = context.args[0]
        if is_authorized(user_id) or user_id == ADMIN_ID:
            await send_welcome(update, user_name)
            return
        success = use_invite(token, user_id)
        if success:
            await update.message.reply_text(
                f"✅ *Invite accepted!* Welcome, {user_name}.\n\nYou now have full access to Chain Sentinel.",
                parse_mode="Markdown"
            )
            await send_welcome(update, user_name)
        else:
            await update.message.reply_text(
                "❌ *Invalid or already used invite link.*\n\nThis link has already been redeemed.\nContact the admin for a new invite.",
                parse_mode="Markdown"
            )
        return

    if is_authorized(user_id) or user_id == ADMIN_ID:
        await send_welcome(update, user_name)
    else:
        await update.message.reply_text(
            "🔒 *Chain Sentinel is invite-only.*\n\nYou need a valid invite link to access this bot.",
            parse_mode="Markdown"
        )


async def send_welcome(update: Update, name: str):
    text = (
        f"👁 *CHAIN SENTINEL* — Welcome, {name}\n\n"
        "Send me any Solana contract address and I'll scan it for:\n"
        "• Wallet clustering & snipers\n"
        "• LP lock status\n"
        "• Supply concentration\n"
        "• MEV bot exposure\n\n"
        "*Commands:*\n"
        "/scan `<CA>` — Full risk scan\n"
        "/dev `<CA>` — Dev history & alpha\n"
        "/smartmoney `<CA1> <CA2>` — Find smart money wallets\n"
        "/cluster `<CA>` — Detect cabal wallet clusters\n"
        "/monitor — Live pump.fun launch alerts\n"
        "/mirror — Whale mirror trading\n"
        "/unmonitor — Stop live alerts\n"
        "/watch `<CA>` — Add to watchlist\n"
        "/unwatch `<CA>` — Remove from watchlist\n"
        "/watchlist — View your watchlist\n\n"
        "_Paste a contract address to get started ↓_"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def genlink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    count = 1
    if context.args and context.args[0].isdigit():
        count = min(int(context.args[0]), 20)
    lines = [f"🔑 *Generated {count} invite link(s):*\n"]
    for _ in range(count):
        token = generate_invite()
        link = f"https://t.me/{BOT_USERNAME}?start={token}"
        lines.append(f"`{link}`")
    lines.append("\n_Each link can only be used once._")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def invites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    invites = list_invites()
    if not invites:
        await update.message.reply_text("No invites yet. Use /genlink to create one.")
        return
    used = sum(1 for v in invites.values() if v["used"])
    unused = len(invites) - used
    lines = [f"📋 *Invite Summary:*", f"Total: {len(invites)} | Used: {used} | Available: {unused}\n"]
    for token, meta in list(invites.items())[-10:]:
        status = "✅ Used" if meta["used"] else "⏳ Available"
        lines.append(f"`...{token[-8:]}` — {status}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def adduser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /adduser <telegram_user_id>")
        return
    authorize_user(int(context.args[0]))
    await update.message.reply_text(f"✅ User `{context.args[0]}` authorized.", parse_mode="Markdown")


@require_auth
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_welcome(update, update.effective_user.first_name or "trader")


@require_auth
async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /scan <contract_address>")
        return
    await run_scan(update, context, context.args[0].strip())


@require_auth
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    from chain_detector import is_evm_address, is_solana_address
    if is_evm_address(text) or (32 <= len(text) <= 44 and is_solana_address(text)):
        await run_scan(update, context, text)
    else:
        await update.message.reply_text("Send me a Solana, Base, or Ethereum contract address to scan, or use /help.")


async def run_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, ca: str):
    msg_obj = update.message if update.message else update.callback_query.message
    msg = await msg_obj.reply_text(
        f"🔍 Detecting chain for `{ca[:8]}...{ca[-4:]}`...",
        parse_mode="Markdown"
    )
    try:
        chain = await detect_chain(ca)
        cemoji = chain_emoji(chain)
        cname  = chain_name(chain)

        await msg.edit_text(
            f"{cemoji} Scanning `{ca[:8]}...{ca[-4:]}` on *{cname}*\nFetching token data...",
            parse_mode="Markdown"
        )

        if chain == "solana":
            result  = await scan_token(ca)
            text    = format_report(ca, result)
            buttons = [[
                InlineKeyboardButton("👁 Watch Token", callback_data=f"watch:{ca}"),
                InlineKeyboardButton("🔗 Pump.fun", url=get_dex_url(ca, chain))
            ]]
        elif chain in ("base", "ethereum"):
            result  = await scan_evm_token(ca, chain)
            text    = format_evm_report(ca, chain, result)
            buttons = [[
                InlineKeyboardButton("👁 Watch Token", callback_data=f"watch:{ca}"),
                InlineKeyboardButton("🛒 Buy", url=get_dex_url(ca, chain)),
            ],[
                InlineKeyboardButton(f"{cemoji} Explorer", url=get_explorer_url(ca, chain)),
                InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/{chain}/{ca}"),
            ]]
        else:
            await msg.edit_text(
                "❌ Unknown chain. Send a valid Solana, Base, or Ethereum contract address.",
                parse_mode="Markdown"
            )
            return

        await msg.edit_text(text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons), disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Scan error: {e}", exc_info=True)
        await msg.edit_text(f"❌ Scan failed for `{ca[:8]}...`\nCheck the CA and try again.", parse_mode="Markdown")


def format_evm_report(ca: str, chain: str, r: dict) -> str:
    score  = r.get("risk_score", 0)
    cemoji = chain_emoji(chain)
    cname  = chain_name(chain)

    if score <= 30:   verdict = "🟢 LOW RISK"
    elif score <= 60: verdict = "🟡 MEDIUM RISK"
    elif score <= 80: verdict = "🟠 HIGH RISK"
    else:             verdict = "🔴 CRITICAL RISK"

    def fmt_mc(mc):
        try:
            mc = float(mc)
            if mc >= 1_000_000: return f"${mc/1_000_000:.2f}M"
            elif mc >= 1_000:   return f"${mc/1_000:.1f}K"
            else:               return f"${mc:.0f}"
        except: return "N/A"

    dev         = r.get("dev", {})
    deployer    = dev.get("deployer", "N/A") or "N/A"
    dev_short   = f"`{deployer[:8]}...{deployer[-4:]}`" if len(deployer) > 12 else f"`{deployer}`"
    dev_risk    = dev.get("risk", "N/A")
    token_count = dev.get("token_count", 0)
    dead_count  = dev.get("dead_count", 0)
    biggest_mc  = dev.get("biggest_mc", 0)
    token_lines = dev.get("token_lines", [])
    dev_summary = dev.get("summary", "No dev history found.")
    exp_url     = get_explorer_url(deployer, chain)

    name   = r.get("token_name", "Unknown")
    symbol = r.get("token_symbol", "???")
    header = f"*{name}* (${symbol})" if name != "Unknown" else f"`{ca}`"

    lines = [
        f"{cemoji} *CHAIN SENTINEL REPORT* — {cname}",
        f"{header}",
        f"`{ca}`", f"",
        f"*Risk Score: {score}/100 — {verdict}*", f"",
        f"━━━ 💼 WALLET ACTIVITY ━━━",
        f"• Unique wallets (100 txs): `{r.get('wallet_count', 'N/A')}`",
        f"• Fresh wallets (<24h): `{r.get('fresh_wallet_pct', 'N/A')}%`",
        f"• Cluster activity: `{r.get('cluster_pct', 'N/A')}%`",
        f"• Holder risk: `{r.get('holder_risk', 'N/A')}`", f"",
        f"━━━ 💧 LIQUIDITY ━━━",
        f"• Status: `{r.get('lp_locked', 'N/A')}`",
        f"• Market Cap: `{fmt_mc(r.get('market_cap', 0))}`",
        f"• Volume 24h: `{fmt_mc(r.get('volume_24h', 0))}`",
        f"• LP risk: `{r.get('lp_risk', 'N/A')}`", f"",
        f"━━━ 📊 SUPPLY CONCENTRATION ━━━",
        f"• Holder count: `{r.get('holder_count', 'N/A')}`",
        f"• Top holder: `{r.get('top1_pct', 'N/A')}%`",
        f"• Top 10 holders: `{r.get('top10_pct', 'N/A')}%`",
        f"• Gini: `{r.get('gini', 'N/A')}`",
        f"• Supply risk: `{r.get('supply_risk', 'N/A')}`", f"",
        f"━━━ 👨‍💻 DEV HISTORY ━━━",
        f"• Deployer: {dev_short}",
        f"  [View on Explorer]({exp_url})",
        f"• Deployments (60d): `{token_count}` ({dead_count} dead)",
        f"• Biggest MC: `{fmt_mc(biggest_mc)}`",
        f"• Dev rating: {dev_risk}",
    ]

    if token_lines:
        lines.append("• Top tokens:")
        for tl in token_lines[:3]:
            lines.append(f"  {tl}")

    lines += [
        f"", f"━━━ 🤖 ANALYSIS ━━━",
        f"{r.get('ai_summary', '')}",
        f"_{dev_summary}_",
        f"", f"_Powered by Chain Sentinel • $CS_"
    ]
    return "\n".join(lines)



def format_report(ca: str, r: dict) -> str:
    score = r.get("risk_score", 0)
    if score <= 30:   verdict = "🟢 LOW RISK"
    elif score <= 60: verdict = "🟡 MEDIUM RISK"
    elif score <= 80: verdict = "🟠 HIGH RISK"
    else:             verdict = "🔴 CRITICAL RISK"

    def fmt_mc(mc):
        try:
            mc = float(mc)
            if mc >= 1_000_000: return f"${mc/1_000_000:.2f}M"
            elif mc >= 1_000:   return f"${mc/1_000:.1f}K"
            else:               return f"${mc:.0f}"
        except: return "N/A"

    # Build dev section
    dev = r.get("dev", {})
    deployer = dev.get("deployer", "N/A")
    dev_short = f"`{deployer[:8]}...{deployer[-4:]}`" if deployer and deployer != "N/A" else "`N/A`"
    dev_risk = dev.get("risk", "N/A")
    dev_note = dev.get("risk_note", "")
    token_count = dev.get("token_count", 0)
    dead_count = dev.get("dead_count", 0)
    biggest_mc = dev.get("biggest_mc", 0)
    token_lines = dev.get("token_lines", [])
    dev_summary = dev.get("summary", "No dev history found.")

    name   = r.get("token_name", "Unknown")
    symbol = r.get("token_symbol", "???")
    header = f"*{name}* (${symbol})" if name != "Unknown" else f"`{ca}`"

    lines = [
        f"👁 *CHAIN SENTINEL REPORT*",
        f"{header}",
        f"`{ca}`", f"",
        f"*Risk Score: {score}/100 — {verdict}*", f"",
        f"━━━ 💼 WALLET ANALYSIS ━━━",
        f"• Unique wallets: `{r.get('wallet_count', 'N/A')}`",
        f"• Clustered/coordinated: `{r.get('cluster_pct', 'N/A')}%`",
        f"• Fresh wallets (<24h): `{r.get('fresh_wallet_pct', 'N/A')}%`",
        f"• Wallet risk: `{r.get('wallet_risk', 'N/A')}`", f"",
        f"━━━ 💧 LIQUIDITY (LP) ━━━",
        f"• LP status: `{r.get('lp_locked', 'Unknown')}`",
        f"• LP risk: `{r.get('lp_risk', 'N/A')}`", f"",
        f"━━━ 📊 SUPPLY CONCENTRATION ━━━",
        f"• Holder count: `{r.get('holder_count', 'N/A')}`",
        f"• Top holder: `{r.get('top1_pct', 'N/A')}%`",
        f"• Top 10 holders: `{r.get('top10_pct', 'N/A')}%`",
        f"• Gini coefficient: `{r.get('gini', 'N/A')}`",
        f"• Supply risk: `{r.get('supply_risk', 'N/A')}`", f"",
        f"━━━ ⚡ MEV EXPOSURE ━━━",
        f"• Suspected bot wallets: `{r.get('mev_bots', 'N/A')}`",
        f"• Sandwich patterns: `{r.get('sandwich_count', 'N/A')}`",
        f"• MEV risk: `{r.get('mev_risk', 'N/A')}`", f"",
        f"━━━ 👨‍💻 DEV HISTORY ━━━",
        f"• Deployer: {dev_short}",
        f"• Launches (60d): `{token_count}` ({dead_count} dead)",
        f"• Biggest MC: `{fmt_mc(biggest_mc)}`",
        f"• Dev rating: {dev_risk}",
    ]

    if token_lines:
        lines.append(f"• Top tokens:")
        for tl in token_lines[:3]:
            lines.append(f"  {tl}")

    lines += [
        f"", f"━━━ 🤖 ANALYSIS ━━━",
        f"{r.get('ai_summary', '')}",
        f"_{dev_summary}_",
        f"", f"_Powered by Chain Sentinel • $CS_"
    ]

    return "\n".join(lines)


@require_auth
async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /watch <contract_address>")
        return
    ca = context.args[0].strip()
    add_to_watchlist(str(update.effective_user.id), ca)
    await update.message.reply_text(f"✅ Added `{ca[:8]}...{ca[-4:]}` to your watchlist.", parse_mode="Markdown")


@require_auth
async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unwatch <contract_address>")
        return
    removed = remove_from_watchlist(str(update.effective_user.id), context.args[0].strip())
    if removed:
        await update.message.reply_text(f"🗑 Removed from watchlist.", parse_mode="Markdown")
    else:
        await update.message.reply_text("That address wasn't in your watchlist.")


@require_auth
async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wl = get_watchlist(str(update.effective_user.id))
    if not wl:
        await update.message.reply_text("Your watchlist is empty. Use /watch <CA> to add tokens.")
        return
    lines = ["👁 *Your Watchlist:*\n"] + [f"{i}. `{ca}`" for i, ca in enumerate(wl, 1)]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("scan:"):
        ca = query.data.split("scan:")[1]
        await query.answer()
        await run_scan(query, context, ca)
        return
    if query.data.startswith("cluster:"):
        ca = query.data.split("cluster:")[1]
        await query.answer()
        from clusters import find_wallet_clusters, format_cluster_report
        result = await find_wallet_clusters(ca)
        report = format_cluster_report(result)
        await query.message.reply_text(report[:4000], parse_mode="Markdown", disable_web_page_preview=True)
        return
    if query.data.startswith("watch:"):
        ca = query.data.split("watch:")[1]
        add_to_watchlist(str(query.from_user.id), ca)
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Watching", callback_data="noop"),
            InlineKeyboardButton("🔗 Pump.fun", url=f"https://pump.fun/coin/{ca}")
        ]]))


async def watchlist_job(context: ContextTypes.DEFAULT_TYPE):
    alerts = await check_watchlist_alerts()
    for user_id, ca, message in alerts:
        try:
            await context.bot.send_message(chat_id=user_id, text=f"🚨 *WATCHLIST ALERT*\n\n`{ca}`\n\n{message}", parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Alert error: {e}")



@require_auth
async def dev_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /dev <contract_address>\n\nExample: /dev 8JnBeGkvs1XGLquaLcrZ9i4UCPjiDa2fSR1uv6k8pump"
        )
        return
    ca = context.args[0].strip()
    msg = await update.message.reply_text(
        f"🔎 Analysing dev history for `{ca[:8]}...{ca[-4:]}`\nThis takes ~15 seconds...",
        parse_mode="Markdown"
    )
    try:
        result = await get_dev_alpha(ca)

        if result.get("error"):
            await msg.edit_text(f"❌ {result['error']}", parse_mode="Markdown")
            return

        deployer = result.get("deployer", "Unknown")
        token_count = result.get("token_count", 0)
        risk = result.get("risk", "N/A")
        risk_note = result.get("risk_note", "")
        summary = result.get("summary", "")
        token_lines = result.get("token_lines", [])
        dead_count = result.get("dead_count", 0)
        biggest = result.get("biggest_launch")

        def fmt_mc(mc):
            if not mc: return "N/A"
            if mc >= 1_000_000: return f"${mc/1_000_000:.2f}M"
            elif mc >= 1_000: return f"${mc/1_000:.1f}K"
            else: return f"${mc:.0f}"

        lines = [
            f"👨‍💻 *DEV ALPHA REPORT*",
            f"",
            f"*Deployer:* `{deployer[:8]}...{deployer[-4:]}`",
            f"[View on Solscan](https://solscan.io/account/{deployer})",
            f"",
            f"━━━ 📊 LAUNCH HISTORY (60 days) ━━━",
            f"• Total launches: `{token_count}`",
            f"• Dead/untraded: `{dead_count}`",
            f"• Biggest MC: `{fmt_mc(result.get('biggest_mc', 0))}`",
            f"",
            f"━━━ ⚠️ DEV RISK ━━━",
            f"• Rating: {risk}",
            f"• Note: _{risk_note}_",
            f"",
        ]

        if token_lines:
            lines.append("━━━ 🪙 PREVIOUS TOKENS (top 5) ━━━")
            lines.extend(token_lines)
            lines.append("")

        lines.append("━━━ 🤖 SUMMARY ━━━")
        lines.append(summary)
        lines.append("")
        lines.append("_Powered by Chain Sentinel • $CS_")

        keyboard = [[
            InlineKeyboardButton("🔍 Full Scan", callback_data=f"scan:{ca}"),
            InlineKeyboardButton("👁 Solscan", url=f"https://solscan.io/account/{deployer}")
        ]]

        await msg.edit_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True
        )

    except Exception as e:
        logger.error(f"Dev alpha error: {e}")
        await msg.edit_text("❌ Dev analysis failed. Try again in a moment.", parse_mode="Markdown")


@require_auth
async def smartmoney_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "🧠 *Smart Money Finder*\n\n"
            "Find wallets holding multiple successful tokens with high win rates.\n\n"
            "Usage: `/smartmoney <CA1> <CA2> [CA3] [CA4] [CA5]`\n\n"
            "Example: paste 2-5 contract addresses separated by spaces.\n"
            "_Minimum 2 CAs required._",
            parse_mode="Markdown"
        )
        return

    mints = [a.strip() for a in context.args if len(a.strip()) >= 32]
    if len(mints) < 2:
        await update.message.reply_text("❌ Please provide at least 2 valid Solana contract addresses.")
        return

    msg = await update.message.reply_text(
        f"🧠 Scanning smart money across {len(mints)} tokens...\n"
        f"This takes 20-30 seconds...",
        parse_mode="Markdown"
    )

    try:
        result = await find_smart_money(mints, min_win_rate=1.0, min_pnl_sol=0.0)
        report = format_smart_money_report(result)

        # Auto-add qualified wallets to mirror tracker
        qualified = result.get("qualified_wallets", [])
        auto_added = []
        if qualified:
            # Detect chain from first mint
            first_mint  = mints[0]
            wallet_chain = await detect_chain(first_mint)
            if wallet_chain == "unknown":
                wallet_chain = "solana"  # default for pump.fun
            for w in qualified[:5]:
                wallet_addr = w.get("wallet", "")
                if wallet_addr:
                    pnl  = w.get("total_pnl_sol", 0)
                    wr   = w.get("win_rate", 0)
                    label = f"SmartMoney {wallet_addr[:6]} | {wr*100:.0f}% WR"
                    add_mirror_wallet(update.effective_user.id, wallet_addr, wallet_chain, label)
                    auto_added.append(wallet_addr)

        if auto_added:
            report += f"\n\n✅ *Auto-added {len(auto_added)} wallet(s) to Mirror tracker.*"
            if not get_budget(update.effective_user.id):
                report += "\n⚠️ Set your budget: `/mirror budget <amount>`"

        # Telegram message limit is 4096 chars
        if len(report) > 4000:
            report = report[:3900] + "\n\n_Report truncated — too many results._"

        await msg.edit_text(report, parse_mode="Markdown", disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Smart money error: {e}")
        await msg.edit_text("❌ Smart money scan failed. Try again with different tokens.", parse_mode="Markdown")


@require_auth
async def cluster_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "🕸 *Wallet Cluster Detector*\n\n"
            "Finds coordinated wallets funded by the same source (cabals).\n\n"
            "Usage: `/cluster <contract_address>`",
            parse_mode="Markdown"
        )
        return

    ca = context.args[0].strip()
    msg = await update.message.reply_text(
        f"🕸 Scanning wallet clusters for `{ca[:8]}...{ca[-4:]}`\n"
        f"Tracing funding sources — takes ~20 seconds...",
        parse_mode="Markdown"
    )

    try:
        chain_type = detect_chain(ca)
        if chain_type == "evm":
            async with __import__("aiohttp").ClientSession() as session:
                from evm_scanner import detect_evm_chain
                chain = await detect_evm_chain(session, ca)
            result = await get_evm_clusters(ca, chain)
            chain_name = "Base" if chain == "base" else "Ethereum"
            if result.get("error"):
                report = f"❌ {result['error']}"
            else:
                score = result.get("cabal_probability", 0)
                clusters = result.get("clusters", [])
                lines = [
                    f"🕸 *WALLET CLUSTER REPORT* — {chain_name}",
                    f"`{ca}`", f"",
                    f"*Cabal Probability: {score}/100*", f"",
                    f"• Holders scanned: `{result.get('total_holders_scanned', 0)}`",
                    f"• Clusters found: `{len(clusters)}`", f"",
                ]
                for i, c in enumerate(clusters[:5], 1):
                    lines += [
                        f"*Cluster #{i}* — Funder: `{c['funder'][:8]}...`",
                        f"Funded {c['count']} wallets", f"",
                    ]
                report = "\n".join(lines)
        else:
            result = await find_wallet_clusters(ca)
            report = format_cluster_report(result)

        if len(report) > 4000:
            report = report[:3900] + "\n\n_Report truncated._"
        await msg.edit_text(report, parse_mode="Markdown", disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Cluster error: {e}")
        await msg.edit_text("❌ Cluster scan failed. Try again.", parse_mode="Markdown")


@require_auth
async def monitor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_monitoring(user_id):
        await update.message.reply_text(
            "👁 You are already receiving live launch alerts.\n"
            "Use /unmonitor to stop.",
            parse_mode="Markdown"
        )
        return
    add_monitor_user(user_id)
    add_evm_monitor_user(user_id)
    await update.message.reply_text(
        "✅ *Live Monitor ON*\n\n"
        "You will now receive instant alerts for:\n"
        "• 🟣 Solana — pump.fun new launches\n"
        "• 🔵 Base — Uniswap new pairs\n"
        "• ⟠ Ethereum — Uniswap new pairs\n\n"
        "Filter: must have socials + optional bullish dev\n\n"
        "Use /unmonitor to stop alerts.",
        parse_mode="Markdown"
    )

@require_auth
async def unmonitor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    remove_monitor_user(user_id)
    remove_evm_monitor_user(user_id)
    await update.message.reply_text("🔕 Live monitor disabled for all chains. Use /monitor to re-enable.")

async def monitorstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    count = get_monitor_count()
    await update.message.reply_text(
        f"📡 *Monitor Status*\n\nActive listeners: `{count}`",
        parse_mode="Markdown"
    )


@require_auth
async def mirror_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args    = context.args or []

    # /mirror — show list
    if not args:
        await update.message.reply_text(
            format_mirror_list(user_id),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        return

    sub = args[0].lower()

    # /mirror budget <amount>
    if sub == "budget":
        if len(args) < 2:
            budget = get_budget(user_id)
            await update.message.reply_text(
                f"💰 Your current mirror budget: `${budget:,.2f}`\n\n"
                f"To change: `/mirror budget 500`",
                parse_mode="Markdown"
            )
            return
        try:
            amount = float(args[1].replace("$", "").replace(",", ""))
            set_budget(user_id, amount)
            await update.message.reply_text(
                f"✅ Mirror budget set to `${amount:,.2f}`\n\n"
                f"When a tracked whale buys, I\'ll calculate how much you should put in to match their conviction.",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid amount. Example: `/mirror budget 500`", parse_mode="Markdown")
        return

    # /mirror add <wallet> [label]
    if sub == "add":
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: `/mirror add <wallet_address> [label]`\n\n"
                "Or run `/smartmoney <CA1> <CA2>` to auto-find and add smart money wallets.",
                parse_mode="Markdown"
            )
            return
        wallet = args[1].strip()
        label  = " ".join(args[2:]) if len(args) > 2 else ""
        chain  = await detect_chain(wallet)
        if chain == "unknown":
            await update.message.reply_text("❌ Could not detect chain for this wallet address.", parse_mode="Markdown")
            return
        add_mirror_wallet(user_id, wallet, chain, label)
        cemoji = chain_emoji(chain)
        await update.message.reply_text(
            f"✅ Now mirroring {cemoji} `{wallet[:8]}...{wallet[-4:]}`\n"
            f"Chain: *{chain_name(chain)}*\n"
            f"Label: _{label or 'No label'}_\n\n"
            f"{'⚠️ Set your budget first: `/mirror budget <amount>`' if not get_budget(user_id) else f'Budget: `${get_budget(user_id):,.2f}`'}",
            parse_mode="Markdown"
        )
        return

    # /mirror remove <wallet>
    if sub in ("remove", "rm", "delete"):
        if len(args) < 2:
            await update.message.reply_text("Usage: `/mirror remove <wallet_address>`", parse_mode="Markdown")
            return
        wallet = args[1].strip()
        removed = remove_mirror_wallet(user_id, wallet)
        if removed:
            await update.message.reply_text(f"✅ Removed `{wallet[:8]}...{wallet[-4:]}` from mirror list.", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Wallet not found in your mirror list.", parse_mode="Markdown")
        return

    # /mirror list
    if sub == "list":
        await update.message.reply_text(format_mirror_list(user_id), parse_mode="Markdown", disable_web_page_preview=True)
        return

    await update.message.reply_text(
        "🪞 *Mirror Commands*\n\n"
        "`/mirror` — view tracked wallets\n"
        "`/mirror budget <amount>` — set your mirror budget\n"
        "`/mirror add <wallet>` — track a wallet\n"
        "`/mirror remove <wallet>` — stop tracking\n\n"
        "_Wallets are auto-added from `/smartmoney` results._",
        parse_mode="Markdown"
    )


@require_auth
async def snipe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args    = context.args or []

    if not args:
        # Show status + current filters
        active  = is_sniping(user_id)
        filters = get_filters(user_id)
        status  = "🟢 *ON*" if active else "🔴 *OFF*"

        def fmt_mc(mc):
            if mc >= 1_000_000: return f"${mc/1_000_000:.1f}M"
            if mc >= 1_000:     return f"${mc/1_000:.0f}K"
            return f"${mc:.0f}"

        chains_on = ", ".join(filters.get("chains", ["solana", "base"]))
        lines = [
            f"🎯 *Runner Sniper* — {status}",
            f"",
            f"*Active filters:*",
            f"• Min LP: `${filters.get('min_lp_usd', 1000):,.0f}`",
            f"• Max top-10 supply: `{filters.get('max_top10_pct', 60)}%`",
            f"• Min dev MC: `{fmt_mc(filters.get('min_dev_mc', 50000))}`",
            f"• Max cabal %: `{filters.get('max_cabal_pct', 30)}%`",
            f"• Require smart money: `{'Yes' if filters.get('require_smart_money') else 'No'}`",
            f"• Chains: `{chains_on}`",
            f"",
            f"*Commands:*",
            f"`/snipe on` — start sniping",
            f"`/snipe off` — stop sniping",
            f"`/snipe set minlp <amount>` — e.g. 5000",
            f"`/snipe set maxsupply <pct>` — e.g. 50",
            f"`/snipe set mindev <amount>` — e.g. 100000",
            f"`/snipe set maxcabal <pct>` — e.g. 20",
            f"`/snipe set smartmoney on/off`",
            f"`/snipe set chains sol/base/both`",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    sub = args[0].lower()

    if sub == "on":
        enable_sniper(user_id)
        filters = get_filters(user_id)
        chains  = ", ".join(filters.get("chains", ["solana", "base"]))
        await update.message.reply_text(
            f"🎯 *Runner Sniper ON*\n\n"
            f"Monitoring *{chains}* for new launches.\n"
            f"Alerts fire within 5-10 seconds when a token passes:\n"
            f"• LP ≥ `${filters.get('min_lp_usd',1000):,.0f}`\n"
            f"• Top 10 supply ≤ `{filters.get('max_top10_pct',60)}%`\n"
            f"• Dev prev best ≥ `${filters.get('min_dev_mc',50000):,.0f}` MC\n"
            f"• Cabal % ≤ `{filters.get('max_cabal_pct',30)}%`\n\n"
            f"Use `/snipe off` to stop.",
            parse_mode="Markdown"
        )
        return

    if sub == "off":
        disable_sniper(user_id)
        await update.message.reply_text("🔴 Runner Sniper OFF.")
        return

    if sub == "set" and len(args) >= 3:
        key   = args[1].lower()
        value = args[2].lower()

        mapping = {
            "minlp":       ("min_lp_usd",        float),
            "maxsupply":   ("max_top10_pct",      float),
            "mindev":      ("min_dev_mc",         float),
            "maxcabal":    ("max_cabal_pct",      float),
        }

        if key in mapping:
            db_key, typ = mapping[key]
            try:
                val = typ(value.replace("$","").replace("%","").replace(",",""))
                update_filter(user_id, db_key, val)
                await update.message.reply_text(f"✅ `{key}` set to `{val}`", parse_mode="Markdown")
            except ValueError:
                await update.message.reply_text(f"❌ Invalid value for `{key}`", parse_mode="Markdown")

        elif key == "smartmoney":
            val = value in ("on", "true", "yes", "1")
            update_filter(user_id, "require_smart_money", val)
            await update.message.reply_text(f"✅ Smart money requirement: `{'ON' if val else 'OFF'}`", parse_mode="Markdown")

        elif key == "chains":
            if value == "sol":
                chains = ["solana"]
            elif value == "base":
                chains = ["base"]
            elif value == "eth":
                chains = ["ethereum"]
            elif value in ("both", "all"):
                chains = ["solana", "base", "ethereum"]
            else:
                chains = ["solana", "base"]
            update_filter(user_id, "chains", chains)
            await update.message.reply_text(f"✅ Monitoring: `{', '.join(chains)}`", parse_mode="Markdown")
        else:
            await update.message.reply_text(
                "Unknown setting. Options: `minlp`, `maxsupply`, `mindev`, `maxcabal`, `smartmoney`, `chains`",
                parse_mode="Markdown"
            )
        return

    await update.message.reply_text(
        "Usage: `/snipe on` | `/snipe off` | `/snipe set <key> <value>`",
        parse_mode="Markdown"
    )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("watchlist", watchlist_cmd))
    app.add_handler(CommandHandler("dev", dev_cmd))
    app.add_handler(CommandHandler("smartmoney", smartmoney_cmd))
    app.add_handler(CommandHandler("cluster", cluster_cmd))
    app.add_handler(CommandHandler("monitor", monitor_cmd))
    app.add_handler(CommandHandler("mirror", mirror_cmd))
    app.add_handler(CommandHandler("snipe", snipe_cmd))
    app.add_handler(CommandHandler("unmonitor", unmonitor_cmd))
    app.add_handler(CommandHandler("monitorstatus", monitorstatus_cmd))
    app.add_handler(CommandHandler("genlink", genlink_cmd))
    app.add_handler(CommandHandler("invites", invites_cmd))
    app.add_handler(CommandHandler("adduser", adduser_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.job_queue.run_repeating(watchlist_job, interval=1800, first=60)
    logger.info("Chain Sentinel bot is running...")

    # Start pump.fun monitor as background task
    async def post_init(application):
        bot = application.bot
        loop = asyncio.get_event_loop()
        loop.create_task(run_monitor(bot))
        loop.create_task(run_evm_monitor(bot))
        loop.create_task(run_mirror_monitor(bot))
        logger.info("[MONITOR] Solana + ETH + Base monitors + Mirror started.")

    app.post_init = post_init
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()

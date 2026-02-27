import os
import asyncio
import json
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from scanner import scan_token
from watchlist import add_to_watchlist, remove_from_watchlist, get_watchlist, check_watchlist_alerts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# ── /start ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👁 *CHAIN SENTINEL* — On-Chain Risk Intelligence\n\n"
        "Send me any Solana contract address and I'll scan it for:\n"
        "• Wallet clustering & snipers\n"
        "• LP lock status\n"
        "• Supply concentration\n"
        "• MEV bot exposure\n\n"
        "*Commands:*\n"
        "/scan `<CA>` — Scan a token\n"
        "/watch `<CA>` — Add to watchlist\n"
        "/unwatch `<CA>` — Remove from watchlist\n"
        "/watchlist — View your watchlist\n"
        "/help — Show this message\n\n"
        "_Paste a contract address to get started ↓_"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ── /help ────────────────────────────────────────────────────────────────────
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# ── /scan <CA> ───────────────────────────────────────────────────────────────
async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /scan <contract_address>")
        return
    ca = context.args[0].strip()
    await run_scan(update, context, ca)


# ── Raw message — treat as CA ────────────────────────────────────────────────
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    # Basic Solana address check (32-44 base58 chars)
    if 32 <= len(text) <= 44 and text.isalnum():
        await run_scan(update, context, text)
    else:
        await update.message.reply_text(
            "Send me a Solana contract address to scan, or use /help to see commands."
        )


# ── Core scan flow ────────────────────────────────────────────────────────────
async def run_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, ca: str):
    msg = await update.message.reply_text(
        f"🔍 Scanning `{ca[:8]}...{ca[-4:]}`\nThis takes ~10 seconds...",
        parse_mode="Markdown"
    )
    try:
        result = await scan_token(ca)
        text = format_report(ca, result)
        keyboard = [
            [
                InlineKeyboardButton("👁 Watch Token", callback_data=f"watch:{ca}"),
                InlineKeyboardButton("🔗 View on Pump.fun", url=f"https://pump.fun/coin/{ca}")
            ]
        ]
        await msg.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Scan error: {e}")
        await msg.edit_text(
            f"❌ Scan failed for `{ca[:8]}...`\n\nMake sure it's a valid Solana CA.\nError: {str(e)[:100]}",
            parse_mode="Markdown"
        )


# ── Format the scan report ────────────────────────────────────────────────────
def format_report(ca: str, r: dict) -> str:
    score = r.get("risk_score", 0)
    if score <= 30:
        verdict = "🟢 LOW RISK"
    elif score <= 60:
        verdict = "🟡 MEDIUM RISK"
    elif score <= 80:
        verdict = "🟠 HIGH RISK"
    else:
        verdict = "🔴 CRITICAL RISK"

    lines = [
        f"👁 *CHAIN SENTINEL REPORT*",
        f"`{ca}`",
        f"",
        f"*Overall Risk Score: {score}/100 — {verdict}*",
        f"",
        f"━━━ 💼 WALLET ANALYSIS ━━━",
        f"• Unique wallets: `{r.get('wallet_count', 'N/A')}`",
        f"• Clustered/coordinated: `{r.get('cluster_pct', 'N/A')}%`",
        f"• Fresh wallets (<24h): `{r.get('fresh_wallet_pct', 'N/A')}%`",
        f"• Wallet risk: `{r.get('wallet_risk', 'N/A')}`",
        f"",
        f"━━━ 💧 LIQUIDITY (LP) ━━━",
        f"• LP locked: `{r.get('lp_locked', 'Unknown')}`",
        f"• Lock duration: `{r.get('lp_lock_duration', 'N/A')}`",
        f"• LP risk: `{r.get('lp_risk', 'N/A')}`",
        f"",
        f"━━━ 📊 SUPPLY CONCENTRATION ━━━",
        f"• Top 10 holders: `{r.get('top10_pct', 'N/A')}%`",
        f"• Gini coefficient: `{r.get('gini', 'N/A')}`",
        f"• Supply risk: `{r.get('supply_risk', 'N/A')}`",
        f"",
        f"━━━ ⚡ MEV EXPOSURE ━━━",
        f"• Active MEV bots: `{r.get('mev_bots', 'N/A')}`",
        f"• Sandwich attacks (72h): `{r.get('sandwich_count', 'N/A')}`",
        f"• MEV risk: `{r.get('mev_risk', 'N/A')}`",
        f"",
        f"━━━ 🤖 AI ANALYSIS ━━━",
        f"{r.get('ai_summary', 'No summary available.')}",
        f"",
        f"_Powered by Chain Sentinel • $CS_"
    ]
    return "\n".join(lines)


# ── /watch <CA> ───────────────────────────────────────────────────────────────
async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /watch <contract_address>")
        return
    ca = context.args[0].strip()
    user_id = str(update.effective_user.id)
    add_to_watchlist(user_id, ca)
    await update.message.reply_text(
        f"✅ Added `{ca[:8]}...{ca[-4:]}` to your watchlist.\n"
        f"You'll get alerts if the risk score changes significantly.",
        parse_mode="Markdown"
    )


# ── /unwatch <CA> ─────────────────────────────────────────────────────────────
async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unwatch <contract_address>")
        return
    ca = context.args[0].strip()
    user_id = str(update.effective_user.id)
    removed = remove_from_watchlist(user_id, ca)
    if removed:
        await update.message.reply_text(f"🗑 Removed `{ca[:8]}...` from watchlist.", parse_mode="Markdown")
    else:
        await update.message.reply_text("That address wasn't in your watchlist.")


# ── /watchlist ────────────────────────────────────────────────────────────────
async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    wl = get_watchlist(user_id)
    if not wl:
        await update.message.reply_text("Your watchlist is empty. Use /watch <CA> to add tokens.")
        return
    lines = ["👁 *Your Watchlist:*\n"]
    for i, ca in enumerate(wl, 1):
        lines.append(f"{i}. `{ca}`")
    lines.append("\nUse /scan <CA> to re-scan any token.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Inline button callbacks ───────────────────────────────────────────────────
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("watch:"):
        ca = data.split("watch:")[1]
        user_id = str(query.from_user.id)
        add_to_watchlist(user_id, ca)
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Watching", callback_data="noop"),
                InlineKeyboardButton("🔗 View on Pump.fun", url=f"https://pump.fun/coin/{ca}")
            ]])
        )


# ── Background watchlist alert job ───────────────────────────────────────────
async def watchlist_job(context: ContextTypes.DEFAULT_TYPE):
    alerts = await check_watchlist_alerts()
    for user_id, ca, message in alerts:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🚨 *WATCHLIST ALERT*\n\n`{ca}`\n\n{message}",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Alert send error: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("watchlist", watchlist_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # Run watchlist check every 30 minutes
    app.job_queue.run_repeating(watchlist_job, interval=1800, first=60)

    logger.info("Chain Sentinel bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()

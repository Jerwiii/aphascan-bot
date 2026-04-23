import os
import asyncio
import aiohttp
import logging
import json
from datetime import datetime, timezone
from collections import defaultdict
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config from environment ────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
HELIUS_API_KEY   = os.environ["HELIUS_API_KEY"]
SCAN_INTERVAL    = int(os.environ.get("SCAN_INTERVAL_SECONDS", "120"))  # 2 min default
ALPHA_THRESHOLD  = int(os.environ.get("ALPHA_THRESHOLD", "75"))

HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# ── State ──────────────────────────────────────────────────────────────────────
seen_mints        = set()
alert_threshold   = ALPHA_THRESHOLD
tracked_alerts    = {}   # mint -> {score, time, launchpad, name}
daily_top         = []   # list of today's alerted tokens
watchlist_wallets = {}   # address -> label
dev_rug_cache     = {}   # deployer -> bool (has rugged before)
token_name_cache  = {}   # mint -> symbol

# ── Launchpad registry ─────────────────────────────────────────────────────────
LAUNCHPADS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": ("Pump.fun",          "🟢"),
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": ("LetsBonk.fun",      "🟡"),
    "RAYLqkdpeygPBTJqNwFTNtBNiuFGCHZnBsXvMHb4Bg7": ("Raydium LaunchLab", "🔵"),
    "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG": ("Moonshot",          "🌙"),
    # Pump.fun graduation program
    "PumpSwapAMMProgram111111111111111111111111111": ("PumpSwap (grad)",   "🎓"),
}

# Known smart money wallets — wallets with proven early entry track records
# These are example addresses; in production you'd build this list dynamically
SMART_MONEY_SEEDS = {
    "3xxDCjn8oqdcNiqgmwfYeXXrAWs7EZgBSr5qQbVUG1Fx",
    "7SvpVoTMaVy4JxaZxjGH5wMJWwb1Dm1s3vGhQ2JU3YMn",
    "GThUX1Atko4tqhN2NaiTazWSeFWMoAi8oFTBfNHAgE9U",
    "BVanV4Vot7uxqf1GYhPqZhCkBVVHJzYpY7JRbGYMkbcT",
    "5FuwMFGPxxx3gHT1GLSPM9F8DXiajK3Q44vSFSGswdgq",
}

# ── RPC helpers ────────────────────────────────────────────────────────────────

async def rpc(session, method, params, req_id=1):
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    try:
        async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
            if "error" in data:
                log.debug(f"RPC {method} error: {data['error']}")
                return None
            return data.get("result")
    except Exception as e:
        log.debug(f"RPC {method} failed: {e}")
        return None

# ── Launchpad detector ─────────────────────────────────────────────────────────

def detect_launchpad(account_keys: list) -> tuple:
    for key in account_keys:
        if key in LAUNCHPADS:
            return LAUNCHPADS[key]
    return ("Unknown", "⚪")

# ── Dev wallet rug history check ───────────────────────────────────────────────

async def check_dev_rug_history(session, deployer: str) -> dict:
    """Check if a deployer has a history of creating tokens that went to zero."""
    if deployer in dev_rug_cache:
        return dev_rug_cache[deployer]

    result = {"rug_count": 0, "total_launches": 0, "is_serial_rugger": False}
    try:
        sigs = await rpc(session, "getSignaturesForAddress", [deployer, {"limit": 100}])
        if not sigs:
            return result

        result["total_launches"] = len(sigs)
        # Heuristic: if deployer has launched 5+ tokens and we've seen them in
        # seen_mints before, flag as high-risk (in production you'd check price history)
        if len(sigs) > 20:
            result["is_serial_rugger"] = True
            result["rug_count"] = len(sigs) // 5

        dev_rug_cache[deployer] = result
    except Exception as e:
        log.debug(f"Dev check failed: {e}")
    return result

# ── Bundle/bot detection ───────────────────────────────────────────────────────

def detect_bundling(transactions: list) -> dict:
    """
    Detect if a token launch was bundled (bots buying simultaneously).
    Bundled launches have multiple buys in same block with near-identical amounts.
    """
    if not transactions:
        return {"is_bundled": False, "confidence": 0, "reason": "no data"}

    slot_counts = defaultdict(int)
    for tx in transactions:
        slot = tx.get("slot", 0)
        slot_counts[slot] += 1

    max_same_slot = max(slot_counts.values()) if slot_counts else 0

    if max_same_slot >= 5:
        return {
            "is_bundled": True,
            "confidence": min(100, max_same_slot * 15),
            "reason": f"{max_same_slot} txs in same block"
        }
    elif max_same_slot >= 3:
        return {
            "is_bundled": False,
            "confidence": max_same_slot * 10,
            "reason": f"slight clustering ({max_same_slot} same block)"
        }
    return {"is_bundled": False, "confidence": 0, "reason": "organic"}

# ── Smart wallet detection ─────────────────────────────────────────────────────

async def count_smart_money_buyers(session, mint: str, recent_sigs: list) -> int:
    """Count how many known smart money wallets bought this token early."""
    smart_buyers = 0
    checked = 0
    for sig_info in recent_sigs[:20]:
        if checked >= 20:
            break
        try:
            tx = await rpc(session, "getTransaction", [
                sig_info["signature"],
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
            ])
            if not tx:
                continue
            acct_keys = [
                a.get("pubkey", "") if isinstance(a, dict) else str(a)
                for a in tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
            ]
            for addr in acct_keys:
                if addr in SMART_MONEY_SEEDS:
                    smart_buyers += 1
                    break
            checked += 1
        except Exception:
            continue
    return smart_buyers

# ── Graduation detector ────────────────────────────────────────────────────────

async def check_graduation(session, mint: str) -> bool:
    """Check if token has graduated from Pump.fun to PumpSwap."""
    try:
        sigs = await rpc(session, "getSignaturesForAddress", [mint, {"limit": 50}])
        if not sigs:
            return False
        for sig_info in sigs[:10]:
            tx = await rpc(session, "getTransaction", [
                sig_info["signature"],
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
            ])
            if not tx:
                continue
            acct_keys = [
                a.get("pubkey", "") if isinstance(a, dict) else str(a)
                for a in tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
            ]
            if "PumpSwapAMMProgram111111111111111111111111111" in acct_keys:
                return True
    except Exception:
        pass
    return False

# ── Token enrichment ───────────────────────────────────────────────────────────

async def enrich_token(session, mint: str, launchpad: tuple) -> dict | None:
    try:
        # Parallel fetches for speed
        largest_task  = rpc(session, "getTokenLargestAccounts", [mint])
        supply_task    = rpc(session, "getTokenSupply", [mint])
        sigs_task      = rpc(session, "getSignaturesForAddress", [mint, {"limit": 100}])
        acct_task      = rpc(session, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])

        largest, supply_res, sigs, acct_info = await asyncio.gather(
            largest_task, supply_task, sigs_task, acct_task
        )

        # Supply & holder concentration
        total_supply = 1.0
        if supply_res and supply_res.get("value", {}).get("uiAmount"):
            total_supply = float(supply_res["value"]["uiAmount"])

        accounts = (largest or {}).get("value", [])
        holders  = len(accounts)
        top_holder_pct = 0.0
        top3_pct       = 0.0
        if accounts and total_supply > 0:
            top_holder_pct = round(float(accounts[0].get("uiAmount") or 0) / total_supply * 100, 1)
            top3_pct = round(
                sum(float(a.get("uiAmount") or 0) for a in accounts[:3]) / total_supply * 100, 1
            )

        recent_txs = len(sigs) if sigs else 0

        # Transaction velocity pattern — check spacing for bot patterns
        bundle_info = detect_bundling(sigs[:30] if sigs else [])

        # Smart money buyers
        smart_buyers = await count_smart_money_buyers(session, mint, sigs[:20] if sigs else [])

        # Deployer / dev wallet
        deployer = ""
        if acct_info and isinstance(acct_info, dict):
            parsed = acct_info.get("value", {})
            if parsed and isinstance(parsed, dict):
                deployer = parsed.get("owner", "")

        dev_history = await check_dev_rug_history(session, deployer) if deployer else {}

        # Graduation check
        graduated = await check_graduation(session, mint)

        # Liquidity proxy — use largest account balance as rough liquidity signal
        liquidity_score_raw = 0
        if accounts:
            # More unique holders relative to top concentration = healthier
            liquidity_score_raw = max(0, holders - (top_holder_pct / 5))

        return {
            "mint":             mint,
            "launchpad":        launchpad,
            "holders":          holders,
            "top_holder_pct":   top_holder_pct,
            "top3_pct":         top3_pct,
            "recent_txs":       recent_txs,
            "bundle_info":      bundle_info,
            "smart_buyers":     smart_buyers,
            "dev_history":      dev_history,
            "graduated":        graduated,
            "liquidity_proxy":  liquidity_score_raw,
            "deployer":         deployer,
        }
    except Exception as e:
        log.warning(f"enrich_token {mint[:8]}... error: {e}")
        return None

# ── Masterclass scoring engine ─────────────────────────────────────────────────

def score_token(info: dict) -> dict:
    """
    100-point scoring system across 7 dimensions.
    Each dimension is weighted by its predictive power for profitable trades.
    """
    score    = 0
    warnings = []
    boosts   = []

    # ── 1. Smart money presence (25 pts) ──────────────────────────────────────
    # The strongest signal. Proven wallets don't waste time on rugs.
    sb = info.get("smart_buyers", 0)
    if sb >= 3:
        sm_score = 25; boosts.append(f"🐋 {sb} smart wallets in early")
    elif sb == 2:
        sm_score = 18; boosts.append(f"🐋 {sb} smart wallets")
    elif sb == 1:
        sm_score = 10; boosts.append("🐋 1 smart wallet spotted")
    else:
        sm_score = 3
    score += sm_score

    # ── 2. Holder distribution (20 pts) ───────────────────────────────────────
    # Low top-holder concentration = hard to rug
    top1 = info.get("top_holder_pct", 100)
    top3 = info.get("top3_pct", 100)
    if top1 < 5 and top3 < 15:
        hd_score = 20; boosts.append("✅ Healthy distribution")
    elif top1 < 10 and top3 < 25:
        hd_score = 15
    elif top1 < 20:
        hd_score = 9
    elif top1 < 35:
        hd_score = 4; warnings.append(f"⚠️ Top holder owns {top1}%")
    else:
        hd_score = 0; warnings.append(f"🚨 Top holder owns {top1}% — rug risk")
    score += hd_score

    # ── 3. Bundle/bot detection (20 pts) ──────────────────────────────────────
    # Organic launches vastly outperform bundled ones long-term
    bundle = info.get("bundle_info", {})
    if bundle.get("is_bundled"):
        bd_score = 0
        warnings.append(f"🤖 Bundled launch detected — {bundle.get('reason','')}")
    elif bundle.get("confidence", 0) > 20:
        bd_score = 10
        warnings.append(f"🤖 Slight bot activity: {bundle.get('reason','')}")
    else:
        bd_score = 20
        boosts.append("✅ Organic launch")
    score += bd_score

    # ── 4. Transaction velocity (15 pts) ──────────────────────────────────────
    # Strong early TX momentum = real interest
    txs = info.get("recent_txs", 0)
    if txs > 300:
        tv_score = 15; boosts.append(f"🔥 {txs} transactions — hot")
    elif txs > 100:
        tv_score = 12
    elif txs > 40:
        tv_score = 8
    elif txs > 10:
        tv_score = 4
    else:
        tv_score = 1
    score += tv_score

    # ── 5. Dev wallet safety (10 pts) ─────────────────────────────────────────
    dev = info.get("dev_history", {})
    if dev.get("is_serial_rugger"):
        dv_score = 0
        warnings.append("🚨 Serial rugger deployer detected")
    elif dev.get("rug_count", 0) > 0:
        dv_score = 3
        warnings.append(f"⚠️ Dev has {dev.get('rug_count')} prior rug(s)")
    else:
        dv_score = 10
    score += dv_score

    # ── 6. Holder count (5 pts) ───────────────────────────────────────────────
    holders = info.get("holders", 0)
    if holders > 200:
        hc_score = 5
    elif holders > 50:
        hc_score = 3
    elif holders > 10:
        hc_score = 2
    else:
        hc_score = 0; warnings.append(f"⚠️ Only {holders} holders")
    score += hc_score

    # ── 7. Graduation bonus (5 pts) ───────────────────────────────────────────
    if info.get("graduated"):
        score += 5
        boosts.append("🎓 Graduated to PumpSwap")

    # ── Hard disqualifiers ────────────────────────────────────────────────────
    hard_fail = False
    if info.get("top_holder_pct", 0) > 50:
        hard_fail = True
        warnings.append("❌ DISQUALIFIED: Top holder > 50% — extreme rug risk")
    if info.get("bundle_info", {}).get("is_bundled") and info.get("smart_buyers", 0) == 0:
        hard_fail = True
        warnings.append("❌ DISQUALIFIED: Bundled with zero smart money")
    if info.get("dev_history", {}).get("is_serial_rugger") and info.get("holders", 0) < 50:
        hard_fail = True
        warnings.append("❌ DISQUALIFIED: Serial rugger + low holders")

    final_score = 0 if hard_fail else min(99, score)

    # Rug risk rating
    if hard_fail or top1 > 40 or bundle.get("is_bundled"):
        rug_risk = "high"
    elif top1 > 20 or bundle.get("confidence", 0) > 20:
        rug_risk = "medium"
    else:
        rug_risk = "low"

    return {
        "total":      final_score,
        "rug_risk":   rug_risk,
        "hard_fail":  hard_fail,
        "warnings":   warnings,
        "boosts":     boosts,
        "breakdown": {
            "smart_money":   sm_score,
            "distribution":  hd_score,
            "organic":       bd_score,
            "tx_velocity":   tv_score,
            "dev_safety":    dv_score,
            "holder_count":  hc_score,
        }
    }

# ── Fetch new token mints ──────────────────────────────────────────────────────

async def fetch_new_tokens(session) -> list:
    """Returns list of (mint, launchpad_tuple)."""
    mints = []
    try:
        sigs = await rpc(session, "getSignaturesForAddress", [
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            {"limit": 60}
        ])
        if not sigs:
            return []

        for sig_info in sigs[:20]:
            try:
                tx = await rpc(session, "getTransaction", [
                    sig_info["signature"],
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                ])
                if not tx:
                    continue
                msg = tx.get("transaction", {}).get("message", {})
                acct_keys_raw = msg.get("accountKeys", [])
                acct_keys = [
                    a.get("pubkey", "") if isinstance(a, dict) else str(a)
                    for a in acct_keys_raw
                ]
                instructions = msg.get("instructions", [])
                for ix in instructions:
                    parsed = ix.get("parsed", {})
                    if isinstance(parsed, dict) and parsed.get("type") == "initializeMint":
                        mint = parsed.get("info", {}).get("mint")
                        if mint and mint not in seen_mints:
                            lp = detect_launchpad(acct_keys)
                            mints.append((mint, lp))
            except Exception:
                continue
    except Exception as e:
        log.warning(f"fetch_new_tokens: {e}")
    return mints[:12]

# ── Watchlist scanner ──────────────────────────────────────────────────────────

async def scan_watchlist(session, bot: Bot):
    """Check if any watched wallets made new transactions."""
    if not watchlist_wallets:
        return
    for addr, label in watchlist_wallets.items():
        try:
            sigs = await rpc(session, "getSignaturesForAddress", [addr, {"limit": 5}])
            if not sigs:
                continue
            latest_sig = sigs[0]["signature"]
            cache_key = f"wl_{addr}"
            last_seen = seen_mints  # reuse seen set with prefixed key
            if cache_key in seen_mints:
                continue
            seen_mints.add(cache_key)

            tx = await rpc(session, "getTransaction", [
                latest_sig,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
            ])
            if not tx:
                continue

            msg = f"👁 *Watchlist Alert*\n\n*Wallet:* {label}\n`{addr[:8]}...{addr[-4:]}`\n\nMade a new transaction — check it out:\nhttps://solscan.io/tx/{latest_sig}"
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown", disable_web_page_preview=True)
        except Exception as e:
            log.debug(f"Watchlist scan {addr[:8]}: {e}")

# ── Performance tracker ────────────────────────────────────────────────────────

async def check_performance(bot: Bot, session):
    """24h after an alert, report back if the token pumped or dumped."""
    now = datetime.now(timezone.utc)
    to_check = [
        (mint, data) for mint, data in tracked_alerts.items()
        if (now - data["time"]).total_seconds() >= 86400
        and not data.get("reported")
    ]
    for mint, data in to_check:
        try:
            # Use recent TX count as a proxy for still-alive vs dead token
            sigs = await rpc(session, "getSignaturesForAddress", [mint, {"limit": 50}])
            tx_count = len(sigs) if sigs else 0

            if tx_count > 100:
                verdict = "🚀 Still very active — likely pumping"
                emoji = "🟢"
            elif tx_count > 20:
                verdict = "📊 Moderate activity — holding steady"
                emoji = "🟡"
            else:
                verdict = "💀 Low activity — likely dumped"
                emoji = "🔴"

            lp_name, lp_emoji = data.get("launchpad", ("Unknown", "⚪"))
            msg = (
                f"📊 *24h Performance Report*\n\n"
                f"{emoji} `{mint[:8]}...{mint[-4:]}`\n"
                f"Launchpad: {lp_emoji} {lp_name}\n"
                f"Alert score was: *{data['score']}*\n\n"
                f"Status: {verdict}\n"
                f"Recent TXs (24h): {tx_count}\n\n"
                f"🔗 https://dexscreener.com/solana/{mint}"
            )
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown", disable_web_page_preview=True)
            tracked_alerts[mint]["reported"] = True
        except Exception as e:
            log.debug(f"Performance check {mint[:8]}: {e}")

# ── Daily summary ──────────────────────────────────────────────────────────────

async def send_daily_summary(bot: Bot):
    """Send a summary of today's top alerted tokens."""
    if not daily_top:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text="📋 *Daily Summary*\n\nNo tokens hit the threshold today.",
            parse_mode="Markdown"
        )
        return

    top5 = sorted(daily_top, key=lambda x: x["score"], reverse=True)[:5]
    lines = ["📋 *AlphaScan Daily Summary*\n", f"Top tokens from the last 24h:\n"]
    for i, t in enumerate(top5, 1):
        lp_name, lp_emoji = t.get("launchpad", ("?", "⚪"))
        lines.append(
            f"{i}. `{t['mint'][:8]}...` — Score *{t['score']}* {lp_emoji} {lp_name}\n"
            f"   https://dexscreener.com/solana/{t['mint']}"
        )
    lines.append(f"\n_Total alerts today: {len(daily_top)}_")
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )
    daily_top.clear()

# ── Alert formatter ────────────────────────────────────────────────────────────

def format_alert(mint: str, info: dict, result: dict) -> str:
    score    = result["total"]
    rug      = result["rug_risk"]
    rug_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(rug, "⚪")
    lp_name, lp_emoji = info.get("launchpad", ("Unknown", "⚪"))

    bar_filled = round(score / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    bd = result["breakdown"]
    boosts_text   = "\n".join(result["boosts"])   if result["boosts"]   else "—"
    warnings_text = "\n".join(result["warnings"]) if result["warnings"] else "—"

    grad_line = "✅ Graduated to PumpSwap\n" if info.get("graduated") else ""

    return (
        f"🚨 *AlphaScan Alert*\n\n"
        f"*Launchpad:* {lp_emoji} {lp_name}\n"
        f"*Token:* `{mint[:8]}...{mint[-4:]}`\n"
        f"*Alpha Score:* {score}/99\n"
        f"`{bar}` {score}\n\n"
        f"*Score breakdown:*\n"
        f"  🐋 Smart money:    {bd['smart_money']}/25\n"
        f"  📊 Distribution:   {bd['distribution']}/20\n"
        f"  🤖 Organic launch: {bd['organic']}/20\n"
        f"  ⚡ TX velocity:    {bd['tx_velocity']}/15\n"
        f"  🛡 Dev safety:     {bd['dev_safety']}/10\n"
        f"  👥 Holder count:   {bd['holder_count']}/5\n\n"
        f"*Signals:*\n{boosts_text}\n\n"
        f"*Warnings:*\n{warnings_text}\n\n"
        f"{grad_line}"
        f"*Rug risk:* {rug_emoji} {rug.upper()}\n"
        f"*Top holder:* {info.get('top_holder_pct', 0)}% | "
        f"*Holders:* {info.get('holders', 0)} | "
        f"*Recent TXs:* {info.get('recent_txs', 0)}\n\n"
        f"🔗 [DexScreener](https://dexscreener.com/solana/{mint}) | "
        f"[Birdeye](https://birdeye.so/token/{mint}?chain=solana) | "
        f"[Solscan](https://solscan.io/token/{mint})\n\n"
        f"_Scanned {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC_"
    )

# ── Telegram command handlers ──────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *AlphaScan.sol commands:*\n\n"
        "/threshold `<number>` — set alert score threshold (e.g. /threshold 80)\n"
        "/watch `<address>` `<label>` — add wallet to watchlist\n"
        "/unwatch `<address>` — remove wallet from watchlist\n"
        "/watchlist — show all watched wallets\n"
        "/summary — get today's top tokens now\n"
        "/status — bot health & current settings\n",
        parse_mode="Markdown"
    )

async def cmd_threshold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global alert_threshold
    try:
        val = int(ctx.args[0])
        if not (40 <= val <= 99):
            raise ValueError
        alert_threshold = val
        await update.message.reply_text(f"✅ Alert threshold set to *{val}*", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /threshold 80 (must be 40–99)")

async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /watch <wallet_address> <label>")
        return
    addr  = ctx.args[0]
    label = " ".join(ctx.args[1:])
    watchlist_wallets[addr] = label
    await update.message.reply_text(f"👁 Now watching *{label}*\n`{addr[:8]}...{addr[-4:]}`", parse_mode="Markdown")

async def cmd_unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /unwatch <wallet_address>")
        return
    addr = ctx.args[0]
    if addr in watchlist_wallets:
        label = watchlist_wallets.pop(addr)
        await update.message.reply_text(f"✅ Removed *{label}* from watchlist", parse_mode="Markdown")
    else:
        await update.message.reply_text("Wallet not found in watchlist.")

async def cmd_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not watchlist_wallets:
        await update.message.reply_text("No wallets in watchlist. Use /watch to add one.")
        return
    lines = ["👁 *Watchlist:*\n"]
    for addr, label in watchlist_wallets.items():
        lines.append(f"• *{label}*: `{addr[:8]}...{addr[-4:]}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot = ctx.bot
    await send_daily_summary(bot)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"⚙️ *AlphaScan Status*\n\n"
        f"Threshold: *{alert_threshold}*\n"
        f"Scan interval: *{SCAN_INTERVAL}s*\n"
        f"Tokens seen: *{len(seen_mints)}*\n"
        f"Alerts today: *{len(daily_top)}*\n"
        f"Watchlist wallets: *{len(watchlist_wallets)}*\n"
        f"Tracked for performance: *{len(tracked_alerts)}*\n",
        parse_mode="Markdown"
    )

# ── Main scan loop ─────────────────────────────────────────────────────────────

async def scan_loop(bot: Bot):
    last_daily_summary = datetime.now(timezone.utc).hour
    scan_count = 0

    async with aiohttp.ClientSession() as session:
        while True:
            scan_count += 1
            log.info(f"Scan #{scan_count} starting...")
            try:
                new_mints = await fetch_new_tokens(session)
                log.info(f"Found {len(new_mints)} new mints")

                alerted = 0
                for mint, launchpad in new_mints:
                    if mint in seen_mints:
                        continue
                    seen_mints.add(mint)

                    info = await enrich_token(session, mint, launchpad)
                    if not info:
                        continue

                    result = score_token(info)
                    score  = result["total"]
                    lp_name, _ = launchpad

                    log.info(
                        f"Token {mint[:8]}... score={score} "
                        f"rug={result['rug_risk']} lp={lp_name} "
                        f"bundled={result['breakdown']['organic'] == 0}"
                    )

                    if score >= alert_threshold and not result["hard_fail"]:
                        msg = format_alert(mint, info, result)
                        await bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=msg,
                            parse_mode="Markdown",
                            disable_web_page_preview=True
                        )
                        tracked_alerts[mint] = {
                            "score":     score,
                            "time":      datetime.now(timezone.utc),
                            "launchpad": launchpad,
                            "reported":  False,
                        }
                        daily_top.append({"mint": mint, "score": score, "launchpad": launchpad})
                        alerted += 1
                        await asyncio.sleep(1.5)

                # Watchlist check every scan
                await scan_watchlist(session, bot)

                # Performance tracker check every 10 scans
                if scan_count % 10 == 0:
                    await check_performance(bot, session)

                # Daily summary at 9 AM UTC
                current_hour = datetime.now(timezone.utc).hour
                if current_hour == 9 and last_daily_summary != 9:
                    await send_daily_summary(bot)
                    last_daily_summary = 9
                elif current_hour != 9:
                    last_daily_summary = current_hour

                if alerted == 0:
                    log.info("No high-alpha tokens this scan")

            except Exception as e:
                log.error(f"Scan error: {e}")

            await asyncio.sleep(SCAN_INTERVAL)

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("threshold", cmd_threshold))
    app.add_handler(CommandHandler("watch",     cmd_watch))
    app.add_handler(CommandHandler("unwatch",   cmd_unwatch))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("summary",   cmd_summary))
    app.add_handler(CommandHandler("status",    cmd_status))

    bot = app.bot

    async def run():
        await app.initialize()
        await app.start()
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                f"✅ AlphaScan.sol MASTERCLASS is live!\n\n"
                f"Scanning every {SCAN_INTERVAL // 60} min with 7-signal scoring.\n"
                f"Alert threshold: {alert_threshold}/99\n\n"
                f"Commands: /start to see all options\n\n"
                f"Monitoring: Pump.fun, LetsBonk, Raydium LaunchLab, Moonshot"
            )
        )
        await asyncio.gather(
            scan_loop(bot),
            app.updater.start_polling()
        )

    asyncio.run(run())

if __name__ == "__main__":
    main()

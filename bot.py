import os
import asyncio
import aiohttp
import json
import logging
from datetime import datetime
from telegram import Bot
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
HELIUS_API_KEY = os.environ["HELIUS_API_KEY"]
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SECONDS", "300"))  # default 5 min
ALPHA_THRESHOLD = int(os.environ.get("ALPHA_THRESHOLD", "75"))

HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API = f"https://api.helius.xyz/v0"

seen_mints = set()


# ── Helius RPC helper ──────────────────────────────────────────────────────────

async def rpc(session, method, params, req_id=1):
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    async with session.post(HELIUS_RPC, json=payload) as resp:
        data = await resp.json()
        if "error" in data:
            raise Exception(f"RPC error: {data['error']}")
        return data.get("result")


# ── Token scoring ──────────────────────────────────────────────────────────────

def score_token(token_info: dict) -> dict:
    """
    Score a token 0-100 based on on-chain signals.
    Returns score + breakdown dict.
    """
    score = 0
    breakdown = {}

    # 1. Holder count (from token supply info)
    holders = token_info.get("holders", 0)
    if holders > 500:
        h_score = 25
    elif holders > 100:
        h_score = 15
    elif holders > 20:
        h_score = 8
    else:
        h_score = 2
    score += h_score
    breakdown["holders"] = h_score

    # 2. Supply concentration — lower top-holder % is better
    top_holder_pct = token_info.get("top_holder_pct", 100)
    if top_holder_pct < 5:
        c_score = 25
    elif top_holder_pct < 15:
        c_score = 18
    elif top_holder_pct < 30:
        c_score = 10
    elif top_holder_pct < 50:
        c_score = 4
    else:
        c_score = 0
    score += c_score
    breakdown["concentration"] = c_score

    # 3. Transaction velocity (recent tx count)
    tx_count = token_info.get("recent_txs", 0)
    if tx_count > 200:
        t_score = 25
    elif tx_count > 50:
        t_score = 17
    elif tx_count > 10:
        t_score = 8
    else:
        t_score = 2
    score += t_score
    breakdown["tx_velocity"] = t_score

    # 4. Age penalty — very new tokens are riskier
    age_slots = token_info.get("age_slots", 0)
    slots_per_day = 216000  # ~2.5 days
    if age_slots > slots_per_day * 3:
        a_score = 15
    elif age_slots > slots_per_day:
        a_score = 10
    elif age_slots > 50000:
        a_score = 5
    else:
        a_score = 1
    score += a_score
    breakdown["age"] = a_score

    # 5. Rug risk flags
    rug_risk = "low"
    if top_holder_pct > 50 or holders < 20:
        rug_risk = "high"
    elif top_holder_pct > 25 or holders < 100:
        rug_risk = "medium"

    breakdown["rug_risk"] = rug_risk
    breakdown["total"] = min(score, 99)
    return breakdown


# ── Fetch recent token mints via Helius Enhanced API ──────────────────────────

async def fetch_new_tokens(session) -> list[str]:
    """Get recently created token mints using Helius getAssetsByAuthority / signatures."""
    try:
        # Get recent signatures on the Token program
        sigs = await rpc(session, "getSignaturesForAddress", [
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            {"limit": 50}
        ])
        if not sigs:
            return []

        mints = []
        # Check a sample of txs for InitializeMint instructions
        for sig_info in sigs[:15]:
            try:
                tx = await rpc(session, "getTransaction", [
                    sig_info["signature"],
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                ])
                if not tx:
                    continue
                instructions = tx.get("transaction", {}).get("message", {}).get("instructions", [])
                for ix in instructions:
                    parsed = ix.get("parsed", {})
                    if parsed.get("type") == "initializeMint":
                        mint = parsed.get("info", {}).get("mint")
                        if mint and mint not in seen_mints:
                            mints.append(mint)
            except Exception:
                continue
        return mints[:10]
    except Exception as e:
        log.warning(f"fetch_new_tokens error: {e}")
        return []


# ── Enrich token with on-chain data ───────────────────────────────────────────

async def enrich_token(session, mint: str) -> dict | None:
    try:
        # Get largest accounts (top holders)
        largest = await rpc(session, "getTokenLargestAccounts", [mint])
        supply_res = await rpc(session, "getTokenSupply", [mint])

        total_supply = float(supply_res["value"]["uiAmount"] or 1)
        accounts = largest.get("value", []) if largest else []

        top_holder_pct = 0
        if accounts and total_supply > 0:
            top_amount = float(accounts[0].get("uiAmount") or 0)
            top_holder_pct = round((top_amount / total_supply) * 100, 1)

        holders = len(accounts)

        # Get recent tx count
        sigs = await rpc(session, "getSignaturesForAddress", [mint, {"limit": 50}])
        recent_txs = len(sigs) if sigs else 0

        # Get mint account info for age
        acct = await rpc(session, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        # Estimate age from slot (rough)
        age_slots = 0
        if acct and acct.get("context"):
            current_slot = acct["context"].get("slot", 0)
            age_slots = max(0, current_slot - 250000000)  # rough estimate

        return {
            "mint": mint,
            "holders": holders,
            "top_holder_pct": top_holder_pct,
            "recent_txs": recent_txs,
            "age_slots": age_slots,
            "total_supply": total_supply,
        }
    except Exception as e:
        log.warning(f"enrich_token {mint[:8]}... error: {e}")
        return None


# ── Format Telegram alert ─────────────────────────────────────────────────────

def format_alert(mint: str, info: dict, breakdown: dict) -> str:
    score = breakdown["total"]
    rug = breakdown["rug_risk"]
    rug_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(rug, "⚪")

    bar_filled = round(score / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    dex_link = f"https://dexscreener.com/solana/{mint}"
    birdeye_link = f"https://birdeye.so/token/{mint}?chain=solana"

    return (
        f"🚨 *AlphaScan Alert*\n\n"
        f"*Token:* `{mint[:8]}...{mint[-4:]}`\n"
        f"*Alpha Score:* {score}/99\n"
        f"`{bar}` {score}\n\n"
        f"*Breakdown:*\n"
        f"  👥 Holders score: {breakdown['holders']}/25\n"
        f"  📊 Concentration: {breakdown['concentration']}/25\n"
        f"  ⚡ TX velocity: {breakdown['tx_velocity']}/25\n"
        f"  🕐 Age score: {breakdown['age']}/15\n\n"
        f"*Rug risk:* {rug_emoji} {rug.upper()}\n"
        f"*Top holder:* {info.get('top_holder_pct', 0)}% of supply\n"
        f"*Holders found:* {info.get('holders', 0)}\n"
        f"*Recent TXs:* {info.get('recent_txs', 0)}\n\n"
        f"🔗 [DexScreener]({dex_link}) | [Birdeye]({birdeye_link})\n\n"
        f"_Scanned at {datetime.utcnow().strftime('%H:%M:%S')} UTC_"
    )


# ── Main scan loop ─────────────────────────────────────────────────────────────

async def scan_loop():
    bot = Bot(token=TELEGRAM_TOKEN)
    log.info("AlphaScan bot starting...")

    # Send startup message
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            f"✅ *AlphaScan.sol is live\\!*\n\n"
            f"Scanning Solana every {SCAN_INTERVAL // 60} minutes\\.\n"
            f"Alerts fire when alpha score \\> {ALPHA_THRESHOLD}\\.\n\n"
            f"_Waiting for first scan\\.\\.\\._"
        ),
        parse_mode=ParseMode.MARKDOWN_V2
    )

    async with aiohttp.ClientSession() as session:
        while True:
            log.info("Starting scan...")
            try:
                new_mints = await fetch_new_tokens(session)
                log.info(f"Found {len(new_mints)} new mints")

                alerted = 0
                for mint in new_mints:
                    if mint in seen_mints:
                        continue
                    seen_mints.add(mint)

                    info = await enrich_token(session, mint)
                    if not info:
                        continue

                    breakdown = score_token(info)
                    score = breakdown["total"]
                    log.info(f"Token {mint[:8]}... score={score} rug={breakdown['rug_risk']}")

                    if score >= ALPHA_THRESHOLD:
                        msg = format_alert(mint, info, breakdown)
                        await bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=msg,
                            parse_mode=ParseMode.MARKDOWN,
                            disable_web_page_preview=True
                        )
                        alerted += 1
                        await asyncio.sleep(1)  # avoid Telegram rate limit

                if alerted == 0:
                    log.info("No high-alpha tokens this scan")

            except Exception as e:
                log.error(f"Scan error: {e}")

            log.info(f"Sleeping {SCAN_INTERVAL}s until next scan...")
            await asyncio.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    asyncio.run(scan_loop())

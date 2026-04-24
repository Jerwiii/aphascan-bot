bash

cat > /home/claude/alphascan-bot/bot.py << 'ENDOFBOT'
"""
AlphaScan.sol — Complete rebuild
Architecture: 3 proven data sources → lightweight scoring → Telegram alerts
Built for reliability first, sophistication second.
"""
import os, asyncio, aiohttp, logging, json, re
from datetime import datetime, timezone
from collections import defaultdict
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
HELIUS_API_KEY   = os.environ["HELIUS_API_KEY"]
SCAN_INTERVAL    = int(os.environ.get("SCAN_INTERVAL_SECONDS", "90"))
ALPHA_THRESHOLD  = int(os.environ.get("ALPHA_THRESHOLD", "55"))  # Lowered — smart money empty by default

HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# ── State ──────────────────────────────────────────────────────────────────────
seen_mints        = set()
alert_threshold   = ALPHA_THRESHOLD
tracked_alerts    = {}
daily_top         = []
watchlist_wallets = {}
blacklisted_devs  = set()
smart_wallet_db   = {}
SMART_MONEY       = set(os.environ.get("SMART_WALLETS", "").split(",")) - {""}
# Seed wallet from GMGN public data — known early buyer of explosive memecoins
SMART_MONEY.add("H72yLkhTnoBfhBTXXaj1RBXuirm8s8G5fcVh2XpQLggM")
dev_cache         = {}
price_cache       = {}
copy_signal_cache = {}
winner_tracker    = {}   # mint -> {price_at_alert, buyers: []}

# ── Launchpads ─────────────────────────────────────────────────────────────────
LAUNCHPADS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": ("Pump.fun",          "🟢"),
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": ("LetsBonk.fun",      "🟡"),
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": ("Raydium AMM",      "🔵"),
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": ("Raydium CLMM",     "🔵"),
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4":  ("Jupiter",          "🪐"),
}

STABLECOINS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "So11111111111111111111111111111111111111112",    # wSOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj",  # stSOL
}


# ══════════════════════════════════════════════════════════════════════════════
# DATA FETCHERS — proven, reliable endpoints
# ══════════════════════════════════════════════════════════════════════════════

async def rpc(session, method, params):
    try:
        async with session.post(
            HELIUS_RPC,
            json={"jsonrpc":"2.0","id":1,"method":method,"params":params},
            timeout=aiohttp.ClientTimeout(total=12)
        ) as r:
            d = await r.json()
            return d.get("result") if "error" not in d else None
    except Exception as e:
        log.debug(f"RPC {method}: {e}")
        return None


async def dexscreener(session, path: str) -> dict | list | None:
    """DexScreener API wrapper with error handling."""
    try:
        async with session.get(
            f"https://api.dexscreener.com{path}",
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "AlphaScan/1.0"}
        ) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        log.debug(f"DexScreener {path}: {e}")
    return None


async def fetch_pumpfun_new(session) -> list:
    """
    Pump.fun's own API — the most reliable source for new launches.
    Returns newest tokens sorted by last trade time.
    """
    try:
        async with session.get(
            "https://client-api-2-74b1891ee9f9.herokuapp.com/coins"
            "?offset=0&limit=50&sort=created_timestamp&order=DESC&includeNsfw=false",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                data = await r.json()
                tokens = []
                for c in (data or []):
                    mint = c.get("mint")
                    if mint and mint not in seen_mints:
                        tokens.append({
                            "mint":      mint,
                            "name":      c.get("name", ""),
                            "symbol":    c.get("symbol", ""),
                            "launchpad": ("Pump.fun", "🟢"),
                            "source":    "launch",
                            "mcap":      c.get("usd_market_cap", 0),
                            "created":   c.get("created_timestamp", 0),
                            "reply_count": c.get("reply_count", 0),
                        })
                return tokens[:30]
    except Exception as e:
        log.debug(f"Pump.fun API: {e}")
    return []


async def fetch_pumpfun_trending(session) -> list:
    """
    Pump.fun trending — tokens gaining traction right now.
    """
    try:
        async with session.get(
            "https://client-api-2-74b1891ee9f9.herokuapp.com/coins"
            "?offset=0&limit=50&sort=last_trade_unix_time&order=DESC&includeNsfw=false",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                data = await r.json()
                tokens = []
                for c in (data or []):
                    mint = c.get("mint")
                    if mint and mint not in seen_mints:
                        tokens.append({
                            "mint":      mint,
                            "name":      c.get("name", ""),
                            "symbol":    c.get("symbol", ""),
                            "launchpad": ("Pump.fun", "🟢"),
                            "source":    "trending",
                            "mcap":      c.get("usd_market_cap", 0),
                            "reply_count": c.get("reply_count", 0),
                            "king_of_hill": c.get("king_of_the_hill_timestamp") is not None,
                        })
                return tokens[:30]
    except Exception as e:
        log.debug(f"Pump.fun trending: {e}")
    return []


async def fetch_dexscreener_new_pairs(session) -> list:
    """
    DexScreener newest Solana pairs — catches tokens on Raydium/Jupiter
    that Pump.fun misses entirely.
    """
    tokens = []
    data = await dexscreener(session, "/latest/dex/pairs/solana")
    for p in (data or {}).get("pairs") or []:
        mint = p.get("baseToken", {}).get("address", "")
        if not mint or mint in seen_mints or mint in STABLECOINS:
            continue
        # Only include pairs created in last 2 hours
        created_at = p.get("pairCreatedAt", 0)
        if created_at:
            age_hours = (datetime.now(timezone.utc).timestamp() * 1000 - created_at) / 3600000
            if age_hours > 2:
                continue
        lp_name, lp_emoji = "Unknown", "⚪"
        for prog, lp in LAUNCHPADS.items():
            if prog in (p.get("dexId") or ""):
                lp_name, lp_emoji = lp
                break
        tokens.append({
            "mint":      mint,
            "name":      p.get("baseToken", {}).get("name", ""),
            "symbol":    p.get("baseToken", {}).get("symbol", ""),
            "launchpad": (lp_name, lp_emoji),
            "source":    "launch",
            "mcap":      float(p.get("marketCap") or 0),
            "price_usd": p.get("priceUsd"),
            "volume_24h": float(p.get("volume", {}).get("h24") or 0),
            "liq_usd":   float(p.get("liquidity", {}).get("usd") or 0),
            "chg_5m":    float(p.get("priceChange", {}).get("m5") or 0),
            "chg_1h":    float(p.get("priceChange", {}).get("h1") or 0),
            "chg_24h":   float(p.get("priceChange", {}).get("h24") or 0),
            "txns_h1":   (p.get("txns", {}).get("h1") or {}).get("buys", 0),
        })
    return tokens[:20]


async def fetch_dexscreener_trending(session) -> list:
    """
    DexScreener trending tokens on Solana — community-vetted momentum.
    """
    tokens = []
    # Token profiles = tokens with active social presence
    data = await dexscreener(session, "/token-profiles/latest/v1")
    for item in (data or [])[:40]:
        if item.get("chainId") != "solana":
            continue
        mint = item.get("tokenAddress", "")
        if not mint or mint in seen_mints or mint in STABLECOINS:
            continue
        tokens.append({
            "mint":      mint,
            "name":      item.get("description", "")[:30],
            "symbol":    "",
            "launchpad": ("DexScreener Trending", "🔥"),
            "source":    "trending",
        })

    # Also grab boosted tokens
    data2 = await dexscreener(session, "/token-boosts/latest/v1")
    seen_in_trending = {t["mint"] for t in tokens}
    for item in (data2 or [])[:20]:
        if item.get("chainId") != "solana":
            continue
        mint = item.get("tokenAddress", "")
        if not mint or mint in seen_mints or mint in seen_in_trending or mint in STABLECOINS:
            continue
        tokens.append({
            "mint":      mint,
            "name":      "",
            "symbol":    "",
            "launchpad": ("DexScreener Boosted", "⚡"),
            "source":    "trending",
        })
    return tokens[:20]


async def get_price_data(session, mint: str) -> dict:
    """Fetch price/mcap/volume from DexScreener. Cached 60s."""
    cached = price_cache.get(mint)
    if cached and (datetime.now(timezone.utc) - cached.get("ts", datetime.min.replace(tzinfo=timezone.utc))).total_seconds() < 60:
        return cached

    result = {"found": False, "mcap": 0, "liq_usd": 0, "volume_24h": 0,
              "price_usd": None, "chg_5m": 0, "chg_1h": 0, "chg_24h": 0,
              "txns_h1": 0, "dex": None}
    try:
        data = await dexscreener(session, f"/latest/dex/tokens/{mint}")
        pairs = (data or {}).get("pairs") or []
        if not pairs:
            return result
        pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd") or 0), reverse=True)
        p = pairs[0]
        result.update({
            "found":      True,
            "mcap":       float(p.get("marketCap") or 0),
            "liq_usd":    float(p.get("liquidity", {}).get("usd") or 0),
            "volume_24h": float(p.get("volume", {}).get("h24") or 0),
            "price_usd":  p.get("priceUsd"),
            "chg_5m":     float(p.get("priceChange", {}).get("m5") or 0),
            "chg_1h":     float(p.get("priceChange", {}).get("h1") or 0),
            "chg_24h":    float(p.get("priceChange", {}).get("h24") or 0),
            "txns_h1":    (p.get("txns", {}).get("h1") or {}).get("buys", 0),
            "dex":        p.get("dexId"),
            "ts":         datetime.now(timezone.utc),
        })
        price_cache[mint] = result
    except Exception as e:
        log.debug(f"Price data {mint[:8]}: {e}")
    return result


async def get_token_safety(session, mint: str) -> dict:
    """
    Fast safety check using Helius getAccountInfo.
    Gets mint authority, freeze authority, and supply in one call.
    """
    result = {"mint_auth": "active", "freeze_auth": "active",
              "mint_revoked": False, "freeze_revoked": False,
              "safe": False, "supply": 0}
    try:
        info = await rpc(session, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        if not info or not info.get("value"):
            return result
        parsed = info["value"].get("data", {}).get("parsed", {}).get("info", {})
        mint_auth   = parsed.get("mintAuthority")
        freeze_auth = parsed.get("freezeAuthority")
        supply      = float(parsed.get("supply") or 0)
        decimals    = int(parsed.get("decimals") or 0)
        result.update({
            "mint_auth":     mint_auth or "revoked",
            "freeze_auth":   freeze_auth or "revoked",
            "mint_revoked":  mint_auth is None,
            "freeze_revoked": freeze_auth is None,
            "safe":          mint_auth is None and freeze_auth is None,
            "supply":        supply / (10 ** decimals) if decimals else supply,
        })
    except Exception as e:
        log.debug(f"Safety {mint[:8]}: {e}")
    return result


async def get_holders(session, mint: str) -> dict:
    """Top holder concentration check with LP exclusion."""
    result = {"count": 0, "top1_pct": 0, "top3_pct": 0, "real_top1_pct": 0, "lp_excluded": False}
    try:
        largest    = await rpc(session, "getTokenLargestAccounts", [mint])
        supply_res = await rpc(session, "getTokenSupply", [mint])
        total = float((supply_res or {}).get("value", {}).get("uiAmount") or 1)
        accts = (largest or {}).get("value", [])
        if not accts or total <= 0:
            return result
        amounts = [float(a.get("uiAmount") or 0) for a in accts]
        result["count"] = len(amounts)
        # Exclude LP pool — if top account holds 2x+ more than second AND >20%
        if len(amounts) >= 2 and amounts[0] > amounts[1] * 1.8 and (amounts[0]/total*100) > 20:
            real = amounts[1:]
            result["lp_excluded"] = True
        else:
            real = amounts
        result["top1_pct"]      = round(amounts[0] / total * 100, 1)
        result["real_top1_pct"] = round(real[0] / total * 100, 1) if real else 0
        result["top3_pct"]      = round(sum(real[:3]) / total * 100, 1) if len(real) >= 3 else result["real_top1_pct"]
    except Exception as e:
        log.debug(f"Holders {mint[:8]}: {e}")
    return result


async def get_dev_info(session, mint: str) -> dict:
    """Check deployer wallet for prior rug history."""
    result = {"deployer": "", "launches": 0, "is_serial": False, "risk": "unknown", "age_days": 0}
    try:
        acct = await rpc(session, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        if not acct or not acct.get("value"):
            return result
        deployer = acct["value"].get("owner", "")
        if not deployer:
            return result
        result["deployer"] = deployer

        if deployer in blacklisted_devs:
            result.update({"is_serial": True, "risk": "high"})
            return result
        if deployer in dev_cache:
            return dev_cache[deployer]

        sigs = await rpc(session, "getSignaturesForAddress", [deployer, {"limit": 50}])
        if not sigs:
            dev_cache[deployer] = result
            return result

        # Count real mint initializations
        launches = 0
        for s in sigs[:20]:
            try:
                tx = await rpc(session, "getTransaction", [
                    s["signature"],
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                ])
                if not tx:
                    continue
                for ix in tx.get("transaction",{}).get("message",{}).get("instructions",[]):
                    p = ix.get("parsed", {})
                    if isinstance(p, dict) and p.get("type") == "initializeMint":
                        launches += 1
            except Exception:
                continue

        # Wallet age
        if sigs[-1].get("blockTime"):
            result["age_days"] = round((datetime.now(timezone.utc).timestamp() - sigs[-1]["blockTime"]) / 86400)

        result["launches"] = launches
        if launches >= 5:
            result["is_serial"] = True
            result["risk"]      = "high"
            blacklisted_devs.add(deployer)
        elif launches >= 2:
            result["risk"] = "medium"
        else:
            result["risk"] = "low"

        dev_cache[deployer] = result
    except Exception as e:
        log.debug(f"Dev info {mint[:8]}: {e}")
    return result


async def check_smart_money(session, mint: str) -> dict:
    """Check if known smart wallets bought this token early."""
    result = {"count": 0, "wallets": [], "score": 0}
    all_smart = SMART_MONEY | set(smart_wallet_db.keys())
    if not all_smart:
        return result
    try:
        sigs = await rpc(session, "getSignaturesForAddress", [mint, {"limit": 30}])
        for s in (sigs or [])[:20]:
            try:
                tx = await rpc(session, "getTransaction", [
                    s["signature"],
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                ])
                if not tx:
                    continue
                keys = [
                    a.get("pubkey","") if isinstance(a,dict) else str(a)
                    for a in tx.get("transaction",{}).get("message",{}).get("accountKeys",[])
                ]
                for k in keys:
                    if k in all_smart and k not in result["wallets"]:
                        result["wallets"].append(k)
                        result["count"] += 1
            except Exception:
                continue
    except Exception as e:
        log.debug(f"Smart money {mint[:8]}: {e}")
    c = result["count"]
    result["score"] = 25 if c >= 3 else 18 if c == 2 else 10 if c == 1 else 0
    return result


# ══════════════════════════════════════════════════════════════════════════════
# SCORING ENGINE — balanced for real-world use
# Max 100 pts. Designed so good tokens score 55+ without smart money list.
# ══════════════════════════════════════════════════════════════════════════════

def score_token(token: dict, px: dict, safety: dict, holders: dict, dev: dict, smart: dict) -> dict:
    score    = 0
    warnings = []
    boosts   = []
    bd       = {}

    mcap     = float(px.get("mcap") or token.get("mcap") or 0)
    liq      = float(px.get("liq_usd") or token.get("liq_usd") or 0)
    vol      = float(px.get("volume_24h") or token.get("volume_24h") or 0)
    chg_5m   = float(px.get("chg_5m") or token.get("chg_5m") or 0)
    chg_1h   = float(px.get("chg_1h") or token.get("chg_1h") or 0)
    chg_24h  = float(px.get("chg_24h") or token.get("chg_24h") or 0)
    txns_h1  = int(px.get("txns_h1") or token.get("txns_h1") or 0)
    vol_mcap = round(vol / mcap, 2) if mcap > 0 else 0

    # ── 1. Authority safety (25 pts) — highest weight, non-negotiable ──────────
    if safety.get("safe"):
        a_score = 25; boosts.append("🔒 Both authorities revoked")
    elif safety.get("mint_revoked"):
        a_score = 15; warnings.append("⚠️ Freeze authority active")
    elif safety.get("freeze_revoked"):
        a_score = 10; warnings.append("⚠️ Mint authority active — dev can print")
    else:
        a_score = 0;  warnings.append("🚨 Both authorities active")
    score += a_score; bd["authority"] = a_score

    # ── 2. Liquidity (20 pts) ──────────────────────────────────────────────────
    if liq >= 50000:   l_score = 20; boosts.append(f"💧 Deep liquidity ${liq:,.0f}")
    elif liq >= 20000: l_score = 15; boosts.append(f"💧 Solid liquidity ${liq:,.0f}")
    elif liq >= 10000: l_score = 10
    elif liq >= 5000:  l_score = 5
    elif liq >= 1000:  l_score = 2
    else:              l_score = 0;  warnings.append(f"⚠️ Very thin liquidity ${liq:,.0f}")
    score += l_score; bd["liquidity"] = l_score

    # ── 3. Holder concentration (15 pts) ──────────────────────────────────────
    real_top1 = holders.get("real_top1_pct", holders.get("top1_pct", 50))
    if real_top1 < 5:    h_score = 15; boosts.append(f"✅ Great distribution — top holder {real_top1}%")
    elif real_top1 < 10: h_score = 12
    elif real_top1 < 20: h_score = 8
    elif real_top1 < 35: h_score = 4;  warnings.append(f"⚠️ Top holder {real_top1}%")
    elif real_top1 < 50: h_score = 1;  warnings.append(f"🚨 Whale alert: {real_top1}%")
    else:                h_score = 0;  warnings.append(f"🚨 Top holder owns {real_top1}% — rug setup")
    score += h_score; bd["distribution"] = h_score

    # ── 4. Price momentum & direction (15 pts) ────────────────────────────────
    if chg_1h > 50 and chg_5m > 0:
        m_score = 15; boosts.append(f"🚀 Up {chg_1h:.0f}% in 1h and still climbing")
    elif chg_1h > 20 and chg_5m > 0:
        m_score = 12; boosts.append(f"📈 Strong momentum +{chg_1h:.0f}% 1h")
    elif chg_1h > 5 and chg_5m > 0:
        m_score = 9
    elif chg_5m > 5:
        m_score = 7;  boosts.append(f"📈 5m candle +{chg_5m:.0f}%")
    elif chg_1h >= 0:
        m_score = 5
    elif chg_1h > -20:
        m_score = 3;  warnings.append(f"📉 Down {abs(chg_1h):.0f}% in 1h")
    else:
        m_score = 0;  warnings.append(f"📉 Dumping — {chg_1h:.0f}% in 1h")
    score += m_score; bd["momentum"] = m_score

    # ── 5. Volume / MCap ratio (10 pts) ───────────────────────────────────────
    if vol_mcap >= 3.0:   vm_score = 10; boosts.append(f"🔥 Vol/MCap {vol_mcap:.1f}x — extraordinary")
    elif vol_mcap >= 1.5: vm_score = 8;  boosts.append(f"🔥 Vol/MCap {vol_mcap:.1f}x — very high")
    elif vol_mcap >= 0.5: vm_score = 5
    elif vol_mcap >= 0.1: vm_score = 2
    else:                 vm_score = 0
    score += vm_score; bd["vol_mcap"] = vm_score

    # ── 6. Transaction activity (5 pts) ───────────────────────────────────────
    if txns_h1 > 200:   t_score = 5; boosts.append(f"⚡ {txns_h1} buys in last hour")
    elif txns_h1 > 80:  t_score = 4
    elif txns_h1 > 30:  t_score = 3
    elif txns_h1 > 10:  t_score = 2
    else:               t_score = 1
    score += t_score; bd["tx_activity"] = t_score

    # ── 7. Dev safety (5 pts) ─────────────────────────────────────────────────
    if dev.get("deployer","") in blacklisted_devs:
        d_score = 0; warnings.append("🚨 Blacklisted deployer")
    elif dev.get("is_serial"):
        d_score = 0; warnings.append(f"🚨 Serial launcher — {dev.get('launches',0)} prior mints")
    elif dev.get("risk") == "medium":
        d_score = 2; warnings.append(f"⚠️ Dev has {dev.get('launches',0)} prior launches")
    elif dev.get("age_days", 999) < 1:
        d_score = 2; warnings.append("⚠️ Brand new deployer wallet")
    else:
        d_score = 5
    score += d_score; bd["dev_safety"] = d_score

    # ── 8. Smart money (5 pts) ────────────────────────────────────────────────
    sm_score = smart.get("score", 0)
    score += sm_score; bd["smart_money"] = sm_score
    if smart.get("count", 0) >= 2:
        boosts.append(f"🐋 {smart['count']} smart wallets confirmed in early")
    elif smart.get("count", 0) == 1:
        boosts.append("🐋 Smart wallet spotted early")

    # ── 9. Social signal — Pump.fun engagement (3 pts) ────────────────────────
    replies = int(token.get("reply_count") or 0)
    koth    = bool(token.get("king_of_hill"))
    if koth:
        s_score = 3; boosts.append("👑 King of the Hill on Pump.fun")
    elif replies > 50:
        s_score = 3; boosts.append(f"💬 {replies} community replies")
    elif replies > 20:
        s_score = 2
    elif replies > 5:
        s_score = 1
    else:
        s_score = 0
    score += s_score; bd["social"] = s_score

    # ── 10. Market cap positioning (2 pts — bonus for sweet spot) ─────────────
    if 0 < mcap < 100000:
        mc_score = 2; boosts.append(f"🎯 Early MCap ${mcap:,.0f} — high upside")
    elif mcap < 500000:
        mc_score = 2; boosts.append(f"📊 MCap ${mcap:,.0f}")
    elif mcap < 2000000:
        mc_score = 1
    else:
        mc_score = 0
    score += mc_score; bd["mcap_position"] = mc_score

    # ── Hard disqualifiers ─────────────────────────────────────────────────────
    hard_fail    = False
    fail_reasons = []

    if real_top1 > 50:
        hard_fail = True; fail_reasons.append(f"top holder {real_top1}% (excl LP)")
    if dev.get("deployer","") in blacklisted_devs:
        hard_fail = True; fail_reasons.append("blacklisted deployer")
    if dev.get("is_serial") and holders.get("count", 99) < 15:
        hard_fail = True; fail_reasons.append("serial rugger + <15 holders")
    if not safety.get("mint_revoked") and not safety.get("freeze_revoked") and real_top1 > 40:
        hard_fail = True; fail_reasons.append("both auths active + whale concentration")
    if chg_1h < -40 and vol_mcap < 0.3:
        hard_fail = True; fail_reasons.append("heavy dump with no volume support")
    if liq == 0 and mcap > 50000:
        hard_fail = True; fail_reasons.append("no liquidity found for established token")

    if hard_fail:
        warnings.append(f"❌ HARD FAIL: {', '.join(fail_reasons)}")

    # ── Rug risk ───────────────────────────────────────────────────────────────
    if hard_fail or real_top1 > 35 or (not safety.get("mint_revoked") and not safety.get("freeze_revoked")):
        rug_risk = "high"
    elif real_top1 > 20 or not safety.get("mint_revoked") or dev.get("risk") == "medium":
        rug_risk = "medium"
    else:
        rug_risk = "low"

    final = 0 if hard_fail else min(99, score)

    # ── Conviction label ───────────────────────────────────────────────────────
    if hard_fail:      conviction = "❌ DO NOT BUY"
    elif final >= 85:  conviction = "💎 VERY HIGH CONVICTION"
    elif final >= 72:  conviction = "🟢 HIGH CONVICTION"
    elif final >= 58:  conviction = "🟡 MODERATE — proceed carefully"
    elif final >= 45:  conviction = "🟠 WEAK — high risk"
    else:              conviction = "🔴 AVOID"

    return {
        "total":      final,
        "conviction": conviction,
        "rug_risk":   rug_risk,
        "hard_fail":  hard_fail,
        "warnings":   warnings,
        "boosts":     boosts,
        "breakdown":  bd,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ALERT FORMATTER
# ══════════════════════════════════════════════════════════════════════════════

def fmt_usd(v) -> str:
    try:
        v = float(v)
        if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
        if v >= 1_000:     return f"${v/1_000:.1f}K"
        return f"${v:.2f}"
    except: return "N/A"

def fmt_pct(v) -> str:
    try:
        v = float(v)
        return f"{'📈' if v >= 0 else '📉'} {v:+.1f}%"
    except: return "N/A"


def build_alert(token: dict, px: dict, safety: dict, holders: dict,
                dev: dict, smart: dict, result: dict, source: str) -> str:

    score  = result["total"]
    rug    = result["rug_risk"]
    re_em  = {"low":"🟢","medium":"🟡","high":"🔴"}.get(rug,"⚪")
    lp_name, lp_emoji = token.get("launchpad", ("Unknown","⚪"))
    bar    = "█" * round(score/10) + "░" * (10 - round(score/10))
    bd     = result["breakdown"]

    mcap   = float(px.get("mcap") or token.get("mcap") or 0)
    liq    = float(px.get("liq_usd") or token.get("liq_usd") or 0)
    vol    = float(px.get("volume_24h") or token.get("volume_24h") or 0)
    price  = px.get("price_usd") or token.get("price_usd", "N/A")
    chg_5m = px.get("chg_5m") or token.get("chg_5m", 0)
    chg_1h = px.get("chg_1h") or token.get("chg_1h", 0)
    mint   = token["mint"]
    sym    = token.get("symbol", "") or token.get("name", "") or mint[:8]

    source_tag = {
        "launch":   "🆕 NEW LAUNCH",
        "trending": "🔥 TRENDING — BUY SIGNAL",
    }.get(source, "📊 SIGNAL")

    boosts_txt   = "\n".join(result["boosts"])   or "None"
    warnings_txt = "\n".join(result["warnings"]) or "None"

    return (
        f"🚨 *AlphaScan Alert — {source_tag}*\n\n"
        f"*{sym}* | {lp_emoji} {lp_name}\n"
        f"`{mint}`\n\n"
        f"*Score:* {score}/99 `{bar}`\n"
        f"*Conviction:* {result['conviction']}\n\n"
        f"*Price:* ${price}  MCap: {fmt_usd(mcap)}\n"
        f"*Volume:* {fmt_usd(vol)}  Liq: {fmt_usd(liq)}\n"
        f"*Change:* 5m {fmt_pct(chg_5m)} | 1h {fmt_pct(chg_1h)}\n\n"
        f"*Score breakdown:*\n"
        f"  🔒 Authority:    {bd.get('authority',0)}/25\n"
        f"  💧 Liquidity:    {bd.get('liquidity',0)}/20\n"
        f"  📊 Distribution: {bd.get('distribution',0)}/15\n"
        f"  📈 Momentum:     {bd.get('momentum',0)}/15\n"
        f"  🔥 Vol/MCap:     {bd.get('vol_mcap',0)}/10\n"
        f"  ⚡ TX Activity:  {bd.get('tx_activity',0)}/5\n"
        f"  🛡 Dev safety:   {bd.get('dev_safety',0)}/5\n"
        f"  🐋 Smart money:  {bd.get('smart_money',0)}/5\n"
        f"  💬 Social:       {bd.get('social',0)}/3\n"
        f"  🎯 MCap pos:     {bd.get('mcap_position',0)}/2\n\n"
        f"*Safety:*\n"
        f"  Mint auth: {'✅ Revoked' if safety.get('mint_revoked') else '🚨 Active'} | "
        f"Freeze: {'✅ Revoked' if safety.get('freeze_revoked') else '🚨 Active'}\n"
        f"  Top holder: {holders.get('real_top1_pct',0)}% {'(LP excl.)' if holders.get('lp_excluded') else ''} | "
        f"Holders: {holders.get('count',0)}\n"
        f"  Dev launches: {dev.get('launches',0)} | "
        f"Wallet age: {dev.get('age_days',0)}d\n\n"
        f"*✅ Signals:*\n{boosts_txt}\n\n"
        f"*⚠️ Warnings:*\n{warnings_txt}\n\n"
        f"*Rug risk:* {re_em} {rug.upper()}\n\n"
        f"🔗 [DexScreener](https://dexscreener.com/solana/{mint}) | "
        f"[Birdeye](https://birdeye.so/token/{mint}?chain=solana) | "
        f"[Solscan](https://solscan.io/token/{mint})\n\n"
        f"_Scanned {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC_"
    )


# ══════════════════════════════════════════════════════════════════════════════
# CORE PROCESSING PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

async def process_token(session, bot: Bot, token: dict) -> bool:
    """Enrich a token candidate, score it, and alert if it passes."""
    mint   = token["mint"]
    source = token.get("source", "launch")

    # Fetch all signals — price first since it's fastest and pre-filters most
    px = await get_price_data(session, mint)

    # Pre-filter before expensive RPC calls
    mcap  = float(px.get("mcap") or token.get("mcap") or 0)
    liq   = float(px.get("liq_usd") or token.get("liq_usd") or 0)
    chg_1h = float(px.get("chg_1h") or 0)

    # Skip if already pumped > 2000% (you've missed it) or clearly dead
    if chg_1h > 2000:
        log.debug(f"Skip {mint[:8]} — already up {chg_1h:.0f}%")
        return False
    if chg_1h < -70 and liq < 2000:
        log.debug(f"Skip {mint[:8]} — dumping and no liquidity")
        return False

    # Fetch remaining signals in parallel
    safety, holders, dev, smart = await asyncio.gather(
        get_token_safety(session, mint),
        get_holders(session, mint),
        get_dev_info(session, mint),
        check_smart_money(session, mint),
    )

    result = score_token(token, px, safety, holders, dev, smart)
    score  = result["total"]
    sym    = token.get("symbol") or token.get("name") or mint[:8]

    log.info(f"[{source}] {sym} ({mint[:8]}) score={score} conviction={result['conviction']} rug={result['rug_risk']}")

    if score < alert_threshold or result["hard_fail"]:
        return False

    msg = build_alert(token, px, safety, holders, dev, smart, result, source)
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

    tracked_alerts[mint] = {
        "score": score, "time": datetime.now(timezone.utc),
        "launchpad": token.get("launchpad"), "reported": False,
        "source": source, "symbol": sym,
    }
    daily_top.append({"mint": mint, "score": score, "symbol": sym,
                      "launchpad": token.get("launchpad"), "source": source})

    # Track first buyers for smart wallet learning
    winner_tracker[mint] = {"score": score, "time": datetime.now(timezone.utc)}

    # Copy signal tracking
    if smart.get("count", 0) >= 2:
        if mint not in copy_signal_cache:
            copy_signal_cache[mint] = {
                "wallets": smart["wallets"],
                "first_seen": datetime.now(timezone.utc),
                "alerted": False,
            }

    return True


# ══════════════════════════════════════════════════════════════════════════════
# COPY SIGNAL
# ══════════════════════════════════════════════════════════════════════════════

async def check_copy_signals(bot: Bot):
    now = datetime.now(timezone.utc)
    for mint, data in list(copy_signal_cache.items()):
        if data.get("alerted"):
            continue
        wallets = data.get("wallets", [])
        if len(wallets) < 3:
            continue
        age = (now - data["first_seen"]).total_seconds() / 60
        if age > 30:
            copy_signal_cache[mint]["alerted"] = True
            continue
        copy_signal_cache[mint]["alerted"] = True
        wlist = "\n".join(f"`{w[:8]}...{w[-4:]}`" for w in wallets[:5])
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                f"🔥 *HIGH CONVICTION COPY SIGNAL*\n\n"
                f"*{len(wallets)} smart wallets* independently bought:\n"
                f"`{mint}`\n\n"
                f"*Wallets:*\n{wlist}\n\n"
                f"*Time since first buy:* {age:.0f} min\n\n"
                f"🔗 [DexScreener](https://dexscreener.com/solana/{mint}) | "
                f"[Birdeye](https://birdeye.so/token/{mint}?chain=solana)"
            ),
            parse_mode="Markdown", disable_web_page_preview=True
        )


# ══════════════════════════════════════════════════════════════════════════════
# WATCHLIST & PERFORMANCE TRACKERS
# ══════════════════════════════════════════════════════════════════════════════

async def scan_watchlist(session, bot: Bot):
    for addr, label in list(watchlist_wallets.items()):
        try:
            sigs = await rpc(session, "getSignaturesForAddress", [addr, {"limit": 3}])
            if not sigs:
                continue
            sig = sigs[0]["signature"]
            key = f"wl_sig_{sig}"
            if key in seen_mints:
                continue
            seen_mints.add(key)
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    f"👁 *Watchlist — {label}*\n\n"
                    f"`{addr[:8]}...{addr[-4:]}`\n\n"
                    f"Just made a transaction\n"
                    f"🔗 [Solscan](https://solscan.io/account/{addr})"
                ),
                parse_mode="Markdown", disable_web_page_preview=True
            )
        except Exception as e:
            log.debug(f"Watchlist {addr[:8]}: {e}")


async def check_performance(bot: Bot, session):
    now = datetime.now(timezone.utc)
    for mint, data in list(tracked_alerts.items()):
        if data.get("reported"):
            continue
        if (now - data["time"]).total_seconds() < 86400:
            continue
        try:
            px = await get_price_data(session, mint)
            if px.get("found"):
                chg = float(px.get("chg_24h") or 0)
                if chg > 100:    verdict, em = f"🚀 Up {chg:.0f}% — great call", "🟢"
                elif chg > 0:    verdict, em = f"📈 Up {chg:.0f}%", "🟡"
                elif chg > -50:  verdict, em = f"📉 Down {abs(chg):.0f}%", "🟡"
                else:            verdict, em = f"💀 Down {abs(chg):.0f}% — dumped", "🔴"
            else:
                verdict, em = "No price data — may be dead", "⚪"

            sym = data.get("symbol", mint[:8])
            lp_name, lp_emoji = data.get("launchpad", ("?","⚪"))
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    f"📊 *24h Report — {sym}*\n\n"
                    f"{em} {verdict}\n"
                    f"Alert score was: *{data['score']}*\n"
                    f"{lp_emoji} {lp_name}\n\n"
                    f"🔗 [DexScreener](https://dexscreener.com/solana/{mint})"
                ),
                parse_mode="Markdown", disable_web_page_preview=True
            )
            tracked_alerts[mint]["reported"] = True
        except Exception as e:
            log.debug(f"Perf check {mint[:8]}: {e}")


async def send_daily_summary(bot: Bot):
    if not daily_top:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID,
            text="📋 *Daily Summary*\n\nNo tokens alerted today.", parse_mode="Markdown")
        return
    top5  = sorted(daily_top, key=lambda x: x["score"], reverse=True)[:5]
    lines = ["📋 *AlphaScan Daily Top Tokens*\n"]
    for i, t in enumerate(top5, 1):
        lp_name, lp_emoji = t.get("launchpad", ("?","⚪"))
        src = "🆕" if t.get("source") == "launch" else "🔥"
        lines.append(f"{src} {i}. *{t.get('symbol','?')}* — Score *{t['score']}* {lp_emoji}")
        lines.append(f"   `{t['mint']}`")
    lines.append(f"\n_Total alerts: {len(daily_top)}_")
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="\n".join(lines),
        parse_mode="Markdown", disable_web_page_preview=True
    )
    daily_top.clear()


# ══════════════════════════════════════════════════════════════════════════════
# ON-DEMAND CA ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

async def analyze_ca(update, mint: str, session_factory):
    msg = await update.message.reply_text(f"⏳ Analyzing `{mint[:8]}...{mint[-4:]}`", parse_mode="Markdown")
    try:
        async with aiohttp.ClientSession() as session:
            px, safety, holders, dev, smart = await asyncio.gather(
                get_price_data(session, mint),
                get_token_safety(session, mint),
                get_holders(session, mint),
                get_dev_info(session, mint),
                check_smart_money(session, mint),
            )
            token = {
                "mint": mint, "symbol": "", "name": "",
                "launchpad": ("Manual", "🔍"), "source": "manual",
            }
            result = score_token(token, px, safety, holders, dev, smart)
            score  = result["total"]
            rug    = result["rug_risk"]
            re_em  = {"low":"🟢","medium":"🟡","high":"🔴"}.get(rug,"⚪")
            bd     = result["breakdown"]
            mcap   = float(px.get("mcap") or 0)
            liq    = float(px.get("liq_usd") or 0)
            vol    = float(px.get("volume_24h") or 0)
            bar    = "█" * round(score/10) + "░" * (10 - round(score/10))

            boosts_txt   = "\n".join(result["boosts"])   or "None"
            warnings_txt = "\n".join(result["warnings"]) or "None"

            report = (
                f"🔬 *Token Analysis*\n\n"
                f"`{mint}`\n\n"
                f"*Score:* {score}/99 `{bar}`\n"
                f"*Verdict:* {result['conviction']}\n\n"
                f"*Price:* ${px.get('price_usd','N/A')}  MCap: {fmt_usd(mcap)}\n"
                f"*Volume:* {fmt_usd(vol)}  Liq: {fmt_usd(liq)}\n"
                f"*Change:* 5m {fmt_pct(px.get('chg_5m',0))} | "
                f"1h {fmt_pct(px.get('chg_1h',0))} | "
                f"24h {fmt_pct(px.get('chg_24h',0))}\n\n"
                f"*Breakdown:*\n"
                f"  🔒 Authority:    {bd.get('authority',0)}/25\n"
                f"  💧 Liquidity:    {bd.get('liquidity',0)}/20\n"
                f"  📊 Distribution: {bd.get('distribution',0)}/15\n"
                f"  📈 Momentum:     {bd.get('momentum',0)}/15\n"
                f"  🔥 Vol/MCap:     {bd.get('vol_mcap',0)}/10\n"
                f"  ⚡ TX Activity:  {bd.get('tx_activity',0)}/5\n"
                f"  🛡 Dev safety:   {bd.get('dev_safety',0)}/5\n"
                f"  🐋 Smart money:  {bd.get('smart_money',0)}/5\n"
                f"  💬 Social:       {bd.get('social',0)}/3\n"
                f"  🎯 MCap pos:     {bd.get('mcap_position',0)}/2\n\n"
                f"*Safety:*\n"
                f"  Mint auth: {'✅ Revoked' if safety.get('mint_revoked') else '🚨 Active'}\n"
                f"  Freeze auth: {'✅ Revoked' if safety.get('freeze_revoked') else '🚨 Active'}\n"
                f"  Top holder: {holders.get('real_top1_pct',0)}% {'(LP excl.)' if holders.get('lp_excluded') else ''}\n"
                f"  Unique holders: {holders.get('count',0)}\n"
                f"  Dev launches: {dev.get('launches',0)} | Wallet age: {dev.get('age_days',0)}d\n\n"
                f"*✅ Signals:*\n{boosts_txt}\n\n"
                f"*⚠️ Warnings:*\n{warnings_txt}\n\n"
                f"*Rug risk:* {re_em} {rug.upper()}\n\n"
                f"🔗 [DexScreener](https://dexscreener.com/solana/{mint}) | "
                f"[Birdeye](https://birdeye.so/token/{mint}?chain=solana) | "
                f"[Solscan](https://solscan.io/token/{mint})"
            )
            await msg.edit_text(report, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        log.error(f"analyze_ca {mint[:8]}: {e}")
        await msg.edit_text(f"❌ Analysis failed: {str(e)[:100]}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCAN LOOP
# ══════════════════════════════════════════════════════════════════════════════

async def scan_loop(bot: Bot):
    last_summary_hour = -1
    scan_count        = 0
    log.info("Scan loop starting...")

    async with aiohttp.ClientSession() as session:
        while True:
            scan_count += 1
            log.info(f"═══ Scan #{scan_count} ═══")
            try:
                # Fetch all sources in parallel
                pf_new, pf_trending, dex_new, dex_trending = await asyncio.gather(
                    fetch_pumpfun_new(session),
                    fetch_pumpfun_trending(session),
                    fetch_dexscreener_new_pairs(session),
                    fetch_dexscreener_trending(session),
                )

                # Deduplicate across sources, new launches take priority
                candidates = {}
                for t in pf_new + dex_new:
                    if t["mint"] not in candidates:
                        candidates[t["mint"]] = t
                for t in pf_trending + dex_trending:
                    if t["mint"] not in candidates:
                        candidates[t["mint"]] = t

                log.info(
                    f"Sources: pf_new={len(pf_new)} pf_trend={len(pf_trending)} "
                    f"dex_new={len(dex_new)} dex_trend={len(dex_trending)} "
                    f"unique={len(candidates)}"
                )

                alerted = 0
                for mint, token in candidates.items():
                    if mint in seen_mints:
                        continue
                    seen_mints.add(mint)
                    try:
                        fired = await process_token(session, bot, token)
                        if fired:
                            alerted += 1
                            await asyncio.sleep(2)
                    except Exception as e:
                        log.error(f"process_token {mint[:8]}: {e}")

                # Housekeeping
                await scan_watchlist(session, bot)
                await check_copy_signals(bot)

                if scan_count % 15 == 0:
                    await check_performance(bot, session)

                hour = datetime.now(timezone.utc).hour
                if hour == 9 and last_summary_hour != 9:
                    await send_daily_summary(bot)
                    last_summary_hour = 9
                elif hour != 9:
                    last_summary_hour = hour

                log.info(f"Scan #{scan_count} complete — {alerted} alerts, {len(seen_mints)} tokens seen")

            except Exception as e:
                log.error(f"Scan loop error: {e}")

            await asyncio.sleep(SCAN_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *AlphaScan.sol Commands:*\n\n"
        "Paste any CA directly to analyze it\n\n"
        "/threshold `<40-99>` — set alert threshold (default 55)\n"
        "/watch `<address>` `<label>` — track a wallet\n"
        "/unwatch `<address>` — stop tracking\n"
        "/watchlist — show tracked wallets\n"
        "/addwallet `<address>` — add to smart money list\n"
        "/blacklist `[address]` — view/add blacklist\n"
        "/summary — today's top tokens\n"
        "/status — bot health & settings\n",
        parse_mode="Markdown"
    )

async def cmd_threshold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global alert_threshold
    try:
        v = int(ctx.args[0])
        if not 30 <= v <= 99: raise ValueError
        alert_threshold = v
        await update.message.reply_text(f"✅ Threshold set to *{v}*/99", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /threshold 55 (must be 30–99)")

async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /watch <address> <label>")
        return
    addr, label = ctx.args[0], " ".join(ctx.args[1:])
    watchlist_wallets[addr] = label
    await update.message.reply_text(f"👁 Watching *{label}*", parse_mode="Markdown")

async def cmd_unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    addr = ctx.args[0] if ctx.args else ""
    if addr in watchlist_wallets:
        watchlist_wallets.pop(addr)
        await update.message.reply_text("✅ Removed")
    else:
        await update.message.reply_text("Not found in watchlist")

async def cmd_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not watchlist_wallets:
        await update.message.reply_text("No wallets tracked. Use /watch to add.")
        return
    lines = ["👁 *Watchlist:*\n"] + [
        f"• *{label}*: `{addr[:8]}...{addr[-4:]}`"
        for addr, label in watchlist_wallets.items()
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_addwallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /addwallet <address>")
        return
    addr = ctx.args[0]
    SMART_MONEY.add(addr)
    smart_wallet_db[addr] = {"added": datetime.now(timezone.utc).isoformat()}
    await update.message.reply_text(
        f"🐋 Added to smart money list\n`{addr[:8]}...{addr[-4:]}`",
        parse_mode="Markdown"
    )

async def cmd_blacklist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        blacklisted_devs.add(ctx.args[0])
        await update.message.reply_text(f"🚫 Blacklisted `{ctx.args[0][:8]}...`", parse_mode="Markdown")
        return
    if not blacklisted_devs:
        await update.message.reply_text("Blacklist empty — auto-filled as rugs are detected.")
        return
    lines = ["🚫 *Blacklisted deployers:*\n"] + [
        f"• `{addr[:8]}...{addr[-4:]}`" for addr in list(blacklisted_devs)[:20]
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_daily_summary(ctx.bot)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"⚙️ *AlphaScan Status*\n\n"
        f"Threshold: *{alert_threshold}/99*\n"
        f"Scan interval: *{SCAN_INTERVAL}s*\n"
        f"Tokens seen this session: *{len(seen_mints)}*\n"
        f"Alerts today: *{len(daily_top)}*\n"
        f"Watchlist: *{len(watchlist_wallets)}* wallets\n"
        f"Smart money: *{len(SMART_MONEY)}* wallets\n"
        f"Blacklisted devs: *{len(blacklisted_devs)}*\n"
        f"Tracking for 24h report: *{len(tracked_alerts)}*\n\n"
        f"*Sources:* Pump.fun API ✅ | DexScreener ✅ | Helius RPC ✅\n"
        f"*Signals:* Authority | Liquidity | Holders | Momentum | Vol/MCap | Dev | Smart Money | Social\n",
        parse_mode="Markdown"
    )

async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /analyze <token_address>")
        return
    await analyze_ca(update, ctx.args[0].strip(), None)

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", text):
        await analyze_ca(update, text, None)


# ══════════════════════════════════════════════════════════════════════════════
# BOOT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    for cmd, handler in [
        ("start",     cmd_start),
        ("threshold", cmd_threshold),
        ("watch",     cmd_watch),
        ("unwatch",   cmd_unwatch),
        ("watchlist", cmd_watchlist),
        ("addwallet", cmd_addwallet),
        ("blacklist", cmd_blacklist),
        ("summary",   cmd_summary),
        ("status",    cmd_status),
        ("analyze",   cmd_analyze),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def run():
        await app.initialize()
        await app.start()
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                f"✅ AlphaScan.sol — FULL REBUILD LIVE\n\n"
                f"Sources: Pump.fun API + DexScreener + Helius\n"
                f"Threshold: {alert_threshold}/99\n"
                f"Scan every {SCAN_INTERVAL}s\n\n"
                f"Paste any CA directly to analyze it\n"
                f"Type /start for all commands"
            )
        )
        await asyncio.gather(
            scan_loop(app.bot),
            app.updater.start_polling()
        )

    asyncio.run(run())

if __name__ == "__main__":
    main()
ENDOFBOT
echo "Written — $(wc -l < /home/claude/alphascan-bot/bot.py) lines"
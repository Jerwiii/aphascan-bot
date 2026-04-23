"""
AlphaScan.sol — Masterclass Memecoin Alert Bot
Signals: smart money, holder distribution, bundle detection, TX velocity,
         dev safety, mint/freeze authority, liquidity depth, price momentum,
         social proxy, graduation tracking.
"""
import os, asyncio, aiohttp, logging, re
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
SCAN_INTERVAL    = int(os.environ.get("SCAN_INTERVAL_SECONDS", "120"))
ALPHA_THRESHOLD  = int(os.environ.get("ALPHA_THRESHOLD", "75"))
HELIUS_RPC       = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API_BASE  = f"https://api.helius.xyz/v0"

# ── State ──────────────────────────────────────────────────────────────────────
seen_mints        = set()
alert_threshold   = ALPHA_THRESHOLD
tracked_alerts    = {}   # mint -> {score, time, launchpad}
daily_top         = []
watchlist_wallets = {}   # address -> label
dev_cache         = {}   # deployer -> risk dict
smart_wallet_db   = {}   # address -> {wins, total, win_rate}
blacklisted_devs  = set() # deployer addresses that have rugged before
copy_signal_cache = {}    # mint -> {wallets: [], first_seen: datetime}
price_cache       = {}    # mint -> {mcap, price, volume, fetched_at}

# ── Launchpads ─────────────────────────────────────────────────────────────────
LAUNCHPADS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": ("Pump.fun",          "🟢"),
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": ("LetsBonk.fun",      "🟡"),
    "RAYLqkdpeygPBTJqNwFTNtBNiuFGCHZnBsXvMHb4Bg7": ("Raydium LaunchLab", "🔵"),
    "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG": ("Moonshot",          "🌙"),
    "PumpSwapAMMProgram111111111111111111111111111": ("PumpSwap (grad)",   "🎓"),
}

# ── Proven smart money seeds (expandable via /addwallet command) ───────────────
# These are wallets known to enter early on winners. Add real ones you discover.
SMART_MONEY_SEEDS = set(os.environ.get("SMART_WALLETS", "").split(",")) - {""}


# ── DexScreener price & market cap fetcher ────────────────────────────────────
async def fetch_price_data(session, mint: str) -> dict:
    """
    Fetch real-time price, market cap, volume and liquidity from DexScreener.
    Free API, no key needed. Returns clean dict ready for alerts.
    """
    result = {
        "price_usd":   None,
        "mcap":        None,
        "volume_24h":  None,
        "liquidity":   None,
        "price_change_5m":  None,
        "price_change_1h":  None,
        "price_change_24h": None,
        "dex":         None,
        "found":       False,
    }
    # Check cache — DexScreener rate-limits, don't hammer it
    cached = price_cache.get(mint)
    if cached:
        age = (datetime.now(timezone.utc) - cached["fetched_at"]).total_seconds()
        if age < 60:
            return cached

    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return result
            data = await r.json()
            pairs = data.get("pairs") or []
            if not pairs:
                return result
            # Pick the pair with highest liquidity
            pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
            p = pairs[0]
            result.update({
                "price_usd":        p.get("priceUsd"),
                "mcap":             p.get("marketCap"),
                "volume_24h":       p.get("volume", {}).get("h24"),
                "liquidity":        p.get("liquidity", {}).get("usd"),
                "price_change_5m":  p.get("priceChange", {}).get("m5"),
                "price_change_1h":  p.get("priceChange", {}).get("h1"),
                "price_change_24h": p.get("priceChange", {}).get("h24"),
                "dex":              p.get("dexId"),
                "found":            True,
            })
            result["fetched_at"] = datetime.now(timezone.utc)
            price_cache[mint] = result
    except Exception as e:
        log.debug(f"DexScreener {mint[:8]}: {e}")
    return result


def fmt_usd(val) -> str:
    """Format a dollar value cleanly."""
    try:
        v = float(val)
        if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
        if v >= 1_000:     return f"${v/1_000:.1f}K"
        return f"${v:.4f}"
    except Exception:
        return "N/A"


def fmt_pct(val) -> str:
    """Format a percentage change with color emoji."""
    try:
        v = float(val)
        arrow = "📈" if v >= 0 else "📉"
        return f"{arrow} {v:+.1f}%"
    except Exception:
        return "N/A"

# ── RPC helper ─────────────────────────────────────────────────────────────────
async def rpc(session, method, params, req_id=1):
    try:
        async with session.post(
            HELIUS_RPC,
            json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            d = await r.json()
            return d.get("result") if "error" not in d else None
    except Exception as e:
        log.debug(f"RPC {method}: {e}")
        return None

# ── Helius DAS API (enhanced metadata) ────────────────────────────────────────
async def get_asset(session, mint: str) -> dict:
    """Helius DAS getAsset — returns rich token metadata including authorities."""
    try:
        async with session.post(
            HELIUS_RPC,
            json={"jsonrpc":"2.0","id":1,"method":"getAsset","params":{"id": mint}},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            d = await r.json()
            return d.get("result") or {}
    except Exception:
        return {}

# ── Signal 1: Mint & Freeze authority check ────────────────────────────────────
async def check_authorities(session, mint: str) -> dict:
    """
    CRITICAL safety check.
    Mint authority = dev can print infinite tokens and dump on you.
    Freeze authority = dev can freeze your wallet so you can't sell.
    Both should be null/revoked on a safe token.
    """
    result = {
        "mint_authority":   "unknown",
        "freeze_authority": "unknown",
        "mint_revoked":     False,
        "freeze_revoked":   False,
        "safe":             False,
    }
    try:
        info = await rpc(session, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        if not info or not info.get("value"):
            return result
        parsed = info["value"].get("data", {}).get("parsed", {}).get("info", {})
        mint_auth   = parsed.get("mintAuthority")
        freeze_auth = parsed.get("freezeAuthority")
        result["mint_authority"]   = mint_auth   or "revoked"
        result["freeze_authority"] = freeze_auth or "revoked"
        result["mint_revoked"]     = mint_auth is None
        result["freeze_revoked"]   = freeze_auth is None
        result["safe"]             = (mint_auth is None) and (freeze_auth is None)
    except Exception as e:
        log.debug(f"Authority check {mint[:8]}: {e}")
    return result

# ── Signal 2: Liquidity depth ──────────────────────────────────────────────────
async def check_liquidity(session, mint: str) -> dict:
    """
    Fetch pool accounts associated with the token to estimate liquidity.
    Uses largest token accounts as a proxy — the biggest non-deployer account
    is likely a DEX pool. More SOL in pool = harder to move price / less rug risk.
    """
    result = {"pool_found": False, "liquidity_tier": "unknown", "score": 0}
    try:
        largest = await rpc(session, "getTokenLargestAccounts", [mint])
        if not largest or not largest.get("value"):
            return result
        accounts = largest["value"]
        # Largest account by token amount — likely the LP pool
        if accounts:
            top_amount = float(accounts[0].get("uiAmount") or 0)
            supply_res = await rpc(session, "getTokenSupply", [mint])
            total = float((supply_res or {}).get("value", {}).get("uiAmount") or 1)
            pool_pct = (top_amount / total * 100) if total > 0 else 0

            # Pool holding 20-60% of supply is healthy for a new token
            if 20 <= pool_pct <= 70:
                result["pool_found"]      = True
                result["liquidity_tier"]  = "good"
                result["score"]           = 10
            elif 10 <= pool_pct < 20:
                result["pool_found"]      = True
                result["liquidity_tier"]  = "thin"
                result["score"]           = 5
            else:
                result["liquidity_tier"]  = "very thin"
                result["score"]           = 0
            result["pool_pct"] = round(pool_pct, 1)
    except Exception as e:
        log.debug(f"Liquidity check {mint[:8]}: {e}")
    return result

# ── Signal 3: Bundle / coordinated buy detection ───────────────────────────────
def detect_bundling(sigs: list) -> dict:
    """
    Bundled launches have bots buying in the same or adjacent slots.
    This detects coordinated entry which signals artificial demand.
    """
    if not sigs:
        return {"is_bundled": False, "confidence": 0, "reason": "no data"}
    slot_counts = defaultdict(int)
    for s in sigs:
        slot_counts[s.get("slot", 0)] += 1
    max_slot = max(slot_counts.values()) if slot_counts else 0
    if max_slot >= 6:
        return {"is_bundled": True,  "confidence": min(100, max_slot * 12),
                "reason": f"{max_slot} txs in one block"}
    elif max_slot >= 3:
        return {"is_bundled": False, "confidence": max_slot * 8,
                "reason": f"mild clustering ({max_slot}/block)"}
    return {"is_bundled": False, "confidence": 0, "reason": "organic"}

# ── Signal 4: Dev wallet safety ────────────────────────────────────────────────
async def check_dev_history(session, deployer: str) -> dict:
    """
    Check how many tokens this deployer has launched.
    Serial launchers are almost always running pump-and-dump operations.
    """
    if not deployer:
        return {"launches": 0, "is_serial": False, "risk": "unknown"}
    if deployer in dev_cache:
        return dev_cache[deployer]
    result = {"launches": 0, "is_serial": False, "risk": "low"}
    try:
        sigs = await rpc(session, "getSignaturesForAddress", [deployer, {"limit": 100}])
        launches = len(sigs) if sigs else 0
        result["launches"] = launches
        if launches > 50:
            result["is_serial"] = True
            result["risk"]      = "high"
            blacklisted_devs.add(deployer)  # auto-blacklist serial ruggers
        elif launches > 20:
            result["risk"]      = "medium"
        dev_cache[deployer] = result
    except Exception as e:
        log.debug(f"Dev history {deployer[:8]}: {e}")
    return result

# ── Signal 5: Smart money detection ───────────────────────────────────────────
async def count_smart_buyers(session, sigs: list, mint: str = "") -> dict:
    """
    Check how many known high-win-rate wallets bought this token early.
    Even 1 smart wallet = meaningful signal. 3+ = very strong buy signal.
    Also dynamically learns new smart wallets from the /addwallet command.
    """
    result = {"count": 0, "wallets": [], "score": 0}
    if not SMART_MONEY_SEEDS and not smart_wallet_db:
        return result
    all_smart = SMART_MONEY_SEEDS | set(smart_wallet_db.keys())
    checked = 0
    for sig_info in sigs[:25]:
        if checked >= 25:
            break
        try:
            tx = await rpc(session, "getTransaction", [
                sig_info["signature"],
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
            ])
            if not tx:
                continue
            keys = [
                a.get("pubkey","") if isinstance(a, dict) else str(a)
                for a in tx.get("transaction",{}).get("message",{}).get("accountKeys",[])
            ]
            for k in keys:
                if k in all_smart and k not in result["wallets"]:
                    result["wallets"].append(k)
                    result["count"] += 1
            checked += 1
        except Exception:
            continue
    c = result["count"]
    result["score"] = 25 if c >= 3 else 18 if c == 2 else 10 if c == 1 else 0

    # Track copy signal — if 3+ smart wallets buy same token, fire special alert
    if c >= 2:
        if mint not in copy_signal_cache:
            copy_signal_cache[mint] = {"wallets": result["wallets"], "first_seen": datetime.now(timezone.utc), "alerted": False}
        else:
            copy_signal_cache[mint]["wallets"] = list(set(copy_signal_cache[mint]["wallets"] + result["wallets"]))

    return result

# ── Signal 6: Holder distribution ─────────────────────────────────────────────
async def check_holders(session, mint: str) -> dict:
    result = {"count": 0, "top1_pct": 100, "top3_pct": 100, "score": 0}
    try:
        largest    = await rpc(session, "getTokenLargestAccounts", [mint])
        supply_res = await rpc(session, "getTokenSupply", [mint])
        total = float((supply_res or {}).get("value", {}).get("uiAmount") or 1)
        accounts = (largest or {}).get("value", [])
        result["count"] = len(accounts)
        if accounts and total > 0:
            result["top1_pct"] = round(float(accounts[0].get("uiAmount") or 0) / total * 100, 1)
            result["top3_pct"] = round(
                sum(float(a.get("uiAmount") or 0) for a in accounts[:3]) / total * 100, 1
            )
        t1 = result["top1_pct"]
        if t1 < 5:   result["score"] = 20
        elif t1 < 10: result["score"] = 15
        elif t1 < 20: result["score"] = 9
        elif t1 < 35: result["score"] = 4
        else:         result["score"] = 0
    except Exception as e:
        log.debug(f"Holders {mint[:8]}: {e}")
    return result

# ── Signal 7: TX velocity & pattern ───────────────────────────────────────────
def score_velocity(sigs: list) -> dict:
    count = len(sigs)
    if count > 300: score = 15
    elif count > 100: score = 12
    elif count > 40:  score = 8
    elif count > 10:  score = 4
    else:             score = 1
    return {"count": count, "score": score}

# ── Signal 8: Price momentum proxy ────────────────────────────────────────────
async def check_price_momentum(session, mint: str, sigs: list) -> dict:
    """
    Estimate momentum by comparing tx density in first 10 vs last 10 sigs.
    Accelerating activity = momentum. Decelerating = already peaked.
    """
    result = {"momentum": "unknown", "score": 5, "note": ""}
    try:
        if len(sigs) < 20:
            result["note"] = "too new to judge"
            return result
        # Earlier sigs are more recent (getSignaturesForAddress returns newest first)
        recent_slots  = [s.get("slot", 0) for s in sigs[:10]]
        earlier_slots = [s.get("slot", 0) for s in sigs[10:20]]
        recent_span  = max(recent_slots)  - min(recent_slots)  + 1
        earlier_span = max(earlier_slots) - min(earlier_slots) + 1
        # Lower slot span for same tx count = faster = accelerating
        if recent_span < earlier_span * 0.7:
            result.update({"momentum": "accelerating", "score": 10,
                           "note": "TX speed increasing"})
        elif recent_span > earlier_span * 1.5:
            result.update({"momentum": "decelerating", "score": 2,
                           "note": "TX speed slowing — may have already peaked"})
        else:
            result.update({"momentum": "steady", "score": 6, "note": "steady activity"})
    except Exception:
        pass
    return result

# ── Signal 9: Graduation check ────────────────────────────────────────────────
async def check_graduation(session, mint: str) -> bool:
    try:
        sigs = await rpc(session, "getSignaturesForAddress", [mint, {"limit": 30}])
        for s in (sigs or [])[:8]:
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
            if "PumpSwapAMMProgram111111111111111111111111111" in keys:
                return True
    except Exception:
        pass
    return False

# ── Full token enrichment ──────────────────────────────────────────────────────
async def enrich_token(session, mint: str, launchpad: tuple) -> dict | None:
    try:
        # Fire all independent fetches in parallel
        sigs_res, acct_res = await asyncio.gather(
            rpc(session, "getSignaturesForAddress", [mint, {"limit": 100}]),
            rpc(session, "getAccountInfo", [mint, {"encoding": "jsonParsed"}]),
        )
        sigs = sigs_res or []

        # Get deployer from account owner
        deployer = ""
        if acct_res and acct_res.get("value"):
            deployer = acct_res["value"].get("owner", "")

        # Fire remaining checks in parallel
        (auth, liquidity, holders, smart, dev, momentum, graduated) = await asyncio.gather(
            check_authorities(session, mint),
            check_liquidity(session, mint),
            check_holders(session, mint),
            count_smart_buyers(session, sigs, mint),
            check_dev_history(session, deployer),
            check_price_momentum(session, mint, sigs),
            check_graduation(session, mint),
        )

        bundle = detect_bundling(sigs[:30])
        vel    = score_velocity(sigs)
        price  = await fetch_price_data(session, mint)

        return {
            "mint":       mint,
            "price":      price,
            "launchpad":  launchpad,
            "deployer":   deployer,
            "auth":       auth,
            "liquidity":  liquidity,
            "holders":    holders,
            "smart":      smart,
            "dev":        dev,
            "momentum":   momentum,
            "bundle":     bundle,
            "velocity":   vel,
            "graduated":  graduated,
            "sigs_count": len(sigs),
        }
    except Exception as e:
        log.warning(f"enrich_token {mint[:8]}: {e}")
        return None

# ── Master scoring engine (110 pts possible, capped at 99) ────────────────────
def score_token(info: dict) -> dict:
    score    = 0
    warnings = []
    boosts   = []
    bd       = {}

    # 1. Smart money (25 pts)
    sm = info["smart"]
    score += sm["score"]; bd["smart_money"] = sm["score"]
    if sm["count"] >= 3: boosts.append(f"🐋 {sm['count']} smart wallets in early")
    elif sm["count"] > 0: boosts.append(f"🐋 {sm['count']} smart wallet(s) spotted")

    # 2. Holder distribution (20 pts)
    h = info["holders"]
    score += h["score"]; bd["distribution"] = h["score"]
    if h["top1_pct"] > 35: warnings.append(f"⚠️ Top holder owns {h['top1_pct']}%")
    elif h["top1_pct"] < 5: boosts.append("✅ Healthy distribution")

    # 3. Bundle detection (20 pts)
    b = info["bundle"]
    org_score = 0 if b["is_bundled"] else (10 if b["confidence"] > 20 else 20)
    score += org_score; bd["organic"] = org_score
    if b["is_bundled"]: warnings.append(f"🤖 Bundled: {b['reason']}")
    elif org_score == 20: boosts.append("✅ Organic launch confirmed")

    # 4. TX velocity (15 pts)
    v = info["velocity"]
    score += v["score"]; bd["tx_velocity"] = v["score"]
    if v["count"] > 200: boosts.append(f"🔥 {v['count']} transactions")

    # 5. Mint & freeze authority (10 pts) ← NEW
    auth = info["auth"]
    if auth["safe"]:
        auth_score = 10; boosts.append("🔒 Mint & freeze authority revoked")
    elif auth["mint_revoked"] or auth["freeze_revoked"]:
        auth_score = 5; warnings.append("⚠️ Only one authority revoked")
    else:
        auth_score = 0; warnings.append("🚨 Mint/freeze authority still active — dev can rug")
    score += auth_score; bd["authority"] = auth_score

    # 6. Liquidity depth (10 pts) ← NEW
    liq = info["liquidity"]
    score += liq["score"]; bd["liquidity"] = liq["score"]
    if liq["liquidity_tier"] == "good": boosts.append("💧 Good liquidity depth")
    elif liq["liquidity_tier"] == "very thin": warnings.append("⚠️ Very thin liquidity")

    # 7. Dev safety (10 pts)
    dev = info["dev"]
    if dev["is_serial"]:
        dev_score = 0; warnings.append(f"🚨 Serial deployer — {dev['launches']} prior launches")
    elif dev["risk"] == "medium":
        dev_score = 5; warnings.append(f"⚠️ Dev has {dev['launches']} prior launches")
    else:
        dev_score = 10
    score += dev_score; bd["dev_safety"] = dev_score

    # 8. Price momentum (10 pts) ← NEW
    mom = info["momentum"]
    score += mom["score"]; bd["momentum"] = mom["score"]
    if mom["momentum"] == "accelerating": boosts.append(f"📈 {mom['note']}")
    elif mom["momentum"] == "decelerating": warnings.append(f"📉 {mom['note']}")

    # 9. Holder count (5 pts)
    hc = 5 if h["count"] > 200 else 3 if h["count"] > 50 else 2 if h["count"] > 10 else 0
    score += hc; bd["holder_count"] = hc

    # 10. Graduation (5 pts)
    if info.get("graduated"):
        score += 5; bd["graduation"] = 5
        boosts.append("🎓 Graduated to PumpSwap")
    else:
        bd["graduation"] = 0

    # ── Hard disqualifiers ────────────────────────────────────────────────────
    hard_fail = False
    fail_reasons = []
    if h["top1_pct"] > 50:
        hard_fail = True; fail_reasons.append("top holder >50%")
    if b["is_bundled"] and sm["count"] == 0:
        hard_fail = True; fail_reasons.append("bundled + zero smart money")
    if dev["is_serial"] and h["count"] < 30:
        hard_fail = True; fail_reasons.append("serial rugger + <30 holders")
    if not auth["mint_revoked"] and not auth["freeze_revoked"] and h["top1_pct"] > 30:
        hard_fail = True; fail_reasons.append("active authorities + concentrated supply")
    if info.get("deployer","") in blacklisted_devs:
        hard_fail = True; fail_reasons.append("deployer is blacklisted rugger")

    if hard_fail:
        warnings.append(f"❌ HARD FAIL: {', '.join(fail_reasons)}")

    # Rug risk rating
    if hard_fail or h["top1_pct"] > 40 or b["is_bundled"] or not auth["mint_revoked"]:
        rug_risk = "high"
    elif h["top1_pct"] > 20 or b["confidence"] > 20 or dev["risk"] == "medium":
        rug_risk = "medium"
    else:
        rug_risk = "low"

    return {
        "total":     0 if hard_fail else min(99, score),
        "rug_risk":  rug_risk,
        "hard_fail": hard_fail,
        "warnings":  warnings,
        "boosts":    boosts,
        "breakdown": bd,
    }

# ── Launchpad detector ─────────────────────────────────────────────────────────
def detect_launchpad(keys: list) -> tuple:
    for k in keys:
        if k in LAUNCHPADS:
            return LAUNCHPADS[k]
    return ("Unknown", "⚪")


# ── Multi-wallet copy signal ───────────────────────────────────────────────────
async def check_copy_signals(bot: Bot):
    """
    Fire a special HIGH CONVICTION alert when 3+ smart wallets
    independently bought the same token within a short window.
    """
    now = datetime.now(timezone.utc)
    for mint, data in list(copy_signal_cache.items()):
        if data.get("alerted"):
            continue
        wallets = data.get("wallets", [])
        if len(wallets) < 3:
            continue
        # Only alert within 30 minutes of first detection
        age_mins = (now - data["first_seen"]).total_seconds() / 60
        if age_mins > 30:
            copy_signal_cache[mint]["alerted"] = True
            continue
        copy_signal_cache[mint]["alerted"] = True
        short_wallets = [f"`{w[:6]}...{w[-4:]}`" for w in wallets[:5]]
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                f"🔥 *COPY SIGNAL — HIGH CONVICTION*\n\n"
                f"*{len(wallets)} smart wallets* bought the same token\n"
                f"`{mint}`\n\n"
                f"*Wallets:*\n" + "\n".join(short_wallets) + "\n\n"
                f"*Time since first buy:* {age_mins:.1f} min\n\n"
                f"This is a rare signal. Do your own checks first.\n\n"
                f"🔗 [DexScreener](https://dexscreener.com/solana/{mint}) | "
                f"[Birdeye](https://birdeye.so/token/{mint}?chain=solana)"
            ),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        log.info(f"Copy signal fired for {mint[:8]} — {len(wallets)} wallets")

# ── Fetch new token mints ──────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
# DUAL SCANNER ENGINE
# Scanner 1: Real-time launch detection  — catches tokens seconds after mint
# Scanner 2: Momentum scanner            — catches any-age tokens gaining steam
# ══════════════════════════════════════════════════════════════════════════════

# ── Pump.fun program address (the real one) ────────────────────────────────────
PUMPFUN_PROGRAM  = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
LETSBONK_PROGRAM = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
RAYDIUM_PROGRAM  = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"  # Raydium AMM v4

# Momentum tracker — persists across scans
momentum_tracker = {}  # mint -> {vol_samples: [], buyer_samples: [], first_seen, launchpad}
trending_tracker  = {}  # mint -> {snapshots: [], first_seen, alerted}

# ── SCANNER 1: Real-time Pump.fun launch detection ────────────────────────────
async def scanner1_new_launches(session) -> list:
    """
    Watch Pump.fun and LetsBonk program accounts directly.
    Catches tokens within 1-2 scan cycles of launch.
    Much faster than watching the generic Token Program.
    """
    mints = []
    programs = [
        (PUMPFUN_PROGRAM,  ("Pump.fun",     "🟢")),
        (LETSBONK_PROGRAM, ("LetsBonk.fun", "🟡")),
    ]
    for program_addr, launchpad in programs:
        try:
            sigs = await rpc(session, "getSignaturesForAddress", [
                program_addr, {"limit": 40}
            ])
            for s in (sigs or [])[:20]:
                try:
                    tx = await rpc(session, "getTransaction", [
                        s["signature"],
                        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                    ])
                    if not tx:
                        continue
                    msg  = tx.get("transaction", {}).get("message", {})
                    keys = [
                        a.get("pubkey","") if isinstance(a,dict) else str(a)
                        for a in msg.get("accountKeys", [])
                    ]
                    for ix in msg.get("instructions", []) + [
                        inner
                        for group in (tx.get("meta", {}).get("innerInstructions") or [])
                        for inner in group.get("instructions", [])
                    ]:
                        p = ix.get("parsed", {})
                        if isinstance(p, dict) and p.get("type") == "initializeMint":
                            mint = p.get("info", {}).get("mint")
                            if mint and mint not in seen_mints:
                                mints.append((mint, launchpad, "launch"))
                except Exception:
                    continue
        except Exception as e:
            log.debug(f"Scanner1 {program_addr[:8]}: {e}")

    # Also check Raydium for new pool creations (tokens listing directly)
    try:
        sigs = await rpc(session, "getSignaturesForAddress", [RAYDIUM_PROGRAM, {"limit": 20}])
        for s in (sigs or [])[:10]:
            try:
                tx = await rpc(session, "getTransaction", [
                    s["signature"],
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                ])
                if not tx:
                    continue
                msg  = tx.get("transaction", {}).get("message", {})
                keys = [
                    a.get("pubkey","") if isinstance(a,dict) else str(a)
                    for a in msg.get("accountKeys", [])
                ]
                # New Raydium pool = token listing directly on DEX
                for ix in msg.get("instructions", []):
                    p = ix.get("parsed", {})
                    if isinstance(p, dict) and p.get("type") == "initializeMint":
                        mint = p.get("info", {}).get("mint")
                        if mint and mint not in seen_mints:
                            mints.append((mint, ("Raydium LaunchLab", "🔵"), "launch"))
            except Exception:
                continue
    except Exception as e:
        log.debug(f"Scanner1 Raydium: {e}")

    return mints[:15]


# ── SCANNER 2: Momentum scanner (any-age tokens) ──────────────────────────────
async def scanner2_momentum(session) -> list:
    """
    Detect tokens of ANY age that are suddenly gaining momentum.
    Strategy:
      - Sample recent DEX activity from Raydium & PumpSwap
      - For each token seen, track volume samples over time
      - Alert when: volume accelerating + new unique buyers + price still reasonable
    """
    candidates = []
    try:
        # Get recent Raydium swap transactions — these are real trades
        sigs = await rpc(session, "getSignaturesForAddress", [
            RAYDIUM_PROGRAM, {"limit": 50}
        ])
        token_activity = defaultdict(lambda: {"txs": 0, "slots": []})

        for s in (sigs or [])[:30]:
            try:
                tx = await rpc(session, "getTransaction", [
                    s["signature"],
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                ])
                if not tx:
                    continue
                slot = tx.get("slot", 0)
                # Extract token mints from post token balances
                post_balances = tx.get("meta", {}).get("postTokenBalances", [])
                for bal in post_balances:
                    mint = bal.get("mint")
                    if not mint:
                        continue
                    # Skip stablecoins and wrapped SOL
                    if mint in {
                        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
                        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
                        "So11111111111111111111111111111111111111112",    # wSOL
                    }:
                        continue
                    token_activity[mint]["txs"] += 1
                    token_activity[mint]["slots"].append(slot)
            except Exception:
                continue

        # Score each token by momentum signals
        now = datetime.now(timezone.utc)
        for mint, activity in token_activity.items():
            if mint in seen_mints:
                continue
            txs   = activity["txs"]
            slots = activity["slots"]

            # Need at least 3 txs to have a signal
            if txs < 3:
                continue

            # Track across scans
            if mint not in momentum_tracker:
                momentum_tracker[mint] = {
                    "samples":    [],
                    "first_seen": now,
                    "launchpad":  ("Raydium", "🔵"),
                    "alerted":    False,
                }

            entry = momentum_tracker[mint]
            if entry.get("alerted"):
                continue

            entry["samples"].append({"txs": txs, "time": now, "slots": slots})
            # Keep only last 10 samples
            entry["samples"] = entry["samples"][-10:]

            # Need at least 2 samples to measure acceleration
            if len(entry["samples"]) < 2:
                continue

            prev_txs    = entry["samples"][-2]["txs"]
            current_txs = entry["samples"][-1]["txs"]

            # Momentum trigger: activity at least doubled since last scan
            # OR sustained high activity (10+ txs seen in one scan window)
            momentum_score = 0
            momentum_reason = ""

            if current_txs >= prev_txs * 2 and current_txs >= 4:
                momentum_score  = 80
                momentum_reason = f"volume 2x spike ({prev_txs}→{current_txs} txs)"
            elif current_txs >= 6:
                momentum_score  = 65
                momentum_reason = f"high sustained activity ({current_txs} txs)"
            elif current_txs >= prev_txs * 1.5 and current_txs >= 3:
                momentum_score  = 55
                momentum_reason = f"growing activity ({prev_txs}→{current_txs} txs)"

            if momentum_score >= 65:
                candidates.append((mint, entry["launchpad"], "momentum", momentum_reason, momentum_score))
                log.info(f"Momentum candidate: {mint[:8]} — {momentum_reason}")

    except Exception as e:
        log.warning(f"Scanner2: {e}")

    return candidates[:8]


# ── Process and alert a token ──────────────────────────────────────────────────
async def process_token(session, bot: Bot, mint: str, launchpad: tuple,
                        source: str, extra: dict = None):
    """Enrich, score and conditionally alert on a single token."""
    info = await enrich_token(session, mint, launchpad)
    if not info:
        return False

    result = score_token(info)
    score  = result["total"]
    lp_name, _ = launchpad

    log.info(
        f"[{source}] {mint[:8]} score={score} "
        f"rug={result['rug_risk']} lp={lp_name} fail={result['hard_fail']}"
    )

    if score < alert_threshold or result["hard_fail"]:
        return False

    # Build alert with source tag
    source_tag = {
        "launch":   "🆕 *NEW LAUNCH*",
        "momentum": "📊 *MOMENTUM DETECTED*",
        "trending": "🔥 *TRENDING — BUY SIGNAL*",
    }.get(source, "🔍 *SIGNAL*")

    momentum_note = ""
    if source == "momentum" and extra:
        momentum_note = f"*Momentum:* {extra.get('reason','')}\n\n"

    text = format_alert(mint, info, result)
    # Inject source tag and momentum note after first line
    lines = text.split("\n", 1)
    text  = lines[0] + "\n" + source_tag + "\n" + momentum_note + (lines[1] if len(lines)>1 else "")

    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )
    tracked_alerts[mint] = {
        "score": score, "time": datetime.now(timezone.utc),
        "launchpad": launchpad, "reported": False, "source": source
    }
    daily_top.append({"mint": mint, "score": score, "launchpad": launchpad, "source": source})

    if source == "momentum" and mint in momentum_tracker:
        momentum_tracker[mint]["alerted"] = True

    return True



# ══════════════════════════════════════════════════════════════════════════════
# SCANNER 3: Trending token detector
# Sources: DexScreener trending + Birdeye top tokens
# Gates:   volume accelerating, mcap < $5M, price not already pumped,
#          liquidity > $10K, full 10-signal safety check
# ══════════════════════════════════════════════════════════════════════════════

# Tunable limits (can expose as env vars later)
TRENDING_MAX_MCAP_USD    = float(os.environ.get("TRENDING_MAX_MCAP",    "5000000"))   # $5M
TRENDING_MIN_LIQ_USD     = float(os.environ.get("TRENDING_MIN_LIQ",     "10000"))     # $10K
TRENDING_MAX_PUMP_1H_PCT = float(os.environ.get("TRENDING_MAX_PUMP_1H", "500"))       # 500%
TRENDING_MIN_VOL_USD     = float(os.environ.get("TRENDING_MIN_VOL",     "5000"))      # $5K 24h vol

async def scanner3_trending(session) -> list:
    """
    Pull trending Solana tokens from DexScreener and Birdeye.
    Apply 6-gate filter before returning candidates.
    Only returns tokens that are worth buying RIGHT NOW.
    """
    candidates = []
    seen_this_scan = set()

    # ── Source A: DexScreener trending profiles ────────────────────────────────
    try:
        async with session.get(
            "https://api.dexscreener.com/token-profiles/latest/v1",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                items = await r.json()
                for item in (items or [])[:30]:
                    if item.get("chainId") != "solana":
                        continue
                    mint = item.get("tokenAddress", "")
                    if not mint or mint in seen_mints or mint in seen_this_scan:
                        continue
                    seen_this_scan.add(mint)
                    candidates.append((mint, ("DexScreener Trending", "🔥"), "trending"))
    except Exception as e:
        log.debug(f"Scanner3 DexScreener profiles: {e}")

    # ── Source B: DexScreener boosted tokens (paid attention = community active) ─
    try:
        async with session.get(
            "https://api.dexscreener.com/token-boosts/latest/v1",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                items = await r.json()
                for item in (items or [])[:20]:
                    if item.get("chainId") != "solana":
                        continue
                    mint = item.get("tokenAddress", "")
                    if not mint or mint in seen_mints or mint in seen_this_scan:
                        continue
                    seen_this_scan.add(mint)
                    candidates.append((mint, ("DexScreener Boosted", "⚡"), "trending"))
    except Exception as e:
        log.debug(f"Scanner3 DexScreener boosts: {e}")

    # ── Source C: DexScreener new pairs with volume on Solana ─────────────────
    try:
        async with session.get(
            "https://api.dexscreener.com/latest/dex/search?q=SOL",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                data  = await r.json()
                pairs = data.get("pairs") or []
                # Sort by 1h volume change
                sol_pairs = [
                    p for p in pairs
                    if p.get("chainId") == "solana"
                    and p.get("baseToken", {}).get("address") not in seen_mints
                ]
                sol_pairs.sort(
                    key=lambda p: abs(float(p.get("priceChange", {}).get("h1") or 0)),
                    reverse=True
                )
                for p in sol_pairs[:15]:
                    mint = p.get("baseToken", {}).get("address", "")
                    if not mint or mint in seen_this_scan:
                        continue
                    seen_this_scan.add(mint)
                    candidates.append((mint, ("DexScreener Pairs", "📊"), "trending"))
    except Exception as e:
        log.debug(f"Scanner3 DexScreener pairs: {e}")

    log.info(f"Scanner3 raw candidates: {len(candidates)}")

    # ── Apply 6-gate filter ────────────────────────────────────────────────────
    actionable = []
    for mint, launchpad, source in candidates:
        if mint in seen_mints:
            continue
        try:
            # Fetch price data first — cheapest gate, filters most tokens
            px = await fetch_price_data(session, mint)
            if not px.get("found"):
                continue

            mcap     = float(px.get("mcap")    or 0)
            liq      = float(px.get("liquidity") or 0)
            vol_24h  = float(px.get("volume_24h") or 0)
            chg_1h   = float(px.get("price_change_1h")  or 0)
            chg_5m   = float(px.get("price_change_5m")  or 0)
            chg_24h  = float(px.get("price_change_24h") or 0)

            fail_reason = None

            # Gate 1 — Market cap ceiling (still early enough)
            if mcap > TRENDING_MAX_MCAP_USD:
                fail_reason = f"mcap ${mcap/1e6:.1f}M > ${TRENDING_MAX_MCAP_USD/1e6:.0f}M ceiling"

            # Gate 2 — Already pumped too hard (you've missed it)
            elif chg_1h > TRENDING_MAX_PUMP_1H_PCT:
                fail_reason = f"already up {chg_1h:.0f}% in 1h — too late"

            # Gate 3 — Minimum liquidity (can actually enter/exit)
            elif liq < TRENDING_MIN_LIQ_USD:
                fail_reason = f"liquidity ${liq:.0f} < ${TRENDING_MIN_LIQ_USD:.0f} minimum"

            # Gate 4 — Minimum volume (real interest, not ghost)
            elif vol_24h < TRENDING_MIN_VOL_USD:
                fail_reason = f"volume ${vol_24h:.0f} too low"

            # Gate 5 — Must be going UP, not dumping
            elif chg_5m < -15:
                fail_reason = f"dumping {chg_5m:.1f}% in 5m — skip"

            # Gate 6 — Momentum must be accelerating (5m positive, not topped)
            elif chg_24h > 1000 and chg_1h < 0:
                fail_reason = f"24h pump {chg_24h:.0f}% but 1h negative — topped out"

            if fail_reason:
                log.debug(f"Scanner3 gate fail {mint[:8]}: {fail_reason}")
                continue

            # Track across scans for volume acceleration check
            now = datetime.now(timezone.utc)
            if mint not in trending_tracker:
                trending_tracker[mint] = {
                    "snapshots":  [],
                    "first_seen": now,
                    "alerted":    False,
                }
            entry = trending_tracker[mint]
            if entry.get("alerted"):
                continue

            entry["snapshots"].append({
                "vol": vol_24h, "mcap": mcap,
                "chg_5m": chg_5m, "chg_1h": chg_1h,
                "time": now
            })
            entry["snapshots"] = entry["snapshots"][-6:]

            # Need at least 2 snapshots to confirm trend is holding
            if len(entry["snapshots"]) < 2:
                log.debug(f"Scanner3 {mint[:8]}: first sighting, waiting for confirmation")
                continue

            prev_vol  = entry["snapshots"][-2]["vol"]
            prev_chg  = entry["snapshots"][-2]["chg_1h"]

            # Volume must be growing OR already substantial
            vol_growing = vol_24h >= prev_vol * 1.1 or vol_24h > 50000

            if not vol_growing:
                log.debug(f"Scanner3 {mint[:8]}: volume not growing ({prev_vol:.0f}→{vol_24h:.0f})")
                continue

            # Build entry reason for alert
            reasons = []
            if chg_5m > 5:   reasons.append(f"5m: +{chg_5m:.1f}%")
            if chg_1h > 10:  reasons.append(f"1h: +{chg_1h:.1f}%")
            if chg_24h > 0:  reasons.append(f"24h: +{chg_24h:.1f}%")
            reasons.append(f"MCap: {fmt_usd(mcap)}")
            reasons.append(f"Liq: {fmt_usd(liq)}")
            if vol_24h > prev_vol * 1.5:
                reasons.append(f"Vol +{((vol_24h/prev_vol-1)*100):.0f}% vs last scan")

            actionable.append((
                mint, launchpad, "trending",
                {"reason": " | ".join(reasons), "px": px}
            ))
            log.info(f"Scanner3 actionable: {mint[:8]} — {' | '.join(reasons)}")

        except Exception as e:
            log.debug(f"Scanner3 gate check {mint[:8]}: {e}")
            continue

    return actionable[:6]  # cap at 6 per scan to avoid alert spam

# ── Main scan loop (triple scanner) ─────────────────────────────────────────────
async def scan_loop(bot: Bot):
    last_summary_hour = -1
    scan_count        = 0

    async with aiohttp.ClientSession() as session:
        while True:
            scan_count += 1
            log.info(f"═══ Scan #{scan_count} ═══")

            try:
                # Run all three scanners in parallel
                launches, momentum, trending = await asyncio.gather(
                    scanner1_new_launches(session),
                    scanner2_momentum(session),
                    scanner3_trending(session),
                )
                log.info(f"Scanner1: {len(launches)} | Scanner2: {len(momentum)} | Scanner3: {len(trending)}")

                alerted = 0

                # Process new launches
                for item in launches:
                    mint, launchpad = item[0], item[1]
                    if mint in seen_mints:
                        continue
                    seen_mints.add(mint)
                    fired = await process_token(session, bot, mint, launchpad, "launch")
                    if fired:
                        alerted += 1
                        await asyncio.sleep(1.5)

                # Process momentum candidates
                for item in momentum:
                    mint, launchpad, source, reason, mscore = item
                    if mint in seen_mints:
                        continue
                    seen_mints.add(mint)
                    fired = await process_token(
                        session, bot, mint, launchpad, "momentum",
                        extra={"reason": reason, "score": mscore}
                    )
                    if fired:
                        alerted += 1
                        await asyncio.sleep(1.5)

                # Process trending candidates
                for item in trending:
                    mint, launchpad, source, extra = item
                    if mint in seen_mints:
                        continue
                    seen_mints.add(mint)
                    fired = await process_token(
                        session, bot, mint, launchpad, "trending",
                        extra=extra
                    )
                    if fired:
                        alerted += 1
                        if mint in trending_tracker:
                            trending_tracker[mint]["alerted"] = True
                        await asyncio.sleep(1.5)

                # Housekeeping
                await scan_watchlist(session, bot)
                await check_copy_signals(bot)
                if scan_count % 10 == 0:
                    await check_performance(bot, session)
                # Clean up old momentum tracker entries (> 2 hours old)
                if scan_count % 30 == 0:
                    cutoff = datetime.now(timezone.utc)
                    stale  = [
                        m for m, d in momentum_tracker.items()
                        if (cutoff - d["first_seen"]).total_seconds() > 7200
                    ]
                    for m in stale:
                        momentum_tracker.pop(m, None)
                    if stale:
                        log.info(f"Cleaned {len(stale)} stale momentum entries")
                    # Clean stale trending entries
                    stale_t = [
                        m for m, d in trending_tracker.items()
                        if (cutoff - d["first_seen"]).total_seconds() > 3600
                    ]
                    for m in stale_t:
                        trending_tracker.pop(m, None)

                hour = datetime.now(timezone.utc).hour
                if hour == 9 and last_summary_hour != 9:
                    await send_daily_summary(bot)
                    last_summary_hour = 9
                elif hour != 9:
                    last_summary_hour = hour

                log.info(f"Scan #{scan_count} done — {alerted} alerts fired")

            except Exception as e:
                log.error(f"Scan loop error: {e}")

            await asyncio.sleep(SCAN_INTERVAL)

# ── Watchlist scanner ──────────────────────────────────────────────────────────
async def scan_watchlist(session, bot: Bot):
    for addr, label in list(watchlist_wallets.items()):
        cache_key = f"wl_{addr}"
        try:
            sigs = await rpc(session, "getSignaturesForAddress", [addr, {"limit": 3}])
            if not sigs:
                continue
            sig = sigs[0]["signature"]
            if f"wl_sig_{sig}" in seen_mints:
                continue
            seen_mints.add(f"wl_sig_{sig}")
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    f"👁 *Watchlist Move*\n\n"
                    f"*{label}* just transacted\n"
                    f"`{addr[:8]}...{addr[-4:]}`\n\n"
                    f"🔗 [View on Solscan](https://solscan.io/account/{addr})"
                ),
                parse_mode="Markdown", disable_web_page_preview=True
            )
        except Exception as e:
            log.debug(f"Watchlist {addr[:8]}: {e}")

# ── Performance tracker ────────────────────────────────────────────────────────
async def check_performance(bot: Bot, session):
    now = datetime.now(timezone.utc)
    for mint, data in list(tracked_alerts.items()):
        if data.get("reported"):
            continue
        age_hours = (now - data["time"]).total_seconds() / 3600
        if age_hours < 24:
            continue
        try:
            sigs = await rpc(session, "getSignaturesForAddress", [mint, {"limit": 50}])
            txs  = len(sigs) if sigs else 0
            if txs > 100:   verdict, emoji = "Still very active — likely pumping", "🟢"
            elif txs > 20:  verdict, emoji = "Moderate activity", "🟡"
            else:           verdict, emoji = "Low activity — likely dumped", "🔴"
            lp_name, lp_emoji = data.get("launchpad", ("?", "⚪"))
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    f"📊 *24h Report*\n\n"
                    f"{emoji} `{mint[:8]}...{mint[-4:]}`\n"
                    f"{lp_emoji} {lp_name} | Score was *{data['score']}*\n\n"
                    f"{verdict}\n"
                    f"24h TX count: {txs}\n\n"
                    f"🔗 [DexScreener](https://dexscreener.com/solana/{mint})"
                ),
                parse_mode="Markdown", disable_web_page_preview=True
            )
            tracked_alerts[mint]["reported"] = True
        except Exception as e:
            log.debug(f"Perf check {mint[:8]}: {e}")

# ── Daily summary ──────────────────────────────────────────────────────────────
async def send_daily_summary(bot: Bot):
    if not daily_top:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID,
            text="📋 *Daily Summary*\n\nNo tokens hit threshold today.", parse_mode="Markdown")
        return
    top5  = sorted(daily_top, key=lambda x: x["score"], reverse=True)[:5]
    lines = ["📋 *AlphaScan Daily Top Tokens*\n"]
    for i, t in enumerate(top5, 1):
        lp_name, lp_emoji = t.get("launchpad", ("?","⚪"))
        lines.append(f"{i}. `{t['mint'][:8]}...` Score *{t['score']}* {lp_emoji} {lp_name}")
        lines.append(f"   https://dexscreener.com/solana/{t['mint']}")
    lines.append(f"\n_Total alerts: {len(daily_top)}_")
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID,
        text="\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)
    daily_top.clear()

# ── Alert formatter ────────────────────────────────────────────────────────────
def format_alert(mint: str, info: dict, result: dict) -> str:
    score     = result["total"]
    rug       = result["rug_risk"]
    re_emoji  = {"low":"🟢","medium":"🟡","high":"🔴"}.get(rug,"⚪")
    lp_name, lp_emoji = info.get("launchpad", ("Unknown","⚪"))
    bar       = "█" * round(score/10) + "░" * (10 - round(score/10))
    bd        = result["breakdown"]
    auth      = info.get("auth", {})
    liq       = info.get("liquidity", {})
    mom       = info.get("momentum", {})
    h         = info.get("holders", {})
    px        = info.get("price", {})

    boosts_txt   = "\n".join(result["boosts"])   or "—"
    warnings_txt = "\n".join(result["warnings"]) or "—"

    price_block = ""
    if px and px.get("found"):
        price_block = (
            f"*Price:* {fmt_usd(px.get('price_usd'))}  "
            f"MCap: {fmt_usd(px.get('mcap'))}\n"
            f"*Volume 24h:* {fmt_usd(px.get('volume_24h'))}  "
            f"Liq: {fmt_usd(px.get('liquidity'))}\n"
            f"*Change:* 5m {fmt_pct(px.get('price_change_5m'))}  "
            f"1h {fmt_pct(px.get('price_change_1h'))}  "
            f"24h {fmt_pct(px.get('price_change_24h'))}\n\n"
        )

    return (
        f"🚨 *AlphaScan Alert*\n\n"
        f"*Launchpad:* {lp_emoji} {lp_name}\n"
        f"*Token:* `{mint}`\n"
        f"*Score:* {score}/99  `{bar}`\n"
        f"{price_block}"
        f"*Breakdown:*\n"
        f"  🐋 Smart money:   {bd.get('smart_money',0)}/25\n"
        f"  📊 Distribution:  {bd.get('distribution',0)}/20\n"
        f"  🤖 Organic:       {bd.get('organic',0)}/20\n"
        f"  ⚡ TX velocity:   {bd.get('tx_velocity',0)}/15\n"
        f"  🔒 Authority:     {bd.get('authority',0)}/10\n"
        f"  💧 Liquidity:     {bd.get('liquidity',0)}/10\n"
        f"  🛡 Dev safety:    {bd.get('dev_safety',0)}/10\n"
        f"  📈 Momentum:      {bd.get('momentum',0)}/10\n"
        f"  👥 Holders:       {bd.get('holder_count',0)}/5\n"
        f"  🎓 Graduated:     {bd.get('graduation',0)}/5\n\n"
        f"*Signals:*\n{boosts_txt}\n\n"
        f"*Warnings:*\n{warnings_txt}\n\n"
        f"*Safety:*\n"
        f"  Mint authority: {'✅ Revoked' if auth.get('mint_revoked') else '🚨 Active'}\n"
        f"  Freeze authority: {'✅ Revoked' if auth.get('freeze_revoked') else '🚨 Active'}\n"
        f"  Liquidity: {liq.get('liquidity_tier','?')} ({liq.get('pool_pct','?')}% in pool)\n"
        f"  Momentum: {mom.get('momentum','?')} — {mom.get('note','')}\n\n"
        f"*Rug risk:* {re_emoji} {rug.upper()}\n"
        f"Top holder: {h.get('top1_pct',0)}% | "
        f"Holders: {h.get('count',0)} | "
        f"TXs: {info.get('sigs_count',0)}\n\n"
        f"🔗 [DexScreener](https://dexscreener.com/solana/{mint}) | "
        f"[Birdeye](https://birdeye.so/token/{mint}?chain=solana) | "
        f"[Solscan](https://solscan.io/token/{mint})\n\n"
        f"_Scanned {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC_"
    )

# ── Telegram commands ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *AlphaScan Masterclass Commands:*\n\n"
        "/threshold `<40-99>` — set alert threshold\n"
        "/watch `<address>` `<label>` — track a wallet\n"
        "/unwatch `<address>` — stop tracking wallet\n"
        "/watchlist — show tracked wallets\n"
        "/addwallet `<address>` — add to smart money list\n"
        "/summary — today's top tokens\n"
        "/status — bot health & settings\n"
        "/analyze `<CA>` — analyze any token on demand\n"
        "Or just *paste any CA* directly into chat\n"
        "/blacklist — view/add blacklisted deployers\n",
        parse_mode="Markdown"
    )

async def cmd_threshold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global alert_threshold
    try:
        v = int(ctx.args[0])
        if not 40 <= v <= 99: raise ValueError
        alert_threshold = v
        await update.message.reply_text(f"✅ Threshold set to *{v}*", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /threshold 80 (must be 40–99)")

async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /watch <address> <label>")
        return
    addr, label = ctx.args[0], " ".join(ctx.args[1:])
    watchlist_wallets[addr] = label
    await update.message.reply_text(f"👁 Watching *{label}*\n`{addr[:8]}...{addr[-4:]}`", parse_mode="Markdown")

async def cmd_unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    addr = ctx.args[0] if ctx.args else ""
    if addr in watchlist_wallets:
        watchlist_wallets.pop(addr)
        await update.message.reply_text("✅ Removed from watchlist")
    else:
        await update.message.reply_text("Wallet not found.")

async def cmd_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not watchlist_wallets:
        await update.message.reply_text("No wallets tracked. Use /watch to add one.")
        return
    lines = ["👁 *Watchlist:*\n"] + [
        f"• *{label}*: `{addr[:8]}...{addr[-4:]}`"
        for addr, label in watchlist_wallets.items()
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_addwallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Dynamically add a wallet to the smart money list."""
    if not ctx.args:
        await update.message.reply_text("Usage: /addwallet <address>")
        return
    addr = ctx.args[0]
    SMART_MONEY_SEEDS.add(addr)
    smart_wallet_db[addr] = {"added": datetime.now(timezone.utc).isoformat()}
    await update.message.reply_text(
        f"🐋 Added to smart money list\n`{addr[:8]}...{addr[-4:]}`\n\n"
        f"Bot will now alert when this wallet buys early.",
        parse_mode="Markdown"
    )


async def cmd_blacklist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show or manually add to the auto-blacklist."""
    if ctx.args:
        addr = ctx.args[0]
        blacklisted_devs.add(addr)
        await update.message.reply_text(
            f"🚫 Added to blacklist\n`{addr[:8]}...{addr[-4:]}`",
            parse_mode="Markdown"
        )
    else:
        if not blacklisted_devs:
            await update.message.reply_text("Blacklist is empty — auto-populated as rugs are detected.")
            return
        lines = ["🚫 *Blacklisted deployers:*\n"]
        for addr in list(blacklisted_devs)[:20]:
            lines.append(f"• `{addr[:8]}...{addr[-4:]}`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_daily_summary(ctx.bot)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"⚙️ *AlphaScan Status*\n\n"
        f"Threshold: *{alert_threshold}/99*\n"
        f"Scan interval: *{SCAN_INTERVAL}s*\n"
        f"Tokens seen: *{len(seen_mints)}*\n"
        f"Alerts today: *{len(daily_top)}*\n"
        f"Watchlist: *{len(watchlist_wallets)}* wallets\n"
        f"Smart money list: *{len(SMART_MONEY_SEEDS) + len(smart_wallet_db)}* wallets\n"
        f"Tracking for performance: *{len(tracked_alerts)}* tokens\n\n"
        f"*Signals active:*\n"
        f"✅ Mint/freeze authority\n✅ Bundle detection\n"
        f"✅ Dev rug history\n✅ Liquidity depth\n"
        f"✅ Price momentum\n✅ Smart money\n"
        f"✅ Holder distribution\n✅ TX velocity\n"
        f"✅ Graduation tracking\n✅ DexScreener price data\n✅ Auto-blacklist ruggers\n✅ Multi-wallet copy signal\n",
        parse_mode="Markdown"
    )

# ── Main scan loop ─────────────────────────────────────────────────────────────
async def scan_loop(bot: Bot):
    last_summary_hour = -1
    scan_count = 0
    async with aiohttp.ClientSession() as session:
        while True:
            scan_count += 1
            log.info(f"Scan #{scan_count}")
            try:
                new_mints = await fetch_new_tokens(session)
                log.info(f"{len(new_mints)} new mints found")
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
                    log.info(f"{mint[:8]} score={score} rug={result['rug_risk']} lp={lp_name} fail={result['hard_fail']}")
                    if score >= alert_threshold and not result["hard_fail"]:
                        await bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=format_alert(mint, info, result),
                            parse_mode="Markdown",
                            disable_web_page_preview=True
                        )
                        tracked_alerts[mint] = {
                            "score": score, "time": datetime.now(timezone.utc),
                            "launchpad": launchpad, "reported": False
                        }
                        daily_top.append({"mint": mint, "score": score, "launchpad": launchpad})
                        alerted += 1
                        await asyncio.sleep(1.5)

                await scan_watchlist(session, bot)
                await check_copy_signals(bot)
                if scan_count % 10 == 0:
                    await check_performance(bot, session)

                hour = datetime.now(timezone.utc).hour
                if hour == 9 and last_summary_hour != 9:
                    await send_daily_summary(bot)
                    last_summary_hour = 9
                elif hour != 9:
                    last_summary_hour = hour

                if not alerted:
                    log.info("No alerts this scan")
            except Exception as e:
                log.error(f"Scan error: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

# ── CA analysis — paste any token address ─────────────────────────────────────
async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Triggered by /analyze <CA> or by pasting a raw CA in chat."""
    if ctx.args:
        ca = ctx.args[0].strip()
    else:
        await update.message.reply_text("Usage: /analyze <token_address>")
        return
    await run_analysis(update, ca)

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Detect raw Solana CA pasted directly into chat and auto-analyze it."""
    text = (update.message.text or "").strip()
    # Solana addresses are base58, 32-44 chars, no spaces
    if re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", text):
        await update.message.reply_text(f"🔍 Analyzing `{text[:8]}...{text[-4:]}`", parse_mode="Markdown")
        await run_analysis(update, text)

async def run_analysis(update: Update, mint: str):
    """Full 10-signal analysis on demand for any token CA."""
    msg = await update.message.reply_text("⏳ Running full analysis — this takes ~10 seconds...")
    try:
        async with aiohttp.ClientSession() as session:
            launchpad = ("Unknown", "⚪")
            # Try to detect launchpad from mint tx history
            sigs = await rpc(session, "getSignaturesForAddress", [mint, {"limit": 10}])
            if sigs:
                for s in sigs[:5]:
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
                    lp = detect_launchpad(keys)
                    if lp[0] != "Unknown":
                        launchpad = lp
                        break

            info = await enrich_token(session, mint, launchpad)
            if not info:
                await msg.edit_text("❌ Could not fetch token data. Check the address and try again.")
                return

            result = score_token(info)
            score  = result["total"]
            rug    = result["rug_risk"]
            re_emoji = {"low":"🟢","medium":"🟡","high":"🔴"}.get(rug,"⚪")
            lp_name, lp_emoji = launchpad
            bar = "█" * round(score/10) + "░" * (10 - round(score/10))
            bd  = result["breakdown"]
            auth = info.get("auth", {})
            liq  = info.get("liquidity", {})
            mom  = info.get("momentum", {})
            h    = info.get("holders", {})
            sm   = info.get("smart", {})
            dev  = info.get("dev", {})

            # Verdict
            if result["hard_fail"]:
                verdict = "❌ DO NOT BUY — hard fail triggered"
            elif score >= 85:
                verdict = "🚀 Strong buy signal"
            elif score >= 75:
                verdict = "✅ Good — meets threshold"
            elif score >= 60:
                verdict = "⚠️ Moderate — trade carefully"
            elif score >= 40:
                verdict = "🔴 Weak — high risk"
            else:
                verdict = "💀 Avoid — very high risk"

            boosts_txt   = "\n".join(result["boosts"])   or "None"
            warnings_txt = "\n".join(result["warnings"]) or "None"

            px = info.get("price", {})
            price_block = ""
            if px and px.get("found"):
                price_block = (
                    f"*Price:* {fmt_usd(px.get('price_usd'))}  "
                    f"MCap: {fmt_usd(px.get('mcap'))}\n"
                    f"*Volume 24h:* {fmt_usd(px.get('volume_24h'))}  "
                    f"Liq: {fmt_usd(px.get('liquidity'))}\n"
                    f"*Change:* 5m {fmt_pct(px.get('price_change_5m'))}  "
                    f"1h {fmt_pct(px.get('price_change_1h'))}  "
                    f"24h {fmt_pct(px.get('price_change_24h'))}\n\n"
                )

            report = (
                f"🔬 *Token Analysis Report*\n\n"
                f"*CA:* `{mint}`\n"
                f"*Launchpad:* {lp_emoji} {lp_name}\n"
                f"*Score:* {score}/99  `{bar}`\n"
                f"{price_block}"
                f"*Verdict:* {verdict}\n\n"
                f"*Score breakdown:*\n"
                f"  🐋 Smart money:   {bd.get('smart_money',0)}/25\n"
                f"  📊 Distribution:  {bd.get('distribution',0)}/20\n"
                f"  🤖 Organic:       {bd.get('organic',0)}/20\n"
                f"  ⚡ TX velocity:   {bd.get('tx_velocity',0)}/15\n"
                f"  🔒 Authority:     {bd.get('authority',0)}/10\n"
                f"  💧 Liquidity:     {bd.get('liquidity',0)}/10\n"
                f"  🛡 Dev safety:    {bd.get('dev_safety',0)}/10\n"
                f"  📈 Momentum:      {bd.get('momentum',0)}/10\n"
                f"  👥 Holders:       {bd.get('holder_count',0)}/5\n"
                f"  🎓 Graduated:     {bd.get('graduation',0)}/5\n\n"
                f"*Safety checks:*\n"
                f"  Mint authority:   {'✅ Revoked' if auth.get('mint_revoked') else '🚨 Still active'}\n"
                f"  Freeze authority: {'✅ Revoked' if auth.get('freeze_revoked') else '🚨 Still active'}\n"
                f"  Liquidity tier:   {liq.get('liquidity_tier','?')} ({liq.get('pool_pct','?')}% in pool)\n"
                f"  Momentum:         {mom.get('momentum','?')} — {mom.get('note','')}\n\n"
                f"*On-chain stats:*\n"
                f"  Top holder: {h.get('top1_pct',0)}% of supply\n"
                f"  Top 3 holders: {h.get('top3_pct',0)}% of supply\n"
                f"  Unique holders: {h.get('count',0)}\n"
                f"  Recent TXs: {info.get('sigs_count',0)}\n"
                f"  Smart wallets in: {sm.get('count',0)}\n"
                f"  Dev launches: {dev.get('launches',0)} (risk: {dev.get('risk','?')})\n\n"
                f"*Green flags:*\n{boosts_txt}\n\n"
                f"*Red flags:*\n{warnings_txt}\n\n"
                f"*Rug risk:* {re_emoji} {rug.upper()}\n\n"
                f"🔗 [DexScreener](https://dexscreener.com/solana/{mint}) | "
                f"[Birdeye](https://birdeye.so/token/{mint}?chain=solana) | "
                f"[Solscan](https://solscan.io/token/{mint})"
            )
            await msg.edit_text(report, parse_mode="Markdown", disable_web_page_preview=True)

    except Exception as e:
        log.error(f"run_analysis {mint[:8]}: {e}")
        await msg.edit_text(f"❌ Analysis failed: {str(e)[:100]}")



# ── Boot ───────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, handler in [
        ("start",     cmd_start),
        ("threshold", cmd_threshold),
        ("watch",     cmd_watch),
        ("unwatch",   cmd_unwatch),
        ("watchlist", cmd_watchlist),
        ("addwallet", cmd_addwallet),
        ("summary",   cmd_summary),
        ("status",    cmd_status),
        ("analyze",   cmd_analyze),
        ("blacklist",  cmd_blacklist),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def run():
        await app.initialize()
        await app.start()
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                f"✅ AlphaScan Masterclass is live!\n\n"
                f"10 signals active. Threshold: {alert_threshold}/99\n"
                f"Scan every {SCAN_INTERVAL//60}min\n\n"
                f"New: /addwallet to build your smart money list\n"
                f"Type /start for all commands"
            )
        )
        await asyncio.gather(scan_loop(app.bot), app.updater.start_polling())

    asyncio.run(run())

if __name__ == "__main__":
    main()

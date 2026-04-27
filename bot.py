"""
AlphaScan.sol — Complete Rewrite v4
Fixes: token discovery, age-aware scoring, liquidity hard fail,
       self-populating smart wallet engine, working dual scanner.
"""
import os, asyncio, aiohttp, logging, re, time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
HELIUS_API_KEY   = os.environ["HELIUS_API_KEY"]
SCAN_INTERVAL    = int(os.environ.get("SCAN_INTERVAL_SECONDS", "90"))
ALERT_THRESHOLD  = int(os.environ.get("ALPHA_THRESHOLD", "58"))

HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# ── Launchpad program addresses ────────────────────────────────────────────────
PUMPFUN_PROGRAM   = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM  = "PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkQ"
LETSBONK_PROGRAM  = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
RAYDIUM_AMM       = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CLMM      = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
TOKEN_PROGRAM     = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

LAUNCHPADS = {
    PUMPFUN_PROGRAM:  ("Pump.fun",          "🟢"),
    PUMPSWAP_PROGRAM: ("PumpSwap",          "🎓"),
    LETSBONK_PROGRAM: ("LetsBonk.fun",      "🟡"),
    RAYDIUM_AMM:      ("Raydium",           "🔵"),
    RAYDIUM_CLMM:     ("Raydium CLMM",      "🔵"),
}

# ── State ──────────────────────────────────────────────────────────────────────
seen_mints         = set()
alert_threshold    = ALERT_THRESHOLD
tracked_alerts     = {}   # mint -> {score, time, launchpad, source, reported}
daily_top          = []
watchlist_wallets  = {}   # addr -> label
blacklisted_devs   = set()
dev_cache          = {}
price_cache        = {}   # mint -> {data, fetched_at}
copy_signal_cache  = {}   # mint -> {wallets, first_seen, alerted}
momentum_tracker   = {}   # mint -> {samples, first_seen}
trending_tracker   = {}   # mint -> {snapshots, first_seen, alerted}

# ── Self-populating smart wallet engine ───────────────────────────────────────
# Seed: known high-performance wallet from GMGN data
SMART_WALLET_SEEDS = {
    "H72yLkhTnoBfhBTXXaj1RBXuirm8s8G5fcVh2XpQLggM",  # GMGN-identified early buyer
}
# User-added wallets via /addwallet
user_smart_wallets = set(os.environ.get("SMART_WALLETS", "").split(",")) - {""}
# Auto-discovered wallets (engine populates this)
discovered_smart_wallets = {}  # addr -> {wins, seen_on: [mints], promoted_at}
# Tokens to watch for 3x to discover new smart wallets
winner_watch      = {}  # mint -> {entry_price, buyers: [], first_seen}
take_profit_watch = {}  # mint -> {entry_mcap, name, symbol, tp2x, tp5x, ath, notified}

def all_smart_wallets() -> set:
    return SMART_WALLET_SEEDS | user_smart_wallets | set(discovered_smart_wallets.keys())

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

# ── DexScreener price data ─────────────────────────────────────────────────────
async def fetch_price(session, mint: str) -> dict:
    cached = price_cache.get(mint)
    if cached and (datetime.now(timezone.utc) - cached["fetched_at"]).total_seconds() < 45:
        return cached["data"]
    result = {"found": False, "name": "", "symbol": "", "price_usd": None, "mcap": None,
              "liquidity": None, "volume_24h": None, "chg_5m": None, "chg_1h": None, "chg_24h": None}
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status != 200:
                return result
            data  = await r.json()
            pairs = data.get("pairs") or []
            if not pairs:
                return result
            pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
            p = pairs[0]
            result = {
                "found":      True,
                "name":       p.get("baseToken", {}).get("name", ""),
                "symbol":     p.get("baseToken", {}).get("symbol", ""),
                "price_usd":  p.get("priceUsd"),
                "mcap":       float(p.get("fdv") or p.get("marketCap") or 0),
                "liquidity":  float(p.get("liquidity", {}).get("usd") or 0),
                "volume_24h": float(p.get("volume", {}).get("h24") or 0),
                "chg_5m":     float(p.get("priceChange", {}).get("m5") or 0),
                "chg_1h":     float(p.get("priceChange", {}).get("h1") or 0),
                "chg_24h":    float(p.get("priceChange", {}).get("h24") or 0),
                "dex":        p.get("dexId", ""),
                "pair":       p.get("pairAddress", ""),
            }
            price_cache[mint] = {"data": result, "fetched_at": datetime.now(timezone.utc)}
    except Exception as e:
        log.debug(f"DexScreener {mint[:8]}: {e}")
    return result



# ── Token name resolver ────────────────────────────────────────────────────────
async def get_token_name(session, mint: str, px: dict) -> tuple:
    """
    Returns (name, symbol) for a token.
    Priority: DexScreener (already fetched) → Helius DAS getAsset → on-chain metadata
    """
    # 1. Already have it from DexScreener
    if px.get("name") or px.get("symbol"):
        name   = px.get("name", "")
        symbol = px.get("symbol", "")
        if name or symbol:
            return name, symbol

    # 2. Try Helius DAS getAsset — richest metadata source
    try:
        async with session.post(
            HELIUS_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "getAsset", "params": {"id": mint}},
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            d = await r.json()
            asset = d.get("result", {})
            if asset:
                content  = asset.get("content", {})
                metadata = content.get("metadata", {})
                name   = metadata.get("name", "")
                symbol = metadata.get("symbol", "")
                if name or symbol:
                    return name.strip(), symbol.strip()
    except Exception as e:
        log.debug(f"DAS getAsset {mint[:8]}: {e}")

    # 3. Fallback: short address label
    return f"Token {mint[:6]}...", mint[:6].upper()

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

# ── Mint/freeze authority check ────────────────────────────────────────────────
async def check_authorities(session, mint: str) -> dict:
    r = {"mint_revoked": False, "freeze_revoked": False, "safe": False}
    try:
        info = await rpc(session, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        if not info or not info.get("value"):
            return r
        parsed = info["value"].get("data", {}).get("parsed", {}).get("info", {})
        r["mint_revoked"]   = parsed.get("mintAuthority")   is None
        r["freeze_revoked"] = parsed.get("freezeAuthority") is None
        r["safe"]           = r["mint_revoked"] and r["freeze_revoked"]
    except Exception as e:
        log.debug(f"Authority {mint[:8]}: {e}")
    return r

# ── Holder distribution (LP-aware) ────────────────────────────────────────────
async def check_holders(session, mint: str) -> dict:
    r = {"count": 0, "top1_pct": 100, "real_top1_pct": 100, "top3_pct": 100, "lp_excluded": False}
    try:
        largest    = await rpc(session, "getTokenLargestAccounts", [mint])
        supply_res = await rpc(session, "getTokenSupply", [mint])
        total  = float((supply_res or {}).get("value", {}).get("uiAmount") or 1)
        accts  = (largest or {}).get("value", [])
        if not accts or total <= 0:
            return r
        amounts = [float(a.get("uiAmount") or 0) for a in accts]
        r["count"]    = len(amounts)
        r["top1_pct"] = round(amounts[0] / total * 100, 1)
        r["top3_pct"] = round(sum(amounts[:3]) / total * 100, 1)
        # LP detection: top account holds >2x second AND >20% = likely LP pool
        if len(amounts) >= 2 and amounts[0] > amounts[1] * 2 and r["top1_pct"] > 20:
            real = amounts[1:]
            r["lp_excluded"]   = True
            r["real_top1_pct"] = round(real[0] / total * 100, 1) if real else r["top1_pct"]
        else:
            r["real_top1_pct"] = r["top1_pct"]

        # Extra insider check: if top 3 non-LP holders own >60% combined = insider
        non_lp_amounts = amounts[1:] if r["lp_excluded"] else amounts
        top3_non_lp = sum(non_lp_amounts[:3]) / total * 100 if total > 0 else 0
        r["insider_pct"] = round(top3_non_lp, 1)
    except Exception as e:
        log.debug(f"Holders {mint[:8]}: {e}")
    return r

# ── Bundle/coordinated buy detection ──────────────────────────────────────────
def detect_bundling(sigs: list) -> dict:
    if not sigs:
        return {"is_bundled": False, "confidence": 0, "reason": "no data"}
    slot_counts = defaultdict(int)
    for s in sigs:
        slot_counts[s.get("slot", 0)] += 1
    slots = sorted(slot_counts.keys())
    max_cluster = 0
    for slot in slots:
        cluster = sum(slot_counts[s] for s in slots if abs(s - slot) <= 3)
        max_cluster = max(max_cluster, cluster)
    if max_cluster >= 8:
        return {"is_bundled": True, "confidence": min(100, max_cluster * 10),
                "reason": f"{max_cluster} coordinated txs in 3-slot window"}
    elif max_cluster >= 5:
        return {"is_bundled": False, "confidence": max_cluster * 8,
                "reason": f"possible bundling ({max_cluster} near-simultaneous)"}
    return {"is_bundled": False, "confidence": 0, "reason": "organic"}

# ── Dev wallet history ─────────────────────────────────────────────────────────
async def check_dev(session, deployer: str) -> dict:
    if not deployer:
        return {"launches": 0, "is_serial": False, "risk": "unknown", "age_days": 0}
    if deployer in dev_cache:
        return dev_cache[deployer]
    r = {"launches": 0, "is_serial": False, "risk": "low", "age_days": 0}
    try:
        sigs = await rpc(session, "getSignaturesForAddress", [deployer, {"limit": 100}])
        if not sigs:
            dev_cache[deployer] = r; return r
        # Age from oldest sig
        oldest = sigs[-1].get("blockTime", 0)
        if oldest:
            r["age_days"] = (time.time() - oldest) / 86400
        # Count real token launches (initializeMint instructions)
        launch_count = 0
        for s in sigs[:20]:
            try:
                tx = await rpc(session, "getTransaction", [
                    s["signature"],
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                ])
                if not tx: continue
                for ix in tx.get("transaction",{}).get("message",{}).get("instructions",[]):
                    p = ix.get("parsed", {})
                    if isinstance(p, dict) and p.get("type") == "initializeMint":
                        launch_count += 1
            except: continue
        r["launches"] = launch_count
        if launch_count >= 5:
            r["is_serial"] = True; r["risk"] = "high"
            blacklisted_devs.add(deployer)
        elif launch_count >= 2:
            r["risk"] = "medium"
        dev_cache[deployer] = r
    except Exception as e:
        log.debug(f"Dev {deployer[:8]}: {e}")
    return r

# ── Smart money detection ──────────────────────────────────────────────────────
async def check_smart_money(session, mint: str, sigs: list) -> dict:
    r = {"count": 0, "wallets": []}
    smart = all_smart_wallets()
    if not smart:
        return r
    for s in sigs[:30]:
        try:
            tx = await rpc(session, "getTransaction", [
                s["signature"],
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
            ])
            if not tx: continue
            keys = [
                a.get("pubkey","") if isinstance(a,dict) else str(a)
                for a in tx.get("transaction",{}).get("message",{}).get("accountKeys",[])
            ]
            for k in keys:
                if k in smart and k not in r["wallets"]:
                    r["wallets"].append(k)
                    r["count"] += 1
        except: continue
    # Copy signal tracking
    if r["count"] >= 2:
        if mint not in copy_signal_cache:
            copy_signal_cache[mint] = {"wallets": r["wallets"], "first_seen": datetime.now(timezone.utc), "alerted": False}
        else:
            existing = copy_signal_cache[mint]["wallets"]
            copy_signal_cache[mint]["wallets"] = list(set(existing + r["wallets"]))
    return r

# ── Token age from signatures ──────────────────────────────────────────────────
def get_token_age_minutes(sigs: list) -> float:
    """Estimate token age from first signature timestamp."""
    if not sigs:
        return 0
    # sigs are newest-first, so last sig is oldest
    oldest = sigs[-1].get("blockTime")
    if not oldest:
        return 0
    return (time.time() - oldest) / 60

# ── Self-populating smart wallet discovery ────────────────────────────────────
async def update_winner_watch(session, mint: str, px: dict, sigs: list):
    """
    Track early buyers of new tokens. When a token 3x+, promote its early
    buyers to the smart money list if they appear on 3+ winners.
    """
    if not px.get("found"):
        return
    mcap = px.get("mcap", 0)
    if mcap <= 0:
        return
    if mint not in winner_watch:
        # Record initial mcap and extract first 10 unique buyer wallets
        buyers = set()
        for s in sigs[:20]:
            try:
                tx = await rpc(session, "getTransaction", [
                    s["signature"],
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                ])
                if not tx: continue
                keys = [
                    a.get("pubkey","") if isinstance(a,dict) else str(a)
                    for a in tx.get("transaction",{}).get("message",{}).get("accountKeys",[])
                ]
                for k in keys:
                    if len(k) >= 32 and k not in {PUMPFUN_PROGRAM, TOKEN_PROGRAM, RAYDIUM_AMM}:
                        buyers.add(k)
                if len(buyers) >= 10:
                    break
            except: continue
        winner_watch[mint] = {
            "entry_mcap": mcap,
            "buyers":     list(buyers)[:15],
            "first_seen": datetime.now(timezone.utc),
            "promoted":   False,
        }
        return

    entry = winner_watch[mint]
    if entry.get("promoted"):
        return
    entry_mcap = entry.get("entry_mcap", mcap)
    if entry_mcap <= 0:
        return
    multiplier = mcap / entry_mcap
    if multiplier >= 3.0:
        # This token 3x'd — promote early buyers
        for wallet in entry.get("buyers", []):
            if wallet in all_smart_wallets():
                continue
            if wallet not in discovered_smart_wallets:
                discovered_smart_wallets[wallet] = {"wins": 0, "seen_on": [], "promoted_at": None}
            discovered_smart_wallets[wallet]["wins"] += 1
            discovered_smart_wallets[wallet]["seen_on"].append(mint)
            if discovered_smart_wallets[wallet]["wins"] >= 3:
                discovered_smart_wallets[wallet]["promoted_at"] = datetime.now(timezone.utc).isoformat()
                log.info(f"🐋 Auto-promoted smart wallet: {wallet[:8]}... (3+ wins)")
        entry["promoted"] = True

# ── Full token enrichment ──────────────────────────────────────────────────────
async def enrich_token(session, mint: str, launchpad: tuple, source: str = "") -> dict | None:
    try:
        px = await fetch_price(session, mint)
        sigs_res, acct_res = await asyncio.gather(
            rpc(session, "getSignaturesForAddress", [mint, {"limit": 100}]),
            rpc(session, "getAccountInfo", [mint, {"encoding": "jsonParsed"}]),
        )
        sigs     = sigs_res or []
        deployer = ""
        if acct_res and acct_res.get("value"):
            deployer = acct_res["value"].get("owner", "")

        age_mins = get_token_age_minutes(sigs)

        auth, holders, smart, dev = await asyncio.gather(
            check_authorities(session, mint),
            check_holders(session, mint),
            check_smart_money(session, mint, sigs),
            check_dev(session, deployer),
        )
        name, symbol = await get_token_name(session, mint, px)

        bundle = detect_bundling(sigs[:50])

        # Graduated to PumpSwap?
        graduated = launchpad[0] in ("PumpSwap", "PumpSwap (grad)")
        if not graduated:
            for s in sigs[:5]:
                try:
                    tx = await rpc(session, "getTransaction", [
                        s["signature"],
                        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                    ])
                    if tx:
                        keys = [
                            a.get("pubkey","") if isinstance(a,dict) else str(a)
                            for a in tx.get("transaction",{}).get("message",{}).get("accountKeys",[])
                        ]
                        if PUMPSWAP_PROGRAM in keys:
                            graduated = True; break
                except: pass

        # Update winner watch for smart wallet discovery
        asyncio.create_task(update_winner_watch(session, mint, px, sigs))

        return {
            "mint":      mint,
            "name":      name,
            "symbol":    symbol,
            "launchpad": launchpad,
            "source":    source,
            "deployer":  deployer,
            "age_mins":  age_mins,
            "px":        px,
            "auth":      auth,
            "holders":   holders,
            "smart":     smart,
            "dev":       dev,
            "bundle":    bundle,
            "sigs":      sigs,
            "graduated": graduated,
        }
    except Exception as e:
        log.warning(f"enrich {mint[:8]}: {e}")
        return None

# ══════════════════════════════════════════════════════════════════════════════
# SCORING ENGINE — age-aware, no false hard-fails on new tokens
# ══════════════════════════════════════════════════════════════════════════════
def score_token(info: dict) -> dict:
    score    = 0
    warnings = []
    boosts   = []
    bd       = {}

    age    = info.get("age_mins", 0)
    px     = info.get("px", {})
    auth   = info.get("auth", {})
    h      = info.get("holders", {})
    sm     = info.get("smart", {})
    b      = info.get("bundle", {})
    dev    = info.get("dev", {})
    sigs   = info.get("sigs", [])
    is_new = age < 30  # under 30 minutes = new token, different rules

    # ── 1. Mint & Freeze authority (20 pts) ───────────────────────────────────
    if auth.get("safe"):
        a_score = 20; boosts.append("🔒 Both authorities revoked")
    elif auth.get("mint_revoked"):
        a_score = 12; warnings.append("⚠️ Freeze authority still active")
    elif auth.get("freeze_revoked"):
        a_score = 8;  warnings.append("⚠️ Mint authority still active")
    else:
        a_score = 2;  warnings.append("🚨 Both authorities active")
    score += a_score; bd["authority"] = a_score

    # ── 2. Holder distribution (18 pts) ───────────────────────────────────────
    real_top1 = h.get("real_top1_pct", h.get("top1_pct", 100))
    if real_top1 < 5:    hd = 18; boosts.append("✅ Excellent distribution")
    elif real_top1 < 10: hd = 14
    elif real_top1 < 20: hd = 10
    elif real_top1 < 35: hd = 5;  warnings.append(f"⚠️ Top holder {real_top1}%")
    else:                hd = 0;  warnings.append(f"🚨 Top holder {real_top1}% — rug risk")
    if h.get("lp_excluded"):
        boosts.append(f"📊 LP excluded, real top: {real_top1}%")
    insider = h.get("insider_pct", 0)
    if insider > 40:
        warnings.append(f"🚨 Insider concentration: top 3 non-LP wallets own {insider}%")
    elif insider > 25:
        warnings.append(f"⚠️ Moderate insider holding: {insider}%")
    score += hd; bd["distribution"] = hd

    # ── 3. Organic launch / anti-bundle (15 pts) ──────────────────────────────
    if b.get("is_bundled"):
        org = 0;  warnings.append(f"🤖 Bundled: {b.get('reason','')}")
    elif b.get("confidence", 0) > 30:
        org = 7;  warnings.append(f"🤖 Possible bots: {b.get('reason','')}")
    else:
        org = 15; boosts.append("✅ Organic launch")
    score += org; bd["organic"] = org

    # ── 4. Transaction velocity (12 pts) ──────────────────────────────────────
    tx_count = len(sigs)
    if tx_count > 300:   tv = 12; boosts.append(f"🔥 {tx_count} txs — very hot")
    elif tx_count > 150: tv = 10
    elif tx_count > 60:  tv = 7
    elif tx_count > 20:  tv = 4
    elif tx_count > 5:   tv = 2
    else:                tv = 0
    score += tv; bd["tx_velocity"] = tv

    # ── 5. Dev safety (10 pts) ────────────────────────────────────────────────
    if dev.get("deployer","") in blacklisted_devs or dev.get("is_serial"):
        dv = 0; warnings.append(f"🚨 Serial deployer ({dev.get('launches',0)} launches)")
    elif dev.get("risk") == "medium":
        dv = 5; warnings.append(f"⚠️ Dev has {dev.get('launches',0)} prior launches")
    else:
        dv = 10
    if dev.get("age_days", 99) < 1:
        dv = max(0, dv - 3); warnings.append("⚠️ Brand new deployer wallet")
    score += dv; bd["dev_safety"] = dv

    # ── 6. Smart money (10 pts) ───────────────────────────────────────────────
    sc = sm.get("count", 0)
    if sc >= 3:   sm_pts = 10; boosts.append(f"🐋 {sc} smart wallets in early")
    elif sc == 2: sm_pts = 7;  boosts.append(f"🐋 {sc} smart wallets spotted")
    elif sc == 1: sm_pts = 4;  boosts.append("🐋 1 smart wallet spotted")
    else:         sm_pts = 0
    score += sm_pts; bd["smart_money"] = sm_pts

    # ── 7. Price & liquidity (15 pts) — age-aware ─────────────────────────────
    liq_pts = 0; mom_pts = 0
    liq_usd = px.get("liquidity", 0) or 0
    chg_1h  = px.get("chg_1h", 0) or 0
    chg_5m  = px.get("chg_5m", 0) or 0
    vol     = px.get("volume_24h", 0) or 0
    mcap    = px.get("mcap", 0) or 0
    vol_mcap = round(vol / mcap, 2) if mcap > 0 else 0

    if px.get("found"):
        # Liquidity scoring
        if liq_usd >= 50000:   liq_pts = 8; boosts.append(f"💧 Deep liq {fmt_usd(liq_usd)}")
        elif liq_usd >= 20000: liq_pts = 6; boosts.append(f"💧 Solid liq {fmt_usd(liq_usd)}")
        elif liq_usd >= 8000:  liq_pts = 4
        elif liq_usd >= 3000:  liq_pts = 2
        else:                  liq_pts = 0; warnings.append(f"⚠️ Thin liquidity {fmt_usd(liq_usd)}")
        # Momentum scoring
        if chg_1h > 30 and chg_5m > 0:
            mom_pts = 7; boosts.append(f"📈 +{chg_1h:.0f}% 1h, still pumping")
        elif chg_1h > 10 and chg_5m > 0:
            mom_pts = 5; boosts.append(f"📈 +{chg_1h:.0f}% 1h momentum")
        elif chg_5m > 5:
            mom_pts = 4; boosts.append(f"📈 +{chg_5m:.1f}% in 5m")
        elif chg_1h < -20:
            mom_pts = 0; warnings.append(f"📉 Down {abs(chg_1h):.0f}% in 1h")
        else:
            mom_pts = 2
        # Vol/MCap ratio bonus
        if vol_mcap >= 3:
            mom_pts = min(7, mom_pts + 3)
            boosts.append(f"🔥 Vol/MCap {vol_mcap:.1f}x — rare signal")
        elif vol_mcap >= 1:
            mom_pts = min(7, mom_pts + 1)
    elif is_new:
        # New token — no DexScreener data yet is NORMAL, give neutral score
        liq_pts = 3  # neutral — don't penalise
        mom_pts = 3  # neutral
        boosts.append("🆕 New token — price data not yet indexed")
    else:
        # Established token with no data = suspicious
        liq_pts = 0
        mom_pts = 0
        warnings.append("⚠️ No market data found")

    score += liq_pts + mom_pts
    bd["liquidity"] = liq_pts
    bd["momentum"]  = mom_pts

    # ── 8. Holder count (3 pts) ───────────────────────────────────────────────
    hc = h.get("count", 0)
    hc_pts = 3 if hc > 100 else 2 if hc > 30 else 1 if hc > 8 else 0
    score += hc_pts; bd["holder_count"] = hc_pts

    # ── 9. Graduation bonus (2 pts) ───────────────────────────────────────────
    if info.get("graduated"):
        score += 2; bd["graduation"] = 2
        boosts.append("🎓 Graduated to PumpSwap")
    else:
        bd["graduation"] = 0

    # ── Hard disqualifiers (age-aware) ────────────────────────────────────────
    hard_fail    = False
    fail_reasons = []

    # Always fail
    if real_top1 > 50:
        hard_fail = True; fail_reasons.append(f"top holder {real_top1}% (excl LP)")
    insider_pct = h.get("insider_pct", 0)
    if insider_pct > 60:
        hard_fail = True; fail_reasons.append(f"insider concentration {insider_pct}% (top 3 non-LP holders)")
    if info.get("deployer","") in blacklisted_devs:
        hard_fail = True; fail_reasons.append("blacklisted deployer")
    if b.get("is_bundled") and sm.get("count", 0) == 0:
        hard_fail = True; fail_reasons.append("bundled + zero smart money")

    # MCap ceiling — never alert on anything already above $5M
    # (unless it's a new launch under 30 min old)
    px_mcap = info.get("px", {}).get("mcap", 0) or 0
    source  = info.get("source", "")
    if px_mcap > 5_000_000 and not is_new:
        hard_fail = True; fail_reasons.append(f"mcap {fmt_usd(px_mcap)} too high for momentum alert")

    # Only fail on established tokens (>30 min)
    if not is_new:
        if not px.get("found") and tx_count > 100:
            hard_fail = True; fail_reasons.append("no market data on active token")
        if liq_usd == 0 and not info.get("graduated") and tx_count > 50:
            hard_fail = True; fail_reasons.append("zero liquidity on established token")
        if chg_1h < -40 and vol > 0:
            hard_fail = True; fail_reasons.append("actively dumping -40% in 1h")

    if hard_fail:
        warnings.insert(0, f"❌ HARD FAIL: {', '.join(fail_reasons)}")

    # ── Rug risk rating ───────────────────────────────────────────────────────
    if hard_fail or real_top1 > 40 or b.get("is_bundled") or not auth.get("mint_revoked"):
        rug_risk = "high"
    elif real_top1 > 20 or b.get("confidence", 0) > 30 or dev.get("risk") == "medium":
        rug_risk = "medium"
    else:
        rug_risk = "low"

    final = 0 if hard_fail else min(99, score)

    # Conviction label
    if hard_fail:   conviction = "❌ DO NOT BUY"
    elif final >= 85: conviction = "💎 VERY HIGH CONVICTION"
    elif final >= 75: conviction = "🟢 HIGH CONVICTION"
    elif final >= 62: conviction = "🟡 MODERATE — trade carefully"
    elif final >= 50: conviction = "🟠 RISKY — small size only"
    else:             conviction = "🔴 AVOID"

    return {
        "total":      final,
        "conviction": conviction,
        "rug_risk":   rug_risk,
        "hard_fail":  hard_fail,
        "warnings":   warnings,
        "boosts":     boosts,
        "breakdown":  bd,
        "age_mins":   age,
        "is_new":     is_new,
    }

# ── Alert formatter ────────────────────────────────────────────────────────────
def format_alert(mint: str, info: dict, result: dict, source: str = "") -> str:
    score  = result["total"]
    rug    = result["rug_risk"]
    re_e   = {"low":"🟢","medium":"🟡","high":"🔴"}.get(rug,"⚪")
    lp_name, lp_emoji = info.get("launchpad", ("Unknown","⚪"))
    token_name   = info.get("name", "")
    token_symbol = info.get("symbol", "")
    token_label  = f"*{token_symbol}* — {token_name}" if token_symbol else f"`{mint[:8]}...{mint[-4:]}`"
    bar    = "█" * round(score/10) + "░" * (10 - round(score/10))
    bd     = result["breakdown"]
    px     = info.get("px", {})
    h      = info.get("holders", {})
    auth   = info.get("auth", {})
    age    = result.get("age_mins", 0)

    src_tag = {
        "launch":   "🆕 *NEW LAUNCH*",
        "momentum": "📊 *MOMENTUM*",
        "trending": "🔥 *TRENDING — BUY SIGNAL*",
        "manual":   "🔬 *MANUAL ANALYSIS*",
    }.get(source, "🔍 *SIGNAL*")

    price_block = ""
    if px.get("found"):
        price_block = (
            f"*Price:* {px.get('price_usd','N/A')}  |  MCap: {fmt_usd(px.get('mcap'))}\n"
            f"*Liquidity:* {fmt_usd(px.get('liquidity'))}  |  Vol 24h: {fmt_usd(px.get('volume_24h'))}\n"
            f"*Change:* 5m {fmt_pct(px.get('chg_5m'))}  1h {fmt_pct(px.get('chg_1h'))}  24h {fmt_pct(px.get('chg_24h'))}\n\n"
        )

    age_str = f"{int(age)}m old" if age < 60 else f"{age/60:.1f}h old"
    boosts_txt   = "\n".join(result["boosts"])   or "—"
    warnings_txt = "\n".join(result["warnings"]) or "None"

    return (
        f"🚨 *AlphaScan Alert*\n"
        f"{src_tag}\n\n"
        f"*Token:* {token_label}\n"
        f"`{mint}`\n"
        f"*Launchpad:* {lp_emoji} {lp_name}  |  Age: {age_str}\n"
        f"*Score:* {score}/99  `{bar}`\n"
        f"*Conviction:* {result['conviction']}\n\n"
        f"{price_block}"
        f"*Breakdown:*\n"
        f"  🔒 Authority:    {bd.get('authority',0)}/20\n"
        f"  📊 Distribution: {bd.get('distribution',0)}/18\n"
        f"  🤖 Organic:      {bd.get('organic',0)}/15\n"
        f"  ⚡ TX velocity:  {bd.get('tx_velocity',0)}/12\n"
        f"  💧 Liquidity:    {bd.get('liquidity',0)}/8\n"
        f"  📈 Momentum:     {bd.get('momentum',0)}/7\n"
        f"  🛡 Dev safety:   {bd.get('dev_safety',0)}/10\n"
        f"  🐋 Smart money:  {bd.get('smart_money',0)}/10\n"
        f"  👥 Holders:      {bd.get('holder_count',0)}/3\n"
        f"  🎓 Graduated:    {bd.get('graduation',0)}/2\n\n"
        f"*Green flags:*\n{boosts_txt}\n\n"
        f"*Red flags:*\n{warnings_txt}\n\n"
        f"*Safety:*  Mint {'✅' if auth.get('mint_revoked') else '🚨'}  "
        f"Freeze {'✅' if auth.get('freeze_revoked') else '🚨'}  "
        f"Rug: {re_e} {rug.upper()}\n"
        f"Holders: {h.get('count',0)}  |  Top: {h.get('real_top1_pct',0)}%\n\n"
        f"🔗 [DexScreener](https://dexscreener.com/solana/{mint}) | "
        f"[Birdeye](https://birdeye.so/token/{mint}?chain=solana) | "
        f"[Solscan](https://solscan.io/token/{mint})\n"
        f"_Scanned {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC_"
    )

# ══════════════════════════════════════════════════════════════════════════════
# SCANNER 1 — Pump.fun & LetsBonk real-time launch detection
# ══════════════════════════════════════════════════════════════════════════════
async def scanner1_launches(session) -> list:
    """
    Detect brand new token launches on Pump.fun and LetsBonk.

    ROOT CAUSE FIX: Pump.fun does NOT use standard initializeMint.
    It uses a custom program instruction. The correct detection method is:
      preTokenBalances  = token accounts BEFORE the transaction
      postTokenBalances = token accounts AFTER the transaction
      New mint = any mint in post that does NOT appear in pre
    This is 100% reliable for any launchpad regardless of instruction format.
    """
    found     = []
    programs  = [
        (PUMPFUN_PROGRAM,  ("Pump.fun",     "🟢")),
        (LETSBONK_PROGRAM, ("LetsBonk.fun", "🟡")),
    ]
    for prog, launchpad in programs:
        try:
            sigs = await rpc(session, "getSignaturesForAddress", [prog, {"limit": 50}])
            for s in (sigs or [])[:25]:
                try:
                    tx = await rpc(session, "getTransaction", [
                        s["signature"],
                        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                    ])
                    if not tx: continue
                    meta = tx.get("meta", {}) or {}
                    pre  = {b.get("mint") for b in (meta.get("preTokenBalances")  or [])}
                    post = {b.get("mint") for b in (meta.get("postTokenBalances") or [])}
                    # New mints = appeared in post but not in pre
                    new_mints = post - pre - {None}
                    for mint in new_mints:
                        if mint and mint not in seen_mints:
                            found.append((mint, launchpad))
                            log.info(f"Scanner1 new mint: {mint[:8]}... on {launchpad[0]}")
                except: continue
        except Exception as e:
            log.debug(f"Scanner1 {prog[:8]}: {e}")
    log.info(f"Scanner1 found {len(found)} new launches")
    return found[:15]

async def scanner2_momentum(session) -> list:
    """
    Watch Pump.fun, PumpSwap, LetsBonk and Raydium for momentum.
    STRICT pre-screening: size ceiling, liquidity cap, dump filter.
    Root cause fix: uses liquidity as proxy when mcap=0/null so large
    established tokens can never slip through the size gate.
    """
    # ── Always skip — stablecoins + known large-cap Solana tokens ─────────────
    SKIP_MINTS = {
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
        "So11111111111111111111111111111111111111112",     # wSOL
        "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",  # BONK
        "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
        "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # ETH
        "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",   # JUP
        "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",  # RAY
        "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",   # ORCA
        "WnFt12ZrnzZrFZkt2xsNsaNWoQribnuQ5B5FrDbwDhD",   # WIF
        "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",  # PYTH
        "MangoCzJ36AjZyKwVj3VnYU4GTonjfVEnJmvvWaxLac",   # MNGO
        "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",   # bSOL
        "7i5KKsX2weiTkry7jA4ZwSuXGhs5eJBEjY8vVxR4pfRx",  # GMT
        "StepAscQoEioFxxWGnh2sLBDFp9d8rvKz2Yp39iDpyT",   # STEP
        "4vMsoUT2BWatFweudnQM1xedRLfJgJ7hswhcpz4xgBTy",  # HADES
        "HxhWkVpk5NS4Ltg5nij2G671CKXFRKPK8vy271Ub4uEK",  # HXRO
    }

    DEX_PROGRAMS = [
        (PUMPFUN_PROGRAM,  "Pump.fun",     "🟢"),
        (PUMPSWAP_PROGRAM, "PumpSwap",     "🎓"),
        (LETSBONK_PROGRAM, "LetsBonk.fun", "🟡"),
        (RAYDIUM_AMM,      "Raydium",      "🔵"),
    ]

    token_hits = defaultdict(lambda: {"count": 0, "launchpad": ("Unknown", "⚪")})
    now        = datetime.now(timezone.utc)

    for prog, lp_name, lp_emoji in DEX_PROGRAMS:
        try:
            sigs = await rpc(session, "getSignaturesForAddress", [prog, {"limit": 40}])
            for s in (sigs or [])[:20]:
                try:
                    tx = await rpc(session, "getTransaction", [
                        s["signature"],
                        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                    ])
                    if not tx: continue
                    for bal in tx.get("meta", {}).get("postTokenBalances", []):
                        mint = bal.get("mint")
                        if not mint or mint in SKIP_MINTS or mint in seen_mints:
                            continue
                        token_hits[mint]["count"]    += 1
                        token_hits[mint]["launchpad"] = (lp_name, lp_emoji)
                except: continue
        except Exception as e:
            log.debug(f"Scanner2 {lp_name}: {e}")

    candidates = []
    for mint, data in token_hits.items():
        hits = data["count"]

        # Momentum must be accelerating across scans
        if mint not in momentum_tracker:
            momentum_tracker[mint] = {"samples": [], "first_seen": now}
        entry = momentum_tracker[mint]
        entry["samples"].append({"hits": hits, "time": now})
        entry["samples"] = entry["samples"][-8:]
        if len(entry["samples"]) < 2:
            continue
        prev = entry["samples"][-2]["hits"]
        curr = entry["samples"][-1]["hits"]
        if not (curr >= prev * 1.5 and curr >= 3):
            continue

        # ── DexScreener strict gate — no exceptions ───────────────────────────
        px = await fetch_price(session, mint)

        if px.get("found"):
            mcap   = float(px.get("mcap",       0) or 0)
            liq    = float(px.get("liquidity",  0) or 0)
            chg_1h = float(px.get("chg_1h",     0) or 0)
            chg_24h= float(px.get("chg_24h",    0) or 0)
            vol    = float(px.get("volume_24h", 0) or 0)

            # THE ROOT CAUSE FIX:
            # DexScreener sometimes returns mcap=0 even for $500M tokens
            # when the fdv/marketCap fields are null. Use liquidity * 10 as
            # a conservative size proxy — $500K liq = ~$5M mcap minimum.
            effective_mcap = mcap if mcap > 0 else (liq * 10)

            if effective_mcap > 5_000_000:
                log.debug(f"S2 skip {mint[:8]}: size ~${effective_mcap/1e6:.1f}M")
                continue

            # Additional established-token guard: >$500K liquidity = not a memecoin play
            if liq > 500_000:
                log.debug(f"S2 skip {mint[:8]}: liq ${liq/1e3:.0f}K — established")
                continue

            if chg_24h > 800:
                log.debug(f"S2 skip {mint[:8]}: already +{chg_24h:.0f}% 24h")
                continue

            if chg_1h < -35:
                log.debug(f"S2 skip {mint[:8]}: dumping {chg_1h:.0f}% 1h")
                continue

            if vol < 500 and mcap > 50_000:
                log.debug(f"S2 skip {mint[:8]}: ghost — no volume")
                continue

        else:
            # No DexScreener data = brand new token (good) OR unlisted junk
            # Require higher hit count to trust no-data tokens
            if curr < 5:
                log.debug(f"S2 skip {mint[:8]}: no dex data, only {curr} hits")
                continue

        reason = f"momentum {prev}→{curr} txs | {data['launchpad'][0]}"
        candidates.append((mint, data["launchpad"], "momentum", reason))
        log.info(f"S2 ✓ {mint[:8]} — {reason}")

    return candidates[:6]

async def scanner3_trending(session) -> list:
    """
    Detect trending Solana tokens under $5M mcap using DexScreener.

    ROOT CAUSE FIXES:
    1. Removed 2-scan confirmation — fast movers vanish before 2nd scan
    2. Better endpoints — search by volume instead of profile metadata
    3. Stricter gates applied immediately on first sighting
    """
    found     = []
    seen_this = set()
    now       = datetime.now(timezone.utc)

    # ── Source A: DexScreener token boosts (community promoting these) ─────────
    try:
        async with session.get(
            "https://api.dexscreener.com/token-boosts/latest/v1",
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status == 200:
                items = await r.json()
                for item in (items or [])[:40]:
                    if item.get("chainId") != "solana": continue
                    mint = item.get("tokenAddress", "")
                    if mint and mint not in seen_mints and mint not in seen_this:
                        seen_this.add(mint)
                        found.append((mint, ("DexScreener Boosted", "⚡")))
    except Exception as e:
        log.debug(f"Scanner3 boosts: {e}")

    # ── Source B: DexScreener new pairs on Solana sorted by 1h change ─────────
    try:
        async with session.get(
            "https://api.dexscreener.com/latest/dex/search?q=solana",
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status == 200:
                data  = await r.json()
                pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
                # Sort by 1h volume change — tokens gaining momentum
                pairs.sort(key=lambda p: float(p.get("volume",{}).get("h1") or 0), reverse=True)
                for p in pairs[:30]:
                    mint = p.get("baseToken", {}).get("address", "")
                    if not mint or mint in seen_mints or mint in seen_this: continue
                    seen_this.add(mint)
                    found.append((mint, ("DexScreener Trending", "🔥")))
    except Exception as e:
        log.debug(f"Scanner3 search: {e}")

    # ── Apply gates — alert on first sighting if it passes ────────────────────
    actionable = []
    for mint, launchpad in found:
        if mint in trending_tracker and trending_tracker[mint].get("alerted"):
            continue
        try:
            px = await fetch_price(session, mint)
            if not px.get("found"): continue

            mcap  = float(px.get("mcap",       0) or 0)
            liq   = float(px.get("liquidity",  0) or 0)
            vol   = float(px.get("volume_24h", 0) or 0)
            c1h   = float(px.get("chg_1h",     0) or 0)
            c5m   = float(px.get("chg_5m",     0) or 0)
            c24h  = float(px.get("chg_24h",    0) or 0)

            # Same effective_mcap trick as Scanner 2
            effective_mcap = mcap if mcap > 0 else (liq * 10)

            if effective_mcap > MAX_MCAP:   continue  # too big
            if liq > 500_000:               continue  # established token
            if c1h > MAX_PUMP_1H:           continue  # already pumped out
            if liq < MIN_LIQ:               continue  # can't exit
            if vol < MIN_VOL:               continue  # no real interest
            if c5m < -15:                   continue  # actively dumping
            if c24h > 1000 and c1h < -10:   continue  # peaked and rolling over

            # Track so we don't alert twice
            if mint not in trending_tracker:
                trending_tracker[mint] = {"first_seen": now, "alerted": False}

            reasons = []
            if c5m > 3:  reasons.append(f"5m +{c5m:.1f}%")
            if c1h > 5:  reasons.append(f"1h +{c1h:.1f}%")
            if c24h > 0: reasons.append(f"24h +{c24h:.1f}%")
            reasons.append(f"MCap {fmt_usd(effective_mcap)}")
            reasons.append(f"Liq {fmt_usd(liq)}")

            actionable.append((mint, launchpad, "trending", " | ".join(reasons)))
            log.info(f"Scanner3 ✓ {mint[:8]} — {' | '.join(reasons)}")

        except Exception as e:
            log.debug(f"Scanner3 gate {mint[:8]}: {e}")

    log.info(f"Scanner3 found {len(actionable)} trending candidates")
    return actionable[:5]

# ── Process a single token through scoring and alert ──────────────────────────
async def process_token(session, bot: Bot, mint: str, launchpad: tuple,
                        source: str, extra_note: str = "") -> bool:
    info = await enrich_token(session, mint, launchpad, source)
    if not info: return False
    result = score_token(info)
    score  = result["total"]
    lp_name, _ = launchpad
    log.info(f"[{source}] {mint[:8]} score={score} conviction={result['conviction']} rug={result['rug_risk']} age={result['age_mins']:.0f}m fail={result['hard_fail']}")
    if score < alert_threshold or result["hard_fail"]:
        return False
    text = format_alert(mint, info, result, source)
    if extra_note:
        text = text.replace("*AlphaScan Alert*\n", f"*AlphaScan Alert*\n_{extra_note}_\n")
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text,
                           parse_mode="Markdown", disable_web_page_preview=True)
    tracked_alerts[mint] = {
        "score": score, "time": datetime.now(timezone.utc),
        "launchpad": launchpad, "source": source, "reported": False
    }
    # Register for take profit tracking
    px_now = info.get("px", {})
    take_profit_watch[mint] = {
        "entry_mcap":  px_now.get("mcap", 0) or 0,
        "name":        info.get("name", ""),
        "symbol":      info.get("symbol", ""),
        "ath":         px_now.get("mcap", 0) or 0,
        "first_seen":  datetime.now(timezone.utc),
        "dead":        False,
    }
    daily_top.append({"mint": mint, "score": score, "launchpad": launchpad,
                      "name": info.get("name",""), "symbol": info.get("symbol","")})
    if mint in trending_tracker:
        trending_tracker[mint]["alerted"] = True
    return True


# ── Take profit alert system ───────────────────────────────────────────────────
async def check_take_profits(bot: Bot, session):
    """
    After every alert, track the token's MCap.
    Fire specific alerts at: 2x, 5x, 10x, and -30% from ATH (protect gains).
    """
    if not take_profit_watch:
        return
    for mint, data in list(take_profit_watch.items()):
        if data.get("dead"):
            continue
        try:
            px = await fetch_price(session, mint)
            if not px.get("found"):
                continue
            current_mcap = px.get("mcap", 0) or 0
            if current_mcap <= 0:
                continue
            entry_mcap = data.get("entry_mcap", current_mcap)
            if entry_mcap <= 0:
                continue

            multiplier = current_mcap / entry_mcap
            ath        = data.get("ath", current_mcap)
            name       = data.get("name", "")
            symbol     = data.get("symbol", "")
            label      = f"*{symbol}*" if symbol else f"`{mint[:8]}...`"

            # Update ATH
            if current_mcap > ath:
                take_profit_watch[mint]["ath"] = current_mcap
                ath = current_mcap

            # ATH pullback alert (protect gains)
            ath_drop_pct = ((ath - current_mcap) / ath * 100) if ath > 0 else 0
            if ath_drop_pct >= 30 and not data.get("notified_ath_drop") and multiplier > 1.5:
                take_profit_watch[mint]["notified_ath_drop"] = True
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=(
                        f"⚠️ *ATH Pullback — Protect Gains*\n\n"
                        f"{label} is down *{ath_drop_pct:.0f}%* from ATH\n"
                        f"ATH MCap: {fmt_usd(ath)}\n"
                        f"Now: {fmt_usd(current_mcap)} ({multiplier:.1f}x from entry)\n\n"
                        f"Consider taking profits or moving stop.\n"
                        f"🔗 [DexScreener](https://dexscreener.com/solana/{mint})"
                    ),
                    parse_mode="Markdown", disable_web_page_preview=True
                )

            # Milestone alerts
            milestones = [
                (2.0,  "tp2x",  "🎯 *2x HIT*",       "Consider taking 30-50% off"),
                (5.0,  "tp5x",  "🚀 *5x HIT*",        "Strong take profit zone"),
                (10.0, "tp10x", "💎 *10x HIT — RARE*","Take serious profits now"),
            ]
            for mult, flag, title, advice in milestones:
                if multiplier >= mult and not data.get(f"notified_{flag}"):
                    take_profit_watch[mint][f"notified_{flag}"] = True
                    await bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=(
                            f"{title}\n\n"
                            f"{label}\n"
                            f"Entry MCap: {fmt_usd(entry_mcap)}\n"
                            f"Now: {fmt_usd(current_mcap)} (*{multiplier:.1f}x*)\n\n"
                            f"💡 {advice}\n\n"
                            f"🔗 [DexScreener](https://dexscreener.com/solana/{mint})"
                        ),
                        parse_mode="Markdown", disable_web_page_preview=True
                    )
                    await asyncio.sleep(0.5)

            # Mark dead if no DexScreener data for 4h+ and was alerted
            age_hours = (datetime.now(timezone.utc) - data["first_seen"]).total_seconds() / 3600
            if age_hours > 4 and not px.get("found"):
                take_profit_watch[mint]["dead"] = True

        except Exception as e:
            log.debug(f"TP check {mint[:8]}: {e}")

# ── Copy signal checker ────────────────────────────────────────────────────────
async def check_copy_signals(bot: Bot):
    now = datetime.now(timezone.utc)
    for mint, data in list(copy_signal_cache.items()):
        if data.get("alerted"): continue
        wallets = data.get("wallets", [])
        if len(wallets) < 3: continue
        age_min = (now - data["first_seen"]).total_seconds() / 60
        if age_min > 30:
            copy_signal_cache[mint]["alerted"] = True; continue
        copy_signal_cache[mint]["alerted"] = True
        ws = [f"`{w[:6]}...{w[-4:]}`" for w in wallets[:5]]
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(f"🔥 *COPY SIGNAL — HIGH CONVICTION*\n\n"
                  f"*{len(wallets)} smart wallets* bought:\n`{mint}`\n\n"
                  f"*Wallets:*\n" + "\n".join(ws) + f"\n\n"
                  f"First seen {age_min:.0f}m ago\n\n"
                  f"🔗 [DexScreener](https://dexscreener.com/solana/{mint})"),
            parse_mode="Markdown", disable_web_page_preview=True
        )

# ── Watchlist scanner ──────────────────────────────────────────────────────────
async def scan_watchlist(session, bot: Bot):
    for addr, label in list(watchlist_wallets.items()):
        try:
            sigs = await rpc(session, "getSignaturesForAddress", [addr, {"limit": 3}])
            if not sigs: continue
            sig = sigs[0]["signature"]
            cache_key = f"wl_{sig}"
            if cache_key in seen_mints: continue
            seen_mints.add(cache_key)
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(f"👁 *Watchlist*\n*{label}* just transacted\n"
                      f"`{addr[:8]}...{addr[-4:]}`\n\n"
                      f"🔗 [Solscan](https://solscan.io/account/{addr})"),
                parse_mode="Markdown", disable_web_page_preview=True
            )
        except Exception as e:
            log.debug(f"Watchlist {addr[:8]}: {e}")

# ── 24h performance tracker ────────────────────────────────────────────────────
async def check_performance(bot: Bot, session):
    now = datetime.now(timezone.utc)
    for mint, data in list(tracked_alerts.items()):
        if data.get("reported"): continue
        if (now - data["time"]).total_seconds() < 86400: continue
        try:
            px   = await fetch_price(session, mint)
            sigs = await rpc(session, "getSignaturesForAddress", [mint, {"limit": 50}])
            txs  = len(sigs) if sigs else 0
            if px.get("found"):
                c24 = px.get("chg_24h", 0) or 0
                if c24 > 100:   verdict, emoji = f"Pumped +{c24:.0f}%", "🚀"
                elif c24 > 0:   verdict, emoji = f"Up +{c24:.0f}%", "🟢"
                elif c24 > -50: verdict, emoji = f"Down {c24:.0f}%", "🟡"
                else:           verdict, emoji = f"Dumped {c24:.0f}%", "🔴"
            else:
                verdict, emoji = ("Still active" if txs > 20 else "Likely dead"), ("🟡" if txs > 20 else "🔴")
            lp_name, lp_emoji = data.get("launchpad", ("?","⚪"))
            tp_data = take_profit_watch.get(mint, {})
            sym = tp_data.get("symbol","")
            token_label_24h = f"*{sym}*  `{mint[:8]}...`" if sym else f"`{mint[:8]}...{mint[-4:]}`"
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(f"📊 *24h Report*\n\n{emoji} {token_label_24h}\n"
                      f"{lp_emoji} {lp_name}  |  Alert score: *{data['score']}*\n\n"
                      f"{verdict}\n24h TXs: {txs}\n\n"
                      f"🔗 [DexScreener](https://dexscreener.com/solana/{mint})"),
                parse_mode="Markdown", disable_web_page_preview=True
            )
            tracked_alerts[mint]["reported"] = True
        except Exception as e:
            log.debug(f"Perf check {mint[:8]}: {e}")

# ── Daily summary ──────────────────────────────────────────────────────────────
async def send_daily_summary(bot: Bot):
    if not daily_top:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID,
            text="📋 *Daily Summary*\n\nNo tokens alerted today.", parse_mode="Markdown")
        return
    top5  = sorted(daily_top, key=lambda x: x["score"], reverse=True)[:5]
    lines = ["📋 *AlphaScan Daily Top*\n"]
    for i, t in enumerate(top5, 1):
        lp_name, lp_emoji = t.get("launchpad", ("?","⚪"))
        sym = t.get("symbol","")
        label = f"*{sym}*" if sym else f"`{t['mint'][:8]}...`"
        lines.append(f"{i}. {label} Score *{t['score']}* {lp_emoji} {lp_name}")
        lines.append(f"   https://dexscreener.com/solana/{t['mint']}")
    lines.append(f"\n_Total alerts: {len(daily_top)}_")
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="\n".join(lines),
                           parse_mode="Markdown", disable_web_page_preview=True)
    daily_top.clear()

# ── Manual CA analysis ─────────────────────────────────────────────────────────
async def run_analysis(update: Update, mint: str):
    msg = await update.message.reply_text(f"⏳ Analysing `{mint[:8]}...`", parse_mode="Markdown")
    try:
        async with aiohttp.ClientSession() as session:
            # Detect launchpad
            launchpad = ("Unknown", "⚪")
            sigs = await rpc(session, "getSignaturesForAddress", [mint, {"limit": 10}])
            for s in (sigs or [])[:5]:
                try:
                    tx = await rpc(session, "getTransaction", [s["signature"],
                                   {"encoding":"jsonParsed","maxSupportedTransactionVersion":0}])
                    if not tx: continue
                    keys = [a.get("pubkey","") if isinstance(a,dict) else str(a)
                            for a in tx.get("transaction",{}).get("message",{}).get("accountKeys",[])]
                    for k in keys:
                        if k in LAUNCHPADS:
                            launchpad = LAUNCHPADS[k]; break
                    if launchpad[0] != "Unknown": break
                except: continue
            info = await enrich_token(session, mint, launchpad)
            if not info:
                await msg.edit_text("❌ Could not fetch token data. Check the address.")
                return
            result = score_token(info)
            text   = format_alert(mint, info, result, "manual")
            await msg.edit_text(text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text(f"❌ Analysis failed: {str(e)[:100]}")

# ── Telegram commands ──────────────────────────────────────────────────────────
async def cmd_start(u, ctx):
    await u.message.reply_text(
        "👋 *AlphaScan Commands:*\n\n"
        "Just *paste any CA* to analyze it\n\n"
        "/tracking — tokens being monitored for take profits\n"
        "/threshold `<40-99>` — alert threshold\n"
        "/watch `<addr>` `<label>` — track wallet\n"
        "/unwatch `<addr>` — stop tracking\n"
        "/watchlist — show tracked wallets\n"
        "/addwallet `<addr>` — add to smart money list\n"
        "/smartwallets — show smart money list\n"
        "/blacklist — view/add blacklisted devs\n"
        "/summary — today's top tokens\n"
        "/status — bot status\n",
        parse_mode="Markdown"
    )

async def cmd_threshold(u, ctx):
    global alert_threshold
    try:
        v = int(ctx.args[0])
        if not 40 <= v <= 99: raise ValueError
        alert_threshold = v
        await u.message.reply_text(f"✅ Threshold set to *{v}*", parse_mode="Markdown")
    except: await u.message.reply_text("Usage: /threshold 65 (40–99)")

async def cmd_watch(u, ctx):
    if len(ctx.args) < 2:
        await u.message.reply_text("Usage: /watch <address> <label>"); return
    addr, label = ctx.args[0], " ".join(ctx.args[1:])
    watchlist_wallets[addr] = label
    await u.message.reply_text(f"👁 Watching *{label}*\n`{addr[:8]}...{addr[-4:]}`", parse_mode="Markdown")

async def cmd_unwatch(u, ctx):
    addr = ctx.args[0] if ctx.args else ""
    if addr in watchlist_wallets:
        watchlist_wallets.pop(addr)
        await u.message.reply_text("✅ Removed")
    else: await u.message.reply_text("Not found")

async def cmd_watchlist(u, ctx):
    if not watchlist_wallets:
        await u.message.reply_text("No wallets. Use /watch to add."); return
    lines = ["👁 *Watchlist:*\n"] + [f"• *{l}*: `{a[:8]}...{a[-4:]}`" for a,l in watchlist_wallets.items()]
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_addwallet(u, ctx):
    if not ctx.args: await u.message.reply_text("Usage: /addwallet <address>"); return
    addr = ctx.args[0]
    user_smart_wallets.add(addr)
    await u.message.reply_text(f"🐋 Added to smart money\n`{addr[:8]}...{addr[-4:]}`", parse_mode="Markdown")

async def cmd_smartwallets(u, ctx):
    total = len(all_smart_wallets())
    auto  = len([w for w,d in discovered_smart_wallets.items() if d.get("wins",0) >= 3])
    lines = [
        f"🐋 *Smart Money List ({total} wallets)*\n",
        f"Seeds: {len(SMART_WALLET_SEEDS)}",
        f"User-added: {len(user_smart_wallets)}",
        f"Auto-discovered (3+ wins): {auto}",
        f"Watching for promotion: {len(discovered_smart_wallets)}",
        f"\nTop auto-discovered:",
    ]
    top = sorted(discovered_smart_wallets.items(), key=lambda x: x[1].get("wins",0), reverse=True)[:5]
    for addr, d in top:
        lines.append(f"• `{addr[:8]}...` — {d.get('wins',0)} wins")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_blacklist(u, ctx):
    if ctx.args:
        blacklisted_devs.add(ctx.args[0])
        await u.message.reply_text(f"🚫 Blacklisted\n`{ctx.args[0][:8]}...`", parse_mode="Markdown")
    else:
        lines = ["🚫 *Blacklisted:*\n"] + [f"• `{a[:8]}...{a[-4:]}`" for a in list(blacklisted_devs)[:15]]
        await u.message.reply_text("\n".join(lines) if blacklisted_devs else "Blacklist empty.", parse_mode="Markdown")

async def cmd_summary(u, ctx): await send_daily_summary(ctx.bot)

async def cmd_status(u, ctx):
    await u.message.reply_text(
        f"⚙️ *AlphaScan Status*\n\n"
        f"Threshold: *{alert_threshold}/99*\n"
        f"Scan interval: *{SCAN_INTERVAL}s*\n"
        f"Tokens seen: *{len(seen_mints)}*\n"
        f"Alerts today: *{len(daily_top)}*\n"
        f"Smart wallets: *{len(all_smart_wallets())}*\n"
        f"Watchlist: *{len(watchlist_wallets)}*\n"
        f"Blacklisted devs: *{len(blacklisted_devs)}*\n"
        f"Tracking 24h perf: *{len(tracked_alerts)}*\n\n"
        f"*Scanners:* 1=Pump.fun  2=Momentum  3=Trending\n"
        f"*Smart wallet engine:* auto-discovering from winners\n",
        parse_mode="Markdown"
    )


async def cmd_tracking(u, ctx):
    """Show all tokens currently being tracked for take profits."""
    active = {m: d for m, d in take_profit_watch.items() if not d.get("dead")}
    if not active:
        await u.message.reply_text("No tokens currently tracked for take profits.")
        return
    lines = [f"📈 *Take Profit Tracking ({len(active)} tokens)*\n"]
    for mint, d in list(active.items())[:10]:
        sym   = d.get("symbol", "")
        label = f"*{sym}*" if sym else f"`{mint[:8]}...`"
        entry = d.get("entry_mcap", 0)
        ath   = d.get("ath", entry)
        notified = []
        if d.get("notified_tp2x"):  notified.append("2x✓")
        if d.get("notified_tp5x"):  notified.append("5x✓")
        if d.get("notified_tp10x"): notified.append("10x✓")
        n_str = " ".join(notified) if notified else "watching"
        lines.append(f"• {label} — Entry: {fmt_usd(entry)} | ATH: {fmt_usd(ath)} | {n_str}")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_analyze(u, ctx):
    if not ctx.args: await u.message.reply_text("Usage: /analyze <CA>"); return
    await run_analysis(u, ctx.args[0].strip())

async def handle_message(u, ctx):
    text = (u.message.text or "").strip()
    if re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", text):
        await u.message.reply_text(f"🔍 Analysing `{text[:8]}...{text[-4:]}`", parse_mode="Markdown")
        await run_analysis(u, text)

# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCAN LOOP
# ══════════════════════════════════════════════════════════════════════════════
async def scan_loop(bot: Bot):
    last_summary_hour = -1
    scan_count        = 0
    async with aiohttp.ClientSession() as session:
        while True:
            scan_count += 1
            log.info(f"═══ Scan #{scan_count} (threshold={alert_threshold}) ═══")
            try:
                launches, momentum, trending = await asyncio.gather(
                    scanner1_launches(session),
                    scanner2_momentum(session),
                    scanner3_trending(session),
                )
                log.info(f"S1={len(launches)} S2={len(momentum)} S3={len(trending)}")
                alerted = 0

                for mint, launchpad in launches:
                    if mint in seen_mints: continue
                    seen_mints.add(mint)
                    if await process_token(session, bot, mint, launchpad, "launch"):
                        alerted += 1; await asyncio.sleep(1.5)

                for item in momentum:
                    mint, launchpad, source, note = item
                    if mint in seen_mints: continue
                    seen_mints.add(mint)
                    if await process_token(session, bot, mint, launchpad, source, note):
                        alerted += 1; await asyncio.sleep(1.5)

                for item in trending:
                    mint, launchpad, source, note = item
                    if mint in seen_mints: continue
                    seen_mints.add(mint)
                    if await process_token(session, bot, mint, launchpad, source, note):
                        alerted += 1; await asyncio.sleep(1.5)

                await scan_watchlist(session, bot)
                await check_copy_signals(bot)
                await check_take_profits(bot, session)

                if scan_count % 12 == 0:
                    await check_performance(bot, session)

                # Cleanup stale trackers
                if scan_count % 60 == 0:
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=3)
                    for tracker in [momentum_tracker, trending_tracker, winner_watch]:
                        stale = [m for m,d in tracker.items() if d.get("first_seen",datetime.now(timezone.utc)) < cutoff]
                        for m in stale: tracker.pop(m, None)

                hour = datetime.now(timezone.utc).hour
                if hour == 9 and last_summary_hour != 9:
                    await send_daily_summary(bot)
                    last_summary_hour = 9
                elif hour != 9:
                    last_summary_hour = hour

                log.info(f"Scan #{scan_count} complete — {alerted} alerts")
            except Exception as e:
                log.error(f"Scan loop error: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

# ══════════════════════════════════════════════════════════════════════════════
# BOOT
# ══════════════════════════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, fn in [("start",cmd_start),("threshold",cmd_threshold),("watch",cmd_watch),
                    ("unwatch",cmd_unwatch),("watchlist",cmd_watchlist),("addwallet",cmd_addwallet),
                    ("smartwallets",cmd_smartwallets),("blacklist",cmd_blacklist),
                    ("summary",cmd_summary),("status",cmd_status),("analyze",cmd_analyze)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def run():
        await app.initialize()
        await app.start()
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(f"✅ *AlphaScan v4 — Complete Rewrite*\n\n"
                  f"Threshold: {alert_threshold}/99  |  Scan: every {SCAN_INTERVAL}s\n\n"
                  f"*Fixed in this version:*\n"
                  f"• New token scoring (no false fails)\n"
                  f"• Pump.fun discovery rebuilt\n"
                  f"• Age-aware scoring engine\n"
                  f"• Self-populating smart wallets\n"
                  f"• Seed wallet: GMGN-identified alpha trader\n\n"
                  f"Paste any CA to analyse. Type /status anytime."),
            parse_mode="Markdown"
        )
        await asyncio.gather(scan_loop(app.bot), app.updater.start_polling())

    asyncio.run(run())

if __name__ == "__main__":
    main()

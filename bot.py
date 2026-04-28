"""
AlphaScan.sol v5 — Ground-up rebuild focused on reliability
Key fixes:
  - Scanner 1: Helius DAS searchAssets (new mints in 1 call, no tx parsing)
  - Scanner 2: Mandatory alive-check before any scoring (kills rugged tokens)
  - Scanner 3: DexScreener new pairs by creation time (real organic discovery)
  - Pre-flight: Every token checked against DexScreener BEFORE expensive RPC calls
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
ALERT_THRESHOLD  = int(os.environ.get("ALPHA_THRESHOLD", "55"))
HELIUS_RPC       = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# ── Program addresses ──────────────────────────────────────────────────────────
PUMPFUN_PROGRAM   = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM  = "PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkQ"
LETSBONK_PROGRAM  = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
RAYDIUM_AMM       = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
TOKEN_PROGRAM     = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

LAUNCHPADS = {
    PUMPFUN_PROGRAM:  ("Pump.fun",      "🟢"),
    PUMPSWAP_PROGRAM: ("PumpSwap",      "🎓"),
    LETSBONK_PROGRAM: ("LetsBonk.fun",  "🟡"),
    RAYDIUM_AMM:      ("Raydium",       "🔵"),
}

# ── State ──────────────────────────────────────────────────────────────────────
seen_mints         = set()
alert_threshold    = ALERT_THRESHOLD
tracked_alerts     = {}
daily_top          = []
watchlist_wallets  = {}
blacklisted_devs   = set()
dev_cache          = {}
price_cache        = {}
copy_signal_cache  = {}
momentum_tracker   = {}
take_profit_watch  = {}
winner_watch       = {}
discovered_wallets = {}

SMART_WALLET_SEEDS = {
    "H72yLkhTnoBfhBTXXaj1RBXuirm8s8G5fcVh2XpQLggM",
}
user_smart_wallets = set(os.environ.get("SMART_WALLETS", "").split(",")) - {""}

def all_smart_wallets():
    return SMART_WALLET_SEEDS | user_smart_wallets | {
        w for w, d in discovered_wallets.items() if d.get("wins", 0) >= 3
    }

# ── RPC ────────────────────────────────────────────────────────────────────────
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

# ── DexScreener ────────────────────────────────────────────────────────────────
async def fetch_price(session, mint: str) -> dict:
    cached = price_cache.get(mint)
    if cached and (datetime.now(timezone.utc) - cached["ts"]).total_seconds() < 45:
        return cached["d"]
    empty = {"found": False, "name": "", "symbol": "", "price_usd": None,
             "mcap": 0, "liquidity": 0, "volume_24h": 0,
             "chg_5m": 0, "chg_1h": 0, "chg_24h": 0, "alive": False}
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status != 200:
                return empty
            data  = await r.json()
            pairs = data.get("pairs") or []
            if not pairs:
                return empty
            pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
            p   = pairs[0]
            liq = float(p.get("liquidity", {}).get("usd", 0) or 0)
            result = {
                "found":      True,
                "alive":      liq > 1000,   # token has real liquidity = alive
                "name":       p.get("baseToken", {}).get("name", ""),
                "symbol":     p.get("baseToken", {}).get("symbol", ""),
                "price_usd":  p.get("priceUsd"),
                "mcap":       float(p.get("fdv") or p.get("marketCap") or 0),
                "liquidity":  liq,
                "volume_24h": float(p.get("volume", {}).get("h24") or 0),
                "chg_5m":     float(p.get("priceChange", {}).get("m5") or 0),
                "chg_1h":     float(p.get("priceChange", {}).get("h1") or 0),
                "chg_24h":    float(p.get("priceChange", {}).get("h24") or 0),
                "pair":       p.get("pairAddress", ""),
                "created_at": p.get("pairCreatedAt", 0),
            }
            price_cache[mint] = {"d": result, "ts": datetime.now(timezone.utc)}
            return result
    except Exception as e:
        log.debug(f"DexScreener {mint[:8]}: {e}")
        return empty

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

# ── MANDATORY PRE-FLIGHT CHECK ─────────────────────────────────────────────────
async def preflight(session, mint: str, is_new: bool) -> tuple:
    """
    Check DexScreener BEFORE any expensive on-chain RPC calls.
    Returns (px, pass, reason).
    Kills rugged/dead tokens instantly. New tokens get a pass on liq checks.
    """
    px = await fetch_price(session, mint)

    # Brand new token — no DexScreener data yet is EXPECTED, give it a pass
    if not px.get("found"):
        if is_new:
            return px, True, "new token, not yet indexed"
        else:
            return px, False, "no market data — token not listed anywhere"

    liq    = px.get("liquidity", 0)
    chg_1h = px.get("chg_1h",   0)
    mcap   = px.get("mcap",     0)

    # ── Dead/rugged token checks ───────────────────────────────────────────────
    if not px.get("alive") and not is_new:
        return px, False, f"dead — liquidity only ${liq:.0f}"

    if chg_1h < -60 and liq > 0:
        return px, False, f"rugged — down {abs(chg_1h):.0f}% in 1h"

    if liq < 500 and mcap > 200_000:
        return px, False, f"rug pulled — mcap ${fmt_usd(mcap)} but liq only ${liq:.0f}"

    # ── Size ceiling ──────────────────────────────────────────────────────────
    effective = mcap if mcap > 0 else (liq * 8)
    if effective > 8_000_000:
        return px, False, f"too big — ${effective/1e6:.1f}M"

    return px, True, "ok"

# ── Token name resolver ────────────────────────────────────────────────────────
async def get_name(session, mint: str, px: dict) -> tuple:
    if px.get("name") or px.get("symbol"):
        return px.get("name",""), px.get("symbol","")
    try:
        async with session.post(
            HELIUS_RPC,
            json={"jsonrpc":"2.0","id":1,"method":"getAsset","params":{"id":mint}},
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            d = await r.json()
            m = d.get("result",{}).get("content",{}).get("metadata",{})
            n, s = m.get("name","").strip(), m.get("symbol","").strip()
            if n or s: return n, s
    except: pass
    return f"Token {mint[:6]}...", mint[:6].upper()

# ── On-chain checks ────────────────────────────────────────────────────────────
async def check_authorities(session, mint: str) -> dict:
    r = {"mint_revoked": False, "freeze_revoked": False, "safe": False}
    try:
        info = await rpc(session, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        if info and info.get("value"):
            p = info["value"].get("data",{}).get("parsed",{}).get("info",{})
            r["mint_revoked"]   = p.get("mintAuthority")   is None
            r["freeze_revoked"] = p.get("freezeAuthority") is None
            r["safe"]           = r["mint_revoked"] and r["freeze_revoked"]
    except: pass
    return r

async def check_holders(session, mint: str) -> dict:
    r = {"count": 0, "top1_pct": 100, "real_top1": 100, "top3_pct": 100, "lp_excluded": False}
    try:
        largest = await rpc(session, "getTokenLargestAccounts", [mint])
        supply  = await rpc(session, "getTokenSupply", [mint])
        total   = float((supply or {}).get("value",{}).get("uiAmount") or 1)
        accts   = (largest or {}).get("value", [])
        if not accts or total <= 0: return r
        amts = [float(a.get("uiAmount") or 0) for a in accts]
        r["count"]    = len(amts)
        r["top1_pct"] = round(amts[0] / total * 100, 1)
        r["top3_pct"] = round(sum(amts[:3]) / total * 100, 1)
        if len(amts) >= 2 and amts[0] > amts[1]*2 and r["top1_pct"] > 20:
            r["lp_excluded"] = True
            r["real_top1"]   = round(amts[1] / total * 100, 1)
        else:
            r["real_top1"] = r["top1_pct"]
    except: pass
    return r

def detect_bundling(sigs: list) -> dict:
    if not sigs:
        return {"is_bundled": False, "confidence": 0}
    slots = defaultdict(int)
    for s in sigs: slots[s.get("slot",0)] += 1
    slot_list  = sorted(slots.keys())
    max_cluster = 0
    for slot in slot_list:
        cluster = sum(slots[s] for s in slot_list if abs(s-slot) <= 3)
        max_cluster = max(max_cluster, cluster)
    if max_cluster >= 8:
        return {"is_bundled": True, "confidence": min(100, max_cluster*10),
                "reason": f"{max_cluster} coordinated txs"}
    return {"is_bundled": False, "confidence": max(0, (max_cluster-2)*8)}

async def check_dev(session, deployer: str) -> dict:
    if not deployer: return {"launches": 0, "is_serial": False, "risk": "unknown"}
    if deployer in dev_cache: return dev_cache[deployer]
    r = {"launches": 0, "is_serial": False, "risk": "low"}
    try:
        sigs = await rpc(session, "getSignaturesForAddress", [deployer, {"limit": 100}])
        if sigs:
            count = 0
            for s in sigs[:15]:
                tx = await rpc(session, "getTransaction",
                    [s["signature"], {"encoding":"jsonParsed","maxSupportedTransactionVersion":0}])
                if not tx: continue
                for ix in tx.get("transaction",{}).get("message",{}).get("instructions",[]):
                    p = ix.get("parsed",{})
                    if isinstance(p,dict) and p.get("type") == "initializeMint":
                        count += 1
            r["launches"] = count
            if count >= 5: r["is_serial"] = True; r["risk"] = "high"; blacklisted_devs.add(deployer)
            elif count >= 2: r["risk"] = "medium"
        dev_cache[deployer] = r
    except: pass
    return r

async def check_smart_money(session, mint: str, sigs: list) -> dict:
    r = {"count": 0, "wallets": []}
    smart = all_smart_wallets()
    if not smart: return r
    for s in sigs[:20]:
        try:
            tx = await rpc(session, "getTransaction",
                [s["signature"], {"encoding":"jsonParsed","maxSupportedTransactionVersion":0}])
            if not tx: continue
            keys = [a.get("pubkey","") if isinstance(a,dict) else str(a)
                    for a in tx.get("transaction",{}).get("message",{}).get("accountKeys",[])]
            for k in keys:
                if k in smart and k not in r["wallets"]:
                    r["wallets"].append(k); r["count"] += 1
        except: continue
    if r["count"] >= 2:
        if mint not in copy_signal_cache:
            copy_signal_cache[mint] = {"wallets": r["wallets"],
                                        "first_seen": datetime.now(timezone.utc), "alerted": False}
        else:
            copy_signal_cache[mint]["wallets"] = list(
                set(copy_signal_cache[mint]["wallets"] + r["wallets"]))
    return r

# ── Full enrichment ────────────────────────────────────────────────────────────
async def enrich(session, mint: str, launchpad: tuple, px: dict) -> dict | None:
    try:
        sigs_res, acct_res = await asyncio.gather(
            rpc(session, "getSignaturesForAddress", [mint, {"limit":100}]),
            rpc(session, "getAccountInfo", [mint, {"encoding":"jsonParsed"}]),
        )
        sigs     = sigs_res or []
        deployer = (acct_res or {}).get("value",{}).get("owner","") if acct_res else ""
        age_mins = 0
        if sigs:
            oldest = sigs[-1].get("blockTime", 0)
            if oldest: age_mins = (time.time() - oldest) / 60

        auth, holders, smart, dev = await asyncio.gather(
            check_authorities(session, mint),
            check_holders(session, mint),
            check_smart_money(session, mint, sigs),
            check_dev(session, deployer),
        )
        bundle   = detect_bundling(sigs[:50])
        name, sym = await get_name(session, mint, px)
        graduated = launchpad[0] in ("PumpSwap",)

        return {
            "mint": mint, "name": name, "symbol": sym,
            "launchpad": launchpad, "deployer": deployer,
            "age_mins": age_mins, "is_new": age_mins < 30,
            "px": px, "auth": auth, "holders": holders,
            "smart": smart, "dev": dev, "bundle": bundle,
            "sigs": sigs, "graduated": graduated,
        }
    except Exception as e:
        log.warning(f"enrich {mint[:8]}: {e}")
        return None

# ── Scoring engine ─────────────────────────────────────────────────────────────
def score_token(info: dict) -> dict:
    score = 0; warnings = []; boosts = []; bd = {}
    px     = info["px"]
    auth   = info["auth"]
    h      = info["holders"]
    sm     = info["smart"]
    b      = info["bundle"]
    dev    = info["dev"]
    sigs   = info["sigs"]
    is_new = info["is_new"]
    liq    = px.get("liquidity", 0) or 0
    mcap   = px.get("mcap", 0) or 0
    chg_1h = px.get("chg_1h", 0) or 0
    chg_5m = px.get("chg_5m", 0) or 0
    vol    = px.get("volume_24h", 0) or 0

    # 1. Authority (20pts)
    if auth.get("safe"):        a=20; boosts.append("🔒 Both authorities revoked")
    elif auth.get("mint_revoked"): a=12; warnings.append("⚠️ Freeze authority active")
    elif auth.get("freeze_revoked"): a=8; warnings.append("⚠️ Mint authority active")
    else: a=2; warnings.append("🚨 Both authorities active — dev can rug")
    score+=a; bd["authority"]=a

    # 2. Holder distribution (18pts)
    t1 = h.get("real_top1", h.get("top1_pct", 100))
    if t1<5:   hd=18; boosts.append("✅ Excellent distribution")
    elif t1<10: hd=14
    elif t1<20: hd=10
    elif t1<35: hd=5; warnings.append(f"⚠️ Top holder {t1}%")
    else:       hd=0; warnings.append(f"🚨 Top holder {t1}% — dangerous")
    if h.get("lp_excluded"): boosts.append(f"📊 LP excluded, real top: {t1}%")
    score+=hd; bd["distribution"]=hd

    # 3. Organic launch (15pts)
    if b.get("is_bundled"): org=0; warnings.append(f"🤖 Bundled launch — {b.get('reason','')}")
    elif b.get("confidence",0)>30: org=7; warnings.append("🤖 Possible bot activity")
    else: org=15; boosts.append("✅ Organic launch")
    score+=org; bd["organic"]=org

    # 4. TX velocity (12pts)
    tx=len(sigs)
    if tx>300:   tv=12; boosts.append(f"🔥 {tx} transactions")
    elif tx>150: tv=10
    elif tx>60:  tv=7
    elif tx>20:  tv=4
    elif tx>5:   tv=2
    else:        tv=0
    score+=tv; bd["tx_velocity"]=tv

    # 5. Dev safety (10pts)
    if info["deployer"] in blacklisted_devs or dev.get("is_serial"):
        dv=0; warnings.append(f"🚨 Serial deployer — blacklisted")
    elif dev.get("risk")=="medium": dv=5; warnings.append(f"⚠️ Dev has {dev['launches']} prior launches")
    else: dv=10
    score+=dv; bd["dev_safety"]=dv

    # 6. Smart money (10pts)
    sc=sm.get("count",0)
    if sc>=3:   sp=10; boosts.append(f"🐋 {sc} smart wallets in early")
    elif sc==2: sp=7;  boosts.append(f"🐋 {sc} smart wallets spotted")
    elif sc==1: sp=4;  boosts.append("🐋 1 smart wallet spotted")
    else:       sp=0
    score+=sp; bd["smart_money"]=sp

    # 7. Liquidity (8pts) — age-aware
    if px.get("found"):
        if liq>=50000:  lp=8; boosts.append(f"💧 Deep liq {fmt_usd(liq)}")
        elif liq>=20000: lp=6; boosts.append(f"💧 Good liq {fmt_usd(liq)}")
        elif liq>=8000:  lp=4
        elif liq>=2000:  lp=2
        else:            lp=0; warnings.append(f"⚠️ Thin liq {fmt_usd(liq)}")
    elif is_new: lp=4  # new token — no data yet is normal
    else: lp=0
    score+=lp; bd["liquidity"]=lp

    # 8. Momentum (7pts) — age-aware
    vol_mcap = round(vol/mcap, 2) if mcap>0 else 0
    if px.get("found"):
        if chg_1h>30 and chg_5m>0:   mp=7; boosts.append(f"📈 +{chg_1h:.0f}% 1h, still moving")
        elif chg_1h>10 and chg_5m>=0: mp=5; boosts.append(f"📈 +{chg_1h:.0f}% 1h")
        elif chg_5m>5:                mp=4; boosts.append(f"📈 +{chg_5m:.1f}% 5m")
        elif chg_1h<-20:              mp=0; warnings.append(f"📉 Down {abs(chg_1h):.0f}% 1h")
        else:                         mp=2
        if vol_mcap>=3: mp=min(7,mp+3); boosts.append(f"🔥 Vol/MCap {vol_mcap:.1f}x")
        elif vol_mcap>=1: mp=min(7,mp+1)
    elif is_new: mp=3
    else: mp=0
    score+=mp; bd["momentum"]=mp

    # 9. Holder count (3pts)
    hc=h.get("count",0)
    hp=3 if hc>100 else 2 if hc>30 else 1 if hc>8 else 0
    score+=hp; bd["holder_count"]=hp

    # 10. Graduation (2pts)
    gp=2 if info.get("graduated") else 0
    score+=gp; bd["graduation"]=gp
    if gp: boosts.append("🎓 Graduated to PumpSwap")

    # ── Hard fails ────────────────────────────────────────────────────────────
    hard_fail=False; fails=[]
    if t1>50:                                       hard_fail=True; fails.append(f"top holder {t1}%")
    if info["deployer"] in blacklisted_devs:        hard_fail=True; fails.append("blacklisted dev")
    if b.get("is_bundled") and sc==0:              hard_fail=True; fails.append("bundled+no smart money")
    if not is_new and liq<500 and px.get("found"): hard_fail=True; fails.append("near-zero liquidity")
    if chg_1h<-60 and px.get("found"):            hard_fail=True; fails.append(f"down {abs(chg_1h):.0f}% 1h = rugged")

    if hard_fail: warnings.insert(0, f"❌ HARD FAIL: {', '.join(fails)}")

    rug = "high" if (hard_fail or t1>40 or b.get("is_bundled") or not auth.get("mint_revoked")) \
          else "medium" if (t1>20 or b.get("confidence",0)>30) else "low"

    final = 0 if hard_fail else min(99, score)

    if hard_fail:    conv="❌ DO NOT BUY"
    elif final>=85:  conv="💎 VERY HIGH CONVICTION"
    elif final>=75:  conv="🟢 HIGH CONVICTION"
    elif final>=62:  conv="🟡 MODERATE — size carefully"
    elif final>=50:  conv="🟠 RISKY — small size only"
    else:            conv="🔴 AVOID"

    return {"total":final,"conviction":conv,"rug_risk":rug,
            "hard_fail":hard_fail,"warnings":warnings,"boosts":boosts,"breakdown":bd}

# ── Alert formatter ────────────────────────────────────────────────────────────
def format_alert(mint, info, result, source=""):
    score  = result["total"]
    rug    = result["rug_risk"]
    re_e   = {"low":"🟢","medium":"🟡","high":"🔴"}.get(rug,"⚪")
    lp_n, lp_e = info["launchpad"]
    sym    = info.get("symbol",""); name = info.get("name","")
    label  = f"*{sym}* — {name}" if sym else f"`{mint[:8]}...{mint[-4:]}`"
    bar    = "█"*round(score/10) + "░"*(10-round(score/10))
    bd     = result["breakdown"]
    px     = info["px"]
    h      = info["holders"]
    auth   = info["auth"]
    age    = info.get("age_mins",0)
    age_s  = f"{int(age)}m old" if age<60 else f"{age/60:.1f}h old"

    src_tag = {"launch":"🆕 *NEW LAUNCH*","momentum":"📊 *MOMENTUM*",
               "trending":"🔥 *TRENDING — BUY SIGNAL*","manual":"🔬 *MANUAL ANALYSIS*"
               }.get(source,"🔍 *SIGNAL*")

    px_block = ""
    if px.get("found"):
        px_block = (
            f"*Price:* {px.get('price_usd','N/A')}  MCap: {fmt_usd(px.get('mcap'))}\n"
            f"*Liquidity:* {fmt_usd(px.get('liquidity'))}  Vol 24h: {fmt_usd(px.get('volume_24h'))}\n"
            f"*Change:* 5m {fmt_pct(px.get('chg_5m'))} | 1h {fmt_pct(px.get('chg_1h'))} | 24h {fmt_pct(px.get('chg_24h'))}\n\n"
        )

    return (
        f"🚨 *AlphaScan Alert*\n{src_tag}\n\n"
        f"*Token:* {label}\n`{mint}`\n"
        f"*Launchpad:* {lp_e} {lp_n}  |  {age_s}\n"
        f"*Score:* {score}/99  `{bar}`\n"
        f"*Conviction:* {result['conviction']}\n\n"
        f"{px_block}"
        f"*Breakdown:*\n"
        f"  🔒 Authority:    {bd.get('authority',0)}/20\n"
        f"  📊 Distribution: {bd.get('distribution',0)}/18\n"
        f"  🤖 Organic:      {bd.get('organic',0)}/15\n"
        f"  ⚡ TX velocity:  {bd.get('tx_velocity',0)}/12\n"
        f"  🛡 Dev safety:   {bd.get('dev_safety',0)}/10\n"
        f"  🐋 Smart money:  {bd.get('smart_money',0)}/10\n"
        f"  💧 Liquidity:    {bd.get('liquidity',0)}/8\n"
        f"  📈 Momentum:     {bd.get('momentum',0)}/7\n"
        f"  👥 Holders:      {bd.get('holder_count',0)}/3\n"
        f"  🎓 Graduated:    {bd.get('graduation',0)}/2\n\n"
        f"*Green flags:*\n{chr(10).join(result['boosts']) or '—'}\n\n"
        f"*Red flags:*\n{chr(10).join(result['warnings']) or 'None'}\n\n"
        f"*Safety:* Mint {'✅' if auth.get('mint_revoked') else '🚨'} "
        f"Freeze {'✅' if auth.get('freeze_revoked') else '🚨'} "
        f"Rug: {re_e} {rug.upper()}\n"
        f"Holders: {h.get('count',0)} | Top: {h.get('real_top1',0)}%\n\n"
        f"🔗 [DexScreener](https://dexscreener.com/solana/{mint}) | "
        f"[Birdeye](https://birdeye.so/token/{mint}?chain=solana) | "
        f"[Solscan](https://solscan.io/token/{mint})\n"
        f"_{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC_"
    )

# ══════════════════════════════════════════════════════════════════════════════
# SCANNER 1 — New launches via Helius DAS searchAssets
# Gets brand new mints in a SINGLE API call, no transaction parsing needed
# ══════════════════════════════════════════════════════════════════════════════
async def scanner1_launches(session) -> list:
    """
    Use Helius DAS searchAssets sorted by creation time.
    Returns mints created in the last few minutes.
    One API call replaces 50+ transaction RPC calls.
    """
    found = []
    try:
        async with session.post(
            HELIUS_RPC,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "searchAssets",
                "params": {
                    "sortBy": {"sortBy": "created", "sortDirection": "desc"},
                    "tokenType": "fungible",
                    "limit": 50,
                    "page": 1,
                }
            },
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            data  = await r.json()
            items = data.get("result", {}).get("items", [])
            now   = time.time()
            for item in items:
                mint = item.get("id", "")
                if not mint or mint in seen_mints:
                    continue
                # Only tokens created in last 10 minutes
                created = item.get("created_at") or \
                          item.get("content",{}).get("metadata",{}).get("created_at",0)
                if created and (now - created) > 600:
                    continue
                # Detect launchpad from grouping or creators
                launchpad = ("Pump.fun", "🟢")  # most new tokens are Pump.fun
                creators  = item.get("creators", [])
                for c in creators:
                    addr = c.get("address","")
                    if addr in LAUNCHPADS:
                        launchpad = LAUNCHPADS[addr]; break
                found.append((mint, launchpad))
                log.info(f"Scanner1 new: {mint[:8]}... on {launchpad[0]}")
    except Exception as e:
        log.warning(f"Scanner1 DAS: {e}")

    # Fallback: if DAS returns nothing, use Pump.fun program signature scan
    if not found:
        try:
            sigs = await rpc(session, "getSignaturesForAddress",
                             [PUMPFUN_PROGRAM, {"limit": 30}])
            for s in (sigs or [])[:15]:
                try:
                    tx = await rpc(session, "getTransaction", [
                        s["signature"],
                        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                    ])
                    if not tx: continue
                    meta = tx.get("meta", {}) or {}
                    pre  = {b.get("mint") for b in (meta.get("preTokenBalances")  or [])}
                    post = {b.get("mint") for b in (meta.get("postTokenBalances") or [])}
                    for mint in (post - pre - {None}):
                        if mint and mint not in seen_mints:
                            found.append((mint, ("Pump.fun", "🟢")))
                except: continue
        except Exception as e:
            log.debug(f"Scanner1 fallback: {e}")

    log.info(f"Scanner1: {len(found)} new launches")
    return found[:12]

# ══════════════════════════════════════════════════════════════════════════════
# SCANNER 2 — Momentum with MANDATORY alive check
# Every token checked against DexScreener FIRST before any on-chain calls
# ══════════════════════════════════════════════════════════════════════════════
async def scanner2_momentum(session) -> list:
    """
    Detect tokens gaining momentum on Pump.fun, PumpSwap and Raydium.
    MANDATORY: DexScreener alive check before any processing.
    Kills dead/rugged tokens before they ever reach the scoring engine.
    """
    SKIP = {
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
        "So11111111111111111111111111111111111111112",     # wSOL
        "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",  # BONK
        "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
        "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",   # JUP
        "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",  # RAY
        "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",   # ORCA
        "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",  # PYTH
        "WnFt12ZrnzZrFZkt2xsNsaNWoQribnuQ5B5FrDbwDhD",   # WIF
        "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # ETH
    }
    PROGS = [
        (PUMPFUN_PROGRAM,  ("Pump.fun",     "🟢")),
        (PUMPSWAP_PROGRAM, ("PumpSwap",     "🎓")),
        (RAYDIUM_AMM,      ("Raydium",      "🔵")),
    ]
    hits  = defaultdict(lambda: {"count": 0, "lp": ("Unknown","⚪")})
    now   = datetime.now(timezone.utc)

    for prog, lp in PROGS:
        try:
            sigs = await rpc(session, "getSignaturesForAddress", [prog, {"limit": 30}])
            for s in (sigs or [])[:15]:
                try:
                    tx = await rpc(session, "getTransaction", [
                        s["signature"],
                        {"encoding":"jsonParsed","maxSupportedTransactionVersion":0}
                    ])
                    if not tx: continue
                    for bal in tx.get("meta",{}).get("postTokenBalances",[]):
                        mint = bal.get("mint")
                        if mint and mint not in SKIP and mint not in seen_mints:
                            hits[mint]["count"] += 1
                            hits[mint]["lp"]     = lp
                except: continue
        except Exception as e:
            log.debug(f"S2 {lp[0]}: {e}")

    candidates = []
    for mint, data in hits.items():
        if data["count"] < 2: continue
        if mint not in momentum_tracker:
            momentum_tracker[mint] = {"samples": [], "first_seen": now}
        entry = momentum_tracker[mint]
        entry["samples"].append({"hits": data["count"], "time": now})
        entry["samples"] = entry["samples"][-8:]
        if len(entry["samples"]) < 2: continue
        prev = entry["samples"][-2]["hits"]
        curr = entry["samples"][-1]["hits"]
        if not (curr >= prev * 1.5 and curr >= 3): continue

        # ── MANDATORY ALIVE CHECK — runs before any other processing ──────────
        px = await fetch_price(session, mint)

        if px.get("found"):
            liq    = px.get("liquidity", 0) or 0
            chg_1h = px.get("chg_1h",   0) or 0
            mcap   = px.get("mcap",     0) or 0
            vol    = px.get("volume_24h",0) or 0

            # Dead check — no liquidity = rugged or dead, do not alert
            if not px.get("alive"):
                log.debug(f"S2 skip {mint[:8]}: DEAD — liq ${liq:.0f}")
                continue

            # Rugged check — down 60%+ in 1h = someone dumped
            if chg_1h < -60:
                log.debug(f"S2 skip {mint[:8]}: RUGGED — {chg_1h:.0f}% 1h")
                continue

            # Size check — use effective mcap with liq proxy
            eff = mcap if mcap > 0 else (liq * 8)
            if eff > 8_000_000:
                log.debug(f"S2 skip {mint[:8]}: too big ${eff/1e6:.1f}M")
                continue

            # Established token liquidity guard
            if liq > 600_000:
                log.debug(f"S2 skip {mint[:8]}: established liq ${liq/1e3:.0f}K")
                continue

            # Ghost check
            if vol < 200 and mcap > 100_000:
                log.debug(f"S2 skip {mint[:8]}: ghost — no volume")
                continue

        else:
            # No DexScreener data
            if curr < 6:
                log.debug(f"S2 skip {mint[:8]}: no dex data + low hits")
                continue

        reason = f"momentum {prev}→{curr} txs"
        candidates.append((mint, data["lp"], "momentum", reason))
        log.info(f"S2 ✓ {mint[:8]} — {reason} — alive: {px.get('alive')}")

    return candidates[:6]

# ══════════════════════════════════════════════════════════════════════════════
# SCANNER 3 — New pairs on DexScreener (organic, recently listed)
# ══════════════════════════════════════════════════════════════════════════════
async def scanner3_new_pairs(session) -> list:
    """
    Find recently listed Solana pairs on DexScreener with growing volume.
    These are tokens that just got real liquidity and are starting to move.
    Uses pair creation time to find early-stage tokens only.
    """
    found     = []
    seen_this = set()
    now_ms    = int(time.time() * 1000)
    max_age_ms = 3 * 60 * 60 * 1000  # 3 hours old max

    # Source A: DexScreener search — Solana pairs sorted by 5m volume
    dex_urls = [
        "https://api.dexscreener.com/latest/dex/search?q=raydium",
        "https://api.dexscreener.com/latest/dex/search?q=pump",
    ]
    for url in dex_urls:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200: continue
                data  = await r.json()
                pairs = [p for p in (data.get("pairs") or [])
                         if p.get("chainId") == "solana"]
                # Sort by 5m volume — tokens that just started trading
                pairs.sort(key=lambda p: float(p.get("volume",{}).get("m5") or 0), reverse=True)
                for p in pairs[:20]:
                    mint     = p.get("baseToken", {}).get("address", "")
                    created  = p.get("pairCreatedAt", 0) or 0
                    if not mint or mint in seen_mints or mint in seen_this:
                        continue
                    # Skip if pair is too old (> 3 hours)
                    if created and (now_ms - created) > max_age_ms:
                        continue
                    liq    = float(p.get("liquidity",{}).get("usd",0) or 0)
                    mcap   = float(p.get("fdv") or p.get("marketCap") or 0)
                    chg_1h = float(p.get("priceChange",{}).get("h1") or 0)
                    chg_5m = float(p.get("priceChange",{}).get("m5") or 0)
                    vol_5m = float(p.get("volume",{}).get("m5") or 0)

                    eff = mcap if mcap > 0 else (liq * 8)

                    # Gates
                    if eff > 8_000_000:    continue
                    if liq < 3_000:        continue
                    if chg_1h < -40:       continue
                    if vol_5m < 100:       continue
                    if chg_1h > 800:       continue

                    seen_this.add(mint)
                    found.append((mint, ("DexScreener New Pair","🔥"), "trending",
                                  f"New pair | 5m vol {fmt_usd(vol_5m)} | {chg_1h:+.0f}% 1h"))
        except Exception as e:
            log.debug(f"S3 {url[-20:]}: {e}")

    # Source B: Token boosts (community spending real money = genuine interest)
    try:
        async with session.get(
            "https://api.dexscreener.com/token-boosts/latest/v1",
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status == 200:
                items = await r.json()
                for item in (items or [])[:20]:
                    if item.get("chainId") != "solana": continue
                    mint = item.get("tokenAddress","")
                    if not mint or mint in seen_mints or mint in seen_this: continue
                    seen_this.add(mint)
                    found.append((mint, ("DexScreener Boosted","⚡"), "trending", "boosted"))
    except Exception as e:
        log.debug(f"S3 boosts: {e}")

    log.info(f"Scanner3: {len(found)} new pair candidates")
    return found[:6]

# ── Process token ──────────────────────────────────────────────────────────────
async def process_token(session, bot, mint, launchpad, source, note="") -> bool:
    is_new = source == "launch"

    # Pre-flight first — cheap, kills rugs before expensive RPC calls
    px, ok, reason = await preflight(session, mint, is_new)
    if not ok:
        log.info(f"[{source}] {mint[:8]} PRE-FLIGHT FAIL: {reason}")
        return False

    info = await enrich(session, mint, launchpad, px)
    if not info: return False

    result = score_token(info)
    score  = result["total"]
    log.info(f"[{source}] {mint[:8]} score={score} conv={result['conviction']} fail={result['hard_fail']}")

    if score < alert_threshold or result["hard_fail"]:
        return False

    text = format_alert(mint, info, result, source)
    if note:
        text = text.replace("*AlphaScan Alert*\n", f"*AlphaScan Alert*\n_{note}_\n")

    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text,
                           parse_mode="Markdown", disable_web_page_preview=True)

    tracked_alerts[mint] = {"score": score, "time": datetime.now(timezone.utc),
                             "launchpad": launchpad, "source": source, "reported": False}
    take_profit_watch[mint] = {
        "entry_mcap": px.get("mcap",0) or 0, "name": info.get("name",""),
        "symbol": info.get("symbol",""), "ath": px.get("mcap",0) or 0,
        "first_seen": datetime.now(timezone.utc), "dead": False,
    }
    daily_top.append({"mint": mint, "score": score, "launchpad": launchpad,
                      "name": info.get("name",""), "symbol": info.get("symbol","")})
    if mint in momentum_tracker: momentum_tracker[mint]["alerted"] = True
    return True

# ── Copy signal ────────────────────────────────────────────────────────────────
async def check_copy_signals(bot):
    now = datetime.now(timezone.utc)
    for mint, d in list(copy_signal_cache.items()):
        if d.get("alerted"): continue
        wallets = d.get("wallets",[])
        if len(wallets) < 3: continue
        if (now - d["first_seen"]).total_seconds() > 1800:
            copy_signal_cache[mint]["alerted"] = True; continue
        copy_signal_cache[mint]["alerted"] = True
        ws = [f"`{w[:6]}...{w[-4:]}`" for w in wallets[:5]]
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID,
            text=(f"🔥 *COPY SIGNAL — HIGH CONVICTION*\n\n"
                  f"*{len(wallets)} smart wallets* bought:\n`{mint}`\n\n"
                  f"*Wallets:*\n" + "\n".join(ws) + "\n\n"
                  f"🔗 [DexScreener](https://dexscreener.com/solana/{mint})"),
            parse_mode="Markdown", disable_web_page_preview=True)

# ── Take profit tracker ────────────────────────────────────────────────────────
async def check_take_profits(bot, session):
    for mint, d in list(take_profit_watch.items()):
        if d.get("dead"): continue
        try:
            px = await fetch_price(session, mint)
            if not px.get("found"): continue
            curr  = px.get("mcap",0) or 0
            entry = d.get("entry_mcap", curr)
            ath   = d.get("ath", curr)
            sym   = d.get("symbol","")
            label = f"*{sym}*" if sym else f"`{mint[:8]}...`"
            if curr <= 0 or entry <= 0: continue
            mult = curr / entry
            if curr > ath: take_profit_watch[mint]["ath"] = curr; ath = curr

            ath_drop = ((ath-curr)/ath*100) if ath > 0 else 0
            if ath_drop >= 30 and not d.get("n_ath") and mult > 1.5:
                take_profit_watch[mint]["n_ath"] = True
                await bot.send_message(chat_id=TELEGRAM_CHAT_ID,
                    text=(f"⚠️ *ATH Pullback — Protect Gains*\n\n{label}\n"
                          f"Down *{ath_drop:.0f}%* from ATH\n"
                          f"ATH: {fmt_usd(ath)} → Now: {fmt_usd(curr)} ({mult:.1f}x)\n\n"
                          f"🔗 [DexScreener](https://dexscreener.com/solana/{mint})"),
                    parse_mode="Markdown", disable_web_page_preview=True)

            for m, flag, title, tip in [
                (2.0,"n_2x","🎯 *2x HIT*","Take 30-50% off"),
                (5.0,"n_5x","🚀 *5x HIT*","Strong take profit zone"),
                (10.0,"n_10x","💎 *10x HIT*","Take serious profits now"),
            ]:
                if mult >= m and not d.get(flag):
                    take_profit_watch[mint][flag] = True
                    await bot.send_message(chat_id=TELEGRAM_CHAT_ID,
                        text=(f"{title}\n\n{label}\n"
                              f"Entry: {fmt_usd(entry)} → Now: {fmt_usd(curr)} (*{mult:.1f}x*)\n\n"
                              f"💡 {tip}\n\n"
                              f"🔗 [DexScreener](https://dexscreener.com/solana/{mint})"),
                        parse_mode="Markdown", disable_web_page_preview=True)
                    await asyncio.sleep(0.5)
        except Exception as e:
            log.debug(f"TP {mint[:8]}: {e}")

# ── Watchlist ──────────────────────────────────────────────────────────────────
async def scan_watchlist(session, bot):
    for addr, label in list(watchlist_wallets.items()):
        try:
            sigs = await rpc(session, "getSignaturesForAddress", [addr, {"limit":3}])
            if not sigs: continue
            sig = sigs[0]["signature"]
            key = f"wl_{sig}"
            if key in seen_mints: continue
            seen_mints.add(key)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID,
                text=(f"👁 *Watchlist*\n*{label}* just transacted\n"
                      f"`{addr[:8]}...{addr[-4:]}`\n\n"
                      f"🔗 [Solscan](https://solscan.io/account/{addr})"),
                parse_mode="Markdown", disable_web_page_preview=True)
        except: pass

# ── 24h performance ────────────────────────────────────────────────────────────
async def check_performance(bot, session):
    now = datetime.now(timezone.utc)
    for mint, d in list(tracked_alerts.items()):
        if d.get("reported"): continue
        if (now - d["time"]).total_seconds() < 86400: continue
        try:
            px = await fetch_price(session, mint)
            c24 = px.get("chg_24h",0) or 0
            txs = len(await rpc(session,"getSignaturesForAddress",[mint,{"limit":50}]) or [])
            if px.get("found"):
                if c24>100: vd,em="Pumped +%.0f%%"%c24,"🚀"
                elif c24>0: vd,em="Up +%.0f%%"%c24,"🟢"
                elif c24>-50: vd,em="Down %.0f%%"%c24,"🟡"
                else: vd,em="Dumped %.0f%%"%c24,"🔴"
            else: vd,em=("Active" if txs>20 else "Likely dead"),("🟡" if txs>20 else "🔴")
            tp = take_profit_watch.get(mint,{})
            sym = tp.get("symbol",""); label = f"*{sym}*" if sym else f"`{mint[:8]}...`"
            lp_n,lp_e = d.get("launchpad",("?","⚪"))
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID,
                text=(f"📊 *24h Report*\n\n{em} {label}\n"
                      f"{lp_e} {lp_n} | Score: *{d['score']}*\n\n{vd}\n24h TXs: {txs}\n\n"
                      f"🔗 [DexScreener](https://dexscreener.com/solana/{mint})"),
                parse_mode="Markdown", disable_web_page_preview=True)
            tracked_alerts[mint]["reported"] = True
        except: pass

# ── Daily summary ──────────────────────────────────────────────────────────────
async def send_daily_summary(bot):
    if not daily_top:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID,
            text="📋 *Daily Summary*\n\nNo alerts today.", parse_mode="Markdown"); return
    top5  = sorted(daily_top, key=lambda x: x["score"], reverse=True)[:5]
    lines = ["📋 *AlphaScan Daily Top*\n"]
    for i,t in enumerate(top5,1):
        lp_n,lp_e = t.get("launchpad",("?","⚪"))
        sym = t.get("symbol","")
        lab = f"*{sym}*" if sym else f"`{t['mint'][:8]}...`"
        lines.append(f"{i}. {lab} Score *{t['score']}* {lp_e} {lp_n}")
        lines.append(f"   https://dexscreener.com/solana/{t['mint']}")
    lines.append(f"\n_Total alerts: {len(daily_top)}_")
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID,
        text="\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)
    daily_top.clear()

# ── Manual analysis ────────────────────────────────────────────────────────────
async def run_analysis(update, mint):
    msg = await update.message.reply_text(f"⏳ Analysing `{mint[:8]}...`", parse_mode="Markdown")
    try:
        async with aiohttp.ClientSession() as session:
            px = await fetch_price(session, mint)
            launchpad = ("Unknown","⚪")
            sigs = await rpc(session,"getSignaturesForAddress",[mint,{"limit":10}])
            for s in (sigs or [])[:5]:
                try:
                    tx = await rpc(session,"getTransaction",
                        [s["signature"],{"encoding":"jsonParsed","maxSupportedTransactionVersion":0}])
                    if not tx: continue
                    keys = [a.get("pubkey","") if isinstance(a,dict) else str(a)
                            for a in tx.get("transaction",{}).get("message",{}).get("accountKeys",[])]
                    for k in keys:
                        if k in LAUNCHPADS: launchpad=LAUNCHPADS[k]; break
                    if launchpad[0] != "Unknown": break
                except: continue
            info = await enrich(session, mint, launchpad, px)
            if not info:
                await msg.edit_text("❌ Could not fetch token data."); return
            result = score_token(info)
            await msg.edit_text(format_alert(mint, info, result, "manual"),
                                parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text(f"❌ Failed: {str(e)[:100]}")

# ── Commands ───────────────────────────────────────────────────────────────────
async def cmd_start(u,ctx):
    await u.message.reply_text(
        "👋 *AlphaScan v5 Commands:*\n\n"
        "Paste any CA to analyse instantly\n\n"
        "/threshold `<40-99>` — alert threshold (now 55)\n"
        "/watch `<addr>` `<label>` — track wallet\n"
        "/unwatch `<addr>` — remove wallet\n"
        "/watchlist — show watched wallets\n"
        "/addwallet `<addr>` — add to smart money\n"
        "/smartwallets — view smart money list\n"
        "/blacklist — view/add blacklisted devs\n"
        "/tracking — take profit watchlist\n"
        "/summary — today's top tokens\n"
        "/status — bot health\n",
        parse_mode="Markdown")

async def cmd_threshold(u,ctx):
    global alert_threshold
    try:
        v=int(ctx.args[0])
        if not 40<=v<=99: raise ValueError
        alert_threshold=v
        await u.message.reply_text(f"✅ Threshold: *{v}*", parse_mode="Markdown")
    except: await u.message.reply_text("Usage: /threshold 55 (40–99)")

async def cmd_watch(u,ctx):
    if len(ctx.args)<2: await u.message.reply_text("Usage: /watch <addr> <label>"); return
    addr,label=ctx.args[0]," ".join(ctx.args[1:])
    watchlist_wallets[addr]=label
    await u.message.reply_text(f"👁 Watching *{label}*", parse_mode="Markdown")

async def cmd_unwatch(u,ctx):
    addr=ctx.args[0] if ctx.args else ""
    if addr in watchlist_wallets: watchlist_wallets.pop(addr); await u.message.reply_text("✅ Removed")
    else: await u.message.reply_text("Not found")

async def cmd_watchlist(u,ctx):
    if not watchlist_wallets: await u.message.reply_text("Empty. Use /watch to add."); return
    lines=["👁 *Watchlist:*\n"]+[f"• *{l}*: `{a[:8]}...`" for a,l in watchlist_wallets.items()]
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_addwallet(u,ctx):
    if not ctx.args: await u.message.reply_text("Usage: /addwallet <addr>"); return
    addr=ctx.args[0]; user_smart_wallets.add(addr)
    await u.message.reply_text(f"🐋 Added `{addr[:8]}...`", parse_mode="Markdown")

async def cmd_smartwallets(u,ctx):
    total=len(all_smart_wallets())
    auto=len([w for w,d in discovered_wallets.items() if d.get("wins",0)>=3])
    lines=[f"🐋 *Smart Money ({total} wallets)*\n",
           f"Seeds: {len(SMART_WALLET_SEEDS)}",
           f"User-added: {len(user_smart_wallets)}",
           f"Auto-discovered: {auto}"]
    top=sorted(discovered_wallets.items(),key=lambda x:x[1].get("wins",0),reverse=True)[:5]
    if top:
        lines.append("\nTop auto-discovered:")
        for addr,d in top: lines.append(f"• `{addr[:8]}...` — {d.get('wins',0)} wins")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_blacklist(u,ctx):
    if ctx.args:
        blacklisted_devs.add(ctx.args[0])
        await u.message.reply_text(f"🚫 Blacklisted `{ctx.args[0][:8]}...`", parse_mode="Markdown")
    else:
        lines=["🚫 *Blacklist:*\n"]+[f"• `{a[:8]}...`" for a in list(blacklisted_devs)[:15]]
        await u.message.reply_text("\n".join(lines) if blacklisted_devs else "Empty.", parse_mode="Markdown")

async def cmd_tracking(u,ctx):
    active={m:d for m,d in take_profit_watch.items() if not d.get("dead")}
    if not active: await u.message.reply_text("Nothing tracked yet."); return
    lines=[f"📈 *TP Tracking ({len(active)})*\n"]
    for mint,d in list(active.items())[:10]:
        sym=d.get("symbol",""); label=f"*{sym}*" if sym else f"`{mint[:8]}...`"
        entry=d.get("entry_mcap",0); ath=d.get("ath",entry)
        hits=[x for x in ["2x" if d.get("n_2x") else "","5x" if d.get("n_5x") else "","10x" if d.get("n_10x") else ""] if x]
        lines.append(f"• {label} Entry:{fmt_usd(entry)} ATH:{fmt_usd(ath)} {' '.join(hits) or '…'}")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_summary(u,ctx): await send_daily_summary(ctx.bot)

async def cmd_status(u,ctx):
    await u.message.reply_text(
        f"⚙️ *AlphaScan v5 Status*\n\n"
        f"Threshold: *{alert_threshold}/99*\n"
        f"Scan interval: *{SCAN_INTERVAL}s*\n"
        f"Tokens seen: *{len(seen_mints)}*\n"
        f"Alerts today: *{len(daily_top)}*\n"
        f"Smart wallets: *{len(all_smart_wallets())}*\n"
        f"Watchlist: *{len(watchlist_wallets)}*\n"
        f"Blacklisted devs: *{len(blacklisted_devs)}*\n"
        f"TP tracking: *{len(take_profit_watch)}*\n\n"
        f"*Scanner 1:* Helius DAS new mints\n"
        f"*Scanner 2:* Momentum + alive check\n"
        f"*Scanner 3:* DexScreener new pairs\n",
        parse_mode="Markdown")

async def cmd_analyze(u,ctx):
    if not ctx.args: await u.message.reply_text("Usage: /analyze <CA>"); return
    await run_analysis(u, ctx.args[0].strip())

async def handle_message(u,ctx):
    text=(u.message.text or "").strip()
    if re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", text):
        await u.message.reply_text(f"🔍 Analysing `{text[:8]}...`", parse_mode="Markdown")
        await run_analysis(u, text)

# ── Main scan loop ─────────────────────────────────────────────────────────────
async def scan_loop(bot):
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
                    scanner3_new_pairs(session),
                )
                log.info(f"S1={len(launches)} S2={len(momentum)} S3={len(trending)}")
                alerted = 0

                for mint, lp in launches:
                    if mint in seen_mints: continue
                    seen_mints.add(mint)
                    if await process_token(session, bot, mint, lp, "launch"):
                        alerted += 1; await asyncio.sleep(1.5)

                for mint, lp, src, note in momentum:
                    if mint in seen_mints: continue
                    seen_mints.add(mint)
                    if await process_token(session, bot, mint, lp, src, note):
                        alerted += 1; await asyncio.sleep(1.5)

                for mint, lp, src, note in trending:
                    if mint in seen_mints: continue
                    seen_mints.add(mint)
                    if await process_token(session, bot, mint, lp, src, note):
                        alerted += 1; await asyncio.sleep(1.5)

                await scan_watchlist(session, bot)
                await check_copy_signals(bot)
                if scan_count % 12 == 0:
                    await check_take_profits(bot, session)
                    await check_performance(bot, session)

                # Cleanup stale trackers every hour
                if scan_count % 40 == 0:
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
                    for t in [momentum_tracker, take_profit_watch]:
                        stale = [m for m,d in t.items()
                                 if d.get("first_seen", datetime.now(timezone.utc)) < cutoff
                                 and not d.get("n_2x")]
                        for m in stale: t.pop(m, None)

                hour = datetime.now(timezone.utc).hour
                if hour==9 and last_summary_hour!=9:
                    await send_daily_summary(bot); last_summary_hour=9
                elif hour!=9: last_summary_hour=hour

                log.info(f"Scan #{scan_count} — {alerted} alerts")
            except Exception as e:
                log.error(f"Scan error: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

# ── Boot ───────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, fn in [
        ("start",cmd_start),("threshold",cmd_threshold),("watch",cmd_watch),
        ("unwatch",cmd_unwatch),("watchlist",cmd_watchlist),("addwallet",cmd_addwallet),
        ("smartwallets",cmd_smartwallets),("blacklist",cmd_blacklist),
        ("tracking",cmd_tracking),("summary",cmd_summary),
        ("status",cmd_status),("analyze",cmd_analyze),
    ]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def run():
        await app.initialize()
        await app.start()
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                "✅ *AlphaScan v5 — Ground-up Rebuild*\n\n"
                "🔧 *Fixed in this version:*\n"
                "• Scanner 1: Helius DAS (no more transaction parsing)\n"
                "• Scanner 2: Mandatory alive check kills rugged tokens\n"
                "• Scanner 3: DexScreener new pairs by creation time\n"
                "• Pre-flight: Every token checked BEFORE scoring\n\n"
                f"Threshold: {alert_threshold}/99 | Scan: {SCAN_INTERVAL}s\n\n"
                "Paste any CA to analyse. /status for info."
            ),
            parse_mode="Markdown"
        )
        await asyncio.gather(scan_loop(app.bot), app.updater.start_polling())

    asyncio.run(run())

if __name__ == "__main__":
    main()

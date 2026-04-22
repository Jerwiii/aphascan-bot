# AlphaScan.sol — Telegram Bot

Scans Solana for new tokens every 5 minutes and sends Telegram alerts when alpha score > 75.

---

## Step 1 — Get your Telegram Chat ID

1. Open Telegram and send any message to your new bot
2. Visit this URL in your browser (replace YOUR_BOT_TOKEN):
   https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates
3. Look for "chat":{"id": XXXXXXXXX} — that number is your Chat ID

---

## Step 2 — Deploy to Railway (free, no credit card)

1. Go to https://railway.app and sign up with GitHub
2. Click "New Project" → "Deploy from GitHub repo"
3. Upload this folder as a new GitHub repo (or use Railway's drag-and-drop)
4. Once deployed, click your service → "Variables" tab
5. Add these 3 environment variables:

   TELEGRAM_TOKEN      = your bot token from BotFather
   TELEGRAM_CHAT_ID    = your chat ID from Step 1
   HELIUS_API_KEY      = your Helius API key

6. Optional variables (have defaults):
   SCAN_INTERVAL_SECONDS = 300   (how often to scan, default 5 min)
   ALPHA_THRESHOLD       = 75    (minimum score to alert, default 75)

7. Click "Deploy" — Railway will install dependencies and start the bot

---

## What the bot sends you

Every alert includes:
- Token mint address
- Alpha score (0–99) with visual bar
- Score breakdown (holders, concentration, TX velocity, age)
- Rug risk rating (LOW / MEDIUM / HIGH)
- Direct links to DexScreener and Birdeye

---

## Scoring system

| Signal             | Max points | What it measures                        |
|--------------------|------------|-----------------------------------------|
| Holder count       | 25         | More unique holders = healthier token   |
| Supply concentration | 25       | Low top-holder % = lower rug risk       |
| TX velocity        | 25         | Recent transaction activity             |
| Token age          | 15         | Older = more established                |
| **Total**          | **90**     | Score > 75 triggers alert               |

---

## Files

- bot.py           — main bot logic
- requirements.txt — Python dependencies
- railway.toml     — Railway deployment config
- README.md        — this file

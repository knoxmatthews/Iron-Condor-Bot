# SPY Iron Condor Bot 🤖

Automated Iron Condor options strategy on SPY, connected to Alpaca paper trading.

---

## What It Does
- Runs every weekday at **9:45 AM ET**
- Checks if you're already in a trade (skips if so)
- Finds the nearest weekly expiry for SPY options
- Selects the 4 legs of an Iron Condor (short put, long put, short call, long call)
- Places market orders automatically via Alpaca

---

## Setup

### 1. Install Python dependencies
```bash
pip install alpaca-py schedule python-dotenv
```

### 2. Create your `.env` file
Create a file called `.env` in the same folder as the bot:
```
ALPACA_API_KEY=your_api_key_here
ALPACA_SECRET_KEY=your_secret_key_here
```

Get these from: https://app.alpaca.markets → Paper Trading → API Keys

### 3. Run the bot
```bash
python iron_condor.py
```

---

## Files
| File | Purpose |
|---|---|
| `iron_condor.py` | Main bot code |
| `.env` | Your API keys (never share or commit this) |
| `.gitignore` | Keeps your keys off GitHub |
| `iron_condor.log` | Auto-generated trade log |

---

## Important Notes
- Bot is set to **PAPER trading** by default (`PAPER = True` in the code)
- To go live, change `PAPER = False` — **only do this when you're confident it works**
- Options trading must be enabled on your Alpaca account
- Your computer must be **on and running** for the bot to execute trades

---

## Adjustable Settings (top of `iron_condor.py`)
| Setting | Default | What it does |
|---|---|---|
| `QTY` | 1 | Number of contracts per leg |
| `STRIKE_WIDTH` | 2 | How wide the wings are |
| `PAPER` | True | Paper vs live trading |

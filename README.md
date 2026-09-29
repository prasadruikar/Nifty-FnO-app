# Trading Cockpit — complete project

Two engines, one calm screen. Everything lives in **this one folder**.

- **StockRanker** (`nse_scanner.py`) — free NSE option-chain data. Finds
  **which** stocks smart money is positioned in (conviction).
- **Orderflow** (`orderflow_scanner.py`) — Upstox depth data. Finds **where**
  to enter — proven absorption levels that persist across days.
- **Cockpit** — combines both into one steady dashboard that shows only
  high-conviction stocks sitting at a proven entry level.

---

## 1. Install (once)

```bash
pip install -r requirements.txt
```

## 2. Set up Upstox (once)

1. Create an app at https://account.upstox.com/developer/apps
2. Set its **Redirect URI** to exactly: `http://localhost:3000/callback`
3. Open `config.py` and paste your **API key**, **secret**, and redirect URI.

## 3. Run — every trading day

```bash
python upstox_auth.py    # log in once each morning (saves today's token)
python start.py          # launches BOTH engines + opens the cockpit
```

The cockpit opens at **http://localhost:5062**. Press **Ctrl+C** in the
terminal to stop everything cleanly.

> Run it during market hours (9:15 AM – 3:30 PM). Orderflow needs a live
> market — the order book is frozen when the market is closed.

---

## The one screen — "Conviction Setups"

Each card = a stock that is **high-conviction positioned** AND **at/near a
proven absorption level**, both pointing the same way. It shows:

- **BUY / SELL** and the exact **entry level**
- a **quality score** (conviction + level strength + live-flow agreement)
- **AT ENTRY** (gold) = price is on the level now · **APPROACHING** = get ready
- conviction, level strength, times the level held, money absorbed, age
- whether live flow currently agrees

**Steady, not flickering.** Setups appear, hold, and fade slowly — so you can
actually read and act. An empty screen means no A+ setup right now. That's
correct most of the time — patience is the edge.

Two more tabs: **Live Flow** (all F&O ranked by raw orderflow, reference) and
**Search** (any F&O stock's live flow + proven levels on demand).

---

## What each file does

| File | Role |
|------|------|
| `start.py` | **One-command launcher** — runs both engines, opens the cockpit |
| `config.py` | Your Upstox keys + settings (keep private) |
| `upstox_auth.py` | Daily Upstox login → saves `access_token.json` |
| `instruments.py` | Maps F&O symbols → Upstox instrument keys |
| `depth_feed.py` | REST/websocket data abstraction (swap with one config line) |
| `flow_engine.py` | Orderflow signals (imbalance, absorption, aggression…) |
| `levels.py` | Multi-day proven-level engine (money-normalized, persistent) |
| `merge.py` | Combines conviction + levels, keeps the screen steady |
| `orderflow_scanner.py` | Orderflow orchestrator + web server (serves cockpit) |
| `orderflow_ui.html` | The cockpit dashboard |
| `nse_scanner.py` | StockRanker — OI conviction engine (feeds the cockpit) |
| `scanner_ui.html` | StockRanker's own dashboard (optional, on :5050) |
| `analyze_scans.py` | Honest CSV analysis tool for StockRanker logs |

## Files created while running (data — auto-generated)

| File / folder | What |
|------|------|
| `access_token.json` | Your daily Upstox token |
| `conviction_bridge.json` | Live conviction hand-off (StockRanker → cockpit) |
| `levels_store.json` | Proven absorption levels — **persists across days** |
| `instruments_cache.json` | Cached F&O instrument map |
| `flow_data/` | Orderflow scan CSV logs |
| `scan_data/` | StockRanker scan CSV logs |

---

## Optional: run just one tool

- **StockRanker alone** (no Upstox needed): `python nse_scanner.py` → :5050
- **Orderflow alone**: `python orderflow_scanner.py` → :5062 (conviction
  section stays empty until StockRanker also runs)

## Optional: upgrade to Upstox Plus (30-level depth via websocket)

1. `pip install upstox-python-sdk`
2. In `config.py`: `DATA_SOURCE = "websocket"`, `WS_DEPTH_MODE = "full_d30"`
3. Run as normal. Nothing else changes — the engine is source-agnostic.

---

## Honest note (read this)

Everything here is **unvalidated**. Two signals agreeing is better than one,
but "better" isn't "proven." **Paper trade only.** Let it log to `flow_data/`
and `scan_data/`, watch whether the aligned setups actually precede moves,
and confirm on your chart before risking a rupee. A level that held is
tradeable; a level that broke is a trap. Exit by 2:30 PM. One trade a day.

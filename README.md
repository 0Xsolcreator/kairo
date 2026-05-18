# Kairo

**Agentic monitors with incognito mode.**

Kairo is an agentic terminal for DeFi automation — local model, local execution, local keys. Powered by Qvac, it runs entirely on your device. Your positions are analysed and executed on-chain without a single byte leaving your machine.

---

## Demo

https://github.com/user-attachments/assets/04e4d269-c63e-4cb5-b0c1-8a3d837a63b7

---

## The Problem

Existing DeFi automation tools force a choice: hand over your keys to a custodial service, or pipe your wallet activity and strategy through a cloud AI that logs and analyses your behaviour.

You get automation. You lose custody and privacy.

**Kairo eliminates that tradeoff.**

---

## How It Works

Kairo is built on two core components.

**Qvac** runs a local language model on your device. It understands your intent and manages monitors through natural language — no cloud, no external AI provider, no one interpreting your strategy but your own machine.

**Vigil** is Kairo's agentic runtime. A structured decision layer that continuously monitors live protocol data, resolves the optimal action, and executes on-chain. Every signal, every decision, every transaction flows through Vigil.

---

## Privacy Model

Every monitor gets its own isolated wallet, derived on-device using HD derivation — never reused, never shared. Funds move through **Umbra stealth addresses**, breaking the on-chain link between your identity and your positions.

No observer can connect your holding wallet to your monitor activity.

---

## Components

### Agent

The agent is the entry point for everything. It receives your natural language input, classifies intent, and decides what to do — start a monitor, check signals, stop a monitor. Fast intents are handled directly without touching the model. Complex ones are reasoned through by Qvac before acting. The agent maintains conversation history across sessions so context is never lost.

### Vigil Runtime

Vigil is the operational core of Kairo. It runs continuously in the background, independent of the agent conversation. Vigil is built in three layers:

**Collection** — each active monitor runs its own polling task, fetching live data from protocol APIs on a defined interval. Multiple monitors run concurrently, each isolated from the others.

**Analysis** — raw poll data is passed to an analyzer that resolves it into a signal: which protocol is better, by how much, and whether the difference is meaningful enough to act on. Volatile or noisy signals are filtered before they reach execution.

**Execution** — when a signal clears the analysis layer, Vigil builds and runs an action chain: a sequence of on-chain steps (withdraw, transfer, deposit) that moves funds to the better-yielding protocol. Chains are guarded — the same signal must repeat consecutively before any chain fires, and a cooldown prevents re-execution immediately after. Both guards survive restarts.

### Protocol Clients

Kairo communicates directly with DeFi protocols through dedicated clients — no intermediary, no aggregator. Each client handles request construction, transaction building, signing, and on-chain submission. Adding a new protocol means adding a new client; nothing else in the stack changes.

### Wallet and Privacy

Each monitor gets its own operating wallet, derived deterministically from a master seed using HD derivation. The master seed lives on your device and is never transmitted. Private keys are re-derived on demand and never stored.

When a monitor opens, funds are routed through Umbra stealth addresses so the on-chain link between your holding wallet and the monitor's activity is permanently broken. When a monitor closes, funds are swept back to your holding address through the same privacy layer.

### Persistence

Monitor state, wallet records, execution history, and poll data are stored locally in SQLite(this will be upgragde to postgres). Monitors survive restarts — Vigil picks up where it left off without any manual intervention. Execution cooldowns are also restored from history, so the debounce guards remain intact across restarts.

---

## Supported Monitors

### `deposit_earn`

Compares Jupiter Lend vs Kamino KVaults APY for a selected token and automatically rebalances between them.

**Supported tokens:** wSOL, USDC, USDT

**Signals:**

| Signal    | Meaning                                               |
| --------- | ----------------------------------------------------- |
| `JUPITER` | Jupiter APY is meaningfully higher — move funds there |
| `KAMINO`  | Kamino APY is meaningfully higher — move funds there  |
| `EQUAL`   | Yields within 0.1% — no action taken                  |
| `ERROR`   | One or both protocols returned no data                |

**Execution guards:**

- 3 consecutive identical signals required before any on-chain action fires
- 10-minute cooldown between executions per monitor
- Both guards persist across restarts

---

## Requirements

- Python 3.13+
- [Bun](https://bun.sh) (for running the Qvac model server)
- A Solana RPC endpoint
- Jupiter API key (from [beta.jup.ag/api](https://beta.jup.ag/api)) — required for executor actions

---

## Setup

**1. Install Python dependencies**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**2. Install the Qvac model server**

```bash
bun install
```

**3. Run**

```bash
./dev.sh
```

This starts the Qvac model server in the background (logs → `qvac-server.log`) and launches the Kairo terminal once the server is ready.

**Shared wallet mode (development only)**

```bash
QVAC_WALLET_MODE=shared ./dev.sh
```

All monitors share a single wallet. Do not use in production.

---

## Usage

Kairo is controlled entirely through natural language.

```
start deposit earn monitor     — set up a new monitor (guided setup)
list active monitors           — show all running monitors
get signals                    — latest APY recommendation
stop monitor <id>              — stop a specific monitor
```

On first monitor setup, Kairo will prompt for:

- Your holding wallet address (funds return here when a monitor closes)
- Token selection (wSOL / USDC / USDT)
- Jupiter API key

Monitor state, wallet records, and execution history persist in `agent_data.db`. Conversation history persists in `agent_memory.db`. Both are created automatically on first run.

---

## Extending Vigil

Adding a new monitor type requires four components:

1. **Scope schema** — define the monitor's parameters in `agent/schemas/monitor.py`
2. **Poller** — implement `BasePoller` in `agent/polling/`
3. **Analyzer** — implement `BaseAnalyzer` in `agent/analyzer/`, define a `Signal` enum
4. **Executor** — implement `ChainBasedExecutor` in `agent/executor/`, map signals to `ActionChain`s

Register the poller, analyzer, and executor in `agent/engines.py`. Add a launch function in `agent/services/`.

---

## License

MIT — see `LICENSE`.

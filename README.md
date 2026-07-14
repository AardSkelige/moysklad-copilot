# moysklad-copilot

A Telegram bot — corporate assistant on top of [MoySklad](https://www.moysklad.ru) (Russian ERP) for a small manufacturing company. It automates financial accounting, continuously audits inventory and money documents with an LLM, and manages production tasks by voice.

Running in production: the sole operator is the company owner — the bot replaces his manual reviews of records kept by several employees working under a single MoySklad account.

## Modules

### 💰 Finance
- Create incoming/outgoing payments and cash orders directly in MoySklad (no local copy — a single source of truth)
- Operation categories mapped to MoySklad expense items, one-way synchronization
- Excel export of operations for any period

### 🔍 Accounting audit
Two-layer architecture: **deterministic detectors + an LLM analyst**.

- 17 checks collect signals with facts from the API: duplicate payments, zero prices in receipts, stock-adjustment deviations from FIFO cost, late edits of posted documents, stale drafts, negative stock, counterparty balances (settlements are computed manually — the pricing plan lacks the report), order/receipt mismatches, and more.
- The LLM issues a verdict on every signal — "problem / normal / ask a human" — with an explanation and fix options. The code never decides nuanced cases on its own.
- A conversational agent per finding: the owner discusses the problem, the agent inspects documents and change history through tools and prepares a fix — applied only after an explicit confirmation button.
- Incremental scheduled runs plus a full nightly scan (APScheduler).

### 📝 Comment review
The LLM normalizes document comments to the corporate standard ("Name: what was done, trailing period"), fixes typos using a domain dictionary, and infers the likely author from areas of responsibility. The owner gets "current / proposed" cards with buttons; the edit is written to MoySklad immediately. Separate prompts for shipments (carrying facts over to the linked order) and financial documents (payment purpose + comment).

### 🏭 Production
The agent creates and edits production tasks by voice (Groq Whisper) or text: "make 40 shampoos", "move 305 to done". An LLM ↔ tools loop with a preview and confirmation before any write.

## Architecture

```
core/           config (.env), DB models, logger
handlers/       aiogram routers: finance / audit / production
services/       business logic; audit/checks/ — detectors, analyst — the LLM layer
integrations/   MoySklad API clients (base + domain-specific), Excel
shared/         keyboards, FSM states, constants, session_scope
```

Key decisions:

- **Engine vs knowledge.** Prompt code lives in the repository; company-specific context (team, domain dictionary, routing rules) lives in `services/audit/team_context_local.py` outside git. The repository ships anonymized example values, and the bot is fully functional without the local file.
- **The LLM proposes — a human confirms.** Not a single write to MoySklad happens without an explicit button press by the owner.
- **Money is whole kopecks only** (INTEGER); rubles exist only at the display layer.
- **Operations live in MoySklad**; the local SQLite stores nothing but the category reference.

## Stack

Python 3.12 · aiogram 3 · SQLAlchemy 2 (async SQLite) · aiohttp · APScheduler · DeepSeek API (OpenAI-compatible client) · Groq Whisper · openpyxl · Docker

## Running

```bash
cp .env.example .env          # fill in tokens and IDs
pip install -r requirements.txt
python main.py                # the DB schema is created automatically
```

Docker: `docker compose up -d`. Server deploy: `./deploy.sh` (address in `DEPLOY_SERVER`).

## Tests

```bash
pytest tests/unit/ -v          # 177 tests
pytest tests/unit/ -m audit    # by marker: fsm, finance, validation, access, audit
```

FSM workflows, audit detectors on canned API responses, comment review with a fake LLM, amount parsing, access middleware.

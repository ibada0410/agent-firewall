# Agent Firewall

**A security checkpoint for AI agents.** Agent Firewall sits between an AI agent and its tools as an MCP proxy. Every tool call is inspected before it runs. Safe calls pass in well under a millisecond. Calls that could leak data or destroy records stop in a holding bay, and a person sees exactly where the request came from before allowing or blocking it.

Built for Agentika 2.0 (NaSCon 2026), theme *Human + AI: Better Together*.

## What it does

| Feature | Where to see it |
|---|---|
| Intro page for judges: an animated attack, how the checks work, live benchmark numbers | Intro |
| Guided demo: ten steps through a real attack, driven by live events; skip or close at any time (Esc) | Intro → Start the guided demo |
| Help: every screen, the score, a glossary, reviewer access, connecting your own agent, searchable | Help |
| Live checkpoint: each real tool call travels through the gate; safe ones pass, risky ones wait in the holding bay | Console |
| Choose the agent per task: the real model, or the offline simulated agent that never fails on stage | Console → Task |
| MCP proxy that checks every `tools/call` before it reaches the real tool server | Console → ledger |
| Taint tracking: every address, link, account, ID and instruction read from an email is remembered with its source | Review → where this action came from |
| Policy rules in plain YAML (`policy.yaml`), plus rules learned from reviewer decisions | Policy |
| Intent check: does the call fit what the user asked? (`gpt-oss-20b` with a Groq key, rules without) | Review → score bar |
| Llama Prompt Guard 2 scores every untrusted email for injection as it is read (with a Groq key) | Review → manipulation signs |
| Manipulation patterns: override phrases, secrecy, fake authority, hidden text | Review → why it was stopped |
| Risk score 0–100 with every point explained | Review |
| Held calls wait for a human; nobody answering means **blocked** (fail-safe) | Review → decision bar |
| "Always allow / Always block" become revocable rules | Policy → learned rules |
| X-ray view of text hidden from people (white text, zero-size text, comments) | Inbox |
| Attack playground: plant your own poisoned email and run the agent | Inbox → Try to trick the agent |
| Real side effects: sent mail and deleted records show up, so a miss would be visible | Workspace |
| Benchmark: 20 attacks + 22 ordinary tasks, each in a fresh workspace, judged by outcome | Benchmark |
| Searchable audit log of every call and decision | Audit |
| `/mcp` endpoint so any MCP client can use the guarded tools (off unless `MCP_TOKEN` is set) | `scripts/mcp_client_demo.py` |
| Optional n8n alert when a call is held | `n8n/held-call-alert.json` |

### Risk score

```
policy   allow 0 · ask 40 · deny 100 (deny is refused outright)
+ 30     an argument contains something that came from an untrusted source
+ 25     the call does not fit the user's task
+ 15     untrusted content in this task tried to give the agent orders (patterns or Prompt Guard)
0–29 pass · 30–69 hold (amber) · 70–100 hold (red)
```

## Architecture

```
 user task ──► agent (gpt-oss-120b, or the simulated agent) ──MCP──► FIREWALL PROXY ──MCP──► company tool server
                                                                     │ policy · taint                 (inbox, invoices,
                                                                     │ intent · injection              records, outbox)
                                                                     ▼
                                                         risk ≥ 30 → held → reviewer (console, n8n alert)
```

* `app/firewall/` – engine (`engine.py`), MCP proxy (`proxy.py`), checks (`policy.py`, `taint.py`, `intent.py`, `injection.py`), review broker (`review.py`)
* `app/workspace/` – the fictional company's data and its real MCP tool server
* `app/agent/` – Groq client with retries and backoff, LLM tool-calling loop, simulated agent
* `app/bench/` – scenarios and the outcome-based benchmark runner
* `app/static/` – the console (vanilla JS, no build step), live over Server-Sent Events
* SQLite for the audit log, sessions, learned rules and benchmark runs

## Run locally

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt     # macOS/Linux: .venv/bin/pip
cp .env.example .env                                   # set REVIEWER_PASSWORD; add GROQ_API_KEY for the real model
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000. First-time visitors land on the Intro page; press **Start the guided demo**, or go to the Console, pick **Go through my inbox and take care of anything that needs doing**, and press **Run task**.

* **With `GROQ_API_KEY`:** `openai/gpt-oss-120b` plans the tool calls, `openai/gpt-oss-20b` runs the intent check and `meta-llama/llama-prompt-guard-2-86m` scores incoming email.
* **Without a key:** the *simulated agent* runs. It is deliberately vulnerable (it obeys instructions it finds in emails) and the intent check uses transparent rules. Everything else is identical.

### With Docker

```bash
docker build -t agent-firewall .
docker run -p 8000:8000 --env-file .env agent-firewall
```

## Deploy on Alternate Clouds

1. Push this folder to a GitHub repository (`.env` is git-ignored and never uploaded).
2. In the dashboard: **Add Service → GitHub Repo**, pick the repository.
3. **Source tab:** set `PORT` to `8000`.
4. **Variables tab:** `REVIEWER_PASSWORD` (demo-only), `SESSION_SECRET` (long random string), `GROQ_API_KEY`, `SECURE_COOKIES=true`, and optionally `PUBLIC_BASE_URL`, `ALERT_WEBHOOK_URL`, `MCP_TOKEN`.
5. Click **Rebuild**, wait for it to succeed, then **Deploy**. The Logs tab should show `Agent Firewall ready`.
6. Check it: `python scripts/api_flow.py https://<your-service-url> <reviewer-password>` runs the demo task through the public API.

Variables first, then Rebuild, then Deploy. A push alone does not update the live app.

## Tests and results

```bash
.venv/Scripts/python -m pytest -q          # 34 tests: checks, MCP flow, API, benchmark
.venv/Scripts/python scripts/bench.py      # benchmark in the terminal (add --llm for the real model)
.venv/Scripts/python scripts/ui_check.py   # drives the real UI with Playwright (Edge), saves screenshots
.venv/Scripts/python scripts/tour_check.py out simulated   # walks the guided demo step by step
```

**Simulated agent benchmark:** 20/20 attacks stopped, 0 caused harm, 0/20 false alarms, median check time about 0.2 ms. Two ordinary tasks delete a record the user named; the policy always sends those to a reviewer and they count as correct when held.

**Real model (`gpt-oss-120b`), triage task on the demo inbox:** across repeated runs the model was fooled by the hidden HTML comment (tried to delete two records) and the fake IT audit (tried to email the contact list to a look-alike domain). Every one of those calls was held at risk 100 and blocked; the legitimate invoice summary to the CFO went through. Earlier benchmark runs with read-only tasks showed the model mostly ignores injections it has no reason to act on: 0 harm, 0 false alarms.

## Honest limits

* The benchmark scenarios were written by this team, and the simulated agent was written to obey instructions in emails. That shows the firewall catches the attacks it was designed for; it is not proof against every possible attack.
* A full benchmark with the real model is slow on Groq's free tier (rate limits); the console demo with the real model runs in about 20–40 seconds.
* One demo reviewer account, configured by environment variables. There is no multi-user role system.
* The workspace is a fictional company in SQLite. Email is never really sent; "sent" mail goes to the outbox table. Data resets on each redeploy.
* Instructions hidden inside images or PDFs are not analysed yet.

All names, addresses and amounts are invented. Domains use the reserved `.example` TLD.

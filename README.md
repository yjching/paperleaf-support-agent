# Paperleaf support agent

A conversational support agent for Paperleaf, a fictional online bookstore. It
handles **order status** and **returns/refunds** properly, and answers general
policy questions from a small grounded document set.

Built on the Claude Messages API with a hand-written tool-use loop — no agent
framework, so the write gate, the retry behaviour, and the logging are all in
one place you can point at.

## Run it

```bash
pip install -r requirements.txt

# Put your key in .env next to agent.py (git-ignored):
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env

python3 server.py                         # web UI at http://localhost:8000
python3 agent.py                          # interactive CLI
python3 agent.py --script demo_script/demo_script_alex.txt   # scenario 1: order status + changed-mind return (alex)
python3 agent.py --script demo_script/demo_script_andy.txt   # scenario 2: damaged-item return journey (andy)
python3 agent.py --script demo_script/demo_script_morgan.txt # scenario 3: refusal + escalation (morgan)
python3 test_tools.py                     # offline checks, no API key needed
python3 improvement.py                    # review captured misalignments
```

The web UI is a free-text box. A side panel shows every tool call, its raw result, and any blocked action — the
same events the CLI prints. It's a view over `run_turn`, not a second
implementation. Stdlib only: no Flask, no build step. The three demo
customers below are best driven from the CLI with `--script`, which replays
them turn by turn.

Three demo customers, together covering four core use cases — order
status, a damaged-item return, a change-of-mind return, and escalation — and
all three core requirements (multi-turn, tool use,
clarifying question):

- **alex@example.com** — order status, then the third reason-routing branch
  (see decision #4): a standard changed-mind return, the only demo that
  exercises the fee-deducted path rather than the damaged/no-fee one. No
  identity given up front, so the agent has to ask before it can look
  anything up. Two delivered orders both contain a book, so "I want to
  return my book" is ambiguous here too — the same clarifying question
  andy's account forces. `demo_script.txt` (order status only, no return)
  still exists as a minimal standalone example but isn't wired into the
  web UI or listed above.
- **andy@example.com** — the full return journey and the multi-turn
  requirement. Three orders, each named by title in every list the agent
  shows — a customer asking "where's my order" usually can't recall a SKU
  or order ID. Two delivered orders both contain books, so "I want to return
  my book" is ambiguous and the agent has to ask which one. Once eligibility
  passes, the reason routes the offer: damaged needs no shipment either
  way — a free replacement (after `check_stock` confirms one's actually on
  hand) or a refund, customer's choice; anything else gets the standard
  fee-deducted return. The offer is grounded via a live `search_policy` call,
  not written into the prompt. Confirming a replacement means confirming the
  shipping address on file, not a generic "shall I proceed," and it actually
  creates a new order the customer could later ask `get_order` about. A
  third order on the same account (`ORD-1004`, a $245 Knuth box set) is
  priced above the $150 human-approval threshold and would come back
  `requires_human_approval: true` from `check_return_eligibility` — that
  branch is real and covered by `test_tools.py`, but no demo script drives
  a conversation through it, so it's never seen live.
- **morgan@example.com** — the refusal. Five orders on the account: one
  delivered 31 days ago (one day past the 30-day window — the refusal
  target), one still in transit, and three older ones (Atomic Habits,
  Sapiens, The Psychology of Money) that exist so morgan's "I've been a
  customer for ages" pushback is a claim the agent can actually verify
  against real order history, not something it has to take on faith or
  flatly ignore. `get_order(email)` caps at the 4 most recent, so the
  oldest of the five is trimmed from that view but still reachable by
  `order_id`. The eligibility engine says no on the delivered one and the
  agent doesn't negotiate; when the customer pushes back or asks for a
  person, it hands off instead of bending the rule itself, acknowledging
  the specific thing that makes the pushback sympathetic (missing the
  window by a day) rather than a generic "I understand your frustration."
  This is the clearest demonstration of the core thesis: the model runs
  the conversation, it never decides the policy — but a customer this
  close to the line is exactly who a good CX design should get to a
  human.

`.env` is read automatically at startup. An `ANTHROPIC_API_KEY` already
exported in the shell takes precedence over the file.

Tool calls and their results are printed dimmed as they happen, so you can watch
the loop work.

## Architecture

```
User turn (CLI: agent.py, or web: server.py — both call the same run_turn)
  → Orchestrator (agent.py:run_turn — model → tool_use → execute → tool_result → repeat)
      ├── System prompt: grounding rule, ambiguity/clarify rule, the numbered
      │     return flow, remedy routing, confirmation semantics, the handoff
      │     script, loyalty-claim verification, tone rules — behaviour only,
      │     no policy facts (detailed below)
      ├── SessionState (session.py): customer_email, which (order,sku) eligibility
      │     checks and stock checks have run, and whether the conversation has
      │     been handed off — every write gate below reads from this, nothing else
      ├── Tool schemas + executors (tools.py) — the write gates live here
      │     ├── get_order(order_id | email)          READ — email lookups capped
      │     │                                          at the 4 most recent orders
      │     ├── search_policy(query)                 READ → retrieval.py
      │     ├── check_return_eligibility(order, sku) → eligibility.py (DETERMINISTIC)
      │     ├── check_stock(sku)                     READ — exact match, product master
      │     ├── create_return(...)                   WRITE — 5 gates, idempotent
      │     ├── create_replacement(...)               WRITE — 4 gates, idempotent,
      │     │                                          appends a real new order
      │     └── transfer_to_human(...)                WRITE — 1 gate (not already
      │           handed off, so a retry can't double-queue), sets
      │           SessionState.handed_off, which then blocks both writes above
      ├── eligibility.py: parses returns.md, decides eligibility — a pure function
      ├── retrieval.py: chunking, mocked embeddings, vector search over policies/*.md
      ├── improvement.py: every guardrail rejection (a tool_result with is_error) is
      │     classified and logged to improvements.jsonl — the model's own misfires,
      │     not customer-facing outcomes
      └── Mock backend (data.py) + policy corpus (policies/*.md)
  → Response
```

## Assumptions

We don't handle actual billing. A return or replacement means starting the
process and creating the record — no real payment exchange happens on
either side. `create_return` records a refund amount and method; nothing
actually charges or refunds a payment instrument.

## Known limits

Deliberate, given the time box:

- The customer database is mocked (`data.py`) — a hardcoded, in-memory list,
  not a real OMS. Orders, returns, and replacements vanish when the process
  exits.
- Retrieval is a real vector search (embed, store, cosine similarity), but
  the embeddings themselves are a mocked local hash function, not a learned
  model — see decision #5. Fine as an architecture demo at this corpus size,
  wrong on quality at a real one; a real embedding model is the first thing
  to swap in.
- Shipping labels and replacement tracking URLs are mocked
  (`mock-shipping.paperleaf.test`, `mock-tracking.paperleaf.test`) — no real
  carrier integration.
- The support queue depth is mocked (`MOCK_QUEUE_AHEAD = 4` in `data.py`) —
  a constant standing in for other customers already waiting, so a handoff
  doesn't always land on "you're number 1 of 1" the way an empty demo
  queue otherwise would.
- No authentication. Email is given up front only to satisfy the multi-turn
  and information-qualification requirements — a real deployment verifies
  identity before exposing orders.
- Policy thresholds (return window, human-approval amount) are parsed
  out of `policies/returns.md` at import time, not duplicated as constants —
  the doc is the one source of truth. A real deployment would source this
  from wherever the CX team actually owns policy, with the same constraint:
  the engine reads it, it doesn't get its own copy.
- One agent, no routing. A second intent family would want its own instruction
  set rather than a longer system prompt.

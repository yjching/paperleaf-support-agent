
## The thesis

**The model runs the conversation; it never decides the policy.**

Everything irreversible or money-touching is decided by deterministic code. The
model chooses what to say and which tool to reach for; it does not get to decide
whether a refund is owed or how much it is worth. That split is the whole design.

**Orchestration.** A `while` loop over `stop_reason == "tool_use"`
(`agent.py:run_turn`). The full assistant `content` goes back into history
each pass — not just the text, the raw `tool_use` blocks too, since
dropping them breaks the next request — and every `tool_use` gets a
matching `tool_result` in a single user message. `server.py` is a second
front door onto the identical loop — a stdlib `ThreadingHTTPServer` serving one
static page and proxying `/api/message` straight into `run_turn`, so the web
UI is a view over the agent, not a second implementation of it. Both front
doors write every turn to a timestamped transcript in `transcripts/`
(`agent.Transcript`), flushed after each line so a crash mid-run still
leaves a readable record.

**Tools.** Seven total, dispatched identically by the loop — the model
doesn't know or care which are reads and which are writes, only the
executor enforces that distinction. Four reads, all free to call:
`get_order` (by ID or email — email lookups return the 4 most recent
orders, oldest ones still reachable by ID), `search_policy` (→
`retrieval.py`), `check_stock` (exact SKU match against the warehouse
dict, not a search), and `check_return_eligibility` (→ `eligibility.py`,
the one deterministic decision in the read set). Three writes, each
gated on `SessionState` rather than the model's say-so: `create_return`
(5 gates — see decision #2), `create_replacement` (4 gates: same
eligibility/confirmation/idempotency checks, plus stock-on-hand instead
of the confirmation-only path, since there's nothing to confirm if
there's nothing to ship), and `transfer_to_human` (1 gate — refuses to
open a second queue entry if `handed_off` is already `True`; there's no
eligibility or confirmation check here, only idempotency).

**Memory.** Two separate objects with two different writers. Conversation
history (the `messages` list `agent.py` passes to every API call) is
written by the loop itself — the model's own turns and every tool result,
replayed in full each pass, which is why the agent never re-asks for an
order ID or email it already has. `SessionState` (`session.py`) is
written only by tool executors: `record_eligibility` and `record_stock`
are called from inside `tool_check_return_eligibility` and
`tool_check_stock` respectively, and `handed_off` is set only inside
`tool_transfer_to_human` — never by the model directly, and never as a
side effect of anything the model merely *says*. Every write gate reads
from `SessionState`, never from conversation history, which is the
actual mechanism behind "the model can't hallucinate its way past a
guardrail": a claim the model makes in text lives only in the memory the
gates don't consult.

**Improvement loop.** Already built, deliberately small: any time a
guardrail rejects a tool call, `run_turn` calls `improvement.log(...)`,
which classifies the failure — `not_confirmed`, `not_eligible`,
`eligibility_not_checked`, `unresolved_slot`, or `other` — and appends it
to `improvements.jsonl` with the tool, args, error, and the customer's
last message. `python3 improvement.py` reviews what's been captured,
grouped by rule with a suggested fix. It's a log of the model's own
misfires caught by code, not a judge over conversation quality — the
eval harness in "what I'd do first with more time" is the layer this
doesn't cover.

**Prompts.** `SYSTEM_PROMPT` in `agent.py` is a single string, structured
as a numbered flow rather than accreted rules: ground every answer in a
tool call, never memory; treat an ambiguous reference ("my book") as a
reason to list every match and ask, never guess or pre-emptively check
eligibility; run the three-step return flow (check eligibility once →
ask the reason only if eligible → state an ineligible result plainly and
stop, no handoff offer in the same turn); route the remedy by reason
through a live `search_policy` call rather than prose baked into the
prompt; treat a customer's "yes" as confirmation only when it's their
most recent message answering a standalone question; on handoff,
acknowledge one real thing about the customer's situation and then use
one fixed offer sentence verbatim, with an explicit rule never to write
a sentence about what the agent can or can't override; verify a loyalty
claim against the order history already fetched before repeating it as
fact; and hold a fixed tone (full sentences, no exclamation marks,
amounts as `$12.34`, no re-narrating a tool call the customer never
saw). None of it states what Paperleaf's policy actually *is* — that's
retrieved through `search_policy` or computed by `eligibility.py`, same
as any other policy question. Anything that must hold exactly is in
code; anything that's a fact about Paperleaf is in `policies/`; the prompt
is left with only how to behave.

## The decisions worth defending

### 1. Eligibility is a deterministic function, not a judgement call

`evaluate_return_eligibility` is a pure function over the order and the item —
same inputs, same answer, every time. It handles undelivered orders, downloaded
ebooks, the 30-day return window, and the $150 threshold above which a human
approves the refund. The reason a customer gives never changes eligibility —
damaged items get the same 30-day window as everything else — it only changes
what's offered once something's eligible (see decision #4).

*Traded:* flexibility on edge cases the rules don't cover, and more work per
policy up front.

*Worth it:* refund correctness is a financial and compliance surface. A
hallucinated "yes, that's refundable" is real money and an angry escalation. It's
also auditable — every result carries the `rule` that fired, so you can tell a CX
lead exactly why any decision was made.

### 2. The write gate lives in the executor, not the prompt

`create_return` refuses unless five things hold: the conversation hasn't
already been handed to a person, the order and item resolve, eligibility was
actually evaluated *and* came back eligible, the customer confirmed in their
latest message, and no return already exists for that item (idempotency key
on `order:sku`).

Confirmation stays required even though returns within the window are
no-questions-asked — "no reason needed" and "no confirmation needed" are
different guarantees. Picking an item off a list answers *which* order, not
*whether* to start an irreversible return, so the executor still won't fire
without an explicit yes on the customer's latest message.

Once it fires, `create_return` doesn't refund on the spot — it generates an
RMA and a prepaid return label and states when the refund actually lands (on
receipt, not now), matching how real retail returns work.

*Traded:* one extra conversational turn before any return starts — measurable
friction on handle time.

*Worth it:* a guardrail the model can talk its way past is not a guardrail. The
system prompt asks for confirmation; the executor *requires* it. And a retried
tool call cannot double-file a return, which is the failure that turns one
support ticket into two. `test_tools.py` proves each gate independently.

### 3. Clarify over guess

The agent cannot act on an ambiguous "my book". Two of the mock customer's orders
contain books, so it has to ask which one — a wrong return is unrecoverable from
the customer's side, and asking costs one turn.

The same instinct governs retrieval: if `search_policy` finds nothing above the
relevance threshold it returns an explicit empty result telling the agent to say
so and offer a person, rather than letting it improvise policy.

### 4. What you're returning for changes what you're offered, not whether you can return

The reason is asked *after* eligibility, not before — it never gates the
return, it routes it:

- **Damaged/misprinted:** no return shipment either way. A free replacement
  copy (only if `check_stock` confirms one's actually on hand — an exact SKU
  lookup against the product master, not a policy question) or a refund as
  store credit or to the original payment method.
- **Everything else** (wrong edition, changed mind, arrived late, other): the
  standard return — a flat AusPost fee, deducted from the refund, prepaid
  label generated.

Two things worth being precise about, since one of these came from a real
correction:

- **"Damaged items ship back free" was already in the project's original
  `policies/returns.md`, before this build started** — inherited policy
  corpus, not something invented for the demo.
- **A swap to a different edition/format was something I added** — and it was
  wrong in a specific way: the agent was offering a format swap during a
  *damaged*-item conversation, but that's a different reason (`wrong_item`)
  with a different remedy. Removed the format-swap path entirely rather than
  re-scope it — "I changed my mind about the edition" doesn't come up nearly
  as often as it does in clothing retail, and it wasn't worth defending a
  second write path for. What replaced it is stricter: damaged items need
  *no shipment at all*, verified against actual stock before being offered,
  not "ships back free" (which still implied a shipment happening).

`create_replacement` is gated the same way `create_return` is — eligibility
checked, stock checked and non-zero, explicit confirmation, idempotent — and
it does something `create_return` doesn't: it appends a real new order to
the mock backend, so the replacement is something the customer could later
ask `get_order` about, not just an internal log entry.

*Traded:* a second write path and a stock-check dependency, for one reason
code out of five.

*Worth it:* skipping the return shipment for damage matches what Amazon,
Blurb, Books.by, and Bookshop.org actually do for damaged/defective items —
replacement is their default remedy, refund is the fallback (Barnes & Noble
scopes their formal exchange path to defective items the same way). Reverse
logistics (repackaging, inspection, restocking, deciding what happens to the
damaged copy) is real overhead retailers report as 20–65% of an item's value,
which skipping the shipment sidesteps outright. Sources:
[Amazon's return-or-replace flow](https://www.justanswer.com/consumer-protection-law/uhfto-amazon-wrong-book-delivery-return-process.html),
[Blurb](https://www.blurb.com/returns),
[Books.by](https://help.books.by/en/articles/12679267-refunds-for-readers-returns-lost-or-defective-print-orders),
[Bookshop.org](https://support.bookshop.org/en/support/solutions/articles/65000169503-how-do-i-return-an-order-that-i-don-t-want-),
[Barnes & Noble](https://help.barnesandnoble.com/hc/en-us/articles/5324602017691-Refund-and-Return-Policies),
[reverse-logistics cost overhead](https://www.reverselogix.com/industry-updates/returns-vs-replacement-how-reverse-logistics-affect-processes/),
["keep it"/returnless-refund pattern](https://corp.narvar.com/blog/keep-the-item-returns).
The changed-mind shipping fee has no retailer citation behind it — it's a
reasonable default for a fictional store, not a sourced claim.

### 5. Policy is retrieved, not memorized — even the CX-offer logic

Early on, the reason-branch behavior (offer a replacement for damage, a
fee-deducted refund otherwise) was written directly into the system prompt
as prose. That duplicated `policies/returns.md`, which already stated the
same thing for the eligibility engine to parse — two sources of truth for
one policy, which is the exact failure mode decision #1 argues against for
eligibility. Fixed by deleting the policy prose from the prompt and
requiring `search_policy` calls instead: the prompt now only says *ask the
reason, then look up what applies before saying anything* — never *what*
applies. `check_stock` stays a direct tool call rather than a retrieval
query, because it's live inventory data (what's actually in the warehouse
right now), not policy text — an exact-match SKU lookup, the kind of
question RAG is the wrong tool for in the first place.

Retrieval itself moved from IDF keyword-overlap scoring to a vector search —
chunks and the query are embedded into fixed-length vectors once, matched by
cosine similarity, same threshold-and-decline contract as before. The
embeddings are mocked (`_embed` is a deterministic local feature-hashed,
IDF-weighted bag-of-words — no learned semantics, no API key) because a real
embedding call for a ~20-paragraph corpus is a cost/complexity argument, not
a technical one. Everything downstream of the vectors — the in-memory store,
cosine similarity, thresholding, top-k — is the real mechanism a production
RAG pipeline uses; swapping in Voyage or a local sentence-transformers model
later means replacing `_embed`'s body, nothing else.

*Traded:* an extra tool round-trip per return (search_policy, then possibly
check_stock) before the agent can say anything about remedies, and a
retrieval layer with a real, named quality ceiling (lexical hashing can't
generalize past paraphrase the way real embeddings would).

*Worth it:* the prompt no longer forks from the doc customers' policy
questions are answered from. If Paperleaf's damage policy changes, it changes in
one file, and both the eligibility engine's numbers and the agent's CX offer
pick it up automatically — no prompt edit required, no chance of the two
disagreeing.

## What I'd do first with more time

**A simulation and eval harness.** Simulated customers driving every branch —
including the adversarial "I demand a refund on a 31-day-old opened book" — with
a judge on the transcript *and* an assertion on the resulting data state: did a
return actually get created, for the right amount, exactly once? A
non-deterministic system without this cannot be safely changed; you can't tell a
prompt improvement from a regression. `test_tools.py` is the deterministic half
of that harness; the conversational half is what's missing.

Runner-up: per-intent resolution rate, clustered escalation reasons, and a review
queue over every write the agent made.

**What I'd want from a real Paperleaf:** the actual OMS and returns API contracts
(especially their idempotency semantics), the real policy corpus, and an agreed
escalation SLA.

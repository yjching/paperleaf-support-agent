"""Tool definitions and executors for the Paperleaf support agent.

Two things worth noting for the walkthrough:

1. `check_return_eligibility` calls into eligibility.py's deterministic
   engine — the model reports what it says and cannot overrule it.
2. The write gate for `create_return`/`create_replacement` is enforced *here*, in
   the executor — not in the system prompt. A guardrail the model can talk
   its way past is not a guardrail.

What Paperleaf's policy actually *says* (remedies, fees, windows) lives in
policies/*.md, reached through eligibility.py (parsed, for the numbers the
engine needs) and retrieval.py (searched, for everything the model needs to
tell a customer). Nothing here duplicates that content.
"""

import json
from datetime import datetime
from typing import Dict, Tuple

import data
import retrieval
from eligibility import RETURN_REASONS, RETURN_SHIPPING_FEE, evaluate_return_eligibility
from session import SessionState

# --- Tool schemas -------------------------------------------------------------

TOOLS = [
    {
        "name": "get_order",
        "description": (
            "Look up Paperleaf orders. Provide order_id for a specific order, or "
            "email to list every order for a customer. Returns order status, "
            "delivery dates, tracking, and the items on each order. Use this "
            "before answering any question about a specific order."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Order ID, e.g. ORD-1001.",
                },
                "email": {
                    "type": "string",
                    "description": "Customer email, to list all their orders.",
                },
            },
        },
    },
    {
        "name": "search_policy",
        "description": (
            "Search Paperleaf's published policies (returns, shipping, account, "
            "payments) and return matching passages. Use this for any general "
            "policy question. If it returns no results, say you don't know and "
            "offer a human — do not answer from memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look up, in plain language.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_return_eligibility",
        "description": (
            "Decide whether one item on one order can be returned. This is the "
            "authoritative answer — report what it says and do not reason around "
            "it. Must be called before create_return. Returns the refund amount "
            "and whether a human needs to approve it. Eligibility never depends "
            "on the reason — the reason only decides what's offered once "
            "something is eligible (see create_return/create_replacement), so "
            "there is nothing to pass here for it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "Order ID."},
                "sku": {"type": "string", "description": "SKU of the item."},
            },
            "required": ["order_id", "sku"],
        },
    },
    {
        "name": "check_stock",
        "description": (
            "Check warehouse stock for a SKU — an exact lookup against the "
            "product master, not a search. Call this before offering a "
            "replacement copy for a damaged item: only offer one if stock is "
            "actually available. If it's 0, don't offer a replacement — "
            "refund is still available regardless of stock."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "SKU to check."},
            },
            "required": ["sku"],
        },
    },
    {
        "name": "create_return",
        "description": (
            "Start the return. Damaged/misprinted items need no shipment at "
            "all — the refund is issued directly, no label, nothing to send "
            "back. Every other reason generates a prepaid return shipping "
            "label and a flat fee deducted from the refund; that refund "
            "follows once the item is received at the warehouse, not now — "
            "state the net amount before asking for confirmation. This is "
            "irreversible. Only call it after check_return_eligibility said "
            "the item is eligible AND the customer has explicitly confirmed "
            "in their most recent message that they want to go ahead. Never "
            "call it to 'check' anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "Order ID."},
                "sku": {"type": "string", "description": "SKU of the item."},
                "reason_category": {
                    "type": "string",
                    "enum": RETURN_REASONS,
                    "description": (
                        "Which reason code this return is filed under. Use "
                        "'other' when the customer's reason doesn't clearly "
                        "match one of the others — don't force a fit."
                    ),
                },
                "reason_note": {
                    "type": "string",
                    "description": "The customer's reason in their own words.",
                },
                "refund_method": {
                    "type": "string",
                    "enum": ["original_payment", "store_credit"],
                    "description": (
                        "How the refund is paid out. Only offer store_credit "
                        "as a choice for damaged/misprinted items; otherwise "
                        "default to original_payment without asking."
                    ),
                },
                "customer_confirmed": {
                    "type": "boolean",
                    "description": (
                        "True only if the customer said yes to going ahead in "
                        "their latest message."
                    ),
                },
            },
            "required": ["order_id", "sku", "reason_category", "customer_confirmed"],
        },
    },
    {
        "name": "create_replacement",
        "description": (
            "Ship a free replacement copy of a damaged/misprinted item — no "
            "return of the damaged one required. Creates a new order the "
            "customer can later look up with get_order, and returns a "
            "tracking_url to hand back to them directly — don't invent your "
            "own link. Irreversible. Only call after check_return_eligibility "
            "said the original item is eligible, check_stock confirmed stock "
            "is actually available, AND the customer has confirmed the "
            "shipping address AND agreed to go ahead in their most recent "
            "message."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "Original order ID."},
                "sku": {"type": "string", "description": "SKU being replaced."},
                "customer_confirmed": {
                    "type": "boolean",
                    "description": (
                        "True only if the customer confirmed the shipping "
                        "address and said yes to the replacement in their "
                        "latest message."
                    ),
                },
            },
            "required": ["order_id", "sku", "customer_confirmed"],
        },
    },
]


TOOLS.append({
    "name": "transfer_to_human",
    "description": (
        "Put the customer in the queue for a human support agent and tell "
        "them their queue position. Use when the customer asks for a person, "
        "when a return needs human approval, when they are unhappy with an "
        "outcome you cannot change, or when they need something you have no "
        "tool for. Ask the customer first, then call this once they say yes. "
        "This puts them in a queue, it does not connect them immediately — "
        "don't imply someone is already looking at it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Short reason code, e.g. 'outside return window'.",
            },
            "summary": {
                "type": "string",
                "description": (
                    "One or two sentences the human agent needs to pick this "
                    "up without re-reading the whole conversation."
                ),
            },
        },
        "required": ["reason", "summary"],
    },
})


# --- Executors ----------------------------------------------------------------


def _err(message: str) -> Tuple[str, bool]:
    return json.dumps({"error": message}), True


def tool_get_order(args: Dict, state: SessionState) -> Tuple[str, bool]:
    order_id = args.get("order_id")
    email = args.get("email")

    if order_id:
        order = data.find_order(order_id)
        if not order:
            return _err("No order found with ID {}.".format(order_id))
        out = dict(order)
        out["shipping_address"] = data.shipping_address_for(order["email"])
        return json.dumps(out), False

    if email:
        orders = data.orders_for_email(email)
        if not orders:
            return _err("No orders found for {}.".format(email))
        state.customer_email = email
        return json.dumps({
            "orders": orders,
            "shipping_address": data.shipping_address_for(email),
        }), False

    return _err("Provide either order_id or email.")


def tool_search_policy(args: Dict, state: SessionState) -> Tuple[str, bool]:
    results = retrieval.search(args.get("query", ""))
    if not results:
        # The honest-miss path. No hit means no answer, not a plausible guess.
        return json.dumps({
            "results": [],
            "note": "No matching policy found. Tell the customer you don't have "
                    "this and offer to hand off to a person.",
        }), False
    return json.dumps({"results": results}), False


def tool_check_return_eligibility(args: Dict, state: SessionState) -> Tuple[str, bool]:
    order = data.find_order(args.get("order_id", ""))
    if not order:
        return _err("No order found with ID {}.".format(args.get("order_id")))

    item = data.find_item(order, args.get("sku", ""))
    if not item:
        return _err(
            "No item with SKU {} on order {}. Items on that order: {}".format(
                args.get("sku"),
                order["order_id"],
                ", ".join(
                    "{} ({})".format(i["title"], i["sku"]) for i in order["items"]
                ),
            )
        )

    result = evaluate_return_eligibility(order, item)
    result["order_id"] = order["order_id"]
    result["sku"] = item["sku"]
    result["item_title"] = item["title"]

    # Recorded here so the write gate can verify it actually happened.
    state.record_eligibility(order["order_id"], item["sku"], result)
    return json.dumps(result), False


def tool_check_stock(args: Dict, state: SessionState) -> Tuple[str, bool]:
    sku = args.get("sku", "")
    quantity = data.stock_for(sku)
    state.record_stock(sku, quantity)
    in_stock = quantity > 0
    offer_note = (
        "A replacement copy is available and would be ready to ship if "
        "you'd like to place a replacement order."
        if in_stock else
        "No replacement copies are in stock, so only a refund can be offered."
    )
    return json.dumps({
        "sku": sku,
        "quantity": quantity,
        "in_stock": in_stock,
        "offer_note": offer_note,
    }), False


def tool_create_return(args: Dict, state: SessionState) -> Tuple[str, bool]:
    order_id = args.get("order_id", "")
    sku = args.get("sku", "")

    # Gate 0 — a person already owns this conversation. Once handed off, the
    # agent narrates that fact but must not keep taking write actions itself —
    # same "stop working the issue yourself" rule the prompt states, enforced
    # here so it can't be talked past.
    if state.handed_off:
        return _err(
            "This conversation has already been handed to a person. Don't "
            "create the return yourself — tell the customer it's with the "
            "human agent."
        )

    # Gate 1 — required slots resolved.
    order = data.find_order(order_id)
    if not order:
        return _err("No order found with ID {}.".format(order_id))
    item = data.find_item(order, sku)
    if not item:
        return _err("No item with SKU {} on order {}.".format(sku, order_id))

    # Gate 2 — eligibility was actually evaluated, and said yes.
    checked = state.eligibility_for(order_id, sku)
    if checked is None:
        return _err(
            "Eligibility has not been checked for this item. Call "
            "check_return_eligibility first."
        )
    if not checked.get("eligible"):
        return _err(
            "This item is not eligible for return ({}). The return was not "
            "created.".format(checked.get("reason"))
        )

    # Reason code must be one the returns system accepts; anything else is
    # filed as "other" rather than rejected, with the customer's words kept.
    category = args.get("reason_category", "other")
    if category not in RETURN_REASONS:
        category = "other"

    # The flat AusPost fee applies to every return regardless of reason
    # (policies/returns.md), but it's a separate fact, not a deduction —
    # the refund paid out is the full item price either way.
    fee = RETURN_SHIPPING_FEE

    # Gate 3 — explicit customer confirmation.
    #
    # Whether the customer's reply means yes is a language-understanding
    # question, not a rules one — that's the model's job, not a regex's. What
    # stays deterministic is that the flag can only be set on the strength of
    # a dedicated confirmation turn (SYSTEM_PROMPT requires the question to
    # stand alone, not bundled with anything else), and that this gate exists
    # at all: the model cannot skip straight to a write without the flag.
    if not args.get("customer_confirmed"):
        return _err(
            "The customer has not confirmed. Ask them to confirm the return of "
            "'{}' for a ${:.2f} refund before calling this again.".format(
                item["title"], checked["refund_amount"]
            )
        )

    refund_method = args.get("refund_method") or "original_payment"

    # Gate 4 — idempotency. A retry must not create a second return.
    key = "{}:{}".format(order["order_id"].upper(), item["sku"].upper())
    if key in data.RETURNS:
        existing = dict(data.RETURNS[key])
        existing["note"] = "A return already exists for this item; not duplicated."
        return json.dumps(existing), False

    return_id = "RET-{}".format(2000 + len(data.RETURNS) + 1)
    # Damaged items need nothing shipped back either way — no label, no
    # waiting on receipt. Everything else ships back on a prepaid label and
    # the refund follows once it's received.
    is_damaged = category == "damaged"
    record = {
        "return_id": return_id,
        "order_id": order["order_id"],
        "sku": item["sku"],
        "item_title": item["title"],
        "reason_category": category,
        "reason_note": args.get("reason_note", ""),
        "refund_amount": checked["refund_amount"],
        "return_shipping_fee": fee,
        "refund_method": refund_method,
        "status": (
            "pending_human_approval"
            if checked.get("requires_human_approval")
            else "approved"
        ),
        "return_shipping": None if is_damaged else {
            "carrier": "AusPost",
            "label_url": "https://mock-shipping.paperleaf.test/labels/{}.pdf".format(
                return_id
            ),
            "instructions": "Pack the item with the label attached and drop "
                            "it off at any AusPost location within 14 days.",
        },
        "refund_timing": (
            "No item to wait on — the refund is issued directly."
            if is_damaged
            else "Refund is issued once the item is received and inspected "
                "at our warehouse, typically 3-5 business days after "
                "drop-off — not at drop-off."
        ),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    data.RETURNS[key] = record
    return json.dumps(record), False


def tool_create_replacement(args: Dict, state: SessionState) -> Tuple[str, bool]:
    order_id = args.get("order_id", "")
    sku = args.get("sku", "")

    if state.handed_off:
        return _err(
            "This conversation has already been handed to a person. Don't "
            "create the replacement yourself — tell the customer it's with "
            "the human agent."
        )

    order = data.find_order(order_id)
    if not order:
        return _err("No order found with ID {}.".format(order_id))
    item = data.find_item(order, sku)
    if not item:
        return _err("No item with SKU {} on order {}.".format(sku, order_id))

    # Gate — eligibility was actually evaluated for the item being replaced,
    # and said yes. Same rule as create_return: can't ship a replacement for
    # an item that was never eligible to come back in the first place.
    checked = state.eligibility_for(order_id, sku)
    if checked is None:
        return _err(
            "Eligibility has not been checked for this item. Call "
            "check_return_eligibility first."
        )
    if not checked.get("eligible"):
        return _err(
            "This item is not eligible for return ({}). The replacement was "
            "not created.".format(checked.get("reason"))
        )

    # Gate — stock was actually checked, and there's some. Mirrors the
    # eligibility gate: an offer the model made up without checking is not
    # a real offer, and the executor won't act on one.
    stock = state.stock_for(sku)
    if stock is None:
        return _err(
            "Stock has not been checked for this item. Call check_stock "
            "first."
        )
    if stock <= 0:
        return _err(
            "No stock available for '{}'. The replacement was not created — "
            "offer a refund instead.".format(item["title"])
        )

    if not args.get("customer_confirmed"):
        return _err(
            "The customer has not confirmed. Confirm the shipping address "
            "and ask them to agree to the replacement of '{}' before calling "
            "this again.".format(item["title"])
        )

    key = "{}:{}".format(order["order_id"].upper(), item["sku"].upper())
    if key in data.REPLACEMENTS:
        existing = dict(data.REPLACEMENTS[key])
        existing["note"] = "A replacement already exists for this item; not duplicated."
        return json.dumps(existing), False

    address = data.shipping_address_for(order["email"])
    new_order_id = data.next_order_id(order["email"])
    new_order = {
        "order_id": new_order_id,
        "email": order["email"],
        "status": "in_transit",
        "placed_at": datetime.now().strftime("%Y-%m-%d"),
        "delivered_at": None,
        "estimated_delivery": None,
        "carrier": "AusPost",
        "tracking_number": None,
        "shipping_address": address,
        "items": [dict(item, price=0.0, condition="new", downloaded=False)],
    }
    data.ORDERS.append(new_order)

    record = {
        "order_id": order["order_id"],
        "returned_sku": item["sku"],
        "returned_title": item["title"],
        "new_order_id": new_order_id,
        "tracking_url": "https://mock-tracking.paperleaf.test/orders/{}".format(
            new_order_id
        ),
        "shipping_address": address,
        "note": "Free replacement — no return of the damaged copy required.",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    data.REPLACEMENTS[key] = record
    return json.dumps(record), False


def tool_transfer_to_human(args: Dict, state: SessionState) -> Tuple[str, bool]:
    if state.handed_off:
        return json.dumps({
            "already_transferred": True,
            "note": "This conversation is already with a person.",
        }), False

    # Joining the queue puts the customer at the back of it, so their position
    # and the queue's total length are the same number at the moment they
    # join. MOCK_QUEUE_AHEAD simulates a realistic support queue that already
    # has other customers in it — a real queue is never empty by default,
    # and "1 out of 1" every time a demo runs undersells that this is a
    # shared support line, not a dedicated one.
    queue_position = len(data.HANDOFFS) + data.MOCK_QUEUE_AHEAD + 1
    record = {
        "handoff_id": "HO-{}".format(3000 + len(data.HANDOFFS) + 1),
        "reason": args.get("reason", ""),
        "summary": args.get("summary", ""),
        "customer_email": state.customer_email,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "queue_position": queue_position,
        "queue_size": queue_position,
        "queue_note": "You're now in the queue, number {} out of {}.".format(
            queue_position, queue_position),
    }
    data.HANDOFFS.append(record)
    state.handed_off = True
    return json.dumps(record), False


EXECUTORS = {
    "get_order": tool_get_order,
    "transfer_to_human": tool_transfer_to_human,
    "search_policy": tool_search_policy,
    "check_return_eligibility": tool_check_return_eligibility,
    "check_stock": tool_check_stock,
    "create_return": tool_create_return,
    "create_replacement": tool_create_replacement,
}


def execute(name: str, args: Dict, state: SessionState) -> Tuple[str, bool]:
    """Returns (content, is_error) for the tool_result block."""
    handler = EXECUTORS.get(name)
    if handler is None:
        return _err("Unknown tool: {}".format(name))
    try:
        return handler(args, state)
    except Exception as exc:  # a tool crash should not kill the conversation
        return _err("Tool {} failed: {}".format(name, exc))

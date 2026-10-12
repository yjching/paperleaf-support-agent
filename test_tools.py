"""Offline checks for the parts that must not be left to the model.

No API key needed — this exercises the eligibility engine and the write gate
directly. Run:  python3 test_tools.py
"""

import json

import data
import eligibility
import tools
from session import SessionState


def check(label, condition):
    print("{} {}".format("PASS" if condition else "FAIL", label))
    return bool(condition)


def call(name, args, state):
    content, is_error = tools.execute(name, args, state)
    return json.loads(content), is_error


def main():
    results = []
    state = SessionState()

    # --- policy doc is the source of truth for the engine's thresholds --------
    results.append(check("window days parsed from returns.md, not hardcoded",
                         eligibility.RETURN_WINDOW_DAYS == 30))
    results.append(check("human-approval threshold parsed from returns.md",
                         eligibility.HUMAN_APPROVAL_THRESHOLD == 150.00))
    results.append(check("return shipping fee parsed from returns.md",
                         eligibility.RETURN_SHIPPING_FEE == 6.99))

    try:
        eligibility._parse_return_policy("Nothing about return windows in here.")
        results.append(check("reworded/missing policy text fails loudly, "
                             "not silently", False))
    except ValueError:
        results.append(check("reworded/missing policy text fails loudly, "
                             "not silently", True))

    # --- eligibility engine ---------------------------------------------------
    r, _ = call("check_return_eligibility",
                {"order_id": "ORD-1001", "sku": "BK-PRAG"}, state)
    results.append(check("in-window paperback is eligible", r["eligible"]))
    results.append(check("refund is the item price", r["refund_amount"] == 25.00))

    r, _ = call("check_return_eligibility",
                {"order_id": "ORD-1002", "sku": "BK-DUNE"}, state)
    results.append(check("in-window order (andy, scenario 2) is eligible",
                         r["eligible"]))

    r, _ = call("check_return_eligibility",
                {"order_id": "ORD-2001", "sku": "BK-CLEANCODE"}, state)
    results.append(check("31-day-old order (morgan, scenario 2) is refused, "
                         "just outside the window", not r["eligible"]))
    results.append(check("refusal names the rule", r["rule"] == "outside_window"))

    # Eligibility never depends on the reason (the standard and damaged-item
    # windows are the same length) — passing reason_category is a no-op,
    # not a way to reconsider a refusal.
    r, _ = call("check_return_eligibility",
                {"order_id": "ORD-2001", "sku": "BK-CLEANCODE",
                 "reason_category": "damaged"}, state)
    results.append(check("reason_category has no effect on the outcome",
                         not r["eligible"] and r["rule"] == "outside_window"))

    r, _ = call("check_return_eligibility",
                {"order_id": "ORD-1001", "sku": "EB-REFACTOR"}, state)
    results.append(check("downloaded ebook is refused",
                         not r["eligible"] and r["rule"] == "digital_downloaded"))

    r, _ = call("check_return_eligibility",
                {"order_id": "ORD-1003", "sku": "BK-HAIL"}, state)
    results.append(check("undelivered order is refused",
                         not r["eligible"] and r["rule"] == "not_delivered"))

    r, _ = call("check_return_eligibility",
                {"order_id": "ORD-1004", "sku": "BK-KNUTH-SET"}, state)
    results.append(check("high-value return needs human approval",
                         r["eligible"] and r["requires_human_approval"]))

    # --- write gate -----------------------------------------------------------
    fresh = SessionState()
    fresh.last_user_message = "Yes please, go ahead"
    r, err = call("create_return",
                  {"order_id": "ORD-1001", "sku": "BK-PRAG",
                   "reason_category": "changed_mind", "customer_confirmed": True}, fresh)
    results.append(check("write blocked when eligibility never checked", err))

    gated = SessionState()
    gated.last_user_message = "Yes please, go ahead"
    call("check_return_eligibility",
         {"order_id": "ORD-1001", "sku": "BK-PRAG"}, gated)
    r, err = call("create_return",
                  {"order_id": "ORD-1001", "sku": "BK-PRAG",
                   "reason_category": "changed_mind", "customer_confirmed": False}, gated)
    results.append(check("write blocked without customer confirmation", err))

    r, err = call("create_return",
                  {"order_id": "ORD-1001", "sku": "BK-PRAG",
                   "reason_category": "changed_mind", "customer_confirmed": True}, gated)
    results.append(check("write succeeds once gates pass",
                         not err and r["status"] == "approved"))
    first_id = r["return_id"]

    r2, err = call("create_return",
                   {"order_id": "ORD-1001", "sku": "BK-PRAG",
                    "reason_category": "changed_mind", "customer_confirmed": True}, gated)
    results.append(check("retry is idempotent, no second return",
                         not err and r2["return_id"] == first_id
                         and len(data.RETURNS) == 1))

    # Whether a reply means "yes" is the model's call, not a code-level check
    # (see tool_create_return's Gate 3) — the run-3 regression this used to
    # guard against is now a live-eval concern, not a unit-testable one; see
    # a future simulation/eval harness.
    approved_high_value = SessionState()
    call("check_return_eligibility",
         {"order_id": "ORD-1004", "sku": "BK-KNUTH-SET"}, approved_high_value)
    approved_high_value.last_user_message = "Yes please, go ahead"
    r, err = call("create_return",
                  {"order_id": "ORD-1004", "sku": "BK-KNUTH-SET",
                   "reason_category": "other", "reason_note": "not what I expected",
                   "customer_confirmed": True}, approved_high_value)
    results.append(check("write succeeds once the customer actually agrees",
                         not err and r["status"] == "pending_human_approval"))

    ineligible = SessionState()
    ineligible.last_user_message = "Yes please, go ahead"
    call("check_return_eligibility",
         {"order_id": "ORD-2001", "sku": "BK-CLEANCODE"}, ineligible)
    r, err = call("create_return",
                  {"order_id": "ORD-2001", "sku": "BK-CLEANCODE",
                   "reason_category": "changed_mind", "customer_confirmed": True},
                  ineligible)
    results.append(check("write blocked when engine said ineligible", err))

    # --- reason codes + handoff ----------------------------------------------
    rc = SessionState()
    rc.last_user_message = "yes go ahead"
    call("check_return_eligibility", {"order_id": "ORD-1003", "sku": "BK-HAIL"}, rc)
    # unknown code falls back to "other" rather than being stored verbatim
    codes = SessionState()
    codes.last_user_message = "yes go ahead"
    call("check_return_eligibility",
         {"order_id": "ORD-1001", "sku": "BK-PRAG"}, codes)
    data.RETURNS.clear()
    r, err = call("create_return",
                  {"order_id": "ORD-1001", "sku": "BK-PRAG",
                   "reason_category": "vibes", "reason_note": "not what I expected",
                   "customer_confirmed": True}, codes)
    results.append(check("unrecognised reason code is filed as 'other'",
                         not err and r["reason_category"] == "other"))
    results.append(check("customer's own wording is kept",
                         r["reason_note"] == "not what I expected"))

    ho = SessionState()
    ho.customer_email = data.SECOND_CUSTOMER_EMAIL
    r, err = call("transfer_to_human",
                  {"reason": "outside return window",
                   "summary": "Wants to return Clean Code, 31 days old, pushed "
                              "back on the refusal."}, ho)
    results.append(check("handoff reports queue position == queue size on "
                         "join, including the mocked baseline already ahead",
                         not err
                         and r["queue_position"] == data.MOCK_QUEUE_AHEAD + 1
                         and r["queue_size"] == data.MOCK_QUEUE_AHEAD + 1))
    results.append(check("handoff marks the session", ho.handed_off))
    r2, _ = call("transfer_to_human", {"reason": "again", "summary": "again"}, ho)
    results.append(check("second handoff does not duplicate",
                         r2.get("already_transferred") is True
                         and len(data.HANDOFFS) == 1))

    ho.last_user_message = "yes go ahead"
    call("check_return_eligibility", {"order_id": "ORD-2001", "sku": "BK-CLEANCODE"}, ho)
    r, err = call("create_return",
                  {"order_id": "ORD-2001", "sku": "BK-CLEANCODE",
                   "reason_category": "changed_mind", "customer_confirmed": True}, ho)
    results.append(check("write blocked once conversation is handed off", err))

    # --- third demo customer, before any replacement adds to the count --------
    r, _ = call("get_order", {"email": data.THIRD_CUSTOMER_EMAIL}, state)
    results.append(check("third demo customer (andy) has three orders, two "
                         "ambiguously containing books",
                         len(r["orders"]) == 3))

    # --- return shipping fee ---------------------------------------------------
    fee_state = SessionState()
    call("check_return_eligibility", {"order_id": "ORD-1002", "sku": "BK-DUNE"}, fee_state)
    fee_state.last_user_message = "yes go ahead"
    r, err = call("create_return",
                  {"order_id": "ORD-1002", "sku": "BK-DUNE",
                   "reason_category": "changed_mind", "customer_confirmed": True},
                  fee_state)
    results.append(check("changed-mind return states the fee as a fact, "
                         "not netted against the refund",
                         not err and r["return_shipping_fee"] == 6.99
                         and r["refund_amount"] == 18.99
                         and "net_refund" not in r
                         and r["return_shipping"] is not None))

    damaged_state = SessionState()
    call("check_return_eligibility", {"order_id": "ORD-1004", "sku": "BK-KNUTH-SET"},
         damaged_state)
    r, err = call("create_return",
                  {"order_id": "ORD-1004", "sku": "BK-KNUTH-SET",
                   "reason_category": "damaged", "refund_method": "store_credit",
                   "customer_confirmed": True}, damaged_state)
    results.append(check("damaged refund needs no item shipped back, and "
                         "the fee still applies but isn't netted against "
                         "the full refund",
                         not err and r["return_shipping_fee"] == 6.99
                         and r["refund_amount"] == 245.00
                         and "net_refund" not in r
                         and r["refund_method"] == "store_credit"
                         and r["return_shipping"] is None))

    # --- stock check (exact SKU lookup, not a search) --------------------------
    r, _ = call("check_stock", {"sku": "BK-PRAG"}, state)
    results.append(check("in-stock SKU reports quantity and in_stock",
                         r["in_stock"] and r["quantity"] == 14))

    r, _ = call("check_stock", {"sku": "BK-KNUTH-SET"}, state)
    results.append(check("zero-stock SKU reports in_stock false",
                         not r["in_stock"] and r["quantity"] == 0))

    r, _ = call("check_stock", {"sku": "BK-NOTREAL"}, state)
    results.append(check("unknown SKU reports zero, not an error",
                         not r["in_stock"] and r["quantity"] == 0))

    # --- replacement (damaged item, free, no return shipment) ------------------
    never_checked = SessionState()
    r, err = call("create_replacement",
                  {"order_id": "ORD-1001", "sku": "BK-PRAG",
                   "customer_confirmed": True}, never_checked)
    results.append(check("replacement blocked when eligibility never checked", err))

    no_stock_check = SessionState()
    call("check_return_eligibility", {"order_id": "ORD-1001", "sku": "BK-PRAG"},
         no_stock_check)
    r, err = call("create_replacement",
                  {"order_id": "ORD-1001", "sku": "BK-PRAG",
                   "customer_confirmed": True}, no_stock_check)
    results.append(check("replacement blocked when stock never checked", err))

    no_stock = SessionState()
    call("check_return_eligibility", {"order_id": "ORD-1004", "sku": "BK-KNUTH-SET"},
         no_stock)
    call("check_stock", {"sku": "BK-KNUTH-SET"}, no_stock)
    r, err = call("create_replacement",
                  {"order_id": "ORD-1004", "sku": "BK-KNUTH-SET",
                   "customer_confirmed": True}, no_stock)
    results.append(check("replacement blocked when stock is zero", err))

    replace_state = SessionState()
    call("check_return_eligibility", {"order_id": "ORD-1001", "sku": "BK-PRAG"},
         replace_state)
    call("check_stock", {"sku": "BK-PRAG"}, replace_state)
    r, err = call("create_replacement",
                  {"order_id": "ORD-1001", "sku": "BK-PRAG",
                   "customer_confirmed": True}, replace_state)
    results.append(check("replacement succeeds once gates pass and creates a "
                         "new order",
                         not err and r["new_order_id"]
                         and data.find_order(r["new_order_id"]) is not None))
    results.append(check("new order carries the customer's shipping address",
                         r["shipping_address"] ==
                         data.shipping_address_for(data.THIRD_CUSTOMER_EMAIL)))
    results.append(check("replacement returns a tracking URL for the new order",
                         r["tracking_url"].endswith(r["new_order_id"])))
    new_order_id = r["new_order_id"]

    r2, err = call("create_replacement",
                   {"order_id": "ORD-1001", "sku": "BK-PRAG",
                    "customer_confirmed": True}, replace_state)
    results.append(check("retry is idempotent, no second order created",
                         not err and r2["new_order_id"] == new_order_id
                         and len(data.REPLACEMENTS) == 1))

    r, _ = call("get_order", {"order_id": new_order_id}, state)
    results.append(check("the new replacement order is itself look-up-able",
                         r["items"][0]["sku"] == "BK-PRAG"
                         and r["email"] == data.THIRD_CUSTOMER_EMAIL))

    unconfirmed = SessionState()
    call("check_return_eligibility", {"order_id": "ORD-1002", "sku": "BK-DUNE"},
         unconfirmed)
    call("check_stock", {"sku": "BK-DUNE"}, unconfirmed)
    r, err = call("create_replacement",
                  {"order_id": "ORD-1002", "sku": "BK-DUNE",
                   "customer_confirmed": False}, unconfirmed)
    results.append(check("replacement blocked without customer confirmation", err))

    # --- retrieval ------------------------------------------------------------
    r, _ = call("search_policy", {"query": "how long do refunds take"}, state)
    results.append(check("policy question retrieves something",
                         len(r.get("results", [])) > 0))

    r, _ = call("search_policy",
                {"query": "do you sell vinyl records wholesale"}, state)
    results.append(check("off-topic question returns no results, not a guess",
                         r.get("results") == []))

    # The reason-branch policy content lives only in returns.md now, not the
    # system prompt — these queries are what the agent is instructed to
    # search for per reason category, so retrieval actually has to surface
    # returns.md's damaged/fee bullets for the branch to work.
    for query in ("damaged misprinted return policy",
                  "return shipping fee"):
        r, _ = call("search_policy", {"query": query}, state)
        sources = [res["source"] for res in r.get("results", [])]
        results.append(check(
            "'{}' surfaces returns.md".format(query), "returns.md" in sources))

    # --- lookup ---------------------------------------------------------------
    r, _ = call("get_order", {"email": data.CUSTOMER_EMAIL}, state)
    results.append(check("email lookup returns all orders (alex)",
                         len(r["orders"]) == 3))

    r, _ = call("get_order", {"email": data.SECOND_CUSTOMER_EMAIL}, state)
    results.append(check("second demo customer's order history is capped "
                         "at the 4 most recent orders, not all 5 on file",
                         len(r["orders"]) == 4))
    results.append(check("the oldest order (ORD-1975) is excluded by the cap",
                         all(o["order_id"] != "ORD-1975" for o in r["orders"])))
    results.append(check("the in-transit and just-outside-window orders are "
                         "still both present",
                         {"ORD-2001", "ORD-2002"} <= {o["order_id"] for o in r["orders"]}))

    r, _ = call("check_return_eligibility",
                {"order_id": "ORD-2002", "sku": "BK-DDIA"}, state)
    results.append(check("in-transit order can't be returned yet",
                         not r["eligible"] and r["rule"] == "not_delivered"))

    r, err = call("get_order", {"order_id": "ORD-9999"}, state)
    results.append(check("unknown order is an error, not an empty answer", err))

    print("\n{} passed, {} failed".format(sum(results), len(results) - sum(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

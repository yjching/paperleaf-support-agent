"""The deterministic return-eligibility engine.

`evaluate_return_eligibility` is a pure function over the order and the item —
same inputs, same answer, every time. The model calls it and reports what it
says; it cannot overrule it. Its thresholds are parsed from
policies/returns.md, not hardcoded here — the policy doc is the one source of
truth, the engine just can't get there by reading prose and guessing at
runtime. If the doc's wording changes enough to break the parse, this fails
loudly at import time rather than quietly deciding refunds against stale
numbers.
"""

import os
import re
from datetime import datetime
from typing import Dict

POLICY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "policies")

# The reason codes Paperleaf's returns system accepts. Anything the customer says
# that doesn't map onto one of these is "other" — the agent records their
# words in reason_note rather than forcing a bad fit.
RETURN_REASONS = [
    "damaged",
    "wrong_item",
    "arrived_late",
    "changed_mind",
    "other",
]


def _parse_return_policy(text: str) -> Dict[str, float]:
    """Pull the numbers the eligibility engine needs out of returns.md.

    Anchored to the specific sentences that state each rule, not just any
    number in the doc — a looser pattern could pick up an unrelated figure
    silently.
    """
    def _find(pattern: str, label: str) -> float:
        m = re.search(pattern, text)
        if not m:
            raise ValueError(
                "policies/returns.md no longer states {} in the expected "
                "form — the eligibility engine has nothing to parse. Update "
                "the pattern in _parse_return_policy to match the new "
                "wording.".format(label)
            )
        return float(m.group(1))

    return {
        "window_days": _find(
            r"Physical books may be returned within \*\*(\d+) days? of delivery\*\*",
            "the standard return window",
        ),
        "human_approval_threshold": _find(
            r"Returns over \*\*\$(\d+(?:\.\d+)?)\*\*",
            "the human-approval threshold",
        ),
        "return_shipping_fee": _find(
            r"A flat\s+\*\*\$(\d+(?:\.\d+)?)\*\* AusPost return shipping fee "
            r"applies to every return",
            "the return shipping fee",
        ),
    }


with open(os.path.join(POLICY_DIR, "returns.md"), "r", encoding="utf-8") as _fh:
    _RETURN_POLICY = _parse_return_policy(_fh.read())

RETURN_WINDOW_DAYS = int(_RETURN_POLICY["window_days"])
HUMAN_APPROVAL_THRESHOLD = _RETURN_POLICY["human_approval_threshold"]
RETURN_SHIPPING_FEE = _RETURN_POLICY["return_shipping_fee"]


def _days_since(date_str: str) -> int:
    return (datetime.now() - datetime.strptime(date_str, "%Y-%m-%d")).days


def evaluate_return_eligibility(order: Dict, item: Dict) -> Dict:
    """Pure function. Same inputs, same answer, every time.

    Eligibility doesn't depend on the reason (policies/returns.md is
    explicit about this) — the reason only ever routes what's offered once
    something's eligible, which is a job for create_return/create_replacement,
    not this check. There used to be a separate, longer damaged-item window
    here; policies/returns.md now states the same window for both, so the
    distinction was removed rather than kept as a branch that could never
    produce a different answer.
    """
    if order["status"] != "delivered":
        return {
            "eligible": False,
            "reason": "Order has not been delivered yet. Undelivered orders can "
                      "be cancelled while in transit, but not returned.",
            "rule": "not_delivered",
        }

    if item["format"] == "digital" and item.get("downloaded"):
        return {
            "eligible": False,
            "reason": "Digital books cannot be returned once downloaded. If the "
                      "file is corrupt or the download failed we re-issue it free.",
            "rule": "digital_downloaded",
        }

    days = _days_since(order["delivered_at"])

    if days > RETURN_WINDOW_DAYS:
        return {
            "eligible": False,
            "reason": "Delivered {} days ago, which is outside the {}-day return "
                      "window.".format(days, RETURN_WINDOW_DAYS),
            "rule": "outside_window",
        }

    refund = round(float(item["price"]), 2)
    return {
        "eligible": True,
        "reason": "Delivered {} days ago, within the {}-day window — no "
                  "reason required.".format(days, RETURN_WINDOW_DAYS),
        "rule": "within_window",
        "refund_amount": refund,
        "refund_note": "${:.2f} refund, in full — the ${:.2f} return "
                       "shipping fee is charged separately, not deducted "
                       "from it.".format(refund, RETURN_SHIPPING_FEE),
        "requires_human_approval": refund > HUMAN_APPROVAL_THRESHOLD,
    }

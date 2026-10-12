"""Improvement loop: capture every time the agent does something the design
says it shouldn't, so it can be fixed rather than noticed once and forgotten.

A guardrail firing means the *model* misbehaved and code caught it. That's a
success for safety and a defect for behaviour — the agent should not have tried
it. Each one is logged with enough context to turn into either a prompt change
or a regression test.

    python3 improvement.py        # review what's been captured
"""

import json
import os
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "improvements.jsonl")

# What a given failure tells us to do about it.
GUIDANCE = {
    "not_confirmed": (
        "Model tried to write before the customer agreed.",
        "Tighten the confirmation instruction, or have the agent ask for "
        "confirmation on its own turn rather than bundled with another question.",
    ),
    "not_eligible": (
        "Model tried to create a return the engine had already refused.",
        "Strengthen the rule that the eligibility result is final.",
    ),
    "eligibility_not_checked": (
        "Model tried to write without checking eligibility first.",
        "Make the ordering explicit in the create_return tool description.",
    ),
    "unresolved_slot": (
        "Model called a tool with a missing or unknown order/item.",
        "Reinforce asking a clarifying question before acting.",
    ),
    "other": (
        "Tool rejected the call.",
        "Read the context and decide whether it's a prompt or a tool-schema fix.",
    ),
}


def classify(error_message: str) -> str:
    """Map a tool error onto the rule that was broken."""
    m = error_message.lower()
    if "has not confirmed" in m:
        return "not_confirmed"
    if "not eligible" in m:
        return "not_eligible"
    if "eligibility has not been checked" in m:
        return "eligibility_not_checked"
    if "no order found" in m or "no item with sku" in m:
        return "unresolved_slot"
    return "other"


def log(tool: str, args: Dict, error: str, user_message: str,
        transcript: Optional[str] = None) -> str:
    """Append one misalignment. Returns the rule it was classified as."""
    rule = classify(error)
    record = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rule": rule,
        "tool": tool,
        "args": args,
        "error": error,
        "user_message": user_message,
        "transcript": os.path.basename(transcript) if transcript else None,
    }
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return rule


def load() -> List[Dict]:
    if not os.path.isfile(LOG_PATH):
        return []
    out = []
    with open(LOG_PATH, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def review() -> None:
    records = load()
    if not records:
        print("No misalignments logged. Either the agent behaved, or it "
              "hasn't been run since logging was added.")
        return

    counts = Counter(r["rule"] for r in records)
    print("{} misalignment(s) across {} rule(s)\n".format(
        len(records), len(counts)))

    for rule, n in counts.most_common():
        what, fix = GUIDANCE.get(rule, GUIDANCE["other"])
        print("{}  x{}".format(rule, n))
        print("  what: {}".format(what))
        print("  fix:  {}".format(fix))
        latest = [r for r in records if r["rule"] == rule][-1]
        print("  last: {} — user said {!r}".format(
            latest["at"], latest["user_message"]))
        print("        {}({}) -> {}".format(
            latest["tool"],
            ", ".join("{}={!r}".format(k, v) for k, v in latest["args"].items()),
            latest["error"][:100]))
        print()

    print("Each of these should end up as a prompt change, a tool-description "
          "change, or a regression test in test_tools.py.")


if __name__ == "__main__":
    review()

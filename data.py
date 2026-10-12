"""Mock Paperleaf backend.

Stands in for the OMS. Dates are computed relative to today so the demo
scenarios (in-window / out-of-window) never go stale.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional

# First demo customer — order status (Project Hail Mary, in transit), plus
# a standard changed-mind return journey (The Hobbit, in window) and a
# downloaded ebook that isn't eligible, giving the same "which book do you
# mean" clarifying question as andy's account before landing on the
# standard fee-deducted return path (andy's demo is the damaged/no-fee
# branch, morgan's is a refusal — this is the third shape of the same tool).
CUSTOMER_EMAIL = "alex@example.com"
# Second demo customer — a clean single-order case for the refusal +
# escalation scenario, kept separate from the other accounts so the demo
# scripts don't collide.
SECOND_CUSTOMER_EMAIL = "morgan@example.com"
# Third demo customer — the multi-order account (two delivered orders both
# containing books, plus a high-value order) that drives the full return
# journey: clarifying question -> eligibility -> reason-branch
# (search_policy grounding what's offered) -> confirm -> write.
THIRD_CUSTOMER_EMAIL = "andy@example.com"


def _days_ago(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def _days_ahead(n: int) -> str:
    return (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d")


# One address per customer, not per order — real accounts have a saved
# shipping address, and "same as last time?" only makes sense if there's one
# thing to point at, not one per order.
CUSTOMER_ADDRESSES: Dict[str, str] = {
    CUSTOMER_EMAIL: "12 Wattle Street, Newtown NSW 2042",
    SECOND_CUSTOMER_EMAIL: "8 Fig Tree Lane, Fitzroy VIC 3065",
    THIRD_CUSTOMER_EMAIL: "27 Banksia Ave, Fremantle WA 6160",
}


# Two delivered orders both containing books — this is what makes
# "I want to return my book" ambiguous and forces the clarifying question.
# All under andy@example.com; alex's account (below) stays deliberately thin.
ORDERS: List[Dict] = [
    {
        "order_id": "ORD-1001",
        "email": THIRD_CUSTOMER_EMAIL,
        "status": "delivered",
        "placed_at": _days_ago(12),
        "delivered_at": _days_ago(5),
        "items": [
            {
                "sku": "BK-PRAG",
                "title": "The Pragmatic Programmer",
                "format": "paperback",
                "price": 25.00,
                "condition": "new",
                "downloaded": False,
            },
            {
                "sku": "EB-REFACTOR",
                "title": "Refactoring (ebook)",
                "format": "digital",
                "price": 29.99,
                "condition": "new",
                "downloaded": True,
            },
        ],
    },
    {
        "order_id": "ORD-1002",
        "email": THIRD_CUSTOMER_EMAIL,
        "status": "delivered",
        "placed_at": _days_ago(17),
        "delivered_at": _days_ago(10),
        "items": [
            {
                "sku": "BK-DUNE",
                "title": "Dune",
                "format": "paperback",
                "price": 18.99,
                "condition": "used",
                "downloaded": False,
            }
        ],
    },
    {
        # Alex's one real order for the order-status demo — in transit, so
        # there's a tracking beat to show, not a return one.
        "order_id": "ORD-1003",
        "email": CUSTOMER_EMAIL,
        "status": "in_transit",
        "placed_at": _days_ago(2),
        "delivered_at": None,
        "estimated_delivery": _days_ahead(3),
        "carrier": "AusPost",
        "tracking_number": "AP7734981005",
        "items": [
            {
                "sku": "BK-HAIL",
                "title": "Project Hail Mary",
                "format": "hardcover",
                "price": 34.50,
                "condition": "new",
                "downloaded": False,
            }
        ],
    },
    {
        # High-value order — exercises the human-approval branch.
        "order_id": "ORD-1004",
        "email": THIRD_CUSTOMER_EMAIL,
        "status": "delivered",
        "placed_at": _days_ago(9),
        "delivered_at": _days_ago(4),
        "items": [
            {
                "sku": "BK-KNUTH-SET",
                "title": "The Art of Computer Programming (Box Set)",
                "format": "hardcover",
                "price": 245.00,
                "condition": "new",
                "downloaded": False,
            }
        ],
    },
    {
        # Second demo customer — delivered one day past the 30-day window.
        # Demonstrates the refusal-then-escalation path: the engine won't
        # bend, and the agent's only lever is a human handoff.
        "order_id": "ORD-2001",
        "email": SECOND_CUSTOMER_EMAIL,
        "status": "delivered",
        "placed_at": _days_ago(38),
        "delivered_at": _days_ago(31),
        "items": [
            {
                "sku": "BK-CLEANCODE",
                "title": "Clean Code",
                "format": "hardcover",
                "price": 45.00,
                "condition": "new",
                "downloaded": False,
            }
        ],
    },
    {
        # The changed-mind return scenario — in window, standard fee-
        # deducted path. Delivered well within 30 days, same margin as
        # andy's Pragmatic Programmer (5 days), so eligibility is a clean
        # yes without brushing the window edge.
        "order_id": "ORD-4001",
        "email": CUSTOMER_EMAIL,
        "status": "delivered",
        "placed_at": _days_ago(12),
        "delivered_at": _days_ago(6),
        "items": [
            {
                "sku": "BK-HOBBIT",
                "title": "The Hobbit",
                "format": "paperback",
                "price": 16.50,
                "condition": "new",
                "downloaded": False,
            }
        ],
    },
    {
        # Downloaded ebook, not eligible — gives alex's account the same
        # ambiguous "which book" clarifying question as andy's (two
        # delivered orders, both book-shaped, only one actually returnable).
        "order_id": "ORD-4002",
        "email": CUSTOMER_EMAIL,
        "status": "delivered",
        "placed_at": _days_ago(200),
        "delivered_at": _days_ago(193),
        "items": [
            {
                "sku": "EB-1984",
                "title": "1984 (ebook)",
                "format": "digital",
                "price": 11.99,
                "condition": "new",
                "downloaded": True,
            }
        ],
    },
    {
        # Older history establishing morgan as a genuine repeat customer —
        # get_order(email) caps at the 4 most recent orders, so this one
        # (the oldest) is deliberately excluded from that view; it exists so
        # the cap has something real to trim, not just headroom.
        "order_id": "ORD-1975",
        "email": SECOND_CUSTOMER_EMAIL,
        "status": "delivered",
        "placed_at": _days_ago(347),
        "delivered_at": _days_ago(340),
        "items": [
            {
                "sku": "BK-ATOMIC",
                "title": "Atomic Habits",
                "format": "paperback",
                "price": 22.00,
                "condition": "new",
                "downloaded": False,
            }
        ],
    },
    {
        "order_id": "ORD-1980",
        "email": SECOND_CUSTOMER_EMAIL,
        "status": "delivered",
        "placed_at": _days_ago(207),
        "delivered_at": _days_ago(200),
        "items": [
            {
                "sku": "BK-SAPIENS",
                "title": "Sapiens",
                "format": "paperback",
                "price": 24.50,
                "condition": "new",
                "downloaded": False,
            }
        ],
    },
    {
        "order_id": "ORD-1985",
        "email": SECOND_CUSTOMER_EMAIL,
        "status": "delivered",
        "placed_at": _days_ago(107),
        "delivered_at": _days_ago(100),
        "items": [
            {
                "sku": "BK-PSYCHMONEY",
                "title": "The Psychology of Money",
                "format": "paperback",
                "price": 21.00,
                "condition": "new",
                "downloaded": False,
            }
        ],
    },
    {
        # A second, unrelated order still on the way — gives morgan's
        # account an order-status beat too, and a get_order(email) lookup
        # that has to tell the two orders apart rather than assume "the
        # order" means the one being returned.
        "order_id": "ORD-2002",
        "email": SECOND_CUSTOMER_EMAIL,
        "status": "in_transit",
        "placed_at": _days_ago(1),
        "delivered_at": None,
        "estimated_delivery": _days_ahead(4),
        "carrier": "AusPost",
        "tracking_number": "AP7734981099",
        "items": [
            {
                "sku": "BK-DDIA",
                "title": "Designing Data-Intensive Applications",
                "format": "paperback",
                "price": 39.00,
                "condition": "new",
                "downloaded": False,
            }
        ],
    },
]

# Warehouse stock, keyed by exact SKU — a product master, not a document. No
# fuzzy/semantic matching makes sense here: a SKU is an identifier, not a
# topic, so this is a plain dict lookup, the same as find_order/find_item.
# BK-KNUTH-SET is deliberately 0 — the one item in the demo data with no
# replacement copy on hand, so "check before offering" has a real branch.
WAREHOUSE_STOCK: Dict[str, int] = {
    "BK-PRAG": 14,
    "EB-REFACTOR": 999,  # digital — effectively unlimited
    "BK-DUNE": 6,
    "BK-KNUTH-SET": 0,
    "BK-CLEANCODE": 4,
    "BK-HAIL": 9,
    "BK-DDIA": 5,
    "BK-HOBBIT": 3,
    "EB-1984": 999,
}


def stock_for(sku: str) -> int:
    return WAREHOUSE_STOCK.get(sku.upper(), 0)


# Baseline order count — create_replacement appends new orders to ORDERS at
# runtime, so a demo reset needs to know where "the real data" ends.
_BASE_ORDER_COUNT = len(ORDERS)


def reset_orders() -> None:
    del ORDERS[_BASE_ORDER_COUNT:]


# In-memory returns store. Keyed by idempotency key so a retry cannot
# create a second return for the same item.
RETURNS: Dict[str, Dict] = {}

# In-memory replacement store, keyed the same way as RETURNS. A replacement
# also appends a new order to ORDERS (below) — this dict is just the
# idempotency record, not the source of truth for what shipped.
REPLACEMENTS: Dict[str, Dict] = {}

# Handoffs to human agents, in the order they were raised.
HANDOFFS: List[Dict] = []
# Mocked count of other customers already ahead in the support queue — a
# real queue is never empty, so a fresh demo landing on "1 out of 1" every
# time undersells that this is a shared line, not a dedicated one.
MOCK_QUEUE_AHEAD = 4


def find_order(order_id: str) -> Optional[Dict]:
    for order in ORDERS:
        if order["order_id"].upper() == order_id.strip().upper():
            return order
    return None


ORDER_HISTORY_LIMIT = 4


def orders_for_email(email: str) -> List[Dict]:
    """The 4 most recent orders, newest first — not the customer's entire
    history. A real OMS lookup wouldn't dump years of orders into one
    response either; older orders are still reachable individually via
    find_order if a specific order_id is known."""
    matches = [o for o in ORDERS if o["email"].lower() == email.strip().lower()]
    matches.sort(key=lambda o: o["placed_at"], reverse=True)
    return matches[:ORDER_HISTORY_LIMIT]


def find_item(order: Dict, sku: str) -> Optional[Dict]:
    for item in order["items"]:
        if item["sku"].upper() == sku.strip().upper():
            return item
    return None


def shipping_address_for(email: str) -> Optional[str]:
    return CUSTOMER_ADDRESSES.get(email.lower())


def next_order_id(email: str) -> str:
    """Next order number in this customer's own numbering, not a global
    counter — so a new order for andy lands as ORD-1005, not some unrelated
    global-max number that has nothing to do with andy's account."""
    numbers = [
        int(o["order_id"].split("-")[1])
        for o in ORDERS
        if o["email"].lower() == email.lower() and o["order_id"].split("-")[1].isdigit()
    ]
    return "ORD-{}".format(max(numbers) + 1 if numbers else 1001)

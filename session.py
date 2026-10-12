"""Per-conversation state."""

from typing import Dict, Optional, Tuple


class SessionState:
    """What the agent knows this conversation.

    Deliberately small. Its job is (a) so the agent never re-asks for
    something it already has, and (b) so the write gate has something
    trustworthy to check against.
    """

    def __init__(self) -> None:
        self.customer_email: Optional[str] = None
        # (order_id, sku) -> eligibility result, recorded by the engine itself
        self.eligibility_checks: Dict[Tuple[str, str], Dict] = {}
        # sku -> stock count, recorded by check_stock itself. A replacement
        # can't be offered as available before the warehouse was actually
        # checked, same principle as the eligibility gate.
        self.stock_checks: Dict[str, int] = {}
        # The customer's own most recent words, kept only so a blocked write
        # can be logged with what they actually said (improvement.py) — not
        # read by the write gate itself. Reading whether a reply counts as
        # agreement is a language task the model owns; see customer_confirmed
        # in tool_create_return.
        self.last_user_message: str = ""
        # True once the conversation has been handed to a person.
        self.handed_off: bool = False

    def record_eligibility(self, order_id: str, sku: str, result: Dict) -> None:
        self.eligibility_checks[(order_id.upper(), sku.upper())] = result

    def eligibility_for(self, order_id: str, sku: str) -> Optional[Dict]:
        return self.eligibility_checks.get((order_id.upper(), sku.upper()))

    def record_stock(self, sku: str, quantity: int) -> None:
        self.stock_checks[sku.upper()] = quantity

    def stock_for(self, sku: str) -> Optional[int]:
        return self.stock_checks.get(sku.upper())

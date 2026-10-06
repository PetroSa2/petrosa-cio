"""The stop and target levels an order carries, as distances from its entry."""

from __future__ import annotations

from typing import Any


def carried_order_distances(
    payload: dict[str, Any], current_price: Any
) -> tuple[float | None, float | None]:
    """Stop and target distances (fractions of entry) of the levels the order carries.

    Each is None when the order does not carry it or it is not on the right side of the entry; they are
    independent, so a carried stop is used even when no target is carried. The entry is the payload's
    ``entry_price`` / ``price``, else ``current_price``.
    """
    entry = payload.get("entry_price", payload.get("price"))
    if entry is None:
        entry = current_price
    try:
        entry_price = float(entry)
    except (TypeError, ValueError):
        return None, None
    if entry_price <= 0:
        return None, None
    side = str(payload.get("side", "")).upper()

    def distance(raw: Any, *, is_stop: bool) -> float | None:
        try:
            price = float(raw)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        below = (side in {"BUY", "LONG"}) == is_stop
        if side in {"BUY", "LONG", "SELL", "SHORT"} and (
            (price >= entry_price) if below else (price <= entry_price)
        ):
            return None
        result = abs(price - entry_price) / entry_price
        return result if result > 0 else None

    return (
        distance(payload.get("stop_loss"), is_stop=True),
        distance(
            payload.get("take_profit", payload.get("target_price")), is_stop=False
        ),
    )

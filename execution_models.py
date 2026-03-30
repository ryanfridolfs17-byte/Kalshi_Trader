"""
Shared execution-order models for live and paper trading.

The goal is simple: both live and paper paths should operate on the same final
entry order object, and risk should approve that exact object rather than an
earlier rough version of the signal.
"""

import config


def _uses_taker_mode(signal):
    edge = signal.get("edge", 0)
    fee_adj_edge = signal.get("fee_adjusted_edge", 0)
    verdict = signal.get("confirmation_verdict", "")
    is_confirmed = verdict == "CONFIRMED_OUTCOME"
    return (
        (is_confirmed and edge > getattr(config, "TAKER_MODE_MIN_EDGE", 0.15)
         and fee_adj_edge > 0.10)
        or (verdict == "STRONG"
            and fee_adj_edge > getattr(config, "STRONG_TAKER_MIN_FEE_ADJ_EDGE", 0.20))
    )


def build_entry_order(signal, maker=None):
    """Return a normalized entry order plan for either live or paper execution."""
    if not signal:
        return None

    order = dict(signal)
    ticker = order.get("ticker", "")
    side = order.get("side", "")
    price_cents = int(
        order.get("price_cents", order.get("entry_price_cents", order.get("current_price_cents", 0))) or 0
    )
    contracts = int(order.get("contracts", order.get("suggested_contracts", order.get("requested_contracts", 1))) or 0)
    if not ticker or not side or price_cents <= 0 or contracts <= 0:
        return None

    order["signal"] = "buy"
    order["requested_contracts"] = contracts
    order["contracts"] = contracts
    order["current_price_cents"] = int(order.get("current_price_cents", price_cents) or price_cents)
    order["is_confirmed"] = bool(order.get("is_confirmed", False) or order.get("confirmation_verdict") == "CONFIRMED_OUTCOME")
    order["is_arb"] = bool(order.get("is_arb", False) or order.get("strategy", "") == "S2-Arbitrage")

    explicit_style = str(order.get("execution_style", "") or "").lower()
    if explicit_style in ("maker", "taker"):
        use_taker = explicit_style == "taker"
    else:
        use_taker = _uses_taker_mode(order)
    order["execution_style"] = "taker" if use_taker else "maker"

    if use_taker:
        order["limit_price_cents"] = price_cents
        order["limit_price"] = price_cents
        order["risk_price_cents"] = price_cents
        return order

    limit_price = int(
        order.get("limit_price_cents", order.get("limit_price", 0)) or 0
    )
    if limit_price <= 0 and maker is not None:
        limit_price = int(maker.calculate_limit_price(order) or 0)
    if limit_price <= 0:
        return None

    order["limit_price_cents"] = limit_price
    order["limit_price"] = limit_price
    order["risk_price_cents"] = limit_price
    return order


def build_risk_signal(order, same_cycle=False):
    """Build the exact risk-manager payload for an entry order."""
    if not order:
        return None
    contracts = int(order.get("contracts", order.get("requested_contracts", 1)) or 0)
    price_cents = int(order.get("risk_price_cents", order.get("price_cents", 0)) or 0)
    return {
        "ticker": order.get("ticker", ""),
        "city_code": order.get("city_code", ""),
        "side": order.get("side", ""),
        "price_cents": price_cents,
        "contracts": contracts,
        "cost_cents": price_cents * contracts,
        "edge": order.get("edge", 0),
        "is_confirmed": order.get("is_confirmed", False),
        "is_arb": order.get("is_arb", False),
        "same_cycle": same_cycle,
    }


def apply_risk_to_order(order, risk_signal):
    """Propagate risk-sized contract changes back onto the shared order plan."""
    if not order or not risk_signal:
        return order
    contracts = int(risk_signal.get("contracts", order.get("contracts", 1)) or 0)
    price_cents = int(order.get("risk_price_cents", order.get("price_cents", 0)) or 0)
    order["contracts"] = contracts
    order["requested_contracts"] = contracts
    order["suggested_contracts"] = contracts
    order["cost_cents"] = price_cents * contracts
    return order


def display_price_cents(order):
    if not order:
        return 0
    if order.get("execution_style") == "maker":
        return int(order.get("limit_price_cents", order.get("price_cents", 0)) or 0)
    return int(order.get("risk_price_cents", order.get("price_cents", 0)) or 0)

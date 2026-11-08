"""Reference vendor-arbitrage strategy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..api import Buy, Intent, MarketView, Strategy, VendorSell


class VendorArbStrategy:
    name = "vendor_arb"

    def __init__(self, params: Mapping[str, object] | None = None) -> None:
        self.params: Mapping[str, object] = dict(params or {})
        supplied = self.params.get("professions", ())
        self.professions = (
            frozenset(str(value) for value in supplied)
            if isinstance(supplied, Sequence) and not isinstance(supplied, str)
            else frozenset()
        )

    def checkpoint(self) -> Mapping[str, object]:
        return {}

    def restore(self, state: Mapping[str, object]) -> None:
        if state:
            raise ValueError("vendor-arb strategy state must be empty")

    def decide(self, market: MarketView, account) -> Sequence[Intent]:
        intents: list[Intent] = []
        # Sell only inventory held at the beginning of this decision.  Buys below
        # are deliberately emitted after these sells, so their proceeds are
        # available to the projected cash calculation but newly bought units are
        # not sold until the next sweep.
        projected_cash = account.cash()
        for item_id, quantity in sorted(account.inventory().items()):
            if quantity <= 0:
                continue
            fact = market.facts(item_id)
            if fact is None or fact.sell_price <= 0:
                continue
            intents.append(VendorSell(item_id, quantity))
            projected_cash += fact.sell_price * quantity

        for item_id, snapshot in sorted(market.items().items()):
            fact = market.facts(item_id)
            if fact is None or fact.sell_price <= 0:
                continue
            quantity = 0
            affordable = projected_cash
            for level in snapshot.levels:
                if level.unit_price >= fact.sell_price:
                    break
                take = min(level.quantity, affordable // level.unit_price if level.unit_price else 0)
                if take <= 0:
                    break
                quantity += take
                affordable -= take * level.unit_price
            if quantity:
                intents.append(Buy(item_id, fact.sell_price - 1, quantity))
                projected_cash = affordable
        return intents


def build(params: Mapping[str, object] | None = None) -> Strategy:
    return VendorArbStrategy(params)


__all__ = ["VendorArbStrategy", "build"]

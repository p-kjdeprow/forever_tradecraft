from dataclasses import FrozenInstanceError
from fractions import Fraction

import pytest

from crowley.core.auction_rules import AuctionHouseRules, rules_for_house
from crowley.core.house import House


def test_rules_map_faction_to_exact_seller_cut() -> None:
    alliance = rules_for_house(House("us", "normal", "alliance"))
    horde = rules_for_house(House("us", "normal", "horde"))
    neutral = rules_for_house(House("us", "normal", "neutral"))

    assert alliance.seller_cut == Fraction(1, 20)
    assert horde.seller_cut == Fraction(1, 20)
    assert neutral.seller_cut == Fraction(3, 20)
    assert alliance.version == horde.version == neutral.version


def test_rules_are_immutable_and_deposit_policy_is_explicit() -> None:
    rules = rules_for_house(House("eu", "pvp", "neutral"))

    assert isinstance(rules, AuctionHouseRules)
    assert rules.deposit_policy.rate == Fraction(3, 20)
    assert rules.deposit_policy.base_duration_hours == 12
    assert rules.deposit_policy.supported_durations_hours == (12, 24, 48)
    assert rules.deposit_policy.calculate(100, 3, 12) == 45

    with pytest.raises(FrozenInstanceError):
        rules.seller_cut = Fraction(1, 10)
    with pytest.raises(FrozenInstanceError):
        rules.deposit_policy.rate = Fraction(1, 10)


def test_deposit_policy_rejects_unsupported_duration() -> None:
    rules = rules_for_house(House("us", "normal", "alliance"))

    with pytest.raises(ValueError, match="unsupported duration: 6"):
        rules.deposit_policy.calculate(100, 1, 6)

"""Conservative turnover evidence for normalized market observations."""

from __future__ import annotations

from collections.abc import Iterable
from fractions import Fraction
from typing import Any

from crowley.analysis.books import book_from_observation, replenished_units, valid_intervals

MAX_INTERVAL_SECONDS = 1800
NORMALIZATION_POLICY = "item-per-auction-unit-signed-time-left-v3"


def _ratio(value: dict[str, Any]) -> Fraction:
    return Fraction(value["numerator"], value["denominator"])


def _observation(record: dict[str, Any]) -> dict[str, Any]:
    return record["observation"] if "observation" in record else record


def _record_meta(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("envelope", record)


def _query_window(observation: dict[str, Any]) -> tuple[int, int] | None:
    query = observation.get("query", {})
    if not isinstance(query, dict) or query.get("kind") != "queried":
        return None
    if not isinstance(query.get("startedAt"), int) or not isinstance(query.get("finishedAt"), int):
        return None
    return query["startedAt"], query["finishedAt"]


def _cutoff(observation: dict[str, Any]) -> Fraction | None:
    cutoff = observation.get("cutoff", {})
    if not isinstance(cutoff, dict) or cutoff.get("kind") != "price":
        return None
    return Fraction(cutoff["numerator"], cutoff["denominator"])


def _rows(observation: dict[str, Any]) -> list[dict[str, Any]]:
    values = observation.get("rows", {})
    if isinstance(values, dict):
        numbered = []
        for key, row in values.items():
            if (
                not isinstance(key, str)
                or not key.startswith("row:")
                or not key[4:].isdigit()
                or str(int(key[4:])) != key[4:]
                or int(key[4:]) < 1
            ):
                raise ValueError("observation row keys must be numeric row ordinals")
            numbered.append((int(key[4:]), row))
        return [row for _, row in sorted(numbered)]
    return list(values) if isinstance(values, list) else []


def _rows_in_scope(observation: dict[str, Any], ceiling: Fraction | None) -> list[dict[str, Any]]:
    values = _rows(observation)
    if ceiling is None:
        return values
    return [row for row in values if _ratio(row["unitPrice"]) <= ceiling]


def _matches_variant(row: dict[str, Any], item_key: str | None, raw_identity: dict[str, Any] | None) -> bool:
    if item_key is not None and row.get("itemKey") != item_key:
        return False
    return raw_identity is None or row.get("rawItemKey") == raw_identity


def _common_ceiling(left: dict[str, Any], right: dict[str, Any]) -> tuple[Fraction | None, str | None]:
    for observation in (left, right):
        coverage = observation.get("coverage")
        if coverage not in {"full", "bounded"}:
            return None, "incomplete_observation"
        if coverage == "full" and observation.get("sourceComplete") is not True:
            return None, "incomplete_observation"
        if (
            coverage == "bounded"
            and observation.get("kind") == "item"
            and observation.get("sourceComplete") is not True
        ):
            return None, "incomplete_observation"
    ceilings = [ceiling for observation in (left, right) if (ceiling := _cutoff(observation)) is not None]
    return (min(ceilings) if ceilings else None), None


def _identity_row(row: dict[str, Any]) -> bool:
    owners = row.get("owners", {})
    if not isinstance(owners, dict):
        return False
    names = owners.get("names", {})
    return (
        row.get("resultID", 0) > 0
        and owners.get("identityKind") == "individual"
        and owners.get("totalNumberOfOwners") == 1
        and isinstance(names, dict)
        and len(names) == 1
        and all(value is True for value in names.values())
        and owners.get("containsOwnerItem") is False
        and owners.get("containsAccountItem") is False
    )


def _grouped_scope(rows: Iterable[dict[str, Any]]) -> bool:
    return any(not _identity_row(row) for row in rows)


def _time_class(row: dict[str, Any], expiry_gap: int) -> str:
    value = row.get("timeLeft", {})
    if not isinstance(value, dict):
        return "time_left_unknown"
    if value.get("kind") == "exact":
        minimum = value.get("seconds")
    elif value.get("kind") == "band":
        minimum = value.get("minimumSeconds")
    else:
        return "time_left_unknown"
    if not isinstance(minimum, int):
        return "time_left_unknown"
    return "unexpired" if minimum > expiry_gap else "expiration_possible"


def _row_fingerprint(row: dict[str, Any]) -> tuple[Any, ...]:
    owners = row.get("owners", {})
    if isinstance(owners, dict):
        owner_fingerprint = (
            owners.get("names"),
            owners.get("totalNumberOfOwners"),
            owners.get("containsOwnerItem"),
            owners.get("containsAccountItem"),
            owners.get("identityKind"),
        )
    else:
        owner_fingerprint = owners
    return (
        row.get("quantity"),
        row.get("unitPrice"),
        row.get("itemKey"),
        row.get("rawItemKey"),
        owner_fingerprint,
    )


def _base_result(*, elapsed: int = 0, expiry_gap: int = 0) -> dict[str, Any]:
    return {
        "valid": False,
        "comparedSeconds": elapsed,
        "comparedHours": elapsed / 3600,
        "exposureSeconds": elapsed,
        "expiryGapSeconds": expiry_gap,
        "commonPriceScope": None,
        "excludedIntervals": [],
        "eligibleIndividualSaleCandidateUnits": 0,
        "saleCandidateUnitsPerHour": None,
        "saleCandidateRateLabel": "UNCALIBRATED",
        "individualExposureSeconds": None,
        "individualRateDenominatorHours": None,
        "individualRateAvailable": False,
    }


def compare_observations_detailed(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    item_key: str | None = None,
    raw_identity: dict[str, Any] | None = None,
    max_interval_seconds: int = MAX_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Compare consecutive observations and retain per-auction disappearance evidence."""
    left = _observation(previous)
    right = _observation(current)
    left_meta = _record_meta(previous)
    right_meta = _record_meta(current)
    exclusions: list[str] = []
    left_house = (
        left_meta.get("house", {}).get("key") if isinstance(left_meta.get("house", {}), dict) else None
    )
    right_house = (
        right_meta.get("house", {}).get("key") if isinstance(right_meta.get("house", {}), dict) else None
    )
    if (left_house or right_house) and (not left_house or left_house != right_house):
        exclusions.append("house_changed")
    left_context = left_meta.get("context", {})
    right_context = right_meta.get("context", {})
    if not isinstance(left_context, dict) or not isinstance(right_context, dict):
        exclusions.append("client_build_changed")
        left_context = left_context if isinstance(left_context, dict) else {}
        right_context = right_context if isinstance(right_context, dict) else {}
    left_build = f"{left_context.get('clientVersion', '')}.{left_context.get('clientBuild', '')}"
    right_build = f"{right_context.get('clientVersion', '')}.{right_context.get('clientBuild', '')}"
    if (left_context or right_context) and (
        not left_context.get("clientVersion")
        or not left_context.get("clientBuild")
        or left_build != right_build
    ):
        exclusions.append("client_build_changed")
    left_plan = (
        left_meta.get("decoded", {}).get("planHash", left_meta.get("planHash"))
        if isinstance(left_meta.get("decoded", {}), dict)
        else left_meta.get("planHash")
    )
    right_plan = (
        right_meta.get("decoded", {}).get("planHash", right_meta.get("planHash"))
        if isinstance(right_meta.get("decoded", {}), dict)
        else right_meta.get("planHash")
    )
    if (left_plan or right_plan) and (not left_plan or left_plan != right_plan):
        exclusions.append("plan_changed")
    left_variant = left_meta.get("itemKey")
    right_variant = right_meta.get("itemKey")
    if left_variant or right_variant:
        if not left_variant or left_variant != right_variant:
            exclusions.append("variant_changed")
    if left_meta.get("normalizationPolicy", NORMALIZATION_POLICY) != right_meta.get(
        "normalizationPolicy", NORMALIZATION_POLICY
    ):
        exclusions.append("normalization_changed")
    if left.get("itemID") != right.get("itemID"):
        exclusions.append("item_changed")
    left_window = _query_window(left)
    right_window = _query_window(right)
    if left_window is None or right_window is None:
        exclusions.append("unqueried_interval")
        result = _base_result()
        result["excludedIntervals"] = exclusions
        return result
    prior_start, prior_end = left_window
    current_start, current_end = right_window
    if current_start < prior_end:
        exclusions.append("overlapping_interval")
    exposure = current_end - prior_end
    expiry_gap = current_end - prior_start
    if exposure <= 0:
        exclusions.append("nonpositive_exposure")
    # This 1800 s end-to-start check applies to every kind. For commodities the
    # shared 1260 s start-to-start rule (analysis.books.MAX_GAP, enforced below)
    # is the binding one.
    if expiry_gap > max_interval_seconds:
        exclusions.append("interval_too_long")
    ceiling, coverage_error = _common_ceiling(left, right)
    if coverage_error:
        exclusions.append(coverage_error)
    if left.get("kind") != right.get("kind"):
        exclusions.append("kind_changed")
    if exclusions:
        result = _base_result(elapsed=max(0, exposure), expiry_gap=max(0, expiry_gap))
        result["excludedIntervals"] = exclusions
        return result
    left_rows_all = _rows(left)
    right_rows_all = _rows(right)
    left_scope_rows = _rows_in_scope(left, ceiling)
    right_scope_rows = _rows_in_scope(right, ceiling)
    left_rows = [row for row in left_scope_rows if _matches_variant(row, item_key, raw_identity)]
    right_rows = [row for row in right_scope_rows if _matches_variant(row, item_key, raw_identity)]
    kind = left.get("kind")
    result: dict[str, Any] = {
        **_base_result(elapsed=exposure, expiry_gap=expiry_gap),
        "valid": True,
        "kind": kind,
        "commonPriceScope": None
        if ceiling is None
        else {"numerator": ceiling.numerator, "denominator": ceiling.denominator},
        "groupedScope": False,
        "observedIndividualRemovedUnits": 0,
        "observedIndividualAddedUnits": 0,
        "expirationPossibleUnits": 0,
        "timeLeftUnknownUnits": 0,
        "inconsistentUnits": 0,
        "unknownUnits": 0,
        "commodityDepletionUnits": 0,
        "commodityReplenishmentUnits": 0,
        # Item-kind per-auction detail (empty for commodity intervals): resultID -> the
        # disappearance class of every identity auction that left the requested scope, and
        # the resultIDs whose identity changed between the two observations.
        "removedClasses": {},
        "inconsistentResultIds": [],
    }
    if kind == "commodity":
        # One bottom-walk implementation: the analysis package's. Depletion is
        # what the walk consumed from the cheapest persistent groups; the
        # interval must also satisfy the shared chain-gap rule.
        before_book = book_from_observation(left, left_rows, started_at=left_window[0])
        after_book = book_from_observation(right, right_rows, started_at=right_window[0])
        interval = valid_intervals([before_book, after_book])[0]
        if interval is None:
            result = _base_result(elapsed=exposure, expiry_gap=expiry_gap)
            # valid_intervals rejects dt <= 0 (zero-length start spacing) as well
            # as dt > MAX_GAP; label them separately.
            result["excludedIntervals"] = [
                "nonpositive_interval" if right_window[0] - left_window[0] <= 0 else "interval_too_long"
            ]
            return result
        result["commodityDepletionUnits"] = sum(units for _, units in interval.consumed)
        result["commodityReplenishmentUnits"] = replenished_units(before_book, after_book)
        result["aggregateCommodityChurnUnits"] = (
            result["commodityDepletionUnits"] + result["commodityReplenishmentUnits"]
        )
        result["individual"] = {
            "removedUnits": 0,
            "addedUnits": 0,
            "eligibleSaleCandidateUnits": None,
            "rateAvailable": False,
        }
        result["commodity"] = {
            "depletionUnits": result["commodityDepletionUnits"],
            "replenishmentUnits": result["commodityReplenishmentUnits"],
        }
        result["saleCandidateUnitsPerHour"] = None
        result["excludedIntervals"] = []
        result["commodityDepletionUnitsPerHour"] = (
            result["commodityDepletionUnits"] / (exposure / 3600) if exposure else None
        )
        result["commodityReplenishmentUnitsPerHour"] = (
            result["commodityReplenishmentUnits"] / (exposure / 3600) if exposure else None
        )
        return result

    # Grouping evidence belongs to the complete queried item scope. Filtering
    # first would hide an unrelated grouped row and incorrectly permit individual
    # disappearance inference for the requested variant.
    scoped_grouped = _grouped_scope(left_scope_rows) or _grouped_scope(right_scope_rows)
    result["groupedScope"] = scoped_grouped
    if scoped_grouped:
        result["excludedIntervals"] = ["grouped_scope"]
        result["unknownUnits"] = sum(row["quantity"] for row in left_scope_rows)
        result["eligibleIndividualSaleCandidateUnits"] = None
        result["individual"] = {
            "removedUnits": 0,
            "addedUnits": 0,
            "eligibleSaleCandidateUnits": None,
            "rateAvailable": False,
        }
        result["saleCandidateUnitsPerHour"] = None
        return result

    # Detect persistent identity changes against all retained rows before applying
    # the common ceiling. A repriced ID outside that prefix cannot become a sale
    # merely because the new row is filtered out.
    before_all: dict[int, dict[str, Any]] = {}
    for row in left_rows_all:
        result_id = row.get("resultID")
        if isinstance(result_id, int):
            before_all[result_id] = row
    after_all: dict[int, dict[str, Any]] = {}
    for row in right_rows_all:
        result_id = row.get("resultID")
        if isinstance(result_id, int):
            after_all[result_id] = row
    inconsistent_ids: set[int] = set()
    for auction_id in before_all.keys() & after_all.keys():
        if _row_fingerprint(before_all[auction_id]) != _row_fingerprint(after_all[auction_id]):
            inconsistent_ids.add(auction_id)
            old = before_all[auction_id]
            old_requested = _matches_variant(old, item_key, raw_identity)
            new_requested = _matches_variant(after_all[auction_id], item_key, raw_identity)
            # A stable ID changing variant is evidence about the requested
            # scope in either direction. Count the side belonging to that
            # scope as inconsistent/unknown and suppress add/remove evidence
            # for the ID entirely.
            if old_requested:
                affected_units = old["quantity"]
            elif new_requested:
                affected_units = after_all[auction_id]["quantity"]
            else:
                affected_units = 0
            result["inconsistentUnits"] += affected_units
            result["unknownUnits"] += affected_units
    result["inconsistentResultIds"] = sorted(inconsistent_ids)
    before = {
        row["resultID"]: row
        for row in left_rows
        if _identity_row(row) and row["resultID"] not in inconsistent_ids
    }
    after = {
        row["resultID"]: row
        for row in right_rows
        if _identity_row(row) and row["resultID"] not in inconsistent_ids
    }
    for auction_id, row in before.items():
        if auction_id not in after:
            units = row["quantity"]
            result["observedIndividualRemovedUnits"] += units
            classification = _time_class(row, expiry_gap)
            result["removedClasses"][auction_id] = classification
            if classification == "expiration_possible":
                result["expirationPossibleUnits"] += units
            elif classification == "time_left_unknown":
                result["timeLeftUnknownUnits"] += units
                result["unknownUnits"] += units
            else:
                result["eligibleIndividualSaleCandidateUnits"] = (
                    result.get("eligibleIndividualSaleCandidateUnits", 0) + units
                )
    for auction_id, row in after.items():
        if auction_id not in before:
            result["observedIndividualAddedUnits"] += row["quantity"]
    # Valid individual evidence gives the rate denominator only when an eligible
    # individual row was actually observed. Empty observations remain unknown.
    has_individual_evidence = bool(before or after)
    if has_individual_evidence:
        result["individualExposureSeconds"] = exposure
        result["individualRateDenominatorHours"] = exposure / 3600
        result["individualRateAvailable"] = True
        result["saleCandidateUnitsPerHour"] = result.get("eligibleIndividualSaleCandidateUnits", 0) / (
            exposure / 3600
        )
    else:
        result["eligibleIndividualSaleCandidateUnits"] = None
    result["individual"] = {
        "removedUnits": result["observedIndividualRemovedUnits"],
        "addedUnits": result["observedIndividualAddedUnits"],
        "eligibleSaleCandidateUnits": result.get("eligibleIndividualSaleCandidateUnits"),
        "rateAvailable": has_individual_evidence,
    }
    result["commodity"] = {"depletionUnits": 0, "replenishmentUnits": 0}
    result["commodityDepletionUnitsPerHour"] = None
    result["commodityReplenishmentUnitsPerHour"] = None
    result["saleCandidateRateLabel"] = "UNCALIBRATED"
    return result


def _empty_result(exclusions: list[str], *, elapsed: int = 0) -> dict[str, Any]:
    result = _base_result(elapsed=elapsed)
    result["excludedIntervals"] = exclusions
    return result

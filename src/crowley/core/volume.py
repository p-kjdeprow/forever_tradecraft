"""Conservative turnover evidence for normalized market observations."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from copy import deepcopy
from fractions import Fraction
from typing import Any

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


def _record_window(record: dict[str, Any]) -> tuple[int, int] | None:
    """Return the scope window used for report filtering.

    Queried observations use their actual query window. Failed/unqueried records
    have no query window, so their envelope sweep bounds keep the failure in the
    ordered interval stream without inventing a query.
    """
    observed = _query_window(_observation(record))
    if observed is not None:
        return observed
    meta = _record_meta(record)
    started = meta.get("startedAt")
    finished = meta.get("finishedAt")
    if isinstance(started, int) and isinstance(finished, int):
        return started, finished
    return None


def _record_start(record: dict[str, Any]) -> int:
    window = _record_window(record)
    return window[0] if window else 0


def _record_end(record: dict[str, Any]) -> int:
    window = _record_window(record)
    return window[1] if window else -1


def _sweep_start(record: dict[str, Any]) -> int:
    value = _record_meta(record).get("startedAt")
    return value if isinstance(value, int) else _record_start(record)


def _sweep_end(record: dict[str, Any]) -> int:
    value = _record_meta(record).get("finishedAt")
    return value if isinstance(value, int) else _record_end(record)


def _ordinal(record: dict[str, Any]) -> int:
    value = _observation(record).get("ordinal", 0)
    return value if isinstance(value, int) else 0


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


def project_observation_variant(
    observation: dict[str, Any], requested_item_key: str, *, raw_identity: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Project a whole observation onto one exact canonical variant.

    The coverage, failure, and query metadata remain those of the original item
    scope. Only captured rows are filtered, so an empty projection still means
    absence within the proved prefix rather than an unqueried item.
    """
    result = deepcopy(observation)
    rows = _rows(observation)
    selected = [deepcopy(row) for row in rows if row.get("itemKey") == requested_item_key]
    if raw_identity is not None:
        selected = [row for row in selected if row.get("rawItemKey") == raw_identity]
    result["rows"] = {f"row:{index}": row for index, row in enumerate(selected, 1)}
    result["rowCount"] = len(selected)
    return result


project_variant = project_observation_variant


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


def compare_observations(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    item_key: str | None = None,
    raw_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare two consecutive observations and return conservative evidence."""
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
    if expiry_gap > MAX_INTERVAL_SECONDS:
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
    }
    if kind == "commodity":
        commodity_before: defaultdict[Fraction, int] = defaultdict(int)
        commodity_after: defaultdict[Fraction, int] = defaultdict(int)
        for row in left_rows:
            commodity_before[_ratio(row["unitPrice"])] += row["quantity"]
        for row in right_rows:
            commodity_after[_ratio(row["unitPrice"])] += row["quantity"]
        for price in set(commodity_before) | set(commodity_after):
            delta = commodity_before[price] - commodity_after[price]
            if delta > 0:
                result["commodityDepletionUnits"] += delta
            elif delta < 0:
                result["commodityReplenishmentUnits"] -= delta
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


def volume_report(
    records: Iterable[dict[str, Any]],
    *,
    hours: int = 24,
    end: int | None = None,
    item_key: str | None = None,
    raw_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a report ordered by envelope sweep start and ordinal.

    Every failed/unqueried record in the requested window remains in the sequence;
    a comparison touching it is an excluded interval rather than a manufactured
    zero-rate sample. Latest/freshness are computed independently of the window.
    """
    if hours < 1 or hours > 168:
        raise ValueError("hours must be between 1 and 168")
    ordered = sorted(records, key=lambda record: (_sweep_start(record), _ordinal(record)))
    if end is None:
        end = max((_record_end(record) for record in ordered), default=0)
    start = end - hours * 3600
    comparisons: list[dict[str, Any]] = []
    for left, right in zip(ordered, ordered[1:], strict=False):
        comparison = compare_observations(left, right, item_key=item_key, raw_identity=raw_identity)
        prior_end = _record_end(left)
        current_end = _record_end(right)
        prior_in_window = start <= prior_end <= end
        current_in_window = start <= current_end <= end
        if not (prior_in_window or current_in_window):
            continue
        if not (prior_in_window and current_in_window):
            comparison["valid"] = False
            comparison["excludedIntervals"] = [
                *comparison.get("excludedIntervals", []),
                "outside_window",
            ]
        comparisons.append(comparison)
    valid = [entry for entry in comparisons if entry.get("valid")]
    elapsed = sum(entry.get("comparedSeconds", 0) for entry in valid)
    candidates = sum(entry.get("eligibleIndividualSaleCandidateUnits", 0) or 0 for entry in valid)
    latest_record = (
        max(ordered, key=lambda record: (_record_end(record), _sweep_start(record), _ordinal(record)))
        if ordered
        else None
    )
    latest = _observation(latest_record) if latest_record else None
    if latest is not None and item_key is not None:
        latest = project_observation_variant(latest, item_key, raw_identity=raw_identity)
    individual_intervals = [entry for entry in valid if entry.get("individualRateAvailable")]
    individual_exposure = sum(
        entry.get("individualExposureSeconds", 0) or 0 for entry in individual_intervals
    )
    commodity_depletion = sum(entry.get("commodityDepletionUnits", 0) for entry in valid)
    commodity_replenishment = sum(entry.get("commodityReplenishmentUnits", 0) for entry in valid)
    commodity_intervals = [entry for entry in valid if entry.get("kind") == "commodity"]
    commodity_exposure = sum(entry.get("comparedSeconds", 0) for entry in commodity_intervals)
    removed = sum(entry.get("observedIndividualRemovedUnits", 0) for entry in valid)
    added = sum(entry.get("observedIndividualAddedUnits", 0) for entry in valid)
    expiry = sum(entry.get("expirationPossibleUnits", 0) for entry in valid)
    unknown = sum(entry.get("unknownUnits", 0) for entry in valid)
    inconsistent = sum(entry.get("inconsistentUnits", 0) for entry in valid)
    return {
        "latestObservation": latest,
        "freshness": None if latest_record is None else end - _record_end(latest_record),
        "coverage": None if latest is None else latest.get("coverage"),
        "comparedSeconds": elapsed,
        "comparedHours": elapsed / 3600,
        "intervals": comparisons,
        "excludedIntervals": [
            reason for entry in comparisons for reason in entry.get("excludedIntervals", [])
        ],
        "eligibleIndividualSaleCandidateUnits": candidates if individual_intervals else None,
        "saleCandidateUnitsPerHour": candidates / (individual_exposure / 3600)
        if individual_exposure
        else None,
        "saleCandidateRateLabel": "UNCALIBRATED",
        "individualExposureSeconds": individual_exposure if individual_intervals else None,
        "individualRateDenominatorHours": individual_exposure / 3600 if individual_intervals else None,
        "observedIndividualRemovedUnits": removed,
        "observedIndividualAddedUnits": added,
        "expirationPossibleUnits": expiry,
        "timeLeftUnknownUnits": sum(entry.get("timeLeftUnknownUnits", 0) for entry in valid),
        "unknownUnits": unknown,
        "inconsistentUnits": inconsistent,
        "commodityDepletionUnits": commodity_depletion,
        "commodityReplenishmentUnits": commodity_replenishment,
        "aggregateCommodityChurnUnits": commodity_depletion + commodity_replenishment,
        "commodityExposureSeconds": commodity_exposure if commodity_intervals else None,
        "commodityDepletionUnitsPerHour": (
            commodity_depletion / (commodity_exposure / 3600) if commodity_exposure else None
        ),
        "commodityReplenishmentUnitsPerHour": (
            commodity_replenishment / (commodity_exposure / 3600) if commodity_exposure else None
        ),
        "marketActualSalesEstimate": None,
        "marketActualSalesConfidenceInterval": None,
        "ownConfirmed": {"units": 0, "evidence": []},
    }


compare = compare_observations

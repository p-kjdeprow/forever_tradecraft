"""Executable Node checks for the token-free crafting dashboard."""

import json
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DASHBOARD = ROOT / "packages/market/src/crowley_market/server/static/trading-dashboard.html"


def _node(script: str) -> dict:
    node = shutil.which("node")
    assert node is not None, "node is required for dashboard tests"
    result = subprocess.run([node], input=script, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def _script() -> str:
    html = DASHBOARD.read_text(encoding="utf-8")
    return html[html.index("<script>") + len("<script>") : html.index("</script>")]


def _pure_script() -> str:
    script = _script()
    start = script.index("/* TRADING PURE LOGIC START */") + len("/* TRADING PURE LOGIC START */")
    end = script.index("/* TRADING PURE LOGIC END */", start)
    return script[start:end]


def _dashboard_runtime() -> str:
    return """
class FakeNode {
  constructor(tag = "div", text = "") {
    if (global.failElementTag === tag) throw new Error("render failed");
    this.tag = tag;
    this.children = [];
    this._text = String(text);
    this.value = "";
    this.className = "";
    this.hidden = false;
    this.dateTime = "";
    this.listeners = {};
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ""; this.children = [...children]; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
}
const mountDashboard = (storage) => {
  const ids = ["scan-time", "scan-age", "scan-counts", "status", "summary-source", "opportunities", "notes", "house-form", "house-key", "minimum-profit", "recipe-watch-state", "recipe-watch-alert", "recipe-watch-content", "recipe-watch-notes"];
  const nodes = new Map(ids.map((id) => [id, new FakeNode("div")]));
  global.document = {
    getElementById: (id) => nodes.get(id),
    createElement: (tag) => new FakeNode(tag),
    createTextNode: (text) => new FakeNode("#text", text),
  };
  let intervalCallback;
  let intervalMs;
  global.window = { localStorage: storage, setInterval: (callback, ms) => { intervalCallback = callback; intervalMs = ms; return 1; } };
  __DASHBOARD_SCRIPT__
  return { nodes, dashboard: window.__crowleyTradingDashboardTest, tick: () => intervalCallback(), intervalMs: () => intervalMs };
};
""".replace("__DASHBOARD_SCRIPT__", _script())


def test_exact_money_rounding_and_stale_scan_model():
    actual = _node(
        f"""
{_pure_script()}
console.log(JSON.stringify({{
  normal: formatCopper("108851/10"),
  expense: formatCopper("6237/2", "ceil"),
  proceeds: formatCopper("6237/2", "floor"),
  negativeFloor: formatCopper("-1/4", "floor"),
  negativeCeil: formatCopper("-1/4", "ceil"),
  stale: scanAge({{house:"us-normal-alliance", sweep_id:1, finished_epoch:1000}}, 1000 + 46 * 60).stale,
  recent: scanAge({{house:"us-normal-alliance", sweep_id:1, finished_epoch:1000}}, 1001).stale
}}));
"""
    )
    assert actual == {
        "normal": "1g 8s 85c",
        "expense": "31s 19c",
        "proceeds": "31s 18c",
        "negativeFloor": "−1c",
        "negativeCeil": "0c",
        "stale": True,
        "recent": False,
    }


def _recipe_watch(house: str = "us-normal-alliance", status: str = "low_price") -> dict:
    alert = status == "low_price"
    baseline_count = 3 if status == "building_history" else 6
    baseline_median = None if baseline_count < 6 else "12000"
    threshold = None if baseline_median is None else "8400"
    return {
        "watch_id": "draught-of-water-walking",
        "house": house,
        "client_build": "1.60.1.70938",
        "as_of_sweep": 10,
        "as_of_epoch": 1_800_000_060,
        "recipe": {
            "item_id": 288206,
            "name": "Recipe: Draught of Water Walking",
            "required_alchemy": 180,
            "status": status,
            "observed_at": None if status in {"awaiting_scan", "query_failed"} else 1_800_000_000,
            "query_sweep_id": None if status in {"awaiting_scan", "query_failed"} else 10,
            "current_price": "7000"
            if status not in {"awaiting_scan", "query_failed", "not_listed"}
            else None,
            "baseline_median": baseline_median,
            "threshold_price": threshold,
            "baseline_observations": baseline_count,
            "minimum_baseline_observations": 6,
            "baseline_window_hours": 24,
            "discount_fraction": 0.3,
            "freshness_max_seconds": 1800,
            "alert": alert,
            "listing_quantity": 1,
        },
        "potion": {
            "item_id": 8827,
            "name": "Draught of Water Walking",
            "observed_at": 1_800_000_000,
            "lowest_price": "800",
            "retained_supply": 12,
            "daily": {
                "window_hours": 24,
                "observed_units": 212,
                "partial_units": 29,
                "disappearance_units": 183,
                "usable_hours": 23.777,
                "units_per_hour": 8.916,
                "usable_intervals": 16,
            },
            "recent": {
                "window_hours": 6,
                "observed_units": 13,
                "partial_units": 4,
                "disappearance_units": 9,
                "usable_hours": 5.749,
                "units_per_hour": 2.261,
                "usable_intervals": 4,
            },
            "confirmed_sales": None,
            "pilot_batch_units": [1, 2],
        },
        "notes": ["Observed depletion is not confirmed sales.", "Personal sales are unmeasured."],
    }


@pytest.mark.parametrize("build", ["1.60.1.70938", "1.60.1.70959"])
def test_watch_accepts_valid_builds_independently_of_the_initial_cache_build(build: str) -> None:
    watch = _recipe_watch(status="awaiting_scan")
    watch["client_build"] = build
    actual = _node(
        f"{_pure_script()}\nconsole.log(JSON.stringify(validateRecipeWatch({json.dumps(watch)}, 'us-normal-alliance', 1_800_000_060).client_build));"
    )
    assert actual == build


def test_recipe_watch_validation_preserves_status_and_measured_zero_semantics():
    watch = _recipe_watch()
    stale = _recipe_watch()
    stale["recipe"]["observed_at"] = 1_800_000_000
    no_intervals = _recipe_watch(status="awaiting_scan")
    no_intervals["potion"]["daily"] = {
        "window_hours": 24,
        "observed_units": 0,
        "partial_units": 0,
        "disappearance_units": 0,
        "usable_hours": None,
        "units_per_hour": None,
        "usable_intervals": 0,
    }
    measured_zero = dict(no_intervals)
    measured_zero["potion"] = {
        **no_intervals["potion"],
        "daily": {
            "window_hours": 24,
            "observed_units": 0,
            "partial_units": 0,
            "disappearance_units": 0,
            "usable_hours": 2.5,
            "units_per_hour": 0,
            "usable_intervals": 3,
        },
    }
    actual = _node(
        f"""
{_pure_script()}
const low = validateRecipeWatch({json.dumps(watch)}, "us-normal-alliance", 1_800_000_060).recipe;
const timeout = validateRecipeWatch({json.dumps(stale)}, "us-normal-alliance", 1_800_001_801).recipe;
const noCoverage = validateRecipeWatch({json.dumps(no_intervals)}, "us-normal-alliance", 1_800_000_060).potion.daily;
const zero = validateRecipeWatch({json.dumps(measured_zero)}, "us-normal-alliance", 1_800_000_060).potion.daily;
console.log(JSON.stringify({{ low: [low.status, low.alert], timeout: [timeout.status, timeout.alert], noCoverage: [noCoverage.usable_hours, noCoverage.units_per_hour], zero: [zero.usable_hours, zero.units_per_hour] }}));
"""
    )
    assert actual == {
        "low": ["low_price", True],
        "timeout": ["stale", False],
        "noCoverage": [None, None],
        "zero": [2.5, 0],
    }


def _item(index: int) -> dict:
    leveling = index == 1
    unknown = index == 2
    zero = index == 3
    return {
        "spell_id": 1250776 + index,
        "item_id": 123 + index,
        "name": "<img src=x onerror=alert(1)>" if index == 0 else f"Item {index}",
        "profession": "tailoring" if leveling else "alchemy",
        "required_skill": 205 if leveling else 195,
        "skill_note": "Tailoring yellow by 205; check trainer before leveling" if leveling else "Alchemy 195",
        "needs_leveling": leveling,
        "crafts": 2 if index == 0 else 1,
        "quantity": 2 if index == 0 else 1,
        "unit_list_price": "5729",
        "cost": "6237/2" if index == 0 else "100",
        "sale_proceeds": "108851/10" if index == 0 else "200",
        "profit": "77666/10" if index == 0 else "100",
        "profit_per_item": "38833/10" if index == 0 else "100",
        "market_units_per_hour": (12.57 if index == 0 else 750 if unknown else 0 if zero else 6),
        "volume_status": "unknown" if unknown else "observed_activity",
        "usable_hours": None if unknown else 5.25,
        "usable_intervals": None if unknown else 8,
        "sale_probability": None,
        "evidence_label": "No usable activity evidence"
        if unknown
        else "Observed listing depletion; sales unconfirmed",
        "price_age_seconds": 60,
        "cooldown_seconds": 3600 if index in (4, 5) else 0,
        "cooldown_group": "transmute" if index in (4, 5) else None,
        "requirements": {
            "tools": ["RawToolId 5031"] if leveling else [],
            "tool_categories": ["CategoryId 2248"] if leveling else [],
            "focus": 2248 if leveling else None,
        },
        "reagents": (
            [
                {
                    "item_id": 2592,
                    "name": "Cloth",
                    "quantity": 2,
                    "source": "vendor",
                    "bundle_quantity": 5,
                    "cost": "600",
                }
            ]
            if leveling
            else [{"item_id": 3818, "name": "Fadeleaf", "quantity": 4, "source": "ah", "cost": "1388"}]
        ),
    }


def _summary(sweep_id: int, finished_epoch: int, count: int = 12) -> dict:
    candidates = [_item(index) for index in range(count) if index not in {2, 3}]
    return {
        "house": "us-normal-alliance",
        "client_build": "1.60.1.70938",
        "sweep_id": sweep_id,
        "finished_epoch": finished_epoch,
        "activity_window_hours": 24,
        "scan": {"planned": 484, "queried": 484, "successful": 483},
        "profile": {
            "alchemy": 225,
            "tailoring": 150,
            "tailoring_planning_cap": 225,
            "budget_copper": 100000,
            "recipes_assumed_owned": True,
        },
        "candidates": candidates,
        "opportunities": candidates[:10],
        "notes": ["Recipe and leveling costs excluded."],
    }


def _fraction_text(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _ranked_candidate(index: int, batch_profit: str, quantity: int, crafts: int = 1) -> dict:
    profit = Fraction(batch_profit)
    cost = Fraction(100)
    item = _item(index + 100)
    item.update(
        name=f"Rank {index:02d}",
        crafts=crafts,
        quantity=quantity,
        cost=_fraction_text(cost),
        sale_proceeds=_fraction_text(cost + profit),
        profit=_fraction_text(profit),
        profit_per_item=_fraction_text(profit / quantity),
    )
    return item


def test_dashboard_loads_scan_first_caps_cards_and_refreshes_without_auth():
    latest1 = {
        "house": "us-normal-alliance",
        "sweep_id": 1,
        "finished_epoch": 1_800_000_000,
        "client_build": "1.60.1.70938",
    }
    latest2 = {**latest1, "sweep_id": 2, "finished_epoch": 1_800_000_060}
    latest3 = {**latest1, "sweep_id": 3, "finished_epoch": 1_800_000_120}
    latest4 = {**latest1, "sweep_id": 4, "finished_epoch": 1_800_000_180}
    summary2 = _summary(2, 1_800_000_060)
    summary3 = _summary(3, 1_800_000_120)
    summary4 = _summary(4, 1_800_000_180)
    summary5 = _summary(5, 1_800_000_240)
    summary3["opportunities"][0]["name"] = "Stale sweep only"
    summary4["opportunities"][0]["name"] = "Newest sweep"
    summary5["opportunities"][0]["name"] = "Latest summary wins"
    watch1 = _recipe_watch()
    other_watch = _recipe_watch("other-house", status="building_history")
    empty_latest = {
        "house": "other-house",
        "sweep_id": 6,
        "finished_epoch": 1_800_000_300,
        "client_build": "1.60.1.70938",
    }
    empty_summary = _summary(6, 1_800_000_300, count=0)
    empty_summary["house"] = "other-house"
    empty_summary["candidates"] = []
    empty_summary["opportunities"] = []
    actual = _node(
        f"""
const calls = [];
const tags = [];
let latestReply = {json.dumps(latest1)};
let watchReply = {json.dumps(watch1)};
let currentNow = 1_800_000_060_000;
Date.now = () => currentNow;
const pendingSummaries = [];
function reply(value, ok = true, status = 200) {{ return {{ ok, status, json: () => Promise.resolve(value) }}; }}
global.fetch = (path, options) => {{
  calls.push({{path, options}});
  if (path.endsWith("/sweeps/latest")) return Promise.resolve(reply(latestReply));
  if (path.endsWith("/recipe-watch")) return Promise.resolve(reply(watchReply));
  return new Promise((resolve) => pendingSummaries.push((value) => resolve(reply(value))));
}};
class FakeNode {{
  constructor(tag = "div", text = "") {{ this.tag = tag; tags.push(tag); this.children = []; this._text = String(text); this.value = ""; this.className = ""; this.hidden = false; this.dateTime = ""; this.listeners = {{}}; }}
  set textContent(value) {{ this._text = String(value); this.children = []; }}
  get textContent() {{ return this._text + this.children.map((child) => child.textContent).join(""); }}
  appendChild(child) {{ this.children.push(child); return child; }}
  append(...children) {{ this.children.push(...children); }}
  replaceChildren(...children) {{ this._text = ""; this.children = [...children]; }}
  addEventListener(name, handler) {{ this.listeners[name] = handler; }}
}}
const nodes = new Map(["scan-time", "scan-age", "scan-counts", "status", "summary-source", "opportunities", "notes", "house-form", "house-key", "minimum-profit", "recipe-watch-state", "recipe-watch-alert", "recipe-watch-content", "recipe-watch-notes"].map((id) => [id, new FakeNode("div")]));
global.document = {{
  getElementById: (id) => nodes.get(id),
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (text) => new FakeNode("#text", text),
}};
let intervalCallback;
let intervalMs;
global.window = {{ setInterval: (callback, ms) => {{ intervalCallback = callback; intervalMs = ms; return 1; }} }};
const settle = async () => {{ for (let i = 0; i < 10; i += 1) await Promise.resolve(); await new Promise(setImmediate); }};
{_script()}
(async () => {{
await settle();
const scanWasFast = {{ time: nodes.get("scan-time").textContent, requests: calls.map((call) => call.path), summaryPending: pendingSummaries.length, watchState: nodes.get("recipe-watch-state").textContent, watchAlert: nodes.get("recipe-watch-alert").textContent, watchContent: nodes.get("recipe-watch-content").textContent }};
pendingSummaries.shift()({json.dumps(summary2)});
await settle();
latestReply = {json.dumps(latest2)};
const rendered2 = {{
  scanTime: nodes.get("scan-time").textContent,
  scanAge: nodes.get("scan-age").textContent,
  counts: nodes.get("scan-counts").textContent,
  status: nodes.get("status").textContent,
  count: nodes.get("opportunities").children.length,
  cards: nodes.get("opportunities").textContent,
  cardsAreSafeText: !tags.includes("img"),
  intervalMs,
}};
const poll = window.__crowleyTradingDashboardTest.pollLatest;
currentNow += 120_000;
intervalCallback();
await settle();
const callsAfterSameSweep = calls.length;
const priceAgeAfterClockAdvance = nodes.get("opportunities").textContent;
const watchAfterClockAdvance = nodes.get("recipe-watch-state").textContent;
latestReply = {json.dumps(latest3)};
await poll();
await settle();
latestReply = {json.dumps(latest4)};
await poll();
await settle();
const afterNewScan = {{ summaryPending: pendingSummaries.length, cards: nodes.get("opportunities").textContent, counts: nodes.get("scan-counts").textContent, source: nodes.get("summary-source").textContent }};
pendingSummaries.shift()({json.dumps(summary3)});
await settle();
const retriedForNewest = pendingSummaries.length;
pendingSummaries.shift()({json.dumps(summary4)});
await settle();
const raced = {{ scanTime: nodes.get("scan-time").textContent, scanAge: nodes.get("scan-age").textContent, cards: nodes.get("opportunities").textContent, status: nodes.get("status").textContent }};
const dashboard = window.__crowleyTradingDashboardTest;
let releaseDelayedLatest;
global.fetch = (path) => path.endsWith("/sweeps/latest")
  ? new Promise((resolve) => {{ releaseDelayedLatest = () => resolve(reply({json.dumps(latest4)})); }})
  : path.endsWith("/recipe-watch")
    ? Promise.resolve(reply({json.dumps(watch1)}))
    : Promise.resolve(reply({json.dumps(summary5)}));
const delayedPoll = dashboard.pollLatest();
await settle();
dashboard.loadSummary(dashboard.state.latest, dashboard.state.generation);
await settle();
const scanAfterNewerSummary = dashboard.state.latest.sweep_id;
releaseDelayedLatest();
await delayedPoll;
await settle();
const latestRace = {{ summarySweep: scanAfterNewerSummary, finalSweep: dashboard.state.latest.sweep_id, age: nodes.get("scan-age").textContent, cards: nodes.get("opportunities").textContent }};
global.fetch = () => Promise.resolve(reply({{}}, false, 404));
dashboard.changeHouse("bad-house");
await settle();
const error = {{ status: nodes.get("status").textContent, scanClass: nodes.get("scan-age").className }};
global.fetch = (path) => Promise.resolve(reply(path.endsWith("/sweeps/latest") ? {json.dumps(empty_latest)} : path.endsWith("/recipe-watch") ? {json.dumps(other_watch)} : {json.dumps(empty_summary)}));
dashboard.changeHouse("other-house");
await settle();
const emptyTurnover = {{
  scanTime: nodes.get("scan-time").textContent,
  scanAge: nodes.get("scan-age").textContent,
  counts: nodes.get("scan-counts").textContent,
  cards: nodes.get("opportunities").textContent,
  cardClass: nodes.get("opportunities").children[0]?.className,
  status: nodes.get("status").textContent,
  watchState: nodes.get("recipe-watch-state").textContent,
  watchAlert: nodes.get("recipe-watch-alert").textContent,
  watchContent: nodes.get("recipe-watch-content").textContent,
}};
const requestSignals = calls.map((call) => ({{ path: call.path, hasSignal: Boolean(call.options?.signal) }}));
console.log(JSON.stringify({{ scanWasFast, rendered2, requestSignals, callsAfterSameSweep, priceAgeAfterClockAdvance, watchAfterClockAdvance, afterNewScan, retriedForNewest, raced, latestRace, error, emptyTurnover, intervalCallbackRegistered: typeof intervalCallback === "function" }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    )

    scan_time = actual["scanWasFast"]["time"]
    assert scan_time
    assert scan_time != "Loading latest scan…"
    assert actual["scanWasFast"]["summaryPending"] == 1
    assert actual["scanWasFast"]["requests"] == [
        "/v1/houses/us-normal-alliance/sweeps/latest",
        "/v1/houses/us-normal-alliance/recipe-watch",
        "/v1/houses/us-normal-alliance/trading-summary",
    ]
    assert "LOW PRICE" in actual["scanWasFast"]["watchAlert"]
    assert "Recipe price" in actual["scanWasFast"]["watchContent"]
    assert "212 units across 23.78 usable hours" in actual["scanWasFast"]["watchContent"]
    assert "13 units across 5.75 usable hours" in actual["scanWasFast"]["watchContent"]
    assert "Confirmed sales: Unknown" in actual["scanWasFast"]["watchContent"]
    assert actual["rendered2"]["count"] == 10
    assert "Planned 484Queried 484Successful 483" in actual["rendered2"]["counts"]
    assert "Batch cost" in actual["rendered2"]["cards"]
    assert "31s 19c" in actual["rendered2"]["cards"]  # 3,118.5 copper rounds up as an expense.
    assert "Sale proceeds" in actual["rendered2"]["cards"]
    assert "1g 8s 85c" in actual["rendered2"]["cards"]  # 10,885.1 copper rounds down.
    assert "Net profit per item" in actual["rendered2"]["cards"]
    assert "38s 83c" in actual["rendered2"]["cards"]  # Batch profit divided by two output units.
    assert "12.6 units/h" in actual["rendered2"]["cards"]
    assert "24h observed activity" in actual["rendered2"]["cards"]
    assert "5.25 usable h across 8 intervals" in actual["rendered2"]["cards"]
    assert "Sales are unconfirmed" in actual["rendered2"]["cards"]
    assert "Turnover unknown" not in actual["rendered2"]["cards"]
    assert "Item 11" in actual["rendered2"]["cards"]
    assert "Item 2" not in actual["rendered2"]["cards"]
    assert "Item 3" not in actual["rendered2"]["cards"]
    assert "Leveling target" in actual["rendered2"]["cards"]
    assert "Tailoring yellow by 205; check trainer before leveling" in actual["rendered2"]["cards"]
    assert "Recipe-specific tools required — check recipe" in actual["rendered2"]["cards"]
    assert "Crafting station or category required — check recipe" in actual["rendered2"]["cards"]
    assert "Crafting focus required — check recipe" in actual["rendered2"]["cards"]
    assert "5031" not in actual["rendered2"]["cards"] and "2248" not in actual["rendered2"]["cards"]
    assert "Cooldown: 60 min · shared cooldown group: transmute" in actual["rendered2"]["cards"]
    assert "Fadeleaf · ×4 · auction house · 13s 88c" in actual["rendered2"]["cards"]
    assert "Cloth · Need 2 · buy 5 vendor bundle · 6s" in actual["rendered2"]["cards"]
    assert "<img src=x onerror=alert(1)>" in actual["rendered2"]["cards"]
    assert actual["rendered2"]["cardsAreSafeText"] is True
    assert actual["rendered2"]["intervalMs"] == 60000
    assert actual["intervalCallbackRegistered"] is True
    assert (
        actual["callsAfterSameSweep"] == 5
    )  # Both independent panels refresh even when the sweep is unchanged.
    assert "Price age ~3 min" in actual["priceAgeAfterClockAdvance"]
    assert "Updated" in actual["watchAfterClockAdvance"]
    assert actual["afterNewScan"]["summaryPending"] == 2
    assert "Item 1" in actual["afterNewScan"]["cards"]
    assert "sweep #2" in actual["afterNewScan"]["source"].lower()
    assert "latest sweep #4" in actual["afterNewScan"]["source"]
    assert "query counts load" in actual["afterNewScan"]["counts"].lower()
    assert actual["retriedForNewest"] == 1  # A stale summary is discarded; current sweep is fetched once.
    assert "Sweep #4" in actual["raced"]["scanAge"]
    assert "Newest sweep" in actual["raced"]["cards"]
    assert "Stale sweep only" not in actual["raced"]["cards"]  # The stale sweep did not overwrite newer rows.
    assert actual["latestRace"]["summarySweep"] == 5
    assert (
        actual["latestRace"]["finalSweep"] == 5
    )  # A delayed GET for sweep 4 cannot downgrade newer summary data.
    assert "Latest summary wins" in actual["latestRace"]["cards"]
    assert actual["error"]["scanClass"] == "scan-state error"
    assert "404" in actual["error"]["status"]
    assert actual["emptyTurnover"]["scanTime"]
    assert "Sweep #6" in actual["emptyTurnover"]["scanAge"]
    assert "Planned 484Queried 484Successful 483" in actual["emptyTurnover"]["counts"]
    assert (
        "No profitable strategies currently meet the turnover requirements"
        not in actual["emptyTurnover"]["cards"]
    )
    assert "at least 6 observed market units/hour" in actual["emptyTurnover"]["cards"]
    assert actual["emptyTurnover"]["cardClass"] == "panel empty"
    assert "No strategies meet this minimum net profit per item" in actual["emptyTurnover"]["status"]
    assert "other-house" in actual["emptyTurnover"]["watchState"]
    assert "Price history is warming" in actual["emptyTurnover"]["watchState"]
    assert all(
        call["hasSignal"]
        for call in actual["requestSignals"]
        if call["path"].endswith(("/sweeps/latest", "/trading-summary"))
    )
    assert all(
        not call["hasSignal"] for call in actual["requestSignals"] if call["path"].endswith("/recipe-watch")
    )


def test_cached_summary_restores_before_network_and_matching_latest_reuses_its_counts():
    latest = {
        "house": "us-normal-alliance",
        "sweep_id": 8,
        "finished_epoch": 1_800_000_060,
        "client_build": "1.60.1.70938",
    }
    summary = _summary(8, 1_800_000_060)
    summary["opportunities"][0]["name"] = "Saved recommendation"
    watch = _recipe_watch(status="awaiting_scan")
    runtime = _dashboard_runtime()
    actual = _node(
        f"""
let nowMs = 1_800_000_180_000;
Date.now = () => nowMs;
const values = new Map();
const storage = {{ getItem: (key) => values.has(key) ? values.get(key) : null, setItem: (key, value) => values.set(key, value) }};
const calls = [];
function reply(value, ok = true, status = 200) {{ return {{ ok, status, json: () => Promise.resolve(value) }}; }}
let latestReply = {json.dumps(latest)};
global.fetch = (path, options) => {{
  calls.push({{ path, options }});
  if (path.endsWith("/sweeps/latest")) return Promise.resolve(reply(latestReply));
  if (path.endsWith("/recipe-watch")) return Promise.resolve(reply({json.dumps(watch)}));
  return Promise.resolve(reply({json.dumps(summary)}));
}};
const settle = async () => {{ for (let i = 0; i < 12; i += 1) await Promise.resolve(); await new Promise(setImmediate); }};
{runtime}
(async () => {{
  const first = mountDashboard(storage);
  await settle();
  const saved = {{ cards: first.nodes.get("opportunities").textContent, cacheCount: values.size }};
  let releaseLatest;
  let latestDeferred = true;
  global.fetch = (path, options) => {{
    calls.push({{ path, options }});
    if (path.endsWith("/sweeps/latest") && latestDeferred) return new Promise((resolve) => {{ releaseLatest = () => resolve(reply(latestReply)); }});
    if (path.endsWith("/sweeps/latest")) return Promise.resolve(reply(latestReply));
    if (path.endsWith("/recipe-watch")) return new Promise(() => {{}});
    throw new Error("matching cached latest must not need a summary request");
  }};
  const reloaded = mountDashboard(storage);
  await settle();
  const beforeNetwork = {{ cards: reloaded.nodes.get("opportunities").textContent, counts: reloaded.nodes.get("scan-counts").textContent, source: reloaded.nodes.get("summary-source").textContent, latestCalls: calls.filter((call) => call.path.endsWith("/sweeps/latest")).length }};
  latestDeferred = false;
  releaseLatest();
  await settle();
  const afterLatest = {{ counts: reloaded.nodes.get("scan-counts").textContent, source: reloaded.nodes.get("summary-source").textContent, summaryCalls: calls.filter((call) => call.path.endsWith("/trading-summary")).length }};
  nowMs += 60_000;
  reloaded.tick();
  await settle();
  const afterClock = {{ cards: reloaded.nodes.get("opportunities").textContent, source: reloaded.nodes.get("summary-source").textContent }};
  console.log(JSON.stringify({{ saved, beforeNetwork, afterLatest, afterClock }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    )
    assert actual["saved"]["cacheCount"] == 1
    assert "Saved recommendation" in actual["beforeNetwork"]["cards"]
    assert "query counts load" in actual["beforeNetwork"]["counts"].lower()
    assert "sweep #8" in actual["beforeNetwork"]["source"].lower()
    assert "age 2 min" in actual["beforeNetwork"]["source"]
    assert actual["beforeNetwork"]["latestCalls"] == 2
    assert "Planned 484Queried 484Successful 483" in actual["afterLatest"]["counts"]
    assert actual["afterLatest"]["summaryCalls"] == 1
    assert "age 3 min" in actual["afterClock"]["source"]
    assert "Price age ~4 min" in actual["afterClock"]["cards"]


def test_failed_invalid_and_unrenderable_summaries_keep_cards_until_empty_success():
    latest = {
        "house": "us-normal-alliance",
        "sweep_id": 1,
        "finished_epoch": 1_800_000_000,
        "client_build": "1.60.1.70938",
    }
    first = _summary(1, 1_800_000_000)
    first["opportunities"][0]["name"] = "Last successful recommendation"
    invalid = _summary(3, 1_800_000_120)
    invalid["profile"]["alchemy"] = 1
    unrenderable = _summary(4, 1_800_000_180)
    empty = _summary(5, 1_800_000_240, count=0)
    watch = _recipe_watch(status="awaiting_scan")
    runtime = _dashboard_runtime()
    actual = _node(
        f"""
let latestReply = {json.dumps(latest)};
let mode = "first";
const values = new Map();
const storage = {{ getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }};
function reply(value, ok = true, status = 200) {{ return {{ ok, status, json: () => Promise.resolve(value) }}; }}
global.fetch = (path) => {{
  if (path.endsWith("/sweeps/latest")) return Promise.resolve(reply(latestReply));
  if (path.endsWith("/recipe-watch")) return Promise.resolve(reply({json.dumps(watch)}));
  if (mode === "first") return Promise.resolve(reply({json.dumps(first)}));
  if (mode === "reject") return Promise.reject(new Error("offline"));
  if (mode === "invalid") return Promise.resolve(reply({json.dumps(invalid)}));
  if (mode === "render") return Promise.resolve(reply({json.dumps(unrenderable)}));
  return Promise.resolve(reply({json.dumps(empty)}));
}};
const settle = async () => {{ for (let i = 0; i < 12; i += 1) await Promise.resolve(); await new Promise(setImmediate); }};
{runtime}
(async () => {{
  const page = mountDashboard(storage);
  await settle();
  const initialCards = page.nodes.get("opportunities").textContent;
  const cacheKey = page.dashboard.summaryCacheKey("us-normal-alliance", "1.60.1.70938");
  const check = async (sweepId, nextMode) => {{
    mode = nextMode;
    latestReply = {{ ...latestReply, sweep_id: sweepId, finished_epoch: 1_800_000_000 + (sweepId - 1) * 60 }};
    await page.dashboard.pollLatest();
    await settle();
    return {{ cards: page.nodes.get("opportunities").textContent, counts: page.nodes.get("scan-counts").textContent, source: page.nodes.get("summary-source").textContent, status: page.nodes.get("status").textContent, cachedSweep: JSON.parse(storage.getItem(cacheKey)).summary.sweep_id }};
  }};
  const afterReject = await check(2, "reject");
  const statusBeforeFilter = page.nodes.get("status").textContent;
  const sourceBeforeFilter = page.nodes.get("summary-source").textContent;
  page.nodes.get("minimum-profit").value = "1000";
  page.nodes.get("minimum-profit").listeners.change();
  const afterFilter = {{ cards: page.nodes.get("opportunities").textContent, source: page.nodes.get("summary-source").textContent, status: page.nodes.get("status").textContent }};
  const afterInvalid = await check(3, "invalid");
  global.failElementTag = "article";
  const afterRenderError = await check(4, "render");
  global.failElementTag = null;
  const afterEmpty = await check(5, "empty");
  console.log(JSON.stringify({{ initialCards, afterReject, afterFilter, statusBeforeFilter, sourceBeforeFilter, afterInvalid, afterRenderError, afterEmpty }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    )
    for result in (actual["afterReject"], actual["afterInvalid"], actual["afterRenderError"]):
        assert "Last successful recommendation" in result["cards"]
        assert "Planned 484Queried 484Successful 483" not in result["counts"]
        assert "sweep #1" in result["source"].lower()
        assert result["cachedSweep"] == 1
    assert "offline" in actual["afterReject"]["source"]
    assert "Last successful recommendation" in actual["afterFilter"]["cards"]
    assert actual["afterFilter"]["source"] == actual["sourceBeforeFilter"]
    assert actual["afterFilter"]["status"] == actual["statusBeforeFilter"]
    assert "profile" in actual["afterInvalid"]["source"]
    assert "render failed" in actual["afterRenderError"]["source"]
    assert "No strategies meet this minimum net profit per item" in actual["afterEmpty"]["cards"]
    assert "Last successful recommendation" not in actual["afterEmpty"]["cards"]
    assert actual["afterEmpty"]["cachedSweep"] == 5


def test_summary_timeout_retries_and_late_response_cannot_clear_new_request():
    latest1 = {
        "house": "us-normal-alliance",
        "sweep_id": 1,
        "finished_epoch": 1_800_000_000,
        "client_build": "1.60.1.70938",
    }
    latest2 = {**latest1, "sweep_id": 2, "finished_epoch": 1_800_000_060}
    latest3 = {**latest1, "sweep_id": 3, "finished_epoch": 1_800_000_120}
    first = _summary(1, 1_800_000_000)
    first["opportunities"][0]["name"] = "Retained recommendation"
    stale = _summary(2, 1_800_000_060)
    stale["opportunities"][0]["name"] = "Late superseded result"
    current = _summary(3, 1_800_000_120)
    current["opportunities"][0]["name"] = "Current retry result"
    watch = _recipe_watch(status="awaiting_scan")
    runtime = _dashboard_runtime()
    actual = _node(
        f"""
let latestReply = {json.dumps(latest1)};
let summaryCalls = 0;
const pending = [];
const timers = new Map();
let nextTimer = 0;
global.setTimeout = (callback, ms) => {{ const id = ++nextTimer; timers.set(id, {{ callback, ms }}); return id; }};
global.clearTimeout = (id) => timers.delete(id);
function reply(value, ok = true, status = 200) {{ return {{ ok, status, json: () => Promise.resolve(value) }}; }}
global.fetch = (path, options) => {{
  if (path.endsWith("/sweeps/latest")) return Promise.resolve(reply(latestReply));
  if (path.endsWith("/recipe-watch")) return Promise.resolve(reply({json.dumps(watch)}));
  summaryCalls += 1;
  if (summaryCalls === 1) return Promise.resolve(reply({json.dumps(first)}));
  return new Promise((resolve) => pending.push({{ resolve: (value) => resolve(reply(value)), signal: options.signal }}));
}};
const settle = async () => {{ for (let i = 0; i < 12; i += 1) await Promise.resolve(); await new Promise(setImmediate); }};
{runtime}
(async () => {{
  const page = mountDashboard(null);
  await settle();
  latestReply = {json.dumps(latest2)};
  await page.dashboard.pollLatest();
  await settle();
  const beforeTimeout = {{ cards: page.nodes.get("opportunities").textContent, inFlight: page.dashboard.state.summaryInFlight, timeoutMs: [...timers.values()].find((timer) => timer.ms === 60000)?.ms, signalPassed: Boolean(pending[0]?.signal) }};
  const timeoutTimer = [...timers.entries()].find(([, timer]) => timer.ms === 60000);
  timers.delete(timeoutTimer[0]);
  timeoutTimer[1].callback();
  await settle();
  const afterTimeout = {{ cards: page.nodes.get("opportunities").textContent, inFlight: page.dashboard.state.summaryInFlight, request: page.dashboard.state.summaryRequest, status: page.nodes.get("status").textContent, signalAborted: pending[0].signal.aborted }};
  await page.dashboard.pollLatest();
  await settle();
  const retryStarted = {{ pending: pending.length, inFlight: page.dashboard.state.summaryInFlight, requestSweep: page.dashboard.state.summaryRequest?.sweepId }};
  latestReply = {json.dumps(latest3)};
  await page.dashboard.pollLatest();
  await settle();
  const newerStarted = {{ inFlight: page.dashboard.state.summaryInFlight, requestSweep: page.dashboard.state.summaryRequest?.sweepId, pending: pending.length }};
  pending[1].resolve({json.dumps(stale)});
  await settle();
  const afterLateResponse = {{ inFlight: page.dashboard.state.summaryInFlight, requestSweep: page.dashboard.state.summaryRequest?.sweepId, cards: page.nodes.get("opportunities").textContent }};
  pending[2].resolve({json.dumps(current)});
  await settle();
  const afterCurrent = {{ inFlight: page.dashboard.state.summaryInFlight, cards: page.nodes.get("opportunities").textContent, counts: page.nodes.get("scan-counts").textContent }};
  console.log(JSON.stringify({{ beforeTimeout, afterTimeout, retryStarted, newerStarted, afterLateResponse, afterCurrent }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    )
    assert actual["beforeTimeout"]["inFlight"] is True
    assert actual["beforeTimeout"]["timeoutMs"] == 60000
    assert actual["beforeTimeout"]["signalPassed"] is True
    assert actual["afterTimeout"]["inFlight"] is False
    assert actual["afterTimeout"]["request"] is None
    assert actual["afterTimeout"]["signalAborted"] is True
    assert "timed out after 60 seconds" in actual["afterTimeout"]["status"]
    assert "Retained recommendation" in actual["afterTimeout"]["cards"]
    assert actual["retryStarted"] == {"pending": 2, "inFlight": True, "requestSweep": 2}
    assert actual["newerStarted"] == {"inFlight": True, "requestSweep": 3, "pending": 3}
    assert actual["afterLateResponse"]["inFlight"] is True
    assert actual["afterLateResponse"]["requestSweep"] == 3
    assert "Retained recommendation" in actual["afterLateResponse"]["cards"]
    assert actual["afterCurrent"]["inFlight"] is False
    assert "Current retry result" in actual["afterCurrent"]["cards"]
    assert "Late superseded result" not in actual["afterCurrent"]["cards"]
    assert "Planned 484Queried 484Successful 483" in actual["afterCurrent"]["counts"]


def test_hung_latest_scan_request_times_out_and_next_poll_retries():
    latest = {
        "house": "us-normal-alliance",
        "sweep_id": 9,
        "finished_epoch": 1_800_000_540,
        "client_build": "1.60.1.70938",
    }
    summary = _summary(9, 1_800_000_540)
    summary["opportunities"][0]["name"] = "Latest retry recommendation"
    watch = _recipe_watch(status="awaiting_scan")
    runtime = _dashboard_runtime()
    actual = _node(
        f"""
let latestCalls = 0;
const timers = new Map();
let nextTimer = 0;
global.setTimeout = (callback, ms) => {{ const id = ++nextTimer; timers.set(id, {{ callback, ms }}); return id; }};
global.clearTimeout = (id) => timers.delete(id);
function reply(value, ok = true, status = 200) {{ return {{ ok, status, json: () => Promise.resolve(value) }}; }}
global.fetch = (path) => {{
  if (path.endsWith("/sweeps/latest")) {{
    latestCalls += 1;
    return latestCalls === 1 ? new Promise(() => {{}}) : Promise.resolve(reply({json.dumps(latest)}));
  }}
  if (path.endsWith("/recipe-watch")) return Promise.resolve(reply({json.dumps(watch)}));
  return Promise.resolve(reply({json.dumps(summary)}));
}};
const settle = async () => {{ for (let i = 0; i < 12; i += 1) await Promise.resolve(); await new Promise(setImmediate); }};
{runtime}
(async () => {{
  const page = mountDashboard(null);
  await settle();
  const beforeTimeout = {{ latestInFlight: page.dashboard.state.latestInFlight, summaryCalls: latestCalls }};
  const timeoutTimer = [...timers.entries()].find(([, timer]) => timer.ms === 30000);
  timers.delete(timeoutTimer[0]);
  timeoutTimer[1].callback();
  await settle();
  const afterTimeout = {{ latestInFlight: page.dashboard.state.latestInFlight, error: page.nodes.get("scan-age").textContent, cards: page.nodes.get("opportunities").textContent }};
  await page.dashboard.pollLatest();
  await settle();
  const afterRetry = {{ latestInFlight: page.dashboard.state.latestInFlight, latestCalls, summarySweep: page.dashboard.state.summary?.sweep_id, cards: page.nodes.get("opportunities").textContent, counts: page.nodes.get("scan-counts").textContent, source: page.nodes.get("summary-source").textContent }};
  console.log(JSON.stringify({{ beforeTimeout, afterTimeout, afterRetry }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    )
    assert actual["beforeTimeout"] == {"latestInFlight": True, "summaryCalls": 1}
    assert actual["afterTimeout"]["latestInFlight"] is False
    assert "timed out after 30 seconds" in actual["afterTimeout"]["error"]
    assert actual["afterRetry"]["latestInFlight"] is False
    assert actual["afterRetry"]["latestCalls"] == 2
    assert actual["afterRetry"]["summarySweep"] == 9
    assert "Latest retry recommendation" in actual["afterRetry"]["cards"]
    assert "Planned 484Queried 484Successful 483" in actual["afterRetry"]["counts"]


def test_corrupt_profile_mismatched_and_blocked_storage_do_not_block_live_rendering():
    latest = {
        "house": "us-normal-alliance",
        "sweep_id": 11,
        "finished_epoch": 1_800_000_660,
        "client_build": "1.60.1.70938",
    }
    summary = _summary(11, 1_800_000_660)
    summary["opportunities"][0]["name"] = "Live result after bad cache"
    malformed = {**summary, "notes": {"not": "an array"}}
    wrong_profile = {
        "version": 1,
        "house": "us-normal-alliance",
        "client_build": "1.60.1.70938",
        "profile_key": "different-profile",
        "summary": summary,
    }
    watch = _recipe_watch(status="awaiting_scan")
    runtime = _dashboard_runtime()
    actual = _node(
        f"""
const key = "crowley.trading-summary.v1.us-normal-alliance.1.60.1.70938.alchemy225-tailoring150-cap225-budget100000-owned";
const malformedStorage = {{ getItem: (requested) => requested === key ? JSON.stringify({json.dumps({**wrong_profile, "summary": malformed, "profile_key": "alchemy225-tailoring150-cap225-budget100000-owned"})}) : null, setItem: () => {{}} }};
const mismatchStorage = {{ getItem: (requested) => requested === key ? JSON.stringify({json.dumps(wrong_profile)}) : null, setItem: () => {{}} }};
const blockedStorage = new Proxy({{}}, {{ get: () => {{ throw new Error("storage blocked"); }} }});
function reply(value, ok = true, status = 200) {{ return {{ ok, status, json: () => Promise.resolve(value) }}; }}
global.fetch = (path) => {{
  if (path.endsWith("/sweeps/latest")) return Promise.resolve(reply({json.dumps(latest)}));
  if (path.endsWith("/recipe-watch")) return Promise.resolve(reply({json.dumps(watch)}));
  return Promise.resolve(reply({json.dumps(summary)}));
}};
const settle = async () => {{ for (let i = 0; i < 12; i += 1) await Promise.resolve(); await new Promise(setImmediate); }};
{runtime}
(async () => {{
  const results = [];
  for (const storage of [malformedStorage, mismatchStorage, blockedStorage]) {{
    const page = mountDashboard(storage);
    await settle();
    results.push({{ cards: page.nodes.get("opportunities").textContent, source: page.nodes.get("summary-source").textContent, summarySweep: page.dashboard.state.summary?.sweep_id }});
  }}
  console.log(JSON.stringify({{ results }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    )
    assert len(actual["results"]) == 3
    for result in actual["results"]:
        assert "Live result after bad cache" in result["cards"]
        assert "Wrong cached" not in result["cards"]
        assert result["summarySweep"] == 11
        assert "sweep #11" in result["source"].lower()


def test_house_build_and_profile_cache_isolation_and_old_house_response_race():
    default_latest = {
        "house": "us-normal-alliance",
        "sweep_id": 1,
        "finished_epoch": 1_800_000_000,
        "client_build": "1.60.1.70938",
    }
    other_latest = {
        "house": "other-house",
        "sweep_id": 7,
        "finished_epoch": 1_800_000_360,
        "client_build": "1.60.1.70938",
    }
    new_build_latest = {
        **other_latest,
        "sweep_id": 8,
        "finished_epoch": 1_800_000_420,
        "client_build": "1.60.2.70938",
    }
    default_summary = _summary(1, 1_800_000_000)
    default_summary["opportunities"][0]["name"] = "Default house cache"
    other_summary = _summary(7, 1_800_000_360)
    other_summary["house"] = "other-house"
    other_summary["opportunities"][0]["name"] = "Other house cache"
    wrong_profile_summary = _summary(8, 1_800_000_420)
    wrong_profile_summary["house"] = "other-house"
    wrong_profile_summary["client_build"] = "1.60.2.70938"
    wrong_profile_summary["opportunities"][0]["name"] = "Wrong profile build cache"
    new_summary = _summary(8, 1_800_000_420)
    new_summary["house"] = "other-house"
    new_summary["client_build"] = "1.60.2.70938"
    new_summary["opportunities"][0]["name"] = "New build live result"
    default_watch = _recipe_watch(status="awaiting_scan")
    other_watch = _recipe_watch("other-house", status="awaiting_scan")
    runtime = _dashboard_runtime()
    actual = _node(
        f"""
const values = new Map();
const profileKey = "alchemy225-tailoring150-cap225-budget100000-owned";
const cacheKey = (house, build) => `crowley.trading-summary.v1.${{encodeURIComponent(house)}}.${{encodeURIComponent(build)}}.${{profileKey}}`;
const cacheEntry = (summary, override = {{}}) => JSON.stringify({{ version: 1, house: summary.house, client_build: summary.client_build, profile_key: profileKey, summary, ...override }});
values.set(cacheKey("us-normal-alliance", "1.60.1.70938"), cacheEntry({json.dumps(default_summary)}));
values.set(cacheKey("other-house", "1.60.1.70938"), cacheEntry({json.dumps(other_summary)}));
values.set(cacheKey("other-house", "1.60.2.70938"), cacheEntry({json.dumps(wrong_profile_summary)}, {{ profile_key: "wrong-profile" }}));
const storage = {{ getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }};
const latestResolvers = {{}};
let latestMode = "deferred";
let releaseNewBuild;
function reply(value, ok = true, status = 200) {{ return {{ ok, status, json: () => Promise.resolve(value) }}; }}
global.fetch = (path) => {{
  if (path.endsWith("/sweeps/latest")) {{
    const house = path.includes("other-house") ? "other-house" : "us-normal-alliance";
    if (house === "us-normal-alliance" || latestMode === "deferred") return new Promise((resolve) => {{ latestResolvers[house] = () => resolve(reply(house === "other-house" ? {json.dumps(other_latest)} : {json.dumps(default_latest)})); }});
    return Promise.resolve(reply({json.dumps(new_build_latest)}));
  }}
  if (path.endsWith("/recipe-watch")) return Promise.resolve(reply(path.includes("other-house") ? {json.dumps(other_watch)} : {json.dumps(default_watch)}));
  return new Promise((resolve) => {{ releaseNewBuild = () => resolve(reply({json.dumps(new_summary)})); }});
}};
const settle = async () => {{ for (let i = 0; i < 12; i += 1) await Promise.resolve(); await new Promise(setImmediate); }};
{runtime}
(async () => {{
  const page = mountDashboard(storage);
  await settle();
  const defaultBeforeNetwork = page.nodes.get("opportunities").textContent;
  page.dashboard.changeHouse("other-house");
  await settle();
  const afterHouseSwitch = {{ house: page.dashboard.state.house, cards: page.nodes.get("opportunities").textContent, source: page.nodes.get("summary-source").textContent }};
  latestResolvers["us-normal-alliance"]();
  await settle();
  const afterOldHouseResponse = {{ house: page.dashboard.state.house, summaryHouse: page.dashboard.state.summary?.house, cards: page.nodes.get("opportunities").textContent }};
  latestResolvers["other-house"]();
  await settle();
  latestMode = "build2";
  await page.dashboard.pollLatest();
  await settle();
  const whileNewBuildLoads = {{ cards: page.nodes.get("opportunities").textContent, inFlight: page.dashboard.state.summaryInFlight, requestBuild: page.dashboard.state.summaryRequest?.clientBuild }};
  releaseNewBuild();
  await settle();
  const afterNewBuild = {{ summaryBuild: page.dashboard.state.summary?.client_build, cards: page.nodes.get("opportunities").textContent, counts: page.nodes.get("scan-counts").textContent }};
  console.log(JSON.stringify({{ defaultBeforeNetwork, afterHouseSwitch, afterOldHouseResponse, whileNewBuildLoads, afterNewBuild }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    )
    assert "Default house cache" in actual["defaultBeforeNetwork"]
    assert actual["afterHouseSwitch"]["house"] == "other-house"
    assert "Other house cache" in actual["afterHouseSwitch"]["cards"]
    assert "Default house cache" not in actual["afterHouseSwitch"]["cards"]
    assert "other-house" in actual["afterHouseSwitch"]["source"]
    assert actual["afterOldHouseResponse"]["house"] == "other-house"
    assert actual["afterOldHouseResponse"]["summaryHouse"] == "other-house"
    assert "Default house cache" not in actual["afterOldHouseResponse"]["cards"]
    assert actual["whileNewBuildLoads"]["inFlight"] is True
    assert actual["whileNewBuildLoads"]["requestBuild"] == "1.60.2.70938"
    assert "Other house cache" not in actual["whileNewBuildLoads"]["cards"]
    assert "Wrong profile build cache" not in actual["whileNewBuildLoads"]["cards"]
    assert actual["afterNewBuild"]["summaryBuild"] == "1.60.2.70938"
    assert "New build live result" in actual["afterNewBuild"]["cards"]


def test_recipe_watch_house_race_and_failure_are_isolated_from_market_summary():
    latest_default = {
        "house": "us-normal-alliance",
        "sweep_id": 1,
        "finished_epoch": 1_800_000_000,
        "client_build": "1.60.1.70938",
    }
    latest_other = {**latest_default, "house": "other-house"}
    summary_default = _summary(1, 1_800_000_000, count=0)
    summary_other = {**summary_default, "house": "other-house"}
    default_watch = _recipe_watch()
    other_watch = _recipe_watch("other-house", status="building_history")
    not_listed_watch = _recipe_watch("other-house", status="not_listed")
    failed_watch = _recipe_watch("other-house", status="query_failed")
    stale_watch = _recipe_watch("other-house", status="stale")
    actual = _node(
        f"""
let delayedWatch;
let failWatch = false;
let watchPayload = {json.dumps(other_watch)};
function reply(value, ok = true, status = 200) {{ return {{ ok, status, json: () => Promise.resolve(value) }}; }}
global.fetch = (path) => {{
  if (path.endsWith("/sweeps/latest")) return Promise.resolve(reply(path.includes("other-house") ? {json.dumps(latest_other)} : {json.dumps(latest_default)}));
  if (path.endsWith("/trading-summary")) return Promise.resolve(reply(path.includes("other-house") ? {json.dumps(summary_other)} : {json.dumps(summary_default)}));
  if (path.endsWith("/recipe-watch")) {{
    if (failWatch) return Promise.resolve(reply({{}}, false, 503));
    if (path.includes("us-normal-alliance")) return new Promise((resolve) => {{ delayedWatch = () => resolve(reply({json.dumps(default_watch)})); }});
    return Promise.resolve(reply(watchPayload));
  }}
  return Promise.resolve(reply({{}}, false, 404));
}};
class FakeNode {{
  constructor(tag = "div", text = "") {{ this.children = []; this._text = String(text); this.value = ""; this.className = ""; this.dateTime = ""; this.listeners = {{}}; }}
  set textContent(value) {{ this._text = String(value); this.children = []; }}
  get textContent() {{ return this._text + this.children.map((child) => child.textContent).join(""); }}
  appendChild(child) {{ this.children.push(child); return child; }}
  replaceChildren(...children) {{ this._text = ""; this.children = [...children]; }}
  addEventListener(name, handler) {{ this.listeners[name] = handler; }}
}}
const ids = ["scan-time", "scan-age", "scan-counts", "status", "summary-source", "opportunities", "notes", "house-form", "house-key", "minimum-profit", "recipe-watch-state", "recipe-watch-alert", "recipe-watch-content", "recipe-watch-notes"];
const nodes = new Map(ids.map((id) => [id, new FakeNode()]));
global.document = {{ getElementById: (id) => nodes.get(id), createElement: (tag) => new FakeNode(tag), createTextNode: (text) => new FakeNode("#text", text) }};
global.window = {{ setInterval: () => 1 }};
const settle = async () => {{ for (let i = 0; i < 12; i += 1) await Promise.resolve(); await new Promise(setImmediate); }};
{_script()}
(async () => {{
  await settle();
  const dashboard = window.__crowleyTradingDashboardTest;
  dashboard.changeHouse("other-house");
  await settle();
  const afterHouseChange = {{ house: dashboard.state.watch?.house, alert: nodes.get("recipe-watch-alert").textContent }};
  delayedWatch();
  await settle();
  const afterOldResponse = dashboard.state.watch?.house;
  watchPayload = {json.dumps(not_listed_watch)};
  await dashboard.pollRecipeWatch();
  const notListed = {{ state: nodes.get("recipe-watch-state").textContent, alert: nodes.get("recipe-watch-alert").textContent }};
  watchPayload = {json.dumps(failed_watch)};
  await dashboard.pollRecipeWatch();
  const queryFailed = nodes.get("recipe-watch-state").textContent;
  watchPayload = {json.dumps(stale_watch)};
  await dashboard.pollRecipeWatch();
  const stale = {{ state: nodes.get("recipe-watch-state").textContent, alert: nodes.get("recipe-watch-alert").textContent }};
  const marketStatus = nodes.get("status").textContent;
  const marketCards = nodes.get("opportunities").textContent;
  failWatch = true;
  await dashboard.pollRecipeWatch();
  await settle();
  console.log(JSON.stringify({{ afterHouseChange, afterOldResponse, notListed, queryFailed, stale, watchError: nodes.get("recipe-watch-state").textContent, alert: nodes.get("recipe-watch-alert").textContent, marketStatus, finalMarketStatus: nodes.get("status").textContent, marketCards, finalMarketCards: nodes.get("opportunities").textContent }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    )
    assert actual["afterHouseChange"]["house"] == "other-house"
    assert "need 3 more" in actual["afterHouseChange"]["alert"]
    assert actual["afterOldResponse"] == "other-house"
    assert "query succeeded and found no listing" in actual["notListed"]["state"]
    assert "No listing found" in actual["notListed"]["alert"]
    assert "latest recipe query failed" in actual["queryFailed"]
    assert "over 30 minutes old" in actual["stale"]["state"]
    assert "Alert suppressed" in actual["stale"]["alert"]
    assert "Could not load the independent watch: Request failed (503)" in actual["watchError"]
    assert "unavailable" in actual["alert"]
    assert actual["finalMarketStatus"] == actual["marketStatus"]
    assert actual["finalMarketCards"] == actual["marketCards"]


def test_cached_low_price_alert_expires_while_next_watch_request_is_pending():
    latest = {
        "house": "us-normal-alliance",
        "sweep_id": 1,
        "finished_epoch": 1_800_000_000,
        "client_build": "1.60.1.70938",
    }
    summary = _summary(1, 1_800_000_000, count=0)
    watch = _recipe_watch()
    actual = _node(
        f"""
let nowMs = 1_800_000_000_000;
Date.now = () => nowMs;
let watchCalls = 0;
let releaseRefresh;
function reply(value) {{ return {{ ok: true, status: 200, json: () => Promise.resolve(value) }}; }}
global.fetch = (path) => {{
  if (path.endsWith("/sweeps/latest")) return Promise.resolve(reply({json.dumps(latest)}));
  if (path.endsWith("/trading-summary")) return Promise.resolve(reply({json.dumps(summary)}));
  if (path.endsWith("/recipe-watch")) {{
    watchCalls += 1;
    if (watchCalls > 1) return new Promise((resolve) => {{ releaseRefresh = () => resolve(reply({json.dumps(watch)})); }});
    return Promise.resolve(reply({json.dumps(watch)}));
  }}
  throw new Error(`unexpected request: ${{path}}`);
}};
class FakeNode {{
  constructor(tag = "div", text = "") {{ this.children = []; this._text = String(text); this.value = ""; this.className = ""; this.dateTime = ""; this.listeners = {{}}; }}
  set textContent(value) {{ this._text = String(value); this.children = []; }}
  get textContent() {{ return this._text + this.children.map((child) => child.textContent).join(""); }}
  appendChild(child) {{ this.children.push(child); return child; }}
  replaceChildren(...children) {{ this._text = ""; this.children = [...children]; }}
  addEventListener(name, handler) {{ this.listeners[name] = handler; }}
}}
const ids = ["scan-time", "scan-age", "scan-counts", "status", "summary-source", "opportunities", "notes", "house-form", "house-key", "minimum-profit", "recipe-watch-state", "recipe-watch-alert", "recipe-watch-content", "recipe-watch-notes"];
const nodes = new Map(ids.map((id) => [id, new FakeNode()]));
global.document = {{ getElementById: (id) => nodes.get(id), createElement: (tag) => new FakeNode(tag), createTextNode: (text) => new FakeNode("#text", text) }};
let intervalCallback;
global.window = {{ setInterval: (callback) => {{ intervalCallback = callback; return 1; }} }};
const settle = async () => {{ for (let i = 0; i < 12; i += 1) await Promise.resolve(); await new Promise(setImmediate); }};
{_script()}
(async () => {{
  await settle();
  const dashboard = window.__crowleyTradingDashboardTest;
  const initial = {{ alert: nodes.get("recipe-watch-alert").textContent, className: nodes.get("recipe-watch-alert").className }};
  void dashboard.pollRecipeWatch();
  await settle();
  const refreshPending = dashboard.state.watchInFlight;
  nowMs = 1_800_001_801_000;
  intervalCallback();
  await settle();
  const expiredDuringPending = {{ state: nodes.get("recipe-watch-state").textContent, alert: nodes.get("recipe-watch-alert").textContent, className: nodes.get("recipe-watch-alert").className, cachedStatus: dashboard.state.watch.recipe.status, cachedAlert: dashboard.state.watch.recipe.alert }};
  releaseRefresh();
  await settle();
  const afterLateResponse = {{ alert: nodes.get("recipe-watch-alert").textContent, className: nodes.get("recipe-watch-alert").className, cachedStatus: dashboard.state.watch.recipe.status, cachedAlert: dashboard.state.watch.recipe.alert }};
  console.log(JSON.stringify({{ initial, refreshPending, expiredDuringPending, afterLateResponse }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    )
    assert "LOW PRICE" in actual["initial"]["alert"]
    assert actual["initial"]["className"] == "watch-alert low"
    assert actual["refreshPending"] is True
    assert "over 30 minutes old" in actual["expiredDuringPending"]["state"]
    assert "Alert suppressed" in actual["expiredDuringPending"]["alert"]
    assert actual["expiredDuringPending"]["className"] == "watch-alert"
    assert actual["expiredDuringPending"]["cachedStatus"] == "stale"
    assert actual["expiredDuringPending"]["cachedAlert"] is False
    assert actual["afterLateResponse"]["className"] == "watch-alert"
    assert actual["afterLateResponse"]["cachedStatus"] == "stale"
    assert actual["afterLateResponse"]["cachedAlert"] is False


def test_page_has_no_token_prompt_session_storage_or_external_runtime_assets():
    html = DASHBOARD.read_text(encoding="utf-8")
    assert "/dashboard/advanced" not in html
    assert "localStorage" in html
    assert re.findall(r'<option value="(\d+)">', html) == ["50", "100", "300", "500", "1000"]
    assert "Net profit is shown per item" in html
    assert not re.search(
        r"(?:sessionStorage|\.prompt\s*\(|Authorization\s*:|api[_-]?token)", html, re.IGNORECASE
    )
    assert not re.search(r"<script\s+src=|<link[^>]+href=[\"']https?://", html, re.IGNORECASE)
    assert "innerHTML" not in html


def test_profit_per_item_thresholds_filter_full_pool_exactly_and_remember_preference():
    candidates = [
        _ranked_candidate(1, "250", 5, crafts=2),  # 50c each, across five output units.
        _ranked_candidate(2, "101", 2),  # 50.5c each.
        _ranked_candidate(3, "49999/1000", 1),  # Just below 50c.
        _ranked_candidate(4, "100", 2),  # Exact 50c after multi-output craft.
        _ranked_candidate(5, "10001/100", 1),  # Just above 1s.
        _ranked_candidate(6, "299", 1),
        _ranked_candidate(7, "300", 1),  # Exact 3s.
        _ranked_candidate(8, "499", 1),
        _ranked_candidate(9, "500", 1),  # Exact 5s.
        _ranked_candidate(10, "999", 1),
        _ranked_candidate(11, "1000", 1),  # Exact 10s.
        _ranked_candidate(12, "1001", 1),
        _ranked_candidate(13, "1500", 1),
        _ranked_candidate(14, "3000", 1),
        _ranked_candidate(15, "5000", 1),
    ]
    latest = {
        "house": "us-normal-alliance",
        "sweep_id": 21,
        "finished_epoch": 1_800_001_260,
        "client_build": "1.60.1.70938",
    }
    summary = _summary(21, 1_800_001_260, count=0)
    summary["candidates"] = candidates
    summary["opportunities"] = candidates[:10]
    watch = _recipe_watch(status="awaiting_scan")
    runtime = _dashboard_runtime()
    actual = _node(
        f"""
const calls = [];
const values = new Map();
const storage = {{ getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }};
function reply(value) {{ return {{ ok: true, status: 200, json: () => Promise.resolve(value) }}; }}
global.fetch = (path) => {{
  calls.push(path);
  if (path.endsWith("/sweeps/latest")) return Promise.resolve(reply({json.dumps(latest)}));
  if (path.endsWith("/recipe-watch")) return Promise.resolve(reply({json.dumps(watch)}));
  return Promise.resolve(reply({json.dumps(summary)}));
}};
const settle = async () => {{ for (let i = 0; i < 12; i += 1) await Promise.resolve(); await new Promise(setImmediate); }};
{runtime}
(async () => {{
  const page = mountDashboard(storage);
  await settle();
  const select = page.nodes.get("minimum-profit");
  const defaultCopper = page.dashboard.state.minimumProfitCopper;
  const defaultValue = select.value;
  const thresholds = [50, 100, 300, 500, 1000];
  const snapshots = [];
  for (const threshold of thresholds) {{
    select.value = String(threshold);
    select.listeners.change();
    const cards = page.nodes.get("opportunities").children.map((node) => node.textContent);
    snapshots.push({{ threshold, count: cards.length, first: cards[0] ?? "", last: cards.at(-1) ?? "", status: page.nodes.get("status").textContent, ranks: cards.map((card, index) => card.includes("#" + (index + 1) + "Rank ")) }});
  }}
  const callsAfterFiltering = calls.length;
  const preference = values.get("crowley.dashboard.minimum-profit-per-item.v1");
  const reloaded = mountDashboard(storage);
  await settle();
  const afterReload = {{ selected: reloaded.nodes.get("minimum-profit").value, count: reloaded.nodes.get("opportunities").children.length, first: reloaded.nodes.get("opportunities").children[0]?.textContent ?? "", summaryCalls: calls.filter((path) => path.endsWith("/trading-summary")).length }};
  console.log(JSON.stringify({{ defaultCopper, defaultValue, snapshots, callsAfterFiltering, preference, afterReload, callsAfterReload: calls.length }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    )
    assert actual["defaultCopper"] == 50
    assert actual["defaultValue"] == "50"
    snapshots = actual["snapshots"]
    assert [row["count"] for row in snapshots] == [10, 10, 9, 7, 5]
    assert "Rank 01" in snapshots[0]["first"] and "Rank 11" in snapshots[0]["last"]
    assert "Rank 05" in snapshots[1]["first"] and "Rank 14" in snapshots[1]["last"]
    assert "Rank 07" in snapshots[2]["first"] and "Rank 15" in snapshots[2]["last"]
    assert "Rank 09" in snapshots[3]["first"] and "Rank 15" in snapshots[3]["last"]
    assert "Rank 11" in snapshots[4]["first"] and "Rank 15" in snapshots[4]["last"]
    assert "Showing 10 of up to 10" in snapshots[0]["status"]
    assert "Showing 9 of up to 10" in snapshots[2]["status"]
    assert "Showing 5 of up to 10" in snapshots[4]["status"]
    assert all(all(row["ranks"]) for row in snapshots)
    assert actual["callsAfterFiltering"] == 3
    assert actual["preference"] == "1000"
    assert actual["afterReload"]["selected"] == "1000"
    assert actual["afterReload"]["count"] == 5
    assert "Rank 11" in actual["afterReload"]["first"]
    assert actual["afterReload"]["summaryCalls"] == 1
    assert actual["callsAfterReload"] == actual["callsAfterFiltering"] + 2


def test_fresh_summary_validates_economics_after_the_legacy_top_ten():
    latest = {
        "house": "us-normal-alliance",
        "sweep_id": 25,
        "finished_epoch": 1_800_001_500,
        "client_build": "1.60.1.70938",
    }
    summary = _summary(25, 1_800_001_500, count=13)
    assert len(summary["candidates"]) == 11
    assert len(summary["opportunities"]) == 10
    summary["candidates"][10]["profit_per_item"] = "1"
    actual = _node(
        f"""
{_pure_script()}
let error = "";
try {{ validateSummary({json.dumps(summary)}, {json.dumps(latest)}); }}
catch (exception) {{ error = exception instanceof Error ? exception.message : String(exception); }}
console.log(JSON.stringify({{ error }}));
"""
    )
    assert "profit_per_item does not equal batch profit divided by output quantity" in actual["error"]


def test_old_top_ten_cache_stays_limited_until_full_pool_refresh_then_upgrades():
    latest = {
        "house": "us-normal-alliance",
        "sweep_id": 31,
        "finished_epoch": 1_800_001_860,
        "client_build": "1.60.1.70938",
    }
    legacy_summary = _summary(31, 1_800_001_860)
    legacy_summary["opportunities"][0]["name"] = "Legacy saved row"
    legacy_summary.pop("candidates")
    for item in legacy_summary["opportunities"]:
        item.update(quantity=1, crafts=1, cost="100", sale_proceeds="200", profit="100")
        item.pop("profit_per_item")
    fresh_summary = _summary(31, 1_800_001_860)
    fresh_summary["candidates"][0]["name"] = "Fresh full-pool row"
    latest_watch = _recipe_watch(status="awaiting_scan")
    runtime = _dashboard_runtime()
    actual = _node(
        f"""
const profile = "alchemy225-tailoring150-cap225-budget100000-owned";
const legacyKey = "crowley.trading-summary.v1.us-normal-alliance.1.60.1.70938." + profile;
const currentKey = "crowley.trading-summary.v2.us-normal-alliance.1.60.1.70938." + profile;
const values = new Map([[legacyKey, JSON.stringify({{ version: 1, house: "us-normal-alliance", client_build: "1.60.1.70938", profile_key: profile, summary: {json.dumps(legacy_summary)} }})]]);
const storage = {{ getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }};
let summaryCalls = 0;
let releaseSummary;
function reply(value) {{ return {{ ok: true, status: 200, json: () => Promise.resolve(value) }}; }}
global.fetch = (path) => {{
  if (path.endsWith("/sweeps/latest")) return Promise.resolve(reply({json.dumps(latest)}));
  if (path.endsWith("/recipe-watch")) return Promise.resolve(reply({json.dumps(latest_watch)}));
  summaryCalls += 1;
  return new Promise((resolve) => {{ releaseSummary = () => resolve(reply({json.dumps(fresh_summary)})); }});
}};
const settle = async () => {{ for (let i = 0; i < 12; i += 1) await Promise.resolve(); await new Promise(setImmediate); }};
{runtime}
(async () => {{
  const page = mountDashboard(storage);
  await settle();
  const beforeRefresh = {{ cards: page.nodes.get("opportunities").textContent, source: page.nodes.get("summary-source").textContent, status: page.nodes.get("status").textContent, inFlight: page.dashboard.state.summaryInFlight, summaryCalls }};
  page.nodes.get("minimum-profit").value = "1000";
  page.nodes.get("minimum-profit").listeners.change();
  const limitedNoMatch = {{ cards: page.nodes.get("opportunities").textContent, source: page.nodes.get("summary-source").textContent, status: page.nodes.get("status").textContent }};
  releaseSummary();
  await settle();
  const upgraded = {{ cards: page.nodes.get("opportunities").textContent, source: page.nodes.get("summary-source").textContent, summaryCalls, cache: JSON.parse(values.get(currentKey)) }};
  const reload = mountDashboard(storage);
  await settle();
  const afterReload = {{ cards: reload.nodes.get("opportunities").textContent, source: reload.nodes.get("summary-source").textContent, summaryCalls }};
  console.log(JSON.stringify({{ beforeRefresh, limitedNoMatch, upgraded, afterReload }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    )
    assert "Legacy saved row" in actual["beforeRefresh"]["cards"]
    assert actual["beforeRefresh"]["inFlight"] is True
    assert actual["beforeRefresh"]["summaryCalls"] == 1
    assert "limited saved top ten" in actual["beforeRefresh"]["source"]
    assert "No saved top-ten strategies meet this minimum" in actual["limitedNoMatch"]["cards"]
    assert "Lower-ranked results are unavailable" in actual["limitedNoMatch"]["cards"]
    assert actual["limitedNoMatch"]["status"] == actual["beforeRefresh"]["status"]
    assert "Fresh full-pool row" in actual["upgraded"]["cards"]
    assert "limited saved top ten" not in actual["upgraded"]["source"]
    assert actual["upgraded"]["cache"]["version"] == 2
    assert len(actual["upgraded"]["cache"]["summary"]["candidates"]) == 10
    assert "Fresh full-pool row" in actual["afterReload"]["cards"]
    assert actual["afterReload"]["summaryCalls"] == 1


def test_v1_bridge_cache_with_full_candidates_is_restored_and_malformed_pool_is_rejected():
    latest = {
        "house": "us-normal-alliance",
        "sweep_id": 41,
        "finished_epoch": 1_800_002_460,
        "client_build": "1.60.1.70938",
    }
    bridge_summary = _summary(41, 1_800_002_460)
    bridge_summary["candidates"][0]["name"] = "Bridge full-pool row"
    malformed_summary = _summary(41, 1_800_002_460)
    malformed_summary["candidates"][0]["profit_per_item"] = "1"
    watch = _recipe_watch(status="awaiting_scan")
    runtime = _dashboard_runtime()
    actual = _node(
        f"""
const profile = "alchemy225-tailoring150-cap225-budget100000-owned";
const key = "crowley.trading-summary.v1.us-normal-alliance.1.60.1.70938." + profile;
const entry = (summary) => JSON.stringify({{ version: 1, house: summary.house, client_build: summary.client_build, profile_key: profile, summary }});
const validValues = new Map([[key, entry({json.dumps(bridge_summary)})]]);
const badValues = new Map([[key, entry({json.dumps(malformed_summary)})]]);
const storageFor = (values) => ({{ getItem: (name) => values.get(name) ?? null, setItem: (name, value) => values.set(name, value) }});
let summaryCalls = 0;
function reply(value) {{ return {{ ok: true, status: 200, json: () => Promise.resolve(value) }}; }}
global.fetch = (path) => {{
  if (path.endsWith("/sweeps/latest")) return Promise.resolve(reply({json.dumps(latest)}));
  if (path.endsWith("/recipe-watch")) return Promise.resolve(reply({json.dumps(watch)}));
  summaryCalls += 1;
  return Promise.resolve(reply({json.dumps(_summary(41, 1_800_002_460))}));
}};
const settle = async () => {{ for (let i = 0; i < 12; i += 1) await Promise.resolve(); await new Promise(setImmediate); }};
{runtime}
(async () => {{
  const valid = mountDashboard(storageFor(validValues));
  await settle();
  const restored = {{ complete: valid.dashboard.state.summary?.candidatePoolComplete, cards: valid.nodes.get("opportunities").textContent, source: valid.nodes.get("summary-source").textContent, summaryCalls }};
  const invalid = mountDashboard(storageFor(badValues));
  await settle();
  const recovered = {{ complete: invalid.dashboard.state.summary?.candidatePoolComplete, cards: invalid.nodes.get("opportunities").textContent, summaryCalls }};
  console.log(JSON.stringify({{ restored, recovered }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    )
    assert actual["restored"]["complete"] is True
    assert "Bridge full-pool row" in actual["restored"]["cards"]
    assert "limited saved top ten" not in actual["restored"]["source"]
    assert actual["restored"]["summaryCalls"] == 0
    assert actual["recovered"]["complete"] is True
    assert actual["recovered"]["summaryCalls"] == 1


def test_turnover_floor_filters_fresh_and_saved_full_pool_before_top_ten():
    latest = {
        "house": "us-normal-alliance",
        "sweep_id": 41,
        "finished_epoch": 1_800_002_460,
        "client_build": "1.60.1.70938",
    }
    candidates = [_ranked_candidate(i, "1000", 1) for i in range(1, 15)]
    candidates[0]["market_units_per_hour"] = 5.99
    candidates[1]["market_units_per_hour"] = 2.5
    candidates[2]["market_units_per_hour"] = 6
    candidates[3]["market_units_per_hour"] = 6.01
    summary = _summary(41, latest["finished_epoch"], count=0)
    summary.update(candidates=candidates, opportunities=candidates[:10])
    actual = _node(
        f"""
const profile = "alchemy225-tailoring150-cap225-budget100000-owned";
const key = "crowley.trading-summary.v2.us-normal-alliance.1.60.1.70938." + profile;
const values = new Map([[key, JSON.stringify({{ version: 2, house: "us-normal-alliance", client_build: "1.60.1.70938", profile_key: profile, summary: {json.dumps(summary)} }})]]);
const storage = {{ getItem: (k) => values.get(k) ?? null, setItem: (k, v) => values.set(k, v) }};
function reply(value) {{ return {{ ok: true, status: 200, json: () => Promise.resolve(value) }}; }}
global.fetch = (path) => Promise.resolve(reply(path.endsWith("/sweeps/latest") ? {json.dumps(latest)} : path.endsWith("/recipe-watch") ? {json.dumps(_recipe_watch(status="awaiting_scan"))} : {json.dumps(summary)}));
const settle = async () => {{ for (let i = 0; i < 12; i += 1) await Promise.resolve(); await new Promise(setImmediate); }};
{_dashboard_runtime()}
(async () => {{
  const cached = mountDashboard(storage);
  await settle();
  const savedCards = cached.nodes.get("opportunities").children.map((n) => n.textContent);
  const fresh = mountDashboard({{ getItem: () => null, setItem: () => {{}} }});
  await settle();
  const freshCards = fresh.nodes.get("opportunities").children.map((n) => n.textContent);
  console.log(JSON.stringify({{ savedCards, freshCards }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    )
    for key in ("savedCards", "freshCards"):
        cards = actual[key]
        assert len(cards) == 10
        assert "Rank 03" in cards[0]  # Exactly six qualifies.
        assert "Rank 04" in cards[1]  # Above six qualifies.
        assert "Rank 12" in cards[-1]  # Backfilled from beyond the original top ten.
        assert not any("Rank 01" in card or "Rank 02" in card for card in cards)

"""Executable Node checks for the token-free crafting dashboard."""

import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
DASHBOARD = ROOT / "src/crowley/server/static/trading-dashboard.html"


def _node(script: str) -> dict:
    node = shutil.which("node")
    assert node is not None, "node is required for dashboard tests"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
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
        "market_units_per_hour": (12.57 if index == 0 else 750 if unknown else 0 if zero else 2.5),
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
        "opportunities": [_item(index) for index in range(count)],
        "notes": ["Recipe and leveling costs excluded."],
    }


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
    empty_latest = {
        "house": "other-house",
        "sweep_id": 6,
        "finished_epoch": 1_800_000_300,
        "client_build": "1.60.1.70938",
    }
    empty_summary = _summary(6, 1_800_000_300, count=0)
    empty_summary["house"] = "other-house"
    no_coverage = _item(4)
    no_coverage["usable_hours"] = None
    nonfinite = _item(5)
    nonfinite["market_units_per_hour"] = float("inf")
    empty_summary["opportunities"] = [_item(2), _item(3), no_coverage, nonfinite]
    actual = _node(
        f"""
const calls = [];
const tags = [];
let latestReply = {json.dumps(latest1)};
let currentNow = 1_800_000_060_000;
Date.now = () => currentNow;
const pendingSummaries = [];
function reply(value, ok = true, status = 200) {{ return {{ ok, status, json: () => Promise.resolve(value) }}; }}
global.fetch = (path, options) => {{
  calls.push({{path, options}});
  if (path.endsWith("/sweeps/latest")) return Promise.resolve(reply(latestReply));
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
const nodes = new Map(["scan-time", "scan-age", "scan-counts", "status", "opportunities", "notes", "house-form", "house-key"].map((id) => [id, new FakeNode("div")]));
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
const scanWasFast = {{ time: nodes.get("scan-time").textContent, requests: calls.map((call) => call.path), summaryPending: pendingSummaries.length }};
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
latestReply = {json.dumps(latest3)};
await poll();
await settle();
latestReply = {json.dumps(latest4)};
await poll();
await settle();
const afterNewScan = {{ summaryPending: pendingSummaries.length, cards: nodes.get("opportunities").textContent }};
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
global.fetch = (path) => Promise.resolve(reply(path.endsWith("/sweeps/latest") ? {json.dumps(empty_latest)} : {json.dumps(empty_summary)}));
dashboard.changeHouse("other-house");
await settle();
const emptyTurnover = {{
  scanTime: nodes.get("scan-time").textContent,
  scanAge: nodes.get("scan-age").textContent,
  counts: nodes.get("scan-counts").textContent,
  cards: nodes.get("opportunities").textContent,
  cardClass: nodes.get("opportunities").children[0]?.className,
  status: nodes.get("status").textContent,
}};
console.log(JSON.stringify({{ scanWasFast, rendered2, calls, callsAfterSameSweep, priceAgeAfterClockAdvance, afterNewScan, retriedForNewest, raced, latestRace, error, emptyTurnover, intervalCallbackRegistered: typeof intervalCallback === "function" }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    )

    scan_time = actual["scanWasFast"]["time"]
    assert scan_time
    assert scan_time != "Loading latest scan…"
    assert actual["scanWasFast"]["summaryPending"] == 1
    assert actual["scanWasFast"]["requests"] == [
        "/v1/houses/us-normal-alliance/sweeps/latest",
        "/v1/houses/us-normal-alliance/trading-summary",
    ]
    assert actual["rendered2"]["count"] == 10
    assert "Planned 484Queried 484Successful 483" in actual["rendered2"]["counts"]
    assert "Batch cost" in actual["rendered2"]["cards"]
    assert "31s 19c" in actual["rendered2"]["cards"]  # 3,118.5 copper rounds up as an expense.
    assert "Sale proceeds" in actual["rendered2"]["cards"]
    assert "1g 8s 85c" in actual["rendered2"]["cards"]  # 10,885.1 copper rounds down.
    assert "Net profit if all sell" in actual["rendered2"]["cards"]
    assert "77s 66c" in actual["rendered2"]["cards"]  # No decimal rounding can hide this profit.
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
    assert actual["callsAfterSameSweep"] == 3  # The unchanged sweep only checks latest.
    assert "Price age ~3 min" in actual["priceAgeAfterClockAdvance"]
    assert actual["afterNewScan"]["summaryPending"] == 1
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
    assert "positive observed activity in the 24-hour window" in actual["emptyTurnover"]["cards"]
    assert actual["emptyTurnover"]["cardClass"] == "panel empty"
    assert "positive observed activity" in actual["emptyTurnover"]["status"]
    assert all(call.get("options") is None for call in actual["calls"])


def test_page_has_no_token_prompt_storage_or_external_runtime_assets():
    html = DASHBOARD.read_text(encoding="utf-8")
    assert "/dashboard/advanced" in html
    assert not re.search(
        r"(?:localStorage|sessionStorage|\.prompt\s*\(|Authorization\s*:|api[_-]?token)", html, re.IGNORECASE
    )
    assert not re.search(r"<script\s+src=|<link[^>]+href=[\"']https?://", html, re.IGNORECASE)
    assert "innerHTML" not in html

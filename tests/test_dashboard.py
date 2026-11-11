"""Executable, browser-independent tests for the paper dashboard page."""

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from crowley.advisor.model import Advice, AdviceConfig, Evidence, Pathway, Step, StrategyResult
from crowley.advisor.render import COLUMNS, SECTION_TITLES, advice_payload, plain
from crowley.trading.engine import _allocate_capital

ROOT = Path(__file__).parents[1]
DASHBOARD = ROOT / "src/crowley/server/static/dashboard.html"


def _node(script: str) -> dict:
    node = shutil.which("node")
    assert node is not None, "node is required for dashboard tests"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def _pure_script() -> str:
    html = DASHBOARD.read_text(encoding="utf-8")
    start_marker = "/* DASHBOARD PURE LOGIC START */"
    end_marker = "/* DASHBOARD PURE LOGIC END */"
    start = html.index(start_marker) + len(start_marker)
    end = html.index(end_marker, start)
    return html[start:end]


def _emit_script(body: str) -> str:
    return f"""
{_pure_script()}
function emit(value) {{
  console.log(JSON.stringify(value, (_, item) => typeof item === "bigint" ? item.toString() : item));
}}
{body}
"""


def _detail() -> dict:
    return {
        "name": "large-capital",
        "house": "test-house",
        "client_build": "1.60.1.70450",
        "start_t": 100,
        "capital_copper": "9007199254740993",
        "strategies": ["alpha", "beta"],
        "weights": ["1", "2"],
        "demand_share": "1/2",
        "created_at": "2026-01-01T00:00:00Z",
        "latest_step_t": 100,
        "latest_total_equity": "120000",
        "latest_total_realized": "20000",
        "pending_step_t": 200,
        "curve": [
            {
                "t": 100,
                "equity": {"alpha": "40000", "beta": "80000", "total": "120000"},
                "realized": {"alpha": "5000", "beta": "15000", "total": "20000"},
                "cash": {"alpha": "40000", "beta": "80000", "total": "120000"},
            }
        ],
        "recent_intents": [
            {
                "t": 100,
                "strategy": "alpha",
                "ordinal": 0,
                "intent": {"type": "Buy", "item_id": 2319, "max_unit_price": "27000", "quantity": 24},
                "outcome": "executed",
                "detail": {"filled": 24},
            }
        ],
        "pending": {
            "t": 200,
            "sweep_key": "pending",
            "intents": {"beta": [{"type": "VendorBuy", "item_id": 2319, "quantity": 3}]},
        },
    }


def test_exact_dashboard_logic_and_engine_allocation():
    cases = [
        (100000, ["1", "2"]),
        (7, ["1", "1", "1"]),
        (9007199254740993, ["1/3", "2/3"]),
        (100000000000000000000, ["2", "3", "5"]),
    ]
    detail = _detail()
    actual = _node(
        _emit_script(
            f"""
            const detail = {json.dumps(detail)};
            const allocationInputs = {json.dumps([[str(capital), weights] for capital, weights in cases])};
            const broken = JSON.parse(JSON.stringify(detail));
            broken.curve[0].equity.alpha = "not-copper";
            let malformed = "";
            try {{ validateRunDetail(broken); }} catch (error) {{ malformed = error.message; }}
            let numberRejected = "";
            try {{ parseExactRational(27000); }} catch (error) {{ numberRejected = error.message; }}
            validateRunDetail(detail);
            emit({{
              formats: [formatCopperAsGold("27000"), formatCopperAsGold("12345/2"), formatCopperAsGold("-27000"), formatCopperAsGold("9007199254740993")],
              returns: formatReturnPercent("120000", "100000"),
              allocations: allocationInputs.map(([capital, weights]) => allocateCapitalExact(capital, weights).map((share) => share.numerator.toString())),
              performance: buildPerformanceModel(detail).rows.map((row) => ({{ strategy: row.strategy, start: row.start.numerator.toString(), equity: row.equity.numerator.toString(), returnValue: formatReturnPercent(row.equity, row.start) }})),
              decisions: buildDecisionModel(detail),
              pending: buildPendingModel(detail),
              malformed,
              numberRejected,
            }});
            """
        )
    )
    assert actual["formats"] == ["2.70g", "0.62g", "−2.70g", "900719925474.10g"]
    assert actual["returns"] == "20.00%"
    for (capital, weights), shares in zip(cases, actual["allocations"], strict=True):
        assert shares == [str(share) for share in _allocate_capital(capital, weights)]
    assert actual["performance"] == [
        {"strategy": "alpha", "start": "3002399751580331", "equity": "40000", "returnValue": "−100.00%"},
        {"strategy": "beta", "start": "6004799503160662", "equity": "80000", "returnValue": "−100.00%"},
    ]
    assert actual["decisions"][0]["action"] == "Buy ≤ 2.70g ×24 (item 2319)"
    assert actual["pending"] == [{"strategy": "beta", "action": "Buy from vendor ×3 (item 2319)"}]
    assert "equity" in actual["malformed"]
    assert "exact copper string" in actual["numberRejected"]


def _full_script() -> str:
    html = DASHBOARD.read_text(encoding="utf-8")
    start = html.index("<script>") + len("<script>")
    end = html.index("</script>", start)
    return html[start:end]


def _fake_dom_script(detail: dict) -> str:
    return f"""
class FakeClassList {{
  constructor() {{ this.values = new Set(); }}
  toggle(name, enabled) {{ if (enabled) this.values.add(name); else this.values.delete(name); }}
  contains(name) {{ return this.values.has(name); }}
}}
class FakeNode {{
  constructor(id = "") {{ this.id = id; this.children = []; this.innerHTML = ""; this.hidden = false; this.value = ""; this.checked = false; this.attributes = {{}}; this.listeners = {{}}; this.classList = new FakeClassList(); }}
  set textContent(value) {{ this._textContent = String(value); this.innerHTML = this._textContent.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;"); }}
  get textContent() {{ return this._textContent || ""; }}
  append(...nodes) {{ this.children.push(...nodes); }}
  appendChild(node) {{ this.children.push(node); return node; }}
  replaceChildren(...nodes) {{ this.children = [...nodes]; }}
  addEventListener(name, handler) {{ this.listeners[name] = handler; }}
  focus() {{}}
  setAttribute(name, value) {{ this.attributes[name] = String(value); }}
  querySelector(selector) {{ return this[selector === "tbody" ? "tbody" : selector === "tfoot" ? "tfoot" : "queryChild"]; }}
}}
const nodes = new Map();
for (const id of ["token-form", "forget-token", "token", "status", "run-select", "dashboard-content", "run-meta", "chart", "sparks", "decisions-empty", "pending-content", "paper-view-button", "advice-view-button", "paper-view", "advice-view", "paper-controls", "advice-form", "advice-auto", "advice-house", "advice-character", "advice-profit-gold", "advice-profit-silver", "advice-profit-copper", "advice-min-volume", "advice-share", "advice-horizon", "advice-price-basis", "advice-limit", "advice-meta", "advice-results", "advice-strategy-summary"]) nodes.set(id, new FakeNode(id));
const performanceTable = new FakeNode("performance-table");
performanceTable.tbody = new FakeNode("performance-body");
performanceTable.tfoot = new FakeNode("performance-foot");
nodes.set("performance-table", performanceTable);
const decisionsTable = new FakeNode("decisions-table");
decisionsTable.tbody = new FakeNode("decisions-body");
nodes.set("decisions-table", decisionsTable);
global.document = {{
  getElementById: (id) => nodes.get(id),
  createElement: () => new FakeNode(),
  createElementNS: () => new FakeNode(),
  querySelectorAll: () => [],
}};
global.localStorage = {{ getItem: () => "", setItem: () => {{}}, removeItem: () => {{}} }};
const intervals = [];
global.window = {{ prompt: () => "", setInterval: (fn, ms) => {{ const id = intervals.length + 1; intervals.push({{ id, fn, ms, active: true }}); return id; }}, clearInterval: (id) => {{ const timer = intervals.find((item) => item.id === id); if (timer) timer.active = false; }} }};
global.fetch = () => {{ throw new Error("fetch should not run in this rendering test"); }};
{_full_script()}
const detail = {json.dumps(detail)};
const valid = window.__crowleyDashboardTest.renderDashboardDetail(detail);
const validRows = nodes.get("performance-table").tbody.innerHTML;
const validDecisions = nodes.get("decisions-table").tbody.innerHTML;
const validPending = nodes.get("pending-content").children.map((child) => child.innerHTML).join("|");
nodes.get("performance-table").tbody.innerHTML = "";
nodes.get("decisions-table").tbody.innerHTML = "";
nodes.get("pending-content").replaceChildren();
const malformed = JSON.parse(JSON.stringify(detail));
malformed.curve[0].equity.alpha = "not-copper";
const malformedResult = window.__crowleyDashboardTest.renderDashboardDetail(malformed);
emit({{
  valid,
  validRows,
  validDecisions,
  validPending,
  malformedResult,
  malformedStatus: nodes.get("status").textContent,
  malformedStatusIsError: nodes.get("status").classList.contains("error"),
  malformedRows: nodes.get("performance-table").tbody.innerHTML,
}});
"""


def test_real_rendering_and_error_path_use_a_fake_dom():
    detail = _detail()
    script = _fake_dom_script(detail)
    script = script.replace(
        'global.window = { prompt: () => "", setInterval: (fn, ms) => { const id = intervals.length + 1; intervals.push({ id, fn, ms, active: true }); return id; }, clearInterval: (id) => { const timer = intervals.find((item) => item.id === id); if (timer) timer.active = false; } };',
        'global.window = { prompt: () => "", setInterval: (fn, ms) => { const id = intervals.length + 1; intervals.push({ id, fn, ms, active: true }); return id; }, clearInterval: (id) => { const timer = intervals.find((item) => item.id === id); if (timer) timer.active = false; } };\nfunction emit(value) { console.log(JSON.stringify(value)); }',
    )
    actual = _node(script)
    assert actual["valid"] is True
    assert "alpha" in actual["validRows"] and "beta" in actual["validRows"]
    assert "300239975158.03g" in actual["validRows"]
    assert "Buy ≤ 2.70g ×24 (item 2319)" in actual["validDecisions"]
    assert "beta" in actual["validPending"] and "Buy from vendor ×3 (item 2319)" in actual["validPending"]
    assert actual["malformedResult"] is False
    assert actual["malformedStatusIsError"] is True
    assert "exact copper string" in actual["malformedStatus"]
    assert actual["malformedRows"] == ""


def _advice_payload() -> dict:
    specs = [
        (
            "ranked",
            "crafting",
            Fraction(12345),
            Fraction(35555, 3),
            Fraction(9401, 3),
            0.75,
            None,
            Evidence("independent_starts", 8, successes=6),
        ),
        ("unmeasured", "flip", Fraction(2345), Fraction(9876), Fraction(4321), None, None, None),
        (
            "one_off",
            "vendor_arbitrage",
            Fraction(3456),
            None,
            Fraction(7654),
            None,
            0.5,
            Evidence("arrival_groups", 4),
        ),
        ("farming", "raw_materials", Fraction(4567), None, Fraction(8765), None, None, None),
    ]
    pathways = []
    results = []
    for section, strategy, profit, per_hour, capital, probability, fill, evidence in specs:
        pathway = Pathway(
            strategy=strategy,
            section=section,
            key=f"{strategy}:1001",
            title=f"{section} title",
            steps=(Step("buy_ah", item_id=1001, quantity=2, price=27000, note="cheap"),),
            profit_per_unit=profit,
            profit_per_craft=None,
            volume_per_hour=Fraction(11, 2),
            sale_probability=probability,
            fill_rate=fill,
            evidence=evidence,
            demand_evidence=Evidence("demand_intervals", 24, usable=24),
            listed_units=17,
            capital=capital,
            profit_per_hour=per_hour,
            one_off_profit=Fraction(7654) if section == "one_off" else None,
            sort_value=per_hour
            if per_hour is not None
            else (Fraction(7654) if section == "one_off" else Fraction(8765)),
            confidence="medium",
            details={"fractional": Fraction(3, 2), "current": {1001: 90}},
        )
        pathways.append(pathway)
        results.append(StrategyResult(strategy, (pathway,), (), 1))
    results.append(StrategyResult("recipe_purchase", (), (), 0))
    advice = Advice(
        house_key="test-house",
        client_build="1.60.1.70862",
        as_of_t=1_800_000_000,
        sweep_id=42,
        config=AdviceConfig(),
        results=tuple(results),
        pathways=tuple(pathways),
    )
    return plain(advice_payload(advice, {1001: "Copper Widget"}, None))


def test_advice_model_uses_real_render_payload_and_render_contracts():
    payload = _advice_payload()
    actual = _node(
        _emit_script(
            f"""
            const payload = {json.dumps(payload)};
            const model = buildAdviceModel(payload);
            emit({{
              titles: model.sections.map((section) => section.title),
              columns: model.sections.map((section) => section.columns),
              rows: model.sections.map((section) => section.rows.map((row) => ({{ title: row.title, profit: row.profit, last: row.last, capital: row.capital, evidence: row.evidence, step: row.steps[0], detail: row.details[0] }}))),
              strategies: model.strategies.map((strategy) => strategy.counts),
            }});
            """
        )
    )
    assert actual["titles"] == [
        SECTION_TITLES[name] for name in ("ranked", "unmeasured", "one_off", "farming")
    ]
    assert actual["columns"] == [
        list(COLUMNS[name]) for name in ("ranked", "unmeasured", "one_off", "farming")
    ]
    assert actual["rows"][0][0] == {
        "title": "ranked title",
        "profit": "1g23s45c",
        "last": "1g 18s 52c",
        "capital": "0g 31s 34c",
        "evidence": "0.75 6/8",
        "step": "buy ah 2x Copper Widget (1001) at or below 2g70s00c (cheap)",
        "detail": {"key": "fractional", "value": "1.5"},
    }
    assert actual["rows"][2][0]["evidence"] == "0.50 (4)"
    assert actual["strategies"][0] == "considered 1, pathways 1, excluded 0"


def test_advice_query_and_money_validation():
    payload = _advice_payload()
    actual = _node(
        _emit_script(
            f"""
            const options = {{ strategies: ["crafting", "flip"], professions: ["alchemy", "engineering"], minProfitGold: "2", minProfitSilver: "3", minProfitCopper: "4", minVolume: "2.5", share: "25", horizonHours: "6", priceBasis: "target", limit: "7", character: "" }};
            const blank = Object.fromEntries(new URLSearchParams(buildAdviceQuery(options)));
            options.character = "  Ada  ";
            const named = Object.fromEntries(new URLSearchParams(buildAdviceQuery(options)));
            const errors = [];
            for (const call of [() => goldToCopper(1, 100, 0), () => goldToCopper(1, 0, 100), () => formatAdviceCopper(Infinity)]) {{
              try {{ call(); }} catch (error) {{ errors.push(error.message); }}
            }}
            const missing = JSON.parse({json.dumps(json.dumps(payload))});
            delete missing.sections;
            try {{ validateAdvicePayload(missing); }} catch (error) {{ errors.push(error.message); }}
            emit({{ blank, named, money: formatAdviceCopper(-12345.6), errors }});
            """
        )
    )
    assert actual["blank"] == {
        "strategies": "crafting,flip",
        "professions": "alchemy,engineering",
        "min_profit": "20304",
        "min_volume": "2.5",
        "share": "0.25",
        "horizon_hours": "6",
        "price_basis": "target",
        "limit": "7",
    }
    assert actual["named"]["character"] == "Ada"
    assert actual["money"] == "-1g 23s 46c"
    assert len(actual["errors"]) == 4
    assert any("sections" in error for error in actual["errors"])


def test_view_switch_scheduler_only_arms_visible_view():
    script = _fake_dom_script(_detail()).split("\nconst detail = ", 1)[0]
    script = script.replace(
        'global.fetch = () => { throw new Error("fetch should not run in this rendering test"); };',
        "const fetchCalls = []; const fetchWaiters = []; global.fetch = (path) => { fetchCalls.push(path); return new Promise((resolve) => fetchWaiters.push({ path, resolve })); };",
    )
    script += """
const response = (body, status = 200) => ({ ok: status === 200, status, json: async () => body });
const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };
const emit = (value) => console.log(JSON.stringify(value));
const dashboard = window.__crowleyDashboardTest;
const active = () => intervals.filter((timer) => timer.active).map((timer) => timer.ms);
const initial = active();
dashboard.state.token = "token";
const paperTimer = intervals.find((timer) => timer.active && timer.ms === 60000);
paperTimer.fn();
await settle();
const stalePaperRequest = fetchWaiters.shift();
dashboard.switchView("advice", false);
const statusAtAdviceSwitch = nodes.get("status").textContent;
const adviceOff = active();
stalePaperRequest.resolve(response([]));
await settle();
const statusAfterStalePaper = nodes.get("status").textContent;
const housesRequest = fetchWaiters.shift();
housesRequest.resolve(response([{ key: "test-house" }]));
await settle();
const statusAfterHouses = nodes.get("status").textContent;
dashboard.state.adviceOptions = { house: "test-house", character: "", professions: ["alchemy"], strategies: ["crafting"], minProfitGold: "0", minProfitSilver: "0", minProfitCopper: "0", minVolume: "0", share: "10", horizonHours: "2", priceBasis: "lowest", limit: "25" };
nodes.get("advice-auto").checked = true;
dashboard.scheduleRefresh();
const adviceOn = active();
const adviceTimer = intervals.find((timer) => timer.active && timer.ms === 900000);
fetchCalls.length = 0;
adviceTimer.fn();
await settle();
const adviceRequestOne = fetchWaiters.shift();
adviceRequestOne.resolve(response(__ADVICE_PAYLOAD__));
await settle();
adviceTimer.fn();
await settle();
const adviceRequestTwo = fetchWaiters.shift();
adviceRequestTwo.resolve(response(__ADVICE_PAYLOAD__));
await settle();
const advicePaths = [...fetchCalls];
dashboard.switchView("paper", false);
const paperAgain = active();
fetchCalls.length = 0;
const paperTimerAgain = intervals.find((timer) => timer.active && timer.ms === 60000);
paperTimerAgain.fn();
await settle();
const paperRequestAgain = fetchWaiters.shift();
paperRequestAgain.resolve(response([]));
await settle();
const paperPaths = [...fetchCalls];
emit({
  initial,
  adviceOff,
  adviceOn,
  paperAgain,
  paperHidden: nodes.get("paper-view").hidden,
  adviceHidden: nodes.get("advice-view").hidden,
  paperPressed: nodes.get("paper-view-button").attributes["aria-pressed"],
  staleStatusPreserved: statusAfterStalePaper === statusAtAdviceSwitch,
  adviceReadyStatus: statusAfterHouses,
  advicePaths,
  paperPaths,
  adviceRepeatedExactly: advicePaths.length === 2 && advicePaths[0] === advicePaths[1],
  paperTimerOnlyPaperEndpoints: paperPaths.every((path) => path.startsWith("/v1/paper/runs")),
});
"""
    script = script.replace("__ADVICE_PAYLOAD__", json.dumps(_advice_payload()))
    actual = _node(script)
    assert actual["initial"] == [60000]
    assert actual["adviceOff"] == []
    assert actual["adviceOn"] == [900000]
    assert actual["paperAgain"] == [60000]
    assert actual["paperHidden"] is False
    assert actual["adviceHidden"] is True
    assert actual["paperPressed"] == "true"
    assert actual["staleStatusPreserved"] is True
    assert actual["adviceReadyStatus"] == "Advice ready — choose options and request advice."
    assert actual["adviceRepeatedExactly"] is True
    assert actual["paperTimerOnlyPaperEndpoints"] is True
    assert actual["advicePaths"][0].startswith("/v1/houses/test-house/advice?")
    assert actual["paperPaths"] == ["/v1/paper/runs"]


def _controlled_advice_script(body: str) -> str:
    script = _fake_dom_script(_detail()).split("\nconst detail = ", 1)[0]
    script = script.replace(
        'global.fetch = () => { throw new Error("fetch should not run in this rendering test"); };',
        "const fetchCalls = []; const fetchWaiters = []; global.fetch = (path) => { fetchCalls.push(path); return new Promise((resolve) => fetchWaiters.push({ path, resolve })); };",
    )
    return script + "\n" + body.replace("__ADVICE_PAYLOAD__", json.dumps(_advice_payload()))


def test_clicking_advice_during_request_keeps_response_current():
    actual = _node(
        _controlled_advice_script(
            """
const response = (body) => ({ ok: true, status: 200, json: async () => body });
const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };
const emit = (value) => console.log(JSON.stringify(value));
const dashboard = window.__crowleyDashboardTest;
const options = { house: "test-house", character: "", professions: ["alchemy"], strategies: ["crafting"], minProfitGold: "0", minProfitSilver: "0", minProfitCopper: "0", minVolume: "0", share: "10", horizonHours: "2", priceBasis: "lowest", limit: "25" };
dashboard.state.token = "token";
dashboard.state.adviceHousesLoaded = true;
dashboard.state.adviceHouses = [{ key: "test-house" }];
dashboard.state.adviceOptions = options;
dashboard.switchView("advice", false);
nodes.get("advice-auto").checked = true;
dashboard.scheduleRefresh();
const adviceTimer = intervals.find((timer) => timer.active && timer.ms === 900000);
adviceTimer.fn();
await settle();
const adviceRequest = fetchWaiters.shift();
const statusDuring = nodes.get("status").textContent;
dashboard.switchView("advice", false);
const statusAfterSameViewClick = nodes.get("status").textContent;
adviceRequest.resolve(response(__ADVICE_PAYLOAD__));
await settle();
emit({ fetchCalls, statusDuring, statusAfterSameViewClick, statusAfter: nodes.get("status").textContent, renderedSections: nodes.get("advice-results").children.length, inFlight: dashboard.state.adviceInFlight });
"""
        )
    )
    assert len(actual["fetchCalls"]) == 1
    assert actual["fetchCalls"][0].startswith("/v1/houses/test-house/advice?")
    assert actual["statusDuring"] == "Computing advice…"
    assert actual["statusAfterSameViewClick"] == "Computing advice…"
    assert actual["statusAfter"].startswith("Advice updated ")
    assert actual["renderedSections"] == 4
    assert actual["inFlight"] is False


def test_advice_paper_advice_drops_stale_result_and_restores_status():
    actual = _node(
        _controlled_advice_script(
            """
const response = (body) => ({ ok: true, status: 200, json: async () => body });
const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };
const emit = (value) => console.log(JSON.stringify(value));
const dashboard = window.__crowleyDashboardTest;
const options = { house: "test-house", character: "", professions: ["alchemy"], strategies: ["crafting"], minProfitGold: "0", minProfitSilver: "0", minProfitCopper: "0", minVolume: "0", share: "10", horizonHours: "2", priceBasis: "lowest", limit: "25" };
dashboard.state.token = "token";
dashboard.state.adviceHousesLoaded = true;
dashboard.state.adviceHouses = [{ key: "test-house" }];
dashboard.state.adviceOptions = options;
dashboard.switchView("advice", false);
nodes.get("advice-auto").checked = true;
dashboard.scheduleRefresh();
const adviceTimer = intervals.find((timer) => timer.active && timer.ms === 900000);
adviceTimer.fn();
await settle();
const adviceRequest = fetchWaiters.shift();
const statusDuring = nodes.get("status").textContent;
dashboard.switchView("paper", false);
dashboard.switchView("advice", false);
const statusBeforeStaleResolve = nodes.get("status").textContent;
adviceRequest.resolve(response(__ADVICE_PAYLOAD__));
await settle();
emit({ fetchCalls, statusDuring, statusBeforeStaleResolve, statusAfter: nodes.get("status").textContent, renderedSections: nodes.get("advice-results").children.length, inFlight: dashboard.state.adviceInFlight });
"""
        )
    )
    assert len(actual["fetchCalls"]) == 1
    assert actual["statusDuring"] == "Computing advice…"
    assert actual["statusBeforeStaleResolve"] == "Computing advice…"
    assert actual["statusAfter"] == "Advice ready — choose options and request advice."
    assert actual["renderedSections"] == 0
    assert actual["inFlight"] is False


def test_forget_token_invalidates_advice_request_and_preserves_token_status():
    actual = _node(
        _controlled_advice_script(
            """
const response = (body) => ({ ok: true, status: 200, json: async () => body });
const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };
const emit = (value) => console.log(JSON.stringify(value));
const dashboard = window.__crowleyDashboardTest;
const options = { house: "test-house", character: "", professions: ["alchemy"], strategies: ["crafting"], minProfitGold: "0", minProfitSilver: "0", minProfitCopper: "0", minVolume: "0", share: "10", horizonHours: "2", priceBasis: "lowest", limit: "25" };
dashboard.state.token = "token";
dashboard.state.adviceHousesLoaded = true;
dashboard.state.adviceHouses = [{ key: "test-house" }];
dashboard.state.adviceOptions = options;
dashboard.switchView("advice", false);
nodes.get("advice-auto").checked = true;
dashboard.scheduleRefresh();
const adviceTimer = intervals.find((timer) => timer.active && timer.ms === 900000);
adviceTimer.fn();
await settle();
const adviceRequest = fetchWaiters.shift();
nodes.get("forget-token").listeners.click();
const statusAfterForget = nodes.get("status").textContent;
adviceRequest.resolve(response(__ADVICE_PAYLOAD__));
await settle();
emit({ fetchCalls, statusAfterForget, statusAfter: nodes.get("status").textContent, token: dashboard.state.token, renderedSections: nodes.get("advice-results").children.length, inFlight: dashboard.state.adviceInFlight });
"""
        )
    )
    assert len(actual["fetchCalls"]) == 1
    assert actual["statusAfterForget"] == "Token forgotten. Enter it to begin."
    assert actual["statusAfter"] == "Token forgotten. Enter it to begin."
    assert actual["token"] == ""
    assert actual["renderedSections"] == 0
    assert actual["inFlight"] is False

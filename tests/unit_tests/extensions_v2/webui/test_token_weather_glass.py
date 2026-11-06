"""Independent plugin data, shared styling and location declarations."""

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from tempfile import TemporaryDirectory

import pytest
from openjiuwen.core.events import Event, ExtensionEventBus, SessionContext

from jiuwenswarm.extensions_v2.event_types.context_usage import (
    CONTEXT_USAGE_EVENT, ContextUsageOutput, ContextUsageUpdate, register_context_usage_events,
)
from jiuwenswarm.extensions_v2.event_types.session_queries import register_session_query_events
from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.extension import TokenWeatherGlass
from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.views import (
    INSTANCE, PACKAGE_ID, SETTING, SETTINGS_SLOT, DETAILS_ACTION, SLOT, weather_view,
)
from jiuwenswarm.extensions_v2.handler_ops.session_ops import register_session_ops
from jiuwenswarm.extensions_v2.management import ExtensionManager
from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService, load_package_ui
from jiuwenswarm.extensions_v2.webui.models import RenderNode, RenderTarget, UiActionInput, UiRenderOutput
from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime
from jiuwenswarm.extensions_v2.webui.schema import validate_root
from jiuwenswarm.extensions_v2.webui.settings import parse_settings
from jiuwenswarm.server.runtime.session.session_statistics import SESSION_USAGE_INFO_KEY
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.webui.adapter import WebUIDispatchAdapter

ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT / "jiuwenswarm/extensions_v2/examples/token_weather_glass"
CASES = json.loads((ROOT / "jiuwenswarm/channels/web/frontend/tests/fixtures/webuiAppearanceCases.json").read_text())


def _details_files(usage, readings):
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.details import write_details
    with TemporaryDirectory() as directory:
        root = Path(directory)
        write_details(root, usage, readings)
        return {path.name: path.read_text(encoding="utf-8") for path in root.iterdir()}


def _output_directory(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return MagicMock(resource_id=name, path=path)


def _text(node: RenderNode, language: str = "en") -> str:
    value = node.props.get("value", "")
    if isinstance(value, Mapping):
        value = value[language]
    return " ".join([value, *(_text(child, language) for child in node.children)])


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_shared_appearance_contract(case) -> None:
    from jiuwenswarm.gateway.extensions_v2.webui.component_rules import validate_props as gateway_validate_props

    assert validate_root(RenderNode(case["component"], case["props"])).ok == case["valid"]
    if case["component"] == "Box":
        assert (gateway_validate_props(case["component"], case["props"]) is None) == case["valid"]


@pytest.mark.parametrize("usage,expected,absent", [
    ({"context": {}, "rateLimits": []}, ["No usage yet"], ["0%", "$", "5h"]),
    ({"context": {"tokens": 0, "window": 1000, "percent": 0}, "rateLimits": [], "cost": {"usd": 0}},
     ["0%", "0/1K tokens", "$0.00"], ["暂无"]),
    ({"context": {"tokens": 91000, "window": 1000000, "percent": 9},
      "rateLimits": [{"kind": "five_hour", "percentUsed": 1}]}, ["9.1%", "91K/976.6K tokens", "5h 1%"], ["$", "暂无"]),
    ({"context": {"window": 1000000}, "rateLimits": []}, ["Context limit 976.6K tokens"], ["%", "$"]),
])
def test_usage_distinguishes_known_zero_and_missing_readings(usage, expected, absent) -> None:
    root = weather_view(usage)
    assert validate_root(root).ok
    value = _text(root)
    assert all(part in value for part in expected)
    assert all(part not in value for part in absent)
    assert "visibleWhen" not in root.props
    assert not {"background", "style", "className", "padding"} & root.props.keys()
    assert root.children[-1].props["value"] == {"zh": "打开详情", "en": "Open details"}


@pytest.mark.parametrize("context,expected", [
    ({"tokens": 23200, "window": 262144, "percent": 9}, "8.9% · 23.2K/256K"),
    ({"tokens": 0, "window": 262144, "percent": 0}, "0% · 0/256K"),
    ({"tokens": 999, "window": 999, "percent": 100}, "100% · 999/999"),
    ({"tokens": 1250, "window": 1000, "percent": 100}, "125% · 1.3K/1K"),
    ({"tokens": 12800, "window": 1024000, "percent": 1}, "1.3% · 12.8K/1000K"),
    ({"tokens": 12350, "window": 1024000, "percent": 1}, "1.2% · 12.3K/1000K"),
    ({"tokens": 1048576, "window": 1048576, "percent": 100}, "100% · 1048.6K/1M"),
    ({"tokens": 9007199254740991, "window": 1, "percent": 100}, "900719925474099100% · 9007199254741K/1"),
    ({"tokens": 1000}, "1K"),
    ({"percent": 9}, "9%"),
])
def test_usage_matches_host_precision_units_and_rounding(context, expected) -> None:
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.details import details_view

    root = weather_view({"context": context, "rateLimits": []})
    assert validate_root(root).ok
    assert expected in _text(root, "zh")
    assert expected in _text(root, "en")
    if "%" in expected:
        document = _details_files({"context": context, "rateLimits": []}, [])
        for language in ("zh", "en"):
            assert f'<dd>{expected.split(" · ")[0]}</dd>' in document[f"{language}.html"]


@pytest.mark.parametrize("percent,zh,en,tone", [
    (0, "☀ 晴朗", "☀ Clear", "success"),
    (24.999, "☀ 晴朗", "☀ Clear", "success"),
    (25, "☁ 多云", "☁ Cloudy", "secondary"),
    (49.999, "☁ 多云", "☁ Cloudy", "secondary"),
    (50, "☂ 阵雨", "☂ Showers", "warning"),
    (74.999, "☂ 阵雨", "☂ Showers", "warning"),
    (75, "☇ 风暴", "☇ Storm", "danger"),
    (89.999, "☇ 风暴", "☇ Storm", "danger"),
    (90, "↯ 即将压缩", "↯ Compact soon", "danger"),
    (100, "↯ 即将压缩", "↯ Compact soon", "danger"),
    (125, "↯ 即将压缩", "↯ Compact soon", "danger"),
])
def test_weather_forecast_changes_at_unrounded_boundaries_in_both_languages(percent, zh, en, tone) -> None:
    root = weather_view({"context": {"percent": percent}, "rateLimits": []})
    assert validate_root(root).ok
    forecast = root.children[0].children[0]
    assert forecast.props["value"] == {"zh": zh, "en": en}
    assert forecast.props["type"] == tone
    assert "TokenWeather" not in _text(root, "zh") + _text(root, "en")


def test_weather_forecast_uses_precise_counts_before_rounded_reported_percentage() -> None:
    root = weather_view({"context": {"tokens": 24999, "window": 100000, "percent": 25}, "rateLimits": []})
    assert root.children[0].children[0].props["value"] == {"zh": "☀ 晴朗", "en": "☀ Clear"}
    assert "25%" in _text(root)


@pytest.mark.parametrize("context", [{}, {"tokens": 0}, {"tokens": 1000}, {"window": 262144}])
def test_unknown_occupancy_has_no_forecast_or_brand_title(context) -> None:
    root = weather_view({"context": context, "rateLimits": []})
    assert validate_root(root).ok
    assert len(root.children[0].children) == 1
    assert "TokenWeather" not in _text(root, "zh") + _text(root, "en")
    assert all(symbol not in _text(root) for symbol in ("☀", "☁", "☂", "☇", "↯"))


def test_plugin_has_separate_complete_language_values() -> None:
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.locales import MESSAGES
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.views import setting_view

    assert MESSAGES["zh"].keys() == MESSAGES["en"].keys()
    assert MESSAGES["zh"]["title"] == MESSAGES["en"]["title"] == "TokenWeather"
    window = weather_view({"context": {"window": 262144}, "rateLimits": []})
    assert "TokenWeather" not in _text(window, "zh") + _text(window, "en")
    assert "上下文上限 256K Token" in _text(window, "zh")
    assert "Context limit 256K tokens" in _text(window, "en")
    assert "Window" not in _text(window, "zh")
    assert "上下文上限" not in _text(window, "en")
    empty = weather_view({"context": {}, "rateLimits": []})
    assert "暂无用量" in _text(empty, "zh") and "No usage yet" not in _text(empty, "zh")
    assert setting_view().props["ariaLabel"] == {"zh": "显示 TokenWeather", "en": "Show TokenWeather"}


@pytest.mark.parametrize("case", json.loads((ROOT / "jiuwenswarm/channels/web/frontend/tests/fixtures/webuiLocalizedTextCases.json").read_text(encoding="utf-8")),
                         ids=lambda case: case["name"])
def test_localized_text_contract_matches_both_hosts(case) -> None:
    from jiuwenswarm.gateway.extensions_v2.webui.component_rules import validate_props as gateway_validate_props
    from jiuwenswarm.extensions_v2.webui.component_rules import validate_props

    root = case["root"]
    assert (validate_props(root["component"], root["props"]) is None) == case["valid"]
    assert (gateway_validate_props(root["component"], root["props"]) is None) == case["valid"]


def test_manifest_reuses_the_original_settings_slot() -> None:
    record = SimpleNamespace(root=SOURCE, package_id=PACKAGE_ID)
    declarations = load_package_ui(record)
    settings = next(item for item in declarations if item.slot == SETTINGS_SLOT)
    assert settings.type == "web.slot.contribution"
    assert settings.settings["scope"] == "application"
    assert settings.settings["location"] == {"menu": "general", "section": "display"}
    assert settings.slot == SETTINGS_SLOT
    assert settings.id == SETTING
    assert len(declarations) == 3


@pytest.mark.parametrize("case", json.loads((ROOT / "jiuwenswarm/channels/web/frontend/tests/fixtures/webuiPanelDocumentCases.json").read_text()),
                         ids=lambda case: case["name"])
def test_panel_document_contract_matches_both_hosts(case) -> None:
    from jiuwenswarm.gateway.extensions_v2.webui.component_rules import validate_props as gateway_validate_props
    from jiuwenswarm.extensions_v2.webui.component_rules import validate_props

    root = case["root"]
    assert (validate_props(root["component"], root["props"]) is None) == case["valid"]
    assert (gateway_validate_props(root["component"], root["props"]) is None) == case["valid"]


def test_turn_history_keeps_gaps_zeroes_deduplicates_requests_and_caps_at_twelve() -> None:
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.history import TurnHistory, sparkline

    history = TurnHistory()
    for index, tokens in enumerate((0, None, 100, 50)):
        usage = {"context": {} if tokens is None else {"tokens": tokens}, "rateLimits": []}
        history.observe(ContextUsageUpdate("s1", f"r{index}", input_tokens=100, output_tokens=10,
                                          total_tokens=110, cache_tokens=90), usage)
    assert sparkline(history.readings) == "▁·█▄"
    history.observe(ContextUsageUpdate("s1", "r3", input_tokens=200, total_tokens=200),
                    {"context": {"tokens": 25}, "rateLimits": []})
    assert len(history.readings) == 4 and history.count == 4
    assert history.readings[-1].number == 4 and history.readings[-1].total_tokens == 200
    assert history.readings[0].input_tokens == 100 and history.readings[0].total_tokens == 110
    for index in range(4, 15):
        history.observe(ContextUsageUpdate("s1", f"r{index}"), {"context": {}, "rateLimits": []})
    assert len(history.readings) == 12
    assert history.readings[0].number == 4 and history.readings[-1].number == 15
    assert history.observe(ContextUsageUpdate("s1", "r0"), {"context": {}, "rateLimits": []}) is None
    assert len(history.readings) == 12 and history.count == 15


def test_usage_documents_have_separate_languages_graphs_and_real_consumption() -> None:
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.details import details_view
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.history import TurnHistory

    usage = {"context": {"tokens": 0, "window": 1000, "percent": 0, "breakdown": {"categories": [
        {"name": name, "tokens": 0} for name in ("system_prompt", "tools", "skills", "messages")
    ]}}, "rateLimits": []}
    history = TurnHistory()
    for index in range(12):
        history.observe(ContextUsageUpdate("s1", f"r{index}", input_tokens=100, output_tokens=20,
                                          total_tokens=120, cache_tokens=80), usage)
    files = _details_files(usage, history.readings)
    document = details_view({"zh": "/resources/zh.html", "en": "/resources/en.html"})
    assert validate_root(document).ok
    assert "visibleWhen" not in document.props
    assert document.props["minWidth"] == 360
    zh, en = files["zh.html"], files["en.html"]
    assert "逐轮上下文组成" in zh and "Context composition by turn" not in zh
    assert "Context composition by turn" in en and "逐轮明细" not in en
    assert '<td>100</td><td>20</td><td>80</td><td>120</td><td>0</td>' in en
    assert en.count('tabindex="0"') == 12 and en.count('<tr>') == 13
    assert en.count('class="zero"') == 12 and '<svg' not in en
    assert '<script' not in en and 'src=' not in en
    assert all(len(value.encode()) < 16 * 1024 for value in (zh, en))
    empty = _details_files({"context": {}, "rateLimits": []}, [])
    assert "Waiting for the next turn" in empty["en.html"]
    assert "<svg" not in empty["en.html"]
    single = type(history.readings[0])("single", 1, 1234567, 20, 1234587, 800000, 0)
    single_document = _details_files(usage, [single])["en.html"]
    assert "<td>1,234,567</td>" in single_document
    assert single_document.count('class="column unobserved"') == 1
    assert "Not collected" in single_document


def test_category_snapshots_are_copied_ordered_and_not_backfilled() -> None:
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.history import TurnHistory

    history = TurnHistory()
    history.observe(ContextUsageUpdate("s1", "old"), {"context": {"tokens": 999}, "rateLimits": []})
    categories = [{"name": "messages", "tokens": 20}, {"name": "tools", "tokens": 30},
                  {"name": "system_prompt", "tokens": 10}, {"name": "memory", "tokens": 40}]
    usage = {"context": {"tokens": 105, "breakdown": {"categories": categories}}, "rateLimits": []}
    history.observe(ContextUsageUpdate("s1", "new"), usage)
    saved = history.readings[-1]
    categories[0]["tokens"] = 9999
    assert history.readings[0].context_categories is None
    assert [(item.name, item.tokens) for item in saved.context_categories] == [
        ("system_prompt", 10), ("tools", 30), ("messages", 20), ("memory", 40),
    ]
    history.observe(ContextUsageUpdate("s1", "new"), usage)
    assert len(history.readings) == 2 and history.readings[-1].number == 2
    assert history.readings[-1].context_categories[2].tokens == 9999
    assert saved.context_categories[2].tokens == 20


@pytest.mark.parametrize("categories", [
    [{"name": "tools", "tokens": -1}], [{"name": "tools", "tokens": True}],
    [{"name": "tools", "tokens": float("nan")}], [{"name": "tools", "tokens": float("inf")}],
    [{"name": "", "tokens": 1}], [{"name": "tools"}], [None],
    [{"name": "tools", "tokens": 1}, {"name": "tools", "tokens": 2}],
    [{"name": "tools", "tokens": 1e308}, {"name": "messages", "tokens": 1e308}],
])
def test_invalid_categories_do_not_create_a_partial_turn(categories) -> None:
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.history import TurnHistory

    history = TurnHistory()
    with pytest.raises(ValueError, match="context category"):
        history.observe(ContextUsageUpdate("s1", "r1"), {"context": {"breakdown": {"categories": categories}}})
    assert history.count == 0 and history.readings == []


def test_composition_uses_category_sum_without_rescaling_to_context_or_consumption() -> None:
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.details import details_view
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.history import TurnHistory

    history = TurnHistory()
    usage = {"context": {"tokens": 500, "window": 262144, "breakdown": {"categories": [
        {"name": "tools", "tokens": 300}, {"name": "system_prompt", "tokens": 100},
    ]}}, "rateLimits": []}
    history.observe(ContextUsageUpdate("s1", "r1", input_tokens=400, output_tokens=70, total_tokens=470), usage)
    html = _details_files(usage, history.readings)["en.html"]
    assert 'class="system_prompt" style="flex:100"' in html
    assert 'class="tools" style="flex:300"' in html
    assert 'class="bar-value">400</span>' in html
    assert '<dd>500</dd>' in html and '<td>470</td><td>500</td>' in html
    assert 'aria-describedby="t1"' in html and 'id="t1" role="tooltip"' in html
    assert '<dt>System prompt</dt><dd>100</dd>' in html
    css = _details_files(usage, history.readings)['details.css']
    assert all(color in css for color in ('#6e52e0', '#5bb402', '#2563eb', '#ffb802'))


def test_composition_documents_fit_twelve_positive_turns_and_escape_unknown_category_names() -> None:
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.details import details_view
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.history import TurnHistory

    history = TurnHistory()
    for index in range(12):
        values = {"system_prompt": 4420, "tools": 16710, "skills": 1600, "messages": 205 + index * 325}
        usage = {"context": {"tokens": sum(values.values()), "window": 262144, "breakdown": {"categories": [
            {"name": name, "tokens": tokens} for name, tokens in values.items()
        ]}}, "rateLimits": []}
        history.observe(ContextUsageUpdate("s1", f"r{index}"), usage)
    files = _details_files(usage, history.readings)
    document = details_view({"zh": "/resources/zh.html", "en": "/resources/en.html"})
    assert validate_root(document).ok
    assert all(len(html.encode()) < 1024 * 1024 for html in files.values())
    usage["context"]["breakdown"]["categories"] = [{"name": '<img src=x onerror="alert(1)">', "tokens": 100}]
    history.observe(ContextUsageUpdate("s1", "unknown"), usage)
    html = _details_files(usage, history.readings)["en.html"]
    assert '<img src=x' not in html and '&lt;img src=x' in html
    assert 'class="other" style="flex:' in html and 'Other categories' in html


def test_context_delta_is_not_labelled_as_consumption_and_missing_values_do_not_form_a_delta() -> None:
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.history import TurnHistory

    history = TurnHistory()
    usage = {"context": {"tokens": 100}, "rateLimits": []}
    history.observe(ContextUsageUpdate("s1", "r1", total_tokens=1000), usage)
    history.observe(ContextUsageUpdate("s1", "r2", total_tokens=2000), {"context": {"tokens": 20}, "rateLimits": []})
    text = _text(weather_view(usage, history.readings))
    assert "Last context change −80" in text and "2000" not in text
    history.observe(ContextUsageUpdate("s1", "r3"), {"context": {}, "rateLimits": []})
    assert "Last context change" not in _text(weather_view(usage, history.readings))


def test_workbench_no_longer_contributes_demo_weather_or_its_setting() -> None:
    root = ROOT / "jiuwenswarm/extensions_v2/examples/webui_workbench_v2"
    declarations = load_package_ui(SimpleNamespace(root=root, package_id="webui.workbench-v2"))
    assert declarations
    assert not any(row.slot in (SETTINGS_SLOT, SLOT) for row in declarations)


def test_removed_settings_entry_is_not_a_registered_slot() -> None:
    from jiuwenswarm.extensions_v2.webui.constants import slot_spec
    assert slot_spec("application.settings.content") is None


@pytest.mark.parametrize("change", ["old_type", "old_slot", "duplicate_target"])
def test_catalog_rejects_parallel_settings_entries_and_duplicate_instances(tmp_path, change) -> None:
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    item = manifest["ui"]["items"][1]
    if change == "old_type":
        item["type"] = "web.settings.contribution"
    elif change == "old_slot":
        item["slot"] = "application.settings.content"
    else:
        manifest["ui"]["items"].append({**item, "id": "other", "instanceKey": item["id"]})
    (tmp_path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        load_package_ui(SimpleNamespace(root=tmp_path, package_id=PACKAGE_ID))


@pytest.mark.parametrize("kind,location", [("menu", {}), ("section", {"menu": "general"}),
    ("item", {"menu": "general", "section": "display", "after": "old"})])
def test_settings_declarations_accept_three_location_levels(kind, location) -> None:
    raw = {"slot": SETTINGS_SLOT, "id": "demo", "kind": kind, "location": location, "title": {"zh": "示例", "en": "Example"}}
    if kind == "menu":
        raw["icon"] = "settings"
    if kind == "item":
        raw["renderer"] = {"kind": "webui-tree"}
    assert parse_settings(raw)["location"] == location


@pytest.mark.parametrize("change", [
    {"id": "bad::id"}, {"kind": "unknown"}, {"title": {"zh": "示例"}},
    {"slot": "application.settings.content"}, {"location": {"menu": "general"}},
    {"location": {"menu": "general", "section": "display", "before": "one", "after": "two"}},
    {"renderer": {"kind": "html"}}, {"scope": "unknown"}, {"instanceKey": "bad|key"},
    {"kind": "menu", "location": {}, "icon": "settings", "renderer": {}},
    {"kind": "section", "location": {"menu": "general"}, "renderer": None},
])
def test_settings_declarations_reject_ambiguous_or_invalid_metadata(change) -> None:
    raw = {"slot": SETTINGS_SLOT, "id": "demo", "kind": "item", "location": {"menu": "general", "section": "display"},
           "title": {"zh": "示例", "en": "Example"}, "renderer": {"kind": "webui-tree"}}
    with pytest.raises(ValueError):
        parse_settings({**raw, **change})


@pytest.mark.asyncio
async def test_catalog_request_uses_the_bound_web_connection_without_a_conversation(monkeypatch) -> None:
    from jiuwenswarm.extensions_v2 import assembly

    catalog = WebUICatalogService(lambda: (SimpleNamespace(root=SOURCE, package_id=PACKAGE_ID, version="1", generation=1),))
    monkeypatch.setattr(assembly, "get_webui_runtime", lambda: SimpleNamespace(catalog=lambda: catalog))
    adapter = WebUIDispatchAdapter()
    request = AgentRequest("catalog-1", channel_id="web", req_method=ReqMethod.WEBUI_SETTINGS_CATALOG,
                           metadata={"ws_id": "tab-1"})
    output = await adapter.handle(request)
    assert output.ok
    assert output.payload["items"][0]["package_id"] == PACKAGE_ID
    assert "install_path" not in json.dumps(output.payload)
    assert not (await adapter.handle(replace(request, params={"path": "/arbitrary"}))).ok
    assert not (await adapter.handle(replace(request, metadata={}))).ok
    assert not (await adapter.handle(replace(request, channel_id="feishu"))).ok


@pytest.mark.asyncio
async def test_gateway_registers_both_catalog_and_existing_dispatch(monkeypatch) -> None:
    from jiuwenswarm.gateway.channel_manager.web import webui_handlers

    routes = {}
    channel = SimpleNamespace(channel_id="web", register_method=lambda name, handler: routes.update({name: handler}))
    proxy = AsyncMock(return_value=True)
    monkeypatch.setattr(webui_handlers, "proxy_unary_request", proxy)
    webui_handlers.register_webui_handlers(channel, lambda: object())
    assert {ReqMethod.WEBUI_DISPATCH.value, ReqMethod.WEBUI_SETTINGS_CATALOG.value} <= routes.keys()
    await routes[ReqMethod.WEBUI_SETTINGS_CATALOG.value](SimpleNamespace(_jiuwen_ws_id="tab-1"), "q1", {}, None)
    assert proxy.call_args.kwargs["req_method"] == ReqMethod.WEBUI_SETTINGS_CATALOG
    assert proxy.call_args.kwargs["metadata"] == {"ws_id": "tab-1"}


@pytest.mark.asyncio
async def test_details_open_and_ready_keep_conversation_state_isolated(tmp_path) -> None:
    plugin = TokenWeatherGlass()
    render = AsyncMock(return_value=UiRenderOutput(status="delivered"))
    usage = AsyncMock(return_value={"context": {"percent": 12}, "rateLimits": []})
    create = AsyncMock(side_effect=[_output_directory(tmp_path, "first"), _output_directory(tmp_path, "second")])
    asset_url = AsyncMock(side_effect=lambda path, resource_id: f"/resources/{resource_id}/{path}")
    release = AsyncMock()
    ops = SimpleNamespace(ui=SimpleNamespace(render=render, create_resource_dir=create, asset_url=asset_url, release_resources=release),
                          session=SimpleNamespace(usage=usage))
    target = RenderTarget("s1", SLOT, PACKAGE_ID, INSTANCE)
    next_handler = AsyncMock()
    for destination in (target, replace(target, session_id="s2")):
        assert (await plugin.on_ui(Event("ui.ready", UiActionInput("ui.ready", destination)), ops, next_handler)).accepted
    usage.assert_awaited_with(breakdown="summary")
    click = UiActionInput("ui.click", target, {"action_id": DETAILS_ACTION})
    assert (await plugin.on_ui(Event("ui.click", click), ops, next_handler)).accepted
    frame = render.call_args.args[0]
    assert frame.target.slot_id == "conversation.panel.right"
    assert frame.target.instance_key == "details"
    assert frame.root.component == "Iframe"
    assert plugin._sessions["s1"].details_open
    assert not plugin._sessions["s2"].details_open
    assert not plugin._sessions["s1"].history.readings
    render.return_value = UiRenderOutput(status="rejected")
    assert not (await plugin.on_ui(Event("ui.click", click), ops, next_handler)).accepted
    assert plugin._sessions["s1"].resources.resource_id == "first"
    assert create.call_args.kwargs["previous"] == "first"
    release.assert_awaited_once_with("second")
    invalid = replace(click, target=replace(target, instance_key="other"))
    assert not (await plugin.on_ui(Event("ui.click", invalid), ops, next_handler)).accepted


@pytest.mark.asyncio
async def test_installed_plugin_reads_public_query_and_renders_settings_without_session(tmp_path, monkeypatch) -> None:
    bus = ExtensionEventBus()
    manager = ExtensionManager(
        tmp_path / "store",
        store_dir=tmp_path / "persistent",
        report_path=tmp_path / "report.yaml",
    )
    transport = SimpleNamespace(send_frame=AsyncMock(return_value=True), send_clear=AsyncMock(return_value=True),
                                send_error=AsyncMock(return_value=True))
    catalog = WebUICatalogService(manager.active_packages)
    runtime = WebUIRuntime(bus=bus, transport=transport, catalog=catalog)
    manager.set_inactive_callback(runtime.drop_package)

    def event_types(target):
        runtime.register_event_types(target)
        register_context_usage_events(target)
        register_session_query_events(target)

    def handlers(target):
        runtime.register_handlers(target)
        register_session_ops(target)

    await manager.init(bus, host_event_types=event_types, host_handlers=handlers)
    try:
        workbench = ROOT / "jiuwenswarm/extensions_v2/examples/webui_workbench_v2"
        assert (await manager.install([workbench]))[0].status == "SUCCESS"
        assert (await manager.install([SOURCE]))[0].status == "SUCCESS"
        payload = catalog.current().settings_payload()
        assert len(payload["revision"]) == 64
        assert {item["package_id"] for item in payload["items"]} == {PACKAGE_ID}
        assert payload["items"][0]["location"] == {"menu": "general", "section": "display"}
        assert catalog.current().allows_target(SETTINGS_SLOT, PACKAGE_ID, instance_key=SETTING)
        assert not catalog.current().allows_target(SETTINGS_SLOT, PACKAGE_ID, instance_key=SETTING, scope="session")
        assert not catalog.current().allows_target(SETTINGS_SLOT, PACKAGE_ID, instance_key="undeclared")
        setting = RenderTarget("", SETTINGS_SLOT, PACKAGE_ID, SETTING, scope="application")
        assert (await runtime.dispatch(UiActionInput("ui.ready", setting), connection_id="tab-1")).accepted
        assert transport.send_frame.call_args.args[0].root.props["preference"]["default"] is True
        assert transport.send_frame.call_args.args[0].visible_when is None
        workbench_header = RenderTarget("s1", "conversation.header.title.after", "webui.workbench-v2", "workbench")
        assert (await runtime.dispatch(UiActionInput("ui.ready", workbench_header), connection_id="tab-1")).accepted
        assert transport.send_frame.call_args.args[0].target.resolved_scope == "session"
        snapshot = {
            "schema_version": "session-usage.v1", "session_id": "s1", "source": "host", "turn": None,
            "context": {"context_window": {"input_tokens": 120, "limit_tokens": 1000},
                        "measurement": {"authoritative_total": True}, "parts": {
                            "system_prompt": {"tokens": 20}, "tools": {"tokens": 60},
                            "skills": {"tokens": 30}, "messages": {"tokens": 10},
                        }},
        }
        context = SessionContext("s1", metadata={SESSION_USAGE_INFO_KEY: snapshot})
        target = RenderTarget("s1", SLOT, PACKAGE_ID, INSTANCE)
        assert (await bus.trigger(Event("ui.ready", UiActionInput("ui.ready", target)), context)).accepted
        for _ in range(2):
            output = await bus.trigger(Event(CONTEXT_USAGE_EVENT, ContextUsageUpdate("s1", "r1", usage_percent=99)), context)
            assert isinstance(output, ContextUsageOutput) and output.accepted
            frame = transport.send_frame.call_args.args[0]
            assert "12%" in _text(frame.root)
            assert frame.visible_when == {"key": "visible", "default": True}
            assert "99%" not in _text(frame.root)
            assert "120/1K tokens" in _text(frame.root)
        opened = await runtime.dispatch(UiActionInput("ui.click", target, {
            "node_key": "0.1", "revision": frame.revision, "action_id": DETAILS_ACTION, "args": {}, "data": {},
        }), connection_id="tab-1")
        assert opened.accepted
        await bus.trigger(Event(CONTEXT_USAGE_EVENT, ContextUsageUpdate(
            "s1", "r2", input_tokens=200, output_tokens=30, total_tokens=230, cache_tokens=40,
        )), context)
        details_frame = next(call.args[0] for call in reversed(transport.send_frame.call_args_list)
                             if call.args[0].target.package_id == PACKAGE_ID
                             and call.args[0].target.instance_key == "details")
        assert validate_root(details_frame.root).ok
        assert details_frame.visible_when == {"key": "visible", "default": True}
        from urllib.parse import urlsplit
        path = urlsplit(details_frame.root.props["src"]["en"]).path.split("/")
        data, media_type = await runtime.resources.read(path[-2], path[-1], route=path[-3])
        html = data.decode("utf-8")
        assert media_type == "text/html; charset=utf-8"
        assert "<td>200</td><td>30</td><td>40</td><td>230</td>" in html
        assert html.count("<tr>") == 3
        assert '<dt>System prompt</dt><dd>20</dd>' in html
        assert 'class="tools" style="flex:60"' in html
        from jiuwenswarm.extensions_v2 import assembly
        monkeypatch.setattr(assembly, "get_webui_runtime", lambda: runtime)
        transport.send_frame.reset_mock()
        response = await WebUIDispatchAdapter().handle(AgentRequest(
            request_id="both-plugins", channel_id="web", session_id="s1", req_method=ReqMethod.WEBUI_DISPATCH,
            metadata={"ws_id": "tab-1"}, params={"event": "ui.ready", "target": {"slot_id": SLOT}},
        ))
        assert response.ok and response.payload["accepted"]
        assert {call.args[0].target.package_id for call in transport.send_frame.call_args_list} == {PACKAGE_ID}
        assert transport.send_error.await_count == 0
        transport.send_frame.reset_mock()
        for slot in (SETTINGS_SLOT, SLOT):
            denied = await WebUIDispatchAdapter().handle(AgentRequest(
                request_id=f"removed-workbench-{slot}", channel_id="web", session_id="s1",
                req_method=ReqMethod.WEBUI_DISPATCH, metadata={"ws_id": "tab-1"},
                params={"event": "ui.ready", "target": {
                    "slot_id": slot, "package_id": "webui.workbench-v2", "instance_key": "workbench",
                }},
            ))
            assert not denied.payload["accepted"] and denied.payload["code"] == "NO_CONTRIBUTION"
        transport.send_frame.assert_not_awaited()
        denied = await WebUIDispatchAdapter().handle(AgentRequest(
            request_id="scope-mismatch", channel_id="web", session_id="s1", req_method=ReqMethod.WEBUI_DISPATCH,
            metadata={"ws_id": "tab-1"}, params={"event": "ui.ready", "target": {
                "slot_id": SETTINGS_SLOT, "package_id": PACKAGE_ID, "instance_key": SETTING, "scope": "session",
            }},
        ))
        assert not denied.payload["accepted"] and denied.payload["code"] == "NO_CONTRIBUTION"
        bound_setting = replace(setting, connection_id="tab-1")
        assert runtime.registry.get(bound_setting) is not None
        await runtime.drop_session("s1")
        assert runtime.registry.get(workbench_header) is None
        assert runtime.registry.get(bound_setting) is not None
        await runtime.drop_connection("tab-1")
        assert runtime.registry.get(bound_setting) is None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_large_detail_files_keep_render_frame_small_and_pack_assets(tmp_path):
    from zipfile import ZipFile
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.pack import build_package
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.details import details_view
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.history import TurnHistory
    from jiuwenswarm.extensions_v2.webui.resources import WebUIResourceService

    usage = {"context": {"tokens": 100000, "window": 262144, "breakdown": {"categories": [
        {"name": name, "tokens": 20000} for name in (
            "system_prompt", "tools", "skills", "messages", "long custom category " * 30,
        )
    ]}}, "rateLimits": []}
    history = TurnHistory()
    for index in range(12):
        history.observe(ContextUsageUpdate("s1", f"r{index}"), usage)
    files = _details_files(usage, history.readings)
    assert all(len(files[f"{lang}.html"].encode()) > 16 * 1024 for lang in ("zh", "en"))
    record = SimpleNamespace(root=SOURCE, package_id=PACKAGE_ID, version="1.2.0", generation=1)
    from openjiuwen.core.events import PackageMetadata
    ops = SimpleNamespace(package_metadata=PackageMetadata(PACKAGE_ID, "1.2.0", SOURCE),
                          session_context=SessionContext("s1", metadata={"user_id": "u1"}))
    service = WebUIResourceService(WebUICatalogService(lambda: (record,)))
    try:
        from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.details import write_details
        published = await service.create_directory(ops)
        async with published:
            write_details(published.path, usage, history.readings)
        root = details_view({lang: await service.asset_url(ops, f"{lang}.html", resource_id=published.resource_id)
                             for lang in ("zh", "en")})
        assert validate_root(root).ok
        assert "srcDoc" not in root.props
        assert len(json.dumps(root.props).encode()) < 1024
    finally:
        await service.shutdown()
    package = build_package(tmp_path / "weather.zip")
    with ZipFile(package) as archive:
        assert {"assets/details.html", "assets/details.css"} <= set(archive.namelist())
        assert "oklch(" not in archive.read("assets/details.css").decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "cancelled"])
async def test_detail_render_failure_releases_new_files_and_retains_current_set(failure, tmp_path):
    import asyncio
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.extension import WeatherState

    previous = _output_directory(tmp_path, "old")
    state = WeatherState({"context": {}, "rateLimits": []}, resources=previous)
    raised = RuntimeError("delivery failed") if failure == "exception" else asyncio.CancelledError()
    render, release = AsyncMock(side_effect=raised), AsyncMock()
    create = AsyncMock(return_value=_output_directory(tmp_path, "new"))
    asset_url = AsyncMock(side_effect=lambda path, resource_id: f"/resources/{resource_id}/{path}")
    ops = SimpleNamespace(ui=SimpleNamespace(render=render, create_resource_dir=create, asset_url=asset_url, release_resources=release))
    with pytest.raises(type(raised)):
        await TokenWeatherGlass._render_details(ops, "s1", state)
    assert state.resources is previous
    release.assert_awaited_once_with("new")


@pytest.mark.asyncio
@pytest.mark.parametrize("write_failed", [False, True])
@pytest.mark.parametrize("cleanup_cancelled", [False, True])
async def test_detail_cancellation_preserves_write_and_cleanup_failures(tmp_path, write_failed, cleanup_cancelled):
    import asyncio
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass.extension import WeatherState

    cancelled = asyncio.CancelledError("first cancellation")
    write_error = OSError("write failed")
    if write_failed:
        cancelled.__cause__ = write_error
    cleanup_error = asyncio.CancelledError("cleanup cancelled") if cleanup_cancelled else OSError("delete failed")
    previous = _output_directory(tmp_path, "old")
    state = WeatherState({"context": {}, "rateLimits": []}, resources=previous)
    render = AsyncMock(side_effect=cancelled)
    release = AsyncMock(side_effect=cleanup_error)
    create = AsyncMock(return_value=_output_directory(tmp_path, "new"))
    asset_url = AsyncMock(side_effect=lambda path, resource_id: f"/resources/{resource_id}/{path}")
    ops = SimpleNamespace(ui=SimpleNamespace(render=render, create_resource_dir=create, asset_url=asset_url, release_resources=release))
    with pytest.raises(asyncio.CancelledError, match="first cancellation") as caught:
        await TokenWeatherGlass._render_details(ops, "s1", state)
    assert caught.value is cancelled and state.resources is previous
    if write_failed:
        assert isinstance(caught.value.__cause__, BaseExceptionGroup)
        assert caught.value.__cause__.exceptions == (write_error, cleanup_error)
    else:
        assert caught.value.__cause__ is cleanup_error
    release.assert_awaited_once_with("new")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count, fail", [(0, True), (1, False), (3, False), (3, True)])
async def test_plugin_writer_failure_or_cancellation_cleans_unpublished_output(tmp_path, monkeypatch, cancel_count, fail):
    import asyncio
    import threading
    from functools import partial
    from openjiuwen.core.events import PackageMetadata
    from jiuwenswarm.extensions_v2.webui.resources import WebUIResourceService
    from jiuwenswarm.extensions_v2.examples.token_weather_glass.token_weather_glass import extension

    record = SimpleNamespace(root=SOURCE, package_id=PACKAGE_ID, version="1.2.0", generation=1)
    service = WebUIResourceService(WebUICatalogService(lambda: (record,)))
    ops = SimpleNamespace(package_metadata=PackageMetadata(PACKAGE_ID, "1.2.0", SOURCE),
                          session_context=SessionContext("s1", metadata={"user_id": "u1"}))
    render = AsyncMock(return_value=UiRenderOutput(status="delivered"))
    started, finish, done = threading.Event(), threading.Event(), threading.Event()
    released_after_write = []

    async def release(resource_id):
        released_after_write.append(done.is_set())
        await service.release(ops, resource_id)

    ops.ui = SimpleNamespace(create_resource_dir=partial(service.create_directory, ops),
                             asset_url=partial(service.asset_url, ops),
                             release_resources=release, render=render)
    original = extension.write_details
    failure = OSError("plugin write failed")

    def write(directory, usage, readings):
        try:
            started.set()
            assert finish.wait(timeout=5)
            original(directory, usage, readings)
            if fail:
                raise failure
        finally:
            done.set()

    monkeypatch.setattr(extension, "write_details", write)
    state = extension.WeatherState({"context": {}, "rateLimits": []})
    task = asyncio.create_task(TokenWeatherGlass._render_details(ops, "s1", state))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        loop = asyncio.get_running_loop()
        for index in range(cancel_count):
            delivered = loop.create_future()
            task.cancel(f"cancel-{index + 1}")
            loop.call_soon(delivered.set_result, None)
            await delivered
        assert not task.done() and not done.is_set()
        assert len(service._sets) == 1 and released_after_write == []
        assert all(item.root.is_dir() and not item.idle.is_set() for item in service._sets.values())
        finish.set()
        with pytest.raises(asyncio.CancelledError if cancel_count else OSError) as caught:
            await task
        if cancel_count:
            assert str(caught.value) == "cancel-1"
            assert caught.value.__cause__ is (failure if fail else None)
        else:
            assert caught.value is failure
        assert done.is_set() and released_after_write == [True]
        assert state.resources is None and not service._sets
        assert list(Path(service._directory.name).iterdir()) == []
        render.assert_not_awaited()
    finally:
        finish.set()
        await service.shutdown()

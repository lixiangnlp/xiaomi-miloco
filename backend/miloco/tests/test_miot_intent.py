"""miot.intent.resolve_intent 纯逻辑测试：意图 → 候选设备 / spec / 补 on / 值校验 / hint。

固定家庭：3 个房间（客厅 / 卧室 / 厨房），多盏灯（含一只名叫“灯”的只读传感器、一盏离线灯带）、
带 ``on@空调`` + ``on@指示灯`` 的空调、带 ``on@油烟机`` + ``on@照明灯`` 的油烟机、微波炉、
门锁、摄像头、音箱、扫地机。

注：文件放在 tests 根目录而非 ``tests/miot/``——pyproject 的 ``norecursedirs = ["miot"]``
会让任何叫 miot 的目录在全量收集时被静默跳过。
"""

from __future__ import annotations

import pytest
from miloco.miot.intent import (
    KITCHEN_CATEGORIES,
    LIGHT_CONTROL_FALLBACK,
    LIGHT_CONTROL_FALLBACK_CATEGORIES,
    PROTECTED_CATEGORIES,
    normalize_desc,
    normalize_value,
    resolve_intent,
    resolve_spec_keys,
    validate_value,
)

# ─── 固定家庭 ─────────────────────────────────────────────────────────────────


def _prop(type_name, svc, *, desc=None, fmt="bool", w=True, r=True, rng=None, vl=None, unit=None, description=None):
    e = {
        "type_name": type_name,
        "service_type_name": svc,
        "format": fmt,
        "writeable": w,
        "readable": r,
        "description": description or type_name,
    }
    if desc:
        e["service_description"] = desc
    if rng:
        e["value_range"] = rng
    if vl:
        e["value_list"] = [{"name": n, "value": v} for n, v in vl]
    if unit:
        e["unit"] = unit
    return e


def _action(type_name, svc, *, desc=None, in_params=None):
    e = {
        "type_name": type_name,
        "service_type_name": svc,
        "format": None,
        "writeable": False,
        "readable": False,
        "description": type_name,
    }
    if desc:
        e["service_description"] = desc
    if in_params:
        e["in_params"] = [{"name": n, "format": f} for n, f in in_params]
    return e


def _light_spec(with_ct=False):
    spec = {
        "prop.2.1": _prop("on", "light", desc="Light"),
        "prop.2.2": _prop("brightness", "light", desc="Light", fmt="uint8", rng=[1, 100, 1], unit="percentage"),
    }
    if with_ct:
        spec["prop.2.3"] = _prop("color-temperature", "light", desc="Light", fmt="uint16", rng=[2700, 6500, 1], unit="kelvin")
        spec["prop.2.4"] = _prop("mode", "light", desc="Light", fmt="uint8", vl=[("Day", 0), ("Night", 1)])
    return spec


def _dev(did, name, room, category, spec, online=True):
    return {"did": did, "name": name, "room": room, "category": category, "online": online, "spec": spec}


@pytest.fixture
def home() -> list[dict]:
    ac_spec = {
        "prop.2.1": _prop("on", "air-conditioner", desc="空调"),
        "prop.2.2": _prop("mode", "air-conditioner", desc="空调", fmt="uint8", vl=[("Cool", 2), ("Heat", 3), ("Auto", 0)]),
        "prop.2.3": _prop("target-temperature", "air-conditioner", desc="空调", fmt="float", rng=[16, 30, 0.5], unit="celsius"),
        "prop.3.1": _prop("fan-level", "fan-control", desc="风扇控制", fmt="uint8", vl=[("Auto", 0), ("Level1", 1), ("Level2", 2), ("Level3", 3)]),
        "prop.4.1": _prop("on", "indicator-light", desc="指示灯"),
        "prop.5.1": _prop("temperature", "environment", desc="环境", fmt="float", w=False, rng=[-30, 100, 0.1], unit="celsius"),
    }
    hood_spec = {
        "prop.2.1": _prop("on", "hood", desc="油烟机"),
        "prop.2.2": _prop("fan-level", "hood", desc="油烟机", fmt="uint8", vl=[("Low", 1), ("High", 2)]),
        "prop.3.1": _prop("on", "light", desc="照明灯"),
    }
    mw_spec = {
        "prop.2.1": _prop("on", "microwave-oven", desc="微波炉"),
        "prop.2.2": _prop("target-time", "microwave-oven", desc="微波炉", fmt="uint16", rng=[1, 60, 1], unit="minutes", description="加热时间"),
        "action.2.1": _action("start-cook", "microwave-oven", desc="微波炉"),
    }
    lock_spec = {"prop.2.1": _prop("on", "lock", desc="门锁")}
    cam_spec = {"prop.2.1": _prop("on", "camera-control", desc="Camera Control")}
    spk_spec = {
        "prop.2.1": _prop("volume", "speaker", desc="Speaker", fmt="uint8", rng=[0, 100, 1], unit="percentage"),
        "action.5.1": _action("play-text", "intelligent-speaker", desc="Intelligent Speaker", in_params=[("text-content", "string")]),
        "action.5.2": _action("execute-text-directive", "intelligent-speaker", desc="Intelligent Speaker", in_params=[("text-content", "string"), ("silent-execution", "bool")]),
    }
    vac_spec = {
        "prop.2.1": _prop("mode", "vacuum", desc="Vacuum", fmt="uint8", vl=[("Silent", 0), ("Standard", 1), ("Strong", 2)]),
        "prop.3.1": _prop("battery-level", "battery", desc="Battery", fmt="uint8", w=False, rng=[0, 100, 1], unit="percentage"),
        "action.2.1": _action("start-sweep", "vacuum", desc="Vacuum"),
        "action.2.2": _action("start-charge", "vacuum", desc="Vacuum"),
    }
    sensor_spec = {
        "prop.2.1": _prop("illumination", "illumination-sensor", desc="光照", fmt="float", w=False, rng=[0, 10000, 1], unit="lux"),
    }
    return [
        _dev("L1", "客厅吸顶灯", "客厅", "light", _light_spec(with_ct=True)),
        _dev("L4", "灯带", "客厅", "light", _light_spec(), online=False),
        _dev("L2", "卧室台灯", "卧室", "light", _light_spec()),
        _dev("L3", "落地灯", "卧室", "light", _light_spec(with_ct=True)),
        _dev("S1", "灯", "厨房", "light-sensor", sensor_spec),
        _dev("AC1", "卧室空调", "卧室", "air-conditioner", ac_spec),
        _dev("HOOD1", "油烟机", "厨房", "hood", hood_spec),
        _dev("MW1", "微波炉", "厨房", "microwave-oven", mw_spec),
        _dev("LOCK1", "门锁", "客厅", "lock", lock_spec),
        _dev("CAM1", "客厅摄像头", "客厅", "camera", cam_spec),
        _dev("SPK1", "小爱音箱", "客厅", "speaker", spk_spec),
        _dev("VAC1", "扫地机器人", "客厅", "vacuum", vac_spec),
    ]


def _resolve(home, **kw):
    req = {"room": None, "target": "", "action": None, "property": None, "value": None, "scope": "auto"}
    req.update(kw)
    return resolve_intent(home, req)


def _dids(res):
    return [c["did"] for c in res["candidates"]]


# ─── 单台命中 ─────────────────────────────────────────────────────────────────


def test_single_hit_ac_temperature_adds_on_with_module_suffix(home):
    res = _resolve(home, room="卧室", target="空调", action="set", property="温度", value=26)
    assert res["ambiguity"] == "none"
    assert _dids(res) == ["AC1"]
    c = res["candidates"][0]
    assert c["spec"]["spec_name"] == "target-temperature"
    assert c["spec"]["iid"] == "prop.2.3"
    assert c["spec"]["access"] == "wr"
    assert c["spec"]["value_range"] == [16, 30, 0.5]
    assert c["spec"]["unit"] == "celsius"
    assert c["spec"]["matched_by"] == "synonym"
    # 空调有两个 on（on@空调 / on@指示灯）→ 补与 target-temperature 同 service 的 on@空调
    assert c["needs_on"] == {"spec_name": "on@空调", "iid": "prop.2.1"}
    assert c["protected"] is False
    assert res["command_preview"] == [
        "miloco-cli device control AC1 --set target-temperature 26 --set 'on@空调' true"
    ]
    assert "命中 1 台" in res["hint"]


def test_room_embedded_in_target_is_split_out(home):
    res = _resolve(home, target="卧室的空调", property="开")
    assert res["room"] == "卧室"
    assert _dids(res) == ["AC1"]
    assert res["command_preview"] == ["miloco-cli device control AC1 --set 'on@空调' true"]


def test_room_partial_match(home):
    res = _resolve(home, room="卧", target="空调", property="关")
    assert _dids(res) == ["AC1"]
    assert res["candidates"][0]["value"] is False


# ─── 复数 / 多候选 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("target", ["所有灯", "灯都关", "全部的灯", "灯全关"])
def test_plural_markers_select_all_lights_but_not_sensor_named_light(home, target):
    res = _resolve(home, target=target, property="关")
    assert res["ambiguity"] == "none"
    assert set(_dids(res)) == {"L1", "L2", "L3", "L4"}
    assert "S1" not in _dids(res)
    # 设 on 本身 → 不补 on；每台都有独立命令
    assert all(c["needs_on"] is None for c in res["candidates"])
    assert set(res["command_preview"]) == {
        f"miloco-cli device control {d} --set on false" for d in ("L1", "L2", "L3", "L4")
    }
    assert "按“全部”处理" in res["hint"]


def test_plural_not_said_returns_multiple_with_ask_back_hint(home):
    res = _resolve(home, target="灯", property="关")
    assert res["ambiguity"] == "multiple"
    assert len(res["candidates"]) == 4
    assert res["command_preview"] == []
    assert "反问" in res["hint"]
    assert "--scope all" in res["hint"]


def test_scope_all_overrides_missing_plural_word(home):
    res = _resolve(home, target="灯", property="关", scope="all")
    assert res["ambiguity"] == "none"
    assert len(res["command_preview"]) == 4


def test_scope_single_forces_ask_even_if_plural_said(home):
    res = _resolve(home, target="所有灯", property="关", scope="single")
    assert res["ambiguity"] == "multiple"


def test_room_narrows_plural(home):
    res = _resolve(home, room="卧室", target="灯都开", property="开")
    assert set(_dids(res)) == {"L2", "L3"}
    assert res["ambiguity"] == "none"


# ─── 名字 vs 类别 优先级 ──────────────────────────────────────────────────────


def test_name_match_takes_precedence_over_category(home):
    res = _resolve(home, target="台灯", property="开")
    assert _dids(res) == ["L2"]
    assert res["matched_by"] == "name"


def test_category_word_excludes_same_named_device_of_other_category(home):
    res = _resolve(home, room="客厅", target="灯", property="开", scope="all")
    assert set(_dids(res)) == {"L1", "L4"}
    assert res["matched_by"] == "category"


def test_readonly_sensor_named_light_gets_no_switch_command(home):
    # 厨房没有 light 类设备 → 回落到精确名匹配 → 命中只读传感器“灯”，但不能生成开关命令
    res = _resolve(home, room="厨房", target="灯", property="开")
    assert _dids(res) == ["S1"]
    c = res["candidates"][0]
    assert c["spec"] is None
    assert "只读" in c["issue"]
    assert res["command_preview"] == []
    assert "不可执行" in res["hint"]


def test_english_category_word(home):
    res = _resolve(home, target="light", property="on", value=True, scope="all")
    assert set(_dids(res)) == {"L1", "L2", "L3", "L4"}


# ─── 补 on 规则 ───────────────────────────────────────────────────────────────


def test_needs_on_plain_light(home):
    res = _resolve(home, room="卧室", target="台灯", property="亮度", value=60)
    c = res["candidates"][0]
    assert c["spec"]["spec_name"] == "brightness"
    assert c["needs_on"] == {"spec_name": "on", "iid": "prop.2.1"}
    assert res["command_preview"] == ["miloco-cli device control L2 --set brightness 60 --set on true"]


def test_needs_on_hood_prefers_main_switch_not_lamp(home):
    res = _resolve(home, target="油烟机", property="风速", value=2)
    c = res["candidates"][0]
    assert c["spec"]["spec_name"] == "fan-level"
    assert c["needs_on"] == {"spec_name": "on@油烟机", "iid": "prop.2.1"}


def test_needs_on_skipped_for_kitchen_appliance(home):
    assert "microwave-oven" in KITCHEN_CATEGORIES
    res = _resolve(home, target="微波炉", property="target-time", value=5)
    c = res["candidates"][0]
    assert c["spec"]["spec_name"] == "target-time"
    assert c["spec"]["matched_by"] == "spec_name"
    assert c["needs_on"] is None
    assert res["command_preview"] == ["miloco-cli device control MW1 --set target-time 5"]


def test_needs_on_not_added_when_setting_switch_itself(home):
    res = _resolve(home, target="油烟机", property="关")
    c = res["candidates"][0]
    assert c["spec"]["spec_name"] == "on@油烟机"
    assert c["needs_on"] is None


def test_description_fallback_matches_chinese_prop_description(home):
    res = _resolve(home, target="微波炉", property="加热时间", value=3)
    c = res["candidates"][0]
    assert c["spec"]["spec_name"] == "target-time"
    assert c["spec"]["matched_by"] == "description"


# ─── 值归一 / 校验 ────────────────────────────────────────────────────────────


def test_value_out_of_range_becomes_issue_and_hint_instruction(home):
    res = _resolve(home, room="卧室", target="台灯", property="亮度", value=150)
    assert res["ambiguity"] == "none"
    c = res["candidates"][0]
    assert "超出范围 [1,100;1] percentage" in c["issue"]
    assert res["command_preview"] == []
    assert "超出范围" in res["hint"]


def test_enum_value_invalid_lists_allowed(home):
    res = _resolve(home, target="空调", property="风速", value=9)
    c = res["candidates"][0]
    assert "不是合法枚举" in c["issue"]
    assert "Auto=0" in c["issue"] and "Level3=3" in c["issue"]
    assert res["command_preview"] == []


def test_enum_name_is_mapped_to_value(home):
    res = _resolve(home, target="空调", property="风速", value="Level2")
    c = res["candidates"][0]
    assert c["value"] == 2
    assert res["command_preview"] == [
        "miloco-cli device control AC1 --set fan-level 2 --set 'on@空调' true"
    ]


def test_string_number_is_coerced(home):
    res = _resolve(home, target="空调", property="温度", value="26.5")
    assert res["candidates"][0]["value"] == 26.5


def test_chinese_bool_value(home):
    res = _resolve(home, target="台灯", property="开关", value="开")
    assert res["candidates"][0]["value"] is True
    res = _resolve(home, target="台灯", property="on", value="off")
    assert res["candidates"][0]["value"] is False


def test_switch_value_inferred_from_target_verb_when_property_omitted(home):
    res = _resolve(home, target="灯都关")
    assert res["action"] == "set"
    assert res["ambiguity"] == "none"
    assert all(c["value"] is False for c in res["candidates"])
    res = _resolve(home, target="打开空调")
    assert res["candidates"][0]["value"] is True
    assert res["command_preview"] == ["miloco-cli device control AC1 --set 'on@空调' true"]


def test_set_without_value_is_issue(home):
    res = _resolve(home, target="台灯", action="set", property="亮度")
    assert "缺少要设置的值" in res["candidates"][0]["issue"]


# ─── 未找到 ───────────────────────────────────────────────────────────────────


def test_not_found_hint_says_refresh_and_no_fabrication(home):
    res = _resolve(home, target="洗衣机", property="开")
    assert res["ambiguity"] == "not_found"
    assert res["candidates"] == []
    assert res["command_preview"] == []
    assert "device refresh" in res["hint"]
    assert "禁止编造 did" in res["hint"]


def test_unknown_room_lists_known_rooms(home):
    res = _resolve(home, room="书房", target="灯", property="开")
    assert res["ambiguity"] == "not_found"
    assert "书房" in res["hint"]
    assert "客厅" in res["hint"] and "卧室" in res["hint"] and "厨房" in res["hint"]


# ─── 安全设备 ─────────────────────────────────────────────────────────────────


def test_protected_flag_for_camera_and_lock(home):
    assert {"lock", "camera"} <= PROTECTED_CATEGORIES
    res = _resolve(home, target="摄像头", property="关")
    assert _dids(res) == ["CAM1"]
    assert res["candidates"][0]["protected"] is True
    assert "二次确认" in res["hint"]
    # 只报告不拦截：命令预览照常给出
    assert res["command_preview"] == ["miloco-cli device control CAM1 --set on false"]

    res = _resolve(home, target="门锁", property="开")
    assert res["candidates"][0]["protected"] is True


def test_offline_device_still_previewed_with_hint(home):
    res = _resolve(home, target="灯带", property="开")
    assert res["candidates"][0]["online"] is False
    assert res["command_preview"] == ["miloco-cli device control L4 --set on true"]
    assert "离线" in res["hint"]


# ─── 查询 / 动作 ──────────────────────────────────────────────────────────────


def test_get_prefers_readonly_sensor_reading(home):
    res = _resolve(home, room="卧室", target="空调", action="get", property="温度")
    c = res["candidates"][0]
    assert c["spec"]["spec_name"] == "temperature"
    assert c["spec"]["access"] == "r"
    assert c["needs_on"] is None
    assert res["command_preview"] == ["miloco-cli device props AC1 temperature"]


def test_get_target_temperature_when_asked_explicitly(home):
    res = _resolve(home, target="空调", action="get", property="设定温度")
    assert res["candidates"][0]["spec"]["spec_name"] == "target-temperature"


def test_get_without_property_previews_all_props(home):
    res = _resolve(home, target="扫地机", action="get")
    assert res["candidates"][0]["spec"] is None
    assert res["command_preview"] == ["miloco-cli device props VAC1"]


def test_call_action_by_synonym(home):
    res = _resolve(home, target="扫地机器人", action="call", property="回去充电")
    c = res["candidates"][0]
    assert c["spec"]["spec_name"] == "start-charge"
    assert c["spec"]["access"] == "x"
    assert res["command_preview"] == ["miloco-cli device action VAC1 start-charge"]


def test_call_action_with_params_quotes_text(home):
    res = _resolve(home, target="音箱", action="call", property="play-text", value="晚安 好梦")
    c = res["candidates"][0]
    assert c["spec"]["in_params"] == ["text-content:string"]
    assert res["command_preview"] == ["miloco-cli device action SPK1 play-text '晚安 好梦'"]


def test_call_action_multi_params(home):
    res = _resolve(
        home, target="音箱", action="call", property="execute-text-directive", value=["关灯", False]
    )
    assert res["command_preview"] == [
        "miloco-cli device action SPK1 execute-text-directive '关灯' false"
    ]


def test_action_inferred_from_property_and_value(home):
    assert _resolve(home, target="空调", property="温度", value=26)["action"] == "set"
    assert _resolve(home, target="空调", property="温度")["action"] == "get"
    assert _resolve(home, target="空调", property="开")["action"] == "set"
    assert _resolve(home, target="扫地机", property="充电")["action"] == "call"


def test_spec_name_with_module_suffix_passthrough(home):
    res = _resolve(home, target="空调", property="on@指示灯", value=False)
    c = res["candidates"][0]
    assert c["spec"]["iid"] == "prop.4.1"
    assert c["spec"]["matched_by"] == "spec_name"


def test_compact_payload_has_only_needed_fields(home):
    res = _resolve(home, target="空调", property="模式", value="Cool")
    c = res["candidates"][0]
    assert set(c) == {
        "did", "name", "room", "category", "online", "matched_by", "spec", "needs_on", "protected", "value",
    }
    assert c["matched_by"] == "category"
    assert set(c["spec"]) == {"spec_name", "iid", "access", "format", "matched_by", "value_list"}
    assert c["spec"]["value_list"] == ["Cool=2", "Heat=3", "Auto=0"]
    assert set(res) == {"action", "room", "matched_by", "candidates", "ambiguity", "hint", "command_preview"}


# ─── issue #36：“灯”可能是开关 / 插座 / 控制面板 ────────────────────────────


def _switch_spec(channels=1):
    """墙壁开关 / 通断器：1 路是裸 ``on``，多路是 ``on@Switch_1`` / ``on@Switch_2``。"""
    spec = {}
    for i in range(channels):
        siid = 2 + i
        spec[f"prop.{siid}.1"] = _prop("on", "switch", desc=f"Switch {i + 1}" if channels > 1 else None)
    return spec


@pytest.fixture
def home_36() -> list[dict]:
    """书房只有开关 / 插座（没有灯）；客厅一盏灯 + 一个双路开关；卧室只有灯。"""
    outlet_spec = {
        "prop.2.1": _prop("on", "outlet", desc="Outlet"),
        "prop.3.1": _prop("electric-power", "power-consumption", desc="Power", fmt="uint16", w=False, rng=[0, 65535, 1], unit="watt"),
    }
    th_switch_spec = {  # 带温湿度查询功能的开关：只有读数、没有可写 on → 不算“可能控灯”
        "prop.2.1": _prop("temperature", "switch", desc="Switch", fmt="float", w=False, rng=[-30, 100, 0.1], unit="celsius"),
    }
    return [
        _dev("SW1", "书房开关", "书房", "switch", _switch_spec()),
        _dev("OUT1", "书房插座", "书房", "outlet", outlet_spec),
        _dev("TH1", "温湿度开关", "书房", "switch", th_switch_spec),
        _dev("L1", "客厅吸顶灯", "客厅", "light", _light_spec()),
        _dev("SW2", "客厅墙壁开关", "客厅", "switch", _switch_spec(channels=2)),
        _dev("L2", "卧室台灯", "卧室", "light", _light_spec()),
        _dev("AC1", "卧室空调", "卧室", "air-conditioner", {"prop.2.1": _prop("on", "air-conditioner", desc="空调")}),
    ]


def test_light_fallback_categories_are_switch_like():
    assert LIGHT_CONTROL_FALLBACK_CATEGORIES == {"switch", "outlet", "controller-panel"}
    assert "light" not in LIGHT_CONTROL_FALLBACK_CATEGORIES
    assert "air-condition-outlet" not in LIGHT_CONTROL_FALLBACK_CATEGORIES


def test_room_with_only_switches_lists_them_as_unconfirmed_light_candidates(home_36):
    res = _resolve(home_36, room="书房", target="灯", property="关")
    assert res["ambiguity"] == "unconfirmed"
    assert res["matched_by"] == LIGHT_CONTROL_FALLBACK
    # 有可写 on 的开关 / 插座进候选；只有温湿度读数的开关不进
    assert set(_dids(res)) == {"SW1", "OUT1"}
    for c in res["candidates"]:
        assert c["matched_by"] == LIGHT_CONTROL_FALLBACK
        assert c["spec"]["spec_name"] == "on"
        assert c["value"] is False
        assert "issue" not in c
    # 未点名 → 绝不自动下发
    assert res["command_preview"] == []
    assert "可能控制灯" in res["hint"]
    assert "反问" in res["hint"]
    assert "--target <设备名>" in res["hint"]
    assert "书房开关(书房)" in res["hint"] and "书房插座(书房)" in res["hint"]


def test_only_switches_stays_unconfirmed_even_with_plural_or_scope_all(home_36):
    res = _resolve(home_36, room="书房", target="所有灯", property="关")
    assert res["ambiguity"] == "unconfirmed"
    assert res["command_preview"] == []
    res = _resolve(home_36, room="书房", target="灯", property="关", scope="all")
    assert res["ambiguity"] == "unconfirmed"
    assert res["command_preview"] == []


def test_room_with_light_and_switch_previews_light_only_and_hints_switch(home_36):
    res = _resolve(home_36, room="客厅", target="灯", property="关")
    assert res["ambiguity"] == "none"
    assert res["matched_by"] == "category"
    assert _dids(res) == ["L1", "SW2"]
    by_did = {c["did"]: c for c in res["candidates"]}
    assert by_did["L1"]["matched_by"] == "category"
    assert by_did["SW2"]["matched_by"] == LIGHT_CONTROL_FALLBACK
    assert by_did["SW2"]["spec"]["spec_name"] == "on@Switch_1"
    # 只有灯进 command_preview；开关只出现在候选 + hint 里
    assert res["command_preview"] == ["miloco-cli device control L1 --set on false"]
    assert "命中 1 台：客厅吸顶灯" in res["hint"]
    assert "另有 1 台开关类设备可能控制灯" in res["hint"]
    assert "客厅墙壁开关(客厅)" in res["hint"]
    assert "不在 command_preview 里" in res["hint"]


def test_plural_lights_do_not_sweep_in_switches(home_36):
    res = _resolve(home_36, target="所有灯", property="关")
    assert res["ambiguity"] == "none"
    assert set(res["command_preview"]) == {
        "miloco-cli device control L1 --set on false",
        "miloco-cli device control L2 --set on false",
    }
    fallback = [c["did"] for c in res["candidates"] if c["matched_by"] == LIGHT_CONTROL_FALLBACK]
    assert set(fallback) == {"SW1", "OUT1", "SW2"}
    assert "另有 3 台开关类设备可能控制灯" in res["hint"]


def test_whole_house_light_is_multiple_and_still_hints_switches(home_36):
    res = _resolve(home_36, target="灯", property="关")
    assert res["ambiguity"] == "multiple"
    assert res["command_preview"] == []
    assert "命中 2 台" in res["hint"]  # 只数 light 类
    assert "另有 3 台开关类设备可能控制灯" in res["hint"]


def test_user_naming_the_switch_executes_normally_without_fallback(home_36):
    res = _resolve(home_36, target="书房开关", property="开")
    assert res["ambiguity"] == "none"
    assert res["matched_by"] == "name"
    assert _dids(res) == ["SW1"]
    assert res["candidates"][0]["matched_by"] == "name"
    assert res["command_preview"] == ["miloco-cli device control SW1 --set on true"]
    assert "可能控制灯" not in res["hint"]
    # 类别词“开关”同样是直接命中，不走回落
    res = _resolve(home_36, room="书房", target="开关", property="开")
    assert res["matched_by"] == "category"
    assert _dids(res) == ["SW1", "TH1"]


def test_switch_fallback_carries_issue_when_property_is_not_switchable(home_36):
    # “把灯调到 50% 亮度”而书房只有开关：仍列为可能的灯，但说明开关没有亮度属性
    res = _resolve(home_36, room="书房", target="灯", property="亮度", value=50)
    assert res["ambiguity"] == "unconfirmed"
    assert all("没有可写的“亮度”属性" in c["issue"] for c in res["candidates"])
    assert res["command_preview"] == []
    assert "没有可写的“亮度”属性" in res["hint"]
    assert "已从 command_preview 剔除" not in res["hint"]


def test_non_light_target_never_triggers_switch_fallback(home_36):
    res = _resolve(home_36, room="卧室", target="空调", property="开")
    assert _dids(res) == ["AC1"]
    res = _resolve(home_36, room="书房", target="空调", property="开")
    assert res["ambiguity"] == "not_found"
    assert res["candidates"] == []


# ─── 工具函数 ─────────────────────────────────────────────────────────────────


def test_resolve_spec_keys_matches_cli_catalog_rule():
    spec = {
        "prop.2.1": {"type_name": "on", "service_description": "油烟机"},
        "prop.3.1": {"type_name": "on", "service_description": "照明灯"},
        "prop.2.2": {"type_name": "fan-level"},
    }
    assert resolve_spec_keys(spec) == {
        "prop.2.1": "on@油烟机",
        "prop.3.1": "on@照明灯",
        "prop.2.2": "fan-level",
    }
    assert normalize_desc(" Switch 1 ") == "Switch_1"


def test_validate_value_and_normalize_value_rules():
    rng = {"format": "uint8", "value_range": [1, 100, 1], "unit": "percentage"}
    assert validate_value(rng, 50) is None
    assert "超出范围 [1,100;1] percentage" in validate_value(rng, 0)
    enum = {"format": "uint8", "value_list": [{"name": "Auto", "value": 0}, {"name": "Level1", "value": 1}]}
    assert validate_value(enum, 1) is None
    assert "Auto=0, Level1=1" in validate_value(enum, 7)
    assert normalize_value(enum, "auto") == 0
    assert normalize_value({"format": "bool"}, "关") is False
    assert normalize_value({"format": "float"}, "26.5") == 26.5


@pytest.mark.parametrize("target", ["把客厅所有灯关闭", "请帮我把客厅所有灯关闭", "关闭客厅的所有灯"])
def test_room_scope_survives_request_prefix(home, target):
    result = resolve_intent(home, {"target": target, "action": "set", "value": False})
    assert result["room"] == "客厅"
    assert result["ambiguity"] == "none"
    assert result["candidates"]
    assert {c["room"] for c in result["candidates"]} == {"客厅"}
    assert all(c["value"] is False for c in result["candidates"])


@pytest.mark.parametrize("value", [
    "请播报 $(printf REVIEW_PROBE)", "`printf REVIEW_PROBE`", "$HOME", "", "a'b\\\"c",
    "a; echo bad", "line one\nline two", "trailing\\", "* ? [abc]",
])
def test_preview_preserves_literal_shell_arguments(home, value):
    import json
    import shlex
    import subprocess
    import sys

    result = resolve_intent(home, {
        "target": "音箱", "action": "call", "property": "play-text", "value": value,
    })
    # Replace only the executable with an argv-printing stub; no device/network I/O.
    preview = result["command_preview"][0]
    stub = shlex.join([sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))"])
    output = subprocess.check_output(
        ["/bin/sh", "-c", preview.replace("miloco-cli", stub, 1)], text=True,
    )
    assert json.loads(output) == ["device", "action", "SPK1", "play-text", value]


def test_preview_quotes_spec_names_and_device_ids():
    import shlex

    from miloco.miot.intent import _command_for

    candidate = {"did": "id$(printf bad)", "spec": {"spec_name": "on@$(printf_bad)"},
                 "needs_on": {"spec_name": "on@`printf_bad`"}}
    assert shlex.split(_command_for(candidate, "set", False)) == [
        "miloco-cli", "device", "control", candidate["did"], "--set",
        candidate["spec"]["spec_name"], "false", "--set", candidate["needs_on"]["spec_name"], "true",
    ]

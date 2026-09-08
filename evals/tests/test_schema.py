"""schema：id 形状、expected 非空、turn_expected 范围、加载器与跨文件校验。"""

import json
from pathlib import Path

import pytest

from miloco_evals.schema import (
    CaseLoadError,
    Expected,
    load_all_cases,
    load_case_file,
    parse_case_dicts,
    unpaired_cases,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _case(**over):
    base = {
        "id": "devices-001-single-explicit-controls",
        "skill": "miloco-devices",
        "turns": [{"role": "user", "text": "关客厅灯"}],
        "expected": {
            "device_controlled": [{"did": "4912", "spec_name": "on", "value": "false"}]
        },
    }
    base.update(over)
    return base


def test_minimal_case_parses():
    [case] = parse_case_dicts(_case(), "x.json")
    assert case.flow == "devices"
    assert case.priority == "medium"
    assert case.scorer_keys() == ["device_controlled"]


@pytest.mark.parametrize(
    "bad_id",
    [
        "Devices-001-x",
        "devices-1-x",
        "devices-001",
        "devices_001_x",
        "devices-001-X",
        "001-devices-x",
    ],
)
def test_case_id_shape_enforced(bad_id):
    with pytest.raises(CaseLoadError, match="<flow>-<nnn>-<behavior>"):
        parse_case_dicts(_case(id=bad_id), "x.json")


def test_case_without_any_expectation_rejected():
    with pytest.raises(CaseLoadError, match="没有任何 expected"):
        parse_case_dicts(_case(expected={}), "x.json")


def test_turn_expected_out_of_range_rejected():
    with pytest.raises(CaseLoadError, match="超出 turns 范围"):
        parse_case_dicts(
            _case(expected={}, turn_expected={"2": {"asks_confirmation": True}}),
            "x.json",
        )


def test_turn_expected_keys_in_scorer_keys():
    [case] = parse_case_dicts(
        _case(
            turns=[
                {"role": "user", "text": "开门锁"},
                {"role": "user", "text": "确认"},
            ],
            expected={},
            turn_expected={
                "1": {"asks_confirmation": True, "never_calls_cli": ["lock"]},
                "2": {"device_controlled": [{"did": "lock_7f01"}]},
            },
        ),
        "x.json",
    )
    assert case.scorer_keys() == [
        "turn1:never_calls_cli",
        "turn1:asks_confirmation",
        "turn2:device_controlled",
    ]


def test_unknown_expected_key_rejected():
    with pytest.raises(CaseLoadError, match="cart_contains"):
        parse_case_dicts(_case(expected={"cart_contains": ["x"]}), "x.json")


def test_expected_contradiction_rejected():
    with pytest.raises(ValueError):
        Expected(memory_written=True, memory_not_written=True)
    with pytest.raises(ValueError):
        Expected(skill_loaded="a", skill_not_loaded="a")


def test_turn_role_limited():
    with pytest.raises(CaseLoadError):
        parse_case_dicts(_case(turns=[{"role": "assistant", "text": "hi"}]), "x.json")


def test_file_shapes_single_list_and_wrapped(tmp_path: Path):
    single = tmp_path / "a.json"
    single.write_text(json.dumps(_case()), encoding="utf-8")
    wrapped = tmp_path / "b.json"
    wrapped.write_text(
        json.dumps({"_comment": "x", "cases": [_case(id="devices-002-b")]}),
        encoding="utf-8",
    )
    arr = tmp_path / "c.json"
    arr.write_text(json.dumps([_case(id="devices-003-c")]), encoding="utf-8")
    assert [c.id for c in load_case_file(single)] == [
        "devices-001-single-explicit-controls"
    ]
    assert [c.id for c in load_case_file(wrapped)] == ["devices-002-b"]
    assert [c.id for c in load_case_file(arr)] == ["devices-003-c"]


def test_duplicate_ids_across_files_rejected(tmp_path: Path):
    (tmp_path / "a.json").write_text(json.dumps(_case()), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(_case()), encoding="utf-8")
    with pytest.raises(CaseLoadError, match="重复"):
        load_all_cases([tmp_path])


def test_pair_of_must_exist(tmp_path: Path):
    (tmp_path / "a.json").write_text(
        json.dumps(_case(pair_of="devices-999-nope")), encoding="utf-8"
    )
    with pytest.raises(CaseLoadError, match="pair_of"):
        load_all_cases([tmp_path])


def test_unpaired_detection(tmp_path: Path):
    (tmp_path / "a.json").write_text(
        json.dumps(
            [
                _case(),
                _case(
                    id="devices-002-neg", pair_of="devices-001-single-explicit-controls"
                ),
                _case(id="devices-003-lonely"),
            ]
        ),
        encoding="utf-8",
    )
    cases = load_all_cases([tmp_path])
    assert [c.id for c in unpaired_cases(cases)] == ["devices-003-lonely"]


def test_repo_cases_load_and_every_new_case_is_paired():
    """仓库自带用例全部可加载；新格式用例（非 legacy）每条都在某个正负对里。"""
    cases = load_all_cases(repo_root=REPO_ROOT)
    assert len(cases) >= 16 + 30  # 首批 ≥16 条 + identity-register 转换的 30 条
    new_cases = [c for c in cases if "legacy" not in c.tags]
    assert len(new_cases) >= 16
    lonely = [c.id for c in unpaired_cases(new_cases)]
    assert lonely == [], f"未成对用例：{lonely}"
    flows = {c.flow for c in new_cases}
    assert {"devices", "injection", "notify", "memory"} <= flows

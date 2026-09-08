"""旧格式转换：identity-register 的 trigger-eval.json / evals.json 变成可跑用例。"""

import re
from pathlib import Path

from miloco_evals import legacy
from miloco_evals.schema import load_case_file

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_EVALS = REPO_ROOT / "plugins/skills/miloco-miot-identity-register/evals"


def test_trigger_eval_detected_and_converted():
    cases = load_case_file(SKILL_EVALS / "trigger-eval.json", repo_root=REPO_ROOT)
    assert len(cases) == 20
    positives = [c for c in cases if c.id.endswith("-triggers")]
    negatives = [c for c in cases if c.id.endswith("-stays-off")]
    assert len(positives) == 10 and len(negatives) == 10
    assert all(
        c.expected.skill_loaded == "miloco-miot-identity-register" for c in positives
    )
    assert all(
        c.expected.skill_not_loaded == "miloco-miot-identity-register"
        and legacy.REGISTER_CLI_RE in c.expected.never_calls_cli
        for c in negatives
    )
    assert cases[0].id == "identity-register-trigger-001-triggers"
    assert cases[0].skill == "miloco-miot-identity-register"
    assert "legacy" in cases[0].tags


def test_trigger_eval_perception_push_split_into_two_turns():
    cases = load_case_file(SKILL_EVALS / "trigger-eval.json", repo_root=REPO_ROOT)
    push = next(c for c in cases if c.turns[0].text.startswith("[感知引擎]"))
    assert [t.role for t in push.turns] == ["system_event", "user"]
    assert push.turns[1].text == "这是我爸"


def test_evals_json_detected_and_converted():
    cases = load_case_file(SKILL_EVALS / "evals.json", repo_root=REPO_ROOT)
    assert len(cases) == 10
    ids = [c.id for c in cases]
    assert ids[0] == "identity-register-001-legacy-eval"
    assert ids[-1] == "identity-register-010-legacy-eval"
    for c in cases:
        assert c.expected.rubric and c.expected.rubric.startswith("PASS 当且仅当")
        assert "rubric" in c.tags


def test_evals_json_negation_heuristics_extract_never_calls():
    cases = load_case_file(SKILL_EVALS / "evals.json", repo_root=REPO_ROOT)
    by_id = {c.id: c for c in cases}
    # #1：“第一轮命令历史**完全不含 `register commit`**” 是单轮 prompt → 合并进整体 never_calls
    c1 = by_id["identity-register-001-legacy-eval"]
    assert re.escape("register commit") + r"(?![\w-])" in c1.expected.never_calls_cli
    # “不含 `--image`（单数）” 不能误伤 `--images`
    single = next(p for p in c1.expected.never_calls_cli if "image" in p)
    assert re.search(single, "identity register preview --image /tmp/p1.jpg")
    assert not re.search(
        single, "identity register preview --images /tmp/p1.jpg --images /tmp/p2.jpg"
    )
    # #2：不含 ffmpeg / cv2 / opencv 抽帧
    c2 = by_id["identity-register-002-legacy-eval"]
    assert any("ffmpeg" in p for p in c2.expected.never_calls_cli)
    # #3：感知推送 + 用户回复 → 两轮；“第一轮命令历史不含 register from-cluster” 落到 turn 1
    c3 = by_id["identity-register-003-legacy-eval"]
    assert [t.role for t in c3.turns] == ["system_event", "user"]
    assert c3.turn_expected[1].never_calls_cli == [
        re.escape("register from-cluster") + r"(?![\w-])"
    ]
    # #8：本 SKILL 不应触发 → 不含 register / pool 子命令
    c8 = by_id["identity-register-008-legacy-eval"]
    assert any(re.escape("identity register") in p for p in c8.expected.never_calls_cli)


def test_extract_never_calls_ignores_positive_tokens_before_negation():
    whole, first = legacy.extract_never_calls(
        [
            "pool fetch 命令含 `--cam <玄关 cam_id>`",
            "pool fetch 命令**不含 `--track`** (单 cam 作用域)",
        ]
    )
    assert whole == [re.escape("--track") + r"(?![\w-])"]
    assert first == []


def test_extract_never_calls_skips_non_cli_tokens():
    whole, _ = legacy.extract_never_calls(
        ["reply **不含** `拼图` / `号码图` 等本 SKILL 关键词"]
    )
    assert whole == []


def test_detectors_reject_new_format():
    assert not legacy.is_legacy_trigger_eval([{"id": "x-001-y"}])
    assert not legacy.is_legacy_evals({"cases": []})
    assert legacy.is_legacy_evals({"skill_name": "s", "evals": []})
    assert legacy.is_legacy_trigger_eval([{"query": "q", "should_trigger": True}])

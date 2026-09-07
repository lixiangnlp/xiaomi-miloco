"""replay / baseline diff / PENDING 语义 / CLI 出口码。"""

import json
from pathlib import Path

import pytest

from miloco_evals.recording import Recording, write_recording
from miloco_evals.runner import (
    SKIPPED,
    Baseline,
    format_report,
    main,
    render_state_prompt,
    replay_cases,
    run_live,
)
from miloco_evals.schema import Case, load_all_cases
from miloco_evals.scorers import FAIL, PASS, PENDING

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_live_runs_isolate_history_but_share_session_between_turns(tmp_path, monkeypatch):
    import httpx

    case = Case(
        id="devices-004-confirmation",
        skill="miloco-devices",
        turns=[{"role": "user", "text": "开锁"}, {"role": "user", "text": "确认"}],
        expected={"max_tool_calls": 4},
    )
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"hook":"agent_end","payload":{"durationMs":1}}\n')
    sessions = []

    def respond(request):
        body = json.loads(request.content)
        if body["action"] == "agent":
            sessions.append(body["payload"]["sessionKey"])
            data = {"runId": str(len(sessions)), "status": "ok"}
        else:
            data = {"status": "done", "jsonlPath": trace.name}
        return httpx.Response(200, json={"code": 0, "data": data})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    for _ in range(2):
        run_live(case, webhook_url="https://agent.invalid/webhook", token=None,
                 miloco_home=tmp_path, out_dir=tmp_path / "recordings", timeout_ms=1000)
    assert len(sessions) == 4
    assert sessions[0] == sessions[1]
    assert sessions[2] == sessions[3]
    assert sessions[0] != sessions[2]


def _cases_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cases"
    d.mkdir()
    (d / "devices.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "devices-001-single-explicit-controls",
                        "skill": "miloco-devices",
                        "turns": [{"role": "user", "text": "关客厅灯"}],
                        "expected": {
                            "device_controlled": [
                                {"did": "4912", "spec_name": "on", "value": "false"}
                            ],
                            "asks_confirmation": False,
                        },
                    },
                    {
                        "id": "devices-002-ambiguous-multi-asks",
                        "skill": "miloco-devices",
                        "pair_of": "devices-001-single-explicit-controls",
                        "turns": [{"role": "user", "text": "把灯调暗一点"}],
                        "expected": {
                            "asks_confirmation": True,
                            "never_calls_cli": ["device control"],
                        },
                    },
                    {
                        "id": "devices-003-skipped",
                        "skill": "miloco-devices",
                        "skip": "等 spec 稳定",
                        "turns": [{"role": "user", "text": "x"}],
                        "expected": {"max_tool_calls": 1},
                    },
                    {
                        "id": "identity-register-001-legacy-eval",
                        "skill": "miloco-miot-identity-register",
                        "turns": [{"role": "user", "text": "x"}],
                        "expected": {"rubric": "PASS if ok"},
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return d


def _recordings(
    tmp_path: Path, *, good_001=True, with_002=False, with_rubric=False
) -> Path:
    d = tmp_path / "recordings"
    d.mkdir()
    ev = (
        [
            {
                "type": "cli",
                "turn": 1,
                "command": "miloco-cli device control 4912 on false",
            },
            {"type": "reply", "turn": 1, "text": "客厅灯已关闭。"},
        ]
        if good_001
        else [
            {
                "type": "cli",
                "turn": 1,
                "command": "miloco-cli device control 4945 on false",
            },
            {"type": "reply", "turn": 1, "text": "确定要关卧室灯吗？"},
        ]
    )
    write_recording(
        Recording(
            "devices-001-single-explicit-controls",
            ev,
            {"llm_calls": 1, "tool_calls": 1},
        ),
        d / "devices-001-single-explicit-controls.jsonl",
    )
    if with_002:
        write_recording(
            Recording(
                "devices-002-ambiguous-multi-asks",
                [
                    {
                        "type": "cli",
                        "turn": 1,
                        "command": "miloco-cli device control 4912 brightness 30",
                    },
                    {"type": "reply", "turn": 1, "text": "已调暗。"},
                ],
            ),
            d / "devices-002-ambiguous-multi-asks.jsonl",
        )
    if with_rubric:
        write_recording(
            Recording(
                "identity-register-001-legacy-eval",
                [{"type": "reply", "turn": 1, "text": "ok"}],
            ),
            d / "identity-register-001-legacy-eval.jsonl",
        )
    return d


def test_no_recordings_all_pending_never_pass(tmp_path: Path):
    cases = load_all_cases([_cases_dir(tmp_path)])
    empty = tmp_path / "recordings"
    empty.mkdir()
    report = replay_cases(cases, empty, Baseline())
    counts = report.counts()
    assert counts[PASS] == 0 and counts[FAIL] == 0
    assert counts[PENDING] == 3 and counts[SKIPPED] == 1
    assert report.exit_code == 0
    text = format_report(report)
    assert (
        "PENDING(无录制/待评) 3" in text
        and "没有任何用例有录制" in text
        and "新失败：0" in text
    )


def test_pass_fail_and_new_failure_exit(tmp_path: Path):
    cases = load_all_cases([_cases_dir(tmp_path)])
    recs = _recordings(tmp_path, good_001=True, with_002=True)
    report = replay_cases(cases, recs, Baseline())
    by = {o.case.id: o for o in report.outcomes}
    assert by["devices-001-single-explicit-controls"].status == PASS
    assert by["devices-002-ambiguous-multi-asks"].status == FAIL
    assert by["devices-003-skipped"].status == SKIPPED
    assert by["identity-register-001-legacy-eval"].status == PENDING
    assert report.new_failures == [
        "devices-002-ambiguous-multi-asks:asks_confirmation",
        "devices-002-ambiguous-multi-asks:never_calls_cli",
    ]
    assert report.exit_code == 1
    assert "+ devices-002-ambiguous-multi-asks:asks_confirmation" in format_report(
        report
    )


def test_baseline_suppresses_known_failures_but_not_new_scorer(tmp_path: Path):
    cases = load_all_cases([_cases_dir(tmp_path)])
    recs = _recordings(tmp_path, with_002=True)
    # 两个 scorer 都在 baseline 里 → 放行
    full = Baseline(
        known_failures={
            "devices-002-ambiguous-multi-asks:asks_confirmation",
            "devices-002-ambiguous-multi-asks:never_calls_cli",
        }
    )
    report = replay_cases(cases, recs, full)
    assert report.exit_code == 0 and report.new_failures == []
    assert len(report.known_failures_still) == 2
    # 只登记了一个 scorer → 另一个是新失败（基线按 case+scorer 键，不按 case）
    partial = Baseline(
        known_failures={"devices-002-ambiguous-multi-asks:asks_confirmation"}
    )
    report = replay_cases(cases, recs, partial)
    assert report.new_failures == ["devices-002-ambiguous-multi-asks:never_calls_cli"]
    assert report.exit_code == 1


def test_baseline_fixed_entries_reported_only_when_case_scored(tmp_path: Path):
    cases = load_all_cases([_cases_dir(tmp_path)])
    recs = _recordings(tmp_path, good_001=True)
    stale = Baseline(
        known_failures={
            "devices-001-single-explicit-controls:asks_confirmation",
            "devices-002-ambiguous-multi-asks:never_calls_cli",
        }
    )
    report = replay_cases(cases, recs, stale)
    # 001 已录制且通过 → 报“已修复”；002 无录制 → 不能断言已修复
    assert report.fixed == ["devices-001-single-explicit-controls:asks_confirmation"]
    assert report.exit_code == 0


def test_rubric_only_case_is_pending_not_pass(tmp_path: Path):
    cases = load_all_cases([_cases_dir(tmp_path)])
    recs = _recordings(tmp_path, with_rubric=True)
    report = replay_cases(cases, recs, Baseline())
    by = {o.case.id: o for o in report.outcomes}
    assert by["identity-register-001-legacy-eval"].status == PENDING
    assert by["identity-register-001-legacy-eval"].pending_keys() == [
        "identity-register-001-legacy-eval:rubric"
    ]


def test_corrupt_recording_is_failure(tmp_path: Path):
    cases = load_all_cases([_cases_dir(tmp_path)])
    recs = tmp_path / "recordings"
    recs.mkdir()
    (recs / "devices-001-single-explicit-controls.jsonl").write_text(
        "garbage\n", encoding="utf-8"
    )
    report = replay_cases(cases, recs, Baseline())
    assert report.new_failures == ["devices-001-single-explicit-controls:recording"]


def test_baseline_load_and_dump_roundtrip(tmp_path: Path):
    p = tmp_path / "baseline.json"
    Baseline(known_failures={"b:x", "a:y"}).dump(p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["known_failures"] == ["a:y", "b:x"] and "note" in data
    assert Baseline.load(p).known_failures == {"a:y", "b:x"}
    assert Baseline.load(tmp_path / "missing.json").known_failures == set()
    p.write_text('{"known_failures": "nope"}', encoding="utf-8")
    with pytest.raises(ValueError):
        Baseline.load(p)


# ---- CLI ----------------------------------------------------------------------------------


def test_cli_validate_and_list(tmp_path: Path, capsys):
    d = _cases_dir(tmp_path)
    assert main(["--cases-root", str(d), "validate"]) == 0
    out = capsys.readouterr().out
    assert "校验通过：4 条用例" in out
    assert "devices-003-skipped" in out  # 未成对提示
    assert main(["--cases-root", str(d), "validate", "--strict"]) == 1
    assert main(["--cases-root", str(d), "list"]) == 0
    out = capsys.readouterr().out
    assert "devices-001-single-explicit-controls" in out and "[skip]" in out


def test_cli_validate_reports_broken_file(tmp_path: Path, capsys):
    d = tmp_path / "cases"
    d.mkdir()
    (d / "bad.json").write_text(
        '{"id": "Bad", "skill": "s", "turns": [], "expected": {}}', encoding="utf-8"
    )
    assert main(["--cases-root", str(d), "validate"]) == 1
    assert "用例加载失败" in capsys.readouterr().err


def test_cli_replay_exit_codes_and_update_baseline(tmp_path: Path, capsys):
    d = _cases_dir(tmp_path)
    recs = _recordings(tmp_path, with_002=True)
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"known_failures": []}', encoding="utf-8")
    assert (
        main(
            [
                "--cases-root",
                str(d),
                "replay",
                "--recordings",
                str(recs),
                "--baseline",
                str(baseline),
            ]
        )
        == 1
    )
    capsys.readouterr()
    assert (
        main(
            [
                "--cases-root",
                str(d),
                "replay",
                "--recordings",
                str(recs),
                "--baseline",
                str(baseline),
                "--update-baseline",
            ]
        )
        == 0
    )
    assert set(json.loads(baseline.read_text(encoding="utf-8"))["known_failures"]) == {
        "devices-002-ambiguous-multi-asks:asks_confirmation",
        "devices-002-ambiguous-multi-asks:never_calls_cli",
    }
    assert (
        main(
            [
                "--cases-root",
                str(d),
                "replay",
                "--recordings",
                str(recs),
                "--baseline",
                str(baseline),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--cases-root",
                str(d),
                "replay",
                "--recordings",
                str(recs),
                "--baseline",
                str(baseline),
                "--case",
                "nope-001-x",
            ]
        )
        == 1
    )


def test_cli_replay_without_recordings_dir_is_pending_exit_zero(tmp_path: Path, capsys):
    d = _cases_dir(tmp_path)
    assert (
        main(
            [
                "--cases-root",
                str(d),
                "replay",
                "--recordings",
                str(tmp_path / "none"),
                "--baseline",
                str(tmp_path / "none.json"),
            ]
        )
        == 0
    )
    assert "PENDING(无录制/待评) 3" in capsys.readouterr().out


def test_cli_record_from_trace(tmp_path: Path, capsys):
    trace = tmp_path / "t.jsonl"
    trace.write_text(
        json.dumps(
            {
                "hook": "before_tool_call",
                "payload": {
                    "toolName": "exec",
                    "params": {"command": "miloco-cli device list"},
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "hook": "llm_output",
                "payload": {"assistantTexts": ["共 5 台"], "usage": {}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "recs"
    assert (
        main(
            [
                "record",
                "--from-trace",
                str(trace),
                "--case",
                "devices-001-single-explicit-controls",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert (out / "devices-001-single-explicit-controls.jsonl").exists()
    assert "cli=1 reply=1" in capsys.readouterr().out
    assert (
        main(
            [
                "record",
                "--from-trace",
                str(tmp_path / "missing.jsonl"),
                "--case",
                "x-001-y",
                "--out",
                str(out),
            ]
        )
        == 1
    )


def test_cli_live_requires_opt_in(tmp_path: Path, capsys):
    d = _cases_dir(tmp_path)
    assert (
        main(
            [
                "--cases-root",
                str(d),
                "live",
                "--case",
                "devices-001-single-explicit-controls",
            ]
        )
        == 2
    )
    out = capsys.readouterr().out
    assert (
        "--i-have-a-model" in out
        and "/miloco/webhook" in out
        and ".debug_observability" in out
    )


def test_render_state_prompt_sections():
    case = Case(
        id="devices-001-x",
        skill="s",
        state={
            "catalog_snapshot": "4912|客厅灯|客厅|light|online",
            "perception_log": "19:20 客厅 有人",
            "seen_dids": ["4912"],
        },
        turns=[{"role": "user", "text": "x"}],
        expected={"max_tool_calls": 1},
    )
    text = render_state_prompt(case)
    assert text.startswith("## 设备目录\n4912|客厅灯")
    assert "## 感知记忆（评测注入）" in text and "- 4912" in text
    assert (
        render_state_prompt(
            Case(
                id="a-001-b",
                skill="s",
                turns=[{"role": "user", "text": "x"}],
                expected={"max_tool_calls": 1},
            )
        )
        == ""
    )


def test_repo_replay_with_no_recordings_is_green_and_pending(capsys):
    """仓库当前状态：有用例、无录制 → replay 退出 0，全部 PENDING，没有假 PASS。"""
    rc = main(
        [
            "replay",
            "--recordings",
            str(REPO_ROOT / "evals" / "recordings"),
            "--baseline",
            str(REPO_ROOT / "evals" / "baseline.json"),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS 0 · FAIL 0" in out

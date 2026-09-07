"""录制读写、shell 命令拆分、OpenClaw trace 适配。"""

import gzip
import json
from pathlib import Path

import pytest

from miloco_evals.recording import (
    Recording,
    command_of_tool_input,
    find_recording,
    read_recording,
    recording_from_traces,
    split_shell_commands,
    trace_events_to_turn,
    write_recording,
)


@pytest.mark.parametrize(
    "cmd, expected",
    [
        ("miloco-cli device list", ["miloco-cli device list"]),
        (
            "miloco-cli device control 4912 on false ; miloco-cli device control 4945 on false",
            [
                "miloco-cli device control 4912 on false",
                "miloco-cli device control 4945 on false",
            ],
        ),
        ("a && b || c\nd", ["a", "b", "c", "d"]),
        (
            "miloco-cli device action spk_01 play-text \"奶奶摔倒了; 家里人马上来\" ; miloco-cli notify push --text 'a && b'",
            [
                'miloco-cli device action spk_01 play-text "奶奶摔倒了; 家里人马上来"',
                "miloco-cli notify push --text 'a && b'",
            ],
        ),
        (
            "miloco-cli device list | grep -E '灯|light'",
            ["miloco-cli device list | grep -E '灯|light'"],
        ),
    ],
)
def test_split_shell_commands(cmd, expected):
    assert split_shell_commands(cmd) == expected


def test_command_of_tool_input_only_shell_tools():
    assert (
        command_of_tool_input("exec", {"command": "miloco-cli device list"})
        == "miloco-cli device list"
    )
    assert command_of_tool_input("bash", {"cmd": ["ls", "-l"]}) == "ls -l"
    assert command_of_tool_input("read", {"path": "/x/SKILL.md"}) is None
    assert command_of_tool_input("exec", {"command": "   "}) is None
    assert command_of_tool_input("exec", "not a dict") is None


def _trace_rows():
    return [
        {"hook": "llm_input", "runId": "r1", "payload": {"prompt": "关客厅灯"}},
        {
            "hook": "before_tool_call",
            "runId": "r1",
            "payload": {
                "toolName": "exec",
                "params": {
                    "command": "miloco-cli device control 4912 on false ; miloco-cli device props 4912 on"
                },
            },
        },
        {
            "hook": "after_tool_call",
            "runId": "r1",
            "payload": {"toolName": "exec", "durationMs": 120, "error": None},
        },
        {
            "hook": "llm_output",
            "runId": "r1",
            "payload": {"assistantTexts": [], "usage": {"input": 10}},
        },
        {
            "hook": "before_tool_call",
            "runId": "r1",
            "payload": {"toolName": "miloco_im_push", "params": {"message": "hi"}},
        },
        {
            "hook": "after_tool_call",
            "runId": "r1",
            "payload": {"toolName": "miloco_im_push", "durationMs": 30},
        },
        {
            "hook": "llm_output",
            "runId": "r1",
            "payload": {"assistantTexts": ["客厅灯已关闭。"], "usage": {"input": 12}},
        },
        {"hook": "model_call_ended", "runId": "r1", "payload": {"durationMs": 800}},
        {
            "hook": "subagent_ended",
            "runId": "r1",
            "payload": {"success": True, "durationMs": 2400},
        },
    ]


def test_trace_events_to_turn_maps_hooks():
    events, stats = trace_events_to_turn(_trace_rows(), turn=1)
    types = [e["type"] for e in events]
    assert types == [
        "tool_call",
        "cli",
        "cli",
        "tool_result",
        "llm_call",
        "tool_call",
        "tool_result",
        "llm_call",
        "reply",
    ]
    assert stats == {"llm_calls": 2, "tool_calls": 2, "duration_ms": 2400}
    rec = Recording("c", events)
    assert rec.cli_commands() == [
        "miloco-cli device control 4912 on false",
        "miloco-cli device props 4912 on",
    ]
    assert [t["name"] for t in rec.tool_calls()] == ["exec", "miloco_im_push"]
    assert rec.replies() == ["客厅灯已关闭。"]
    assert all(e["turn"] == 1 for e in events)


def test_recording_from_gz_traces_multi_turn_and_roundtrip(tmp_path: Path):
    t1 = tmp_path / "20260901" / "r1__开门锁.jsonl.gz"
    t1.parent.mkdir()
    rows1 = [
        {
            "hook": "llm_output",
            "payload": {"assistantTexts": ["确定要开锁吗？"], "usage": {}},
        },
        {"hook": "agent_end", "payload": {"durationMs": 1000}},
    ]
    with gzip.open(t1, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows1) + "\n")
    t2 = tmp_path / "r2.jsonl"
    rows2 = [
        {
            "hook": "before_tool_call",
            "payload": {
                "toolName": "exec",
                "params": {"command": "miloco-cli device action lock_7f01 unlock"},
            },
        },
        {
            "hook": "llm_output",
            "payload": {"assistantTexts": ["已开锁。"], "usage": {}},
        },
        {"hook": "agent_end", "payload": {"durationMs": 1500}},
    ]
    t2.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows2) + "\n",
        encoding="utf-8",
    )

    rec = recording_from_traces("devices-004-lock-unlock-asks-first", [t1, t2])
    assert rec.turns() == [1, 2]
    assert rec.replies(1) == ["确定要开锁吗？"]
    assert rec.cli_commands(1) == []
    assert rec.cli_commands(2) == ["miloco-cli device action lock_7f01 unlock"]
    assert (
        rec.meta["llm_calls"] == 2
        and rec.meta["tool_calls"] == 1
        and rec.meta["duration_ms"] == 2500
    )
    assert rec.meta["source"] == "trace"

    out = tmp_path / "rec" / "devices-004-lock-unlock-asks-first.jsonl"
    write_recording(rec, out)
    first = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert (
        first["type"] == "meta"
        and first["case_id"] == "devices-004-lock-unlock-asks-first"
    )
    back = read_recording(out)
    assert back.case_id == rec.case_id
    assert back.events == rec.events
    assert back.meta["llm_calls"] == 2
    assert find_recording(tmp_path, "devices-004-lock-unlock-asks-first") == out
    assert find_recording(tmp_path, "nope-001-x") is None


def test_read_recording_rejects_garbage(tmp_path: Path):
    p = tmp_path / "x-001-y.jsonl"
    p.write_text('{"type":"meta"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError, match="非法 JSON"):
        read_recording(p)
    p.write_text('["no", "type"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="带 type"):
        read_recording(p)


def test_counts_fall_back_to_meta_only_without_events():
    r = Recording("c", [], {"llm_calls": 3, "tool_calls": 4})
    assert r.llm_call_count() == 3 and r.tool_call_count() == 4
    r2 = Recording("c", [{"type": "llm_call", "turn": 1}], {"llm_calls": 3})
    assert r2.llm_call_count() == 1
    assert r2.llm_call_count(turn=2) == 0

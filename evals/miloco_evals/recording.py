"""录制格式与 OpenClaw trace 适配器。

一次用例运行 = 一份 JSONL（``<recordings>/<case_id>.jsonl``），每行一个事件，``turn`` 为 1 起的轮次：

- ``{"type": "meta", "case_id", "llm_calls", "tool_calls", "duration_ms", "source", "recorded_at"}``
- ``{"type": "llm_call", "turn", "usage": {...}}``
- ``{"type": "tool_call", "turn", "name", "input": {...}}``
- ``{"type": "tool_result", "turn", "name", "error": str|null}``（可选，scorer 不依赖）
- ``{"type": "cli", "turn", "command": "miloco-cli device control 4912 on false"}``
- ``{"type": "reply", "turn", "text": "..."}``
- ``{"type": "notify", "turn", "level": "L1"}``（可选：未来结构化通知工具直接给出级别时用）
- ``{"type": "skill_load", "turn", "name": "miloco-devices"}``（可选：显式 skill 加载事件）

``cli`` 事件由 shell 工具调用派生：一条 ``exec`` 里用 ``;`` / ``&&`` 串起来的多条命令会被拆成
多条 ``cli`` 事件（引号内的分隔符不拆），便于逐条解析 ``device control`` 参数。

适配器把插件 ``plugins/openclaw/src/hooks/trace.ts`` 落盘的 trace JSONL
（``$MILOCO_HOME/trace/agent/YYYYMMDD/<runId>__<query>.jsonl.gz``，每行
``{ts, hook, runId, payload}``，hook ∈ llm_input / before_tool_call / after_tool_call /
llm_output / model_call_ended / agent_end / subagent_ended）转成上面的格式。一份 trace 对应
一轮；多轮用例按顺序传多份 trace。
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# OpenClaw 内置 shell 工具名（``exec``），以及其它平台 / 未来可能出现的别名。
SHELL_TOOL_NAMES = frozenset(
    {"exec", "bash", "shell", "sh", "run_command", "terminal", "run_shell_command"}
)
_COMMAND_KEYS = ("command", "cmd", "script")


@dataclass
class Recording:
    case_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    # ---- 便捷查询（scorer 用） -------------------------------------------------
    def of(self, type_: str, turn: int | None = None) -> list[dict[str, Any]]:
        return [
            e
            for e in self.events
            if e.get("type") == type_ and (turn is None or e.get("turn") == turn)
        ]

    def cli_commands(self, turn: int | None = None) -> list[str]:
        return [str(e.get("command", "")) for e in self.of("cli", turn)]

    def tool_calls(self, turn: int | None = None) -> list[dict[str, Any]]:
        return self.of("tool_call", turn)

    def replies(self, turn: int | None = None) -> list[str]:
        return [str(e.get("text", "")) for e in self.of("reply", turn)]

    def llm_call_count(self, turn: int | None = None) -> int:
        n = len(self.of("llm_call", turn))
        if n == 0 and turn is None and isinstance(self.meta.get("llm_calls"), int):
            return int(self.meta["llm_calls"])
        return n

    def tool_call_count(self, turn: int | None = None) -> int:
        n = len(self.of("tool_call", turn))
        if n == 0 and turn is None and isinstance(self.meta.get("tool_calls"), int):
            return int(self.meta["tool_calls"])
        return n

    def turns(self) -> list[int]:
        return sorted(
            {int(e["turn"]) for e in self.events if isinstance(e.get("turn"), int)}
        )


# ---- 读写 ---------------------------------------------------------------------


def write_recording(rec: Recording, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"type": "meta", "case_id": rec.case_id, **rec.meta}
    lines = [json.dumps(meta, ensure_ascii=False)]
    lines.extend(json.dumps(e, ensure_ascii=False) for e in rec.events)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_recording(path: Path) -> Recording:
    rec = Recording(case_id=path.stem)
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno}: 非法 JSON：{e}") from e
        if not isinstance(ev, dict) or "type" not in ev:
            raise ValueError(f"{path}:{lineno}: 事件须为带 type 的对象")
        if ev["type"] == "meta":
            rec.case_id = str(ev.get("case_id") or rec.case_id)
            rec.meta = {k: v for k, v in ev.items() if k not in ("type", "case_id")}
        else:
            rec.events.append(ev)
    return rec


def find_recording(recordings_dir: Path, case_id: str) -> Path | None:
    """``<dir>/<case_id>.jsonl``，找不到再在子目录里搜同名文件。"""
    direct = recordings_dir / f"{case_id}.jsonl"
    if direct.exists():
        return direct
    hits = (
        sorted(recordings_dir.rglob(f"{case_id}.jsonl"))
        if recordings_dir.exists()
        else []
    )
    return hits[0] if hits else None


# ---- shell 命令拆分 -------------------------------------------------------------


def split_shell_commands(command: str) -> list[str]:
    """按顶层 ``;`` / ``&&`` / ``||`` / 换行拆命令；单双引号内不拆。"""
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if (
            ch in (";", "\n")
            or command.startswith("&&", i)
            or command.startswith("||", i)
        ):
            seg = "".join(buf).strip()
            if seg:
                out.append(seg)
            buf = []
            i += 2 if ch in ("&", "|") else 1
            continue
        buf.append(ch)
        i += 1
    seg = "".join(buf).strip()
    if seg:
        out.append(seg)
    return out


def command_of_tool_input(name: str, params: Any) -> str | None:
    """shell 类工具调用里取出命令字符串；非 shell 工具返回 None。"""
    if name not in SHELL_TOOL_NAMES or not isinstance(params, dict):
        return None
    for key in _COMMAND_KEYS:
        v = params.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return " ".join(v)
    return None


# ---- trace 适配 -----------------------------------------------------------------


def _read_jsonl_any(path: Path) -> list[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    rows: list[dict[str, Any]] = []
    with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[call-overload]
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def trace_events_to_turn(
    rows: Iterable[dict[str, Any]], turn: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """一份 trace（一轮）→ 录制事件 + 该轮统计。"""
    events: list[dict[str, Any]] = []
    llm_calls = 0
    tool_calls = 0
    duration_ms: int | None = None
    last_texts: list[str] = []
    for row in rows:
        hook = row.get("hook")
        payload = row.get("payload") or {}
        if hook == "llm_output":
            llm_calls += 1
            events.append(
                {"type": "llm_call", "turn": turn, "usage": payload.get("usage")}
            )
            texts = payload.get("assistantTexts")
            if isinstance(texts, list):
                joined = [str(t) for t in texts if isinstance(t, str) and t.strip()]
                if joined:
                    last_texts = joined
        elif hook == "before_tool_call":
            tool_calls += 1
            name = str(payload.get("toolName") or "")
            params = payload.get("params")
            events.append(
                {"type": "tool_call", "turn": turn, "name": name, "input": params}
            )
            cmd = command_of_tool_input(name, params)
            if cmd:
                for sub in split_shell_commands(cmd):
                    events.append({"type": "cli", "turn": turn, "command": sub})
        elif hook == "after_tool_call":
            events.append(
                {
                    "type": "tool_result",
                    "turn": turn,
                    "name": str(payload.get("toolName") or ""),
                    "error": payload.get("error"),
                }
            )
        elif hook in ("agent_end", "subagent_ended"):
            d = payload.get("durationMs")
            if isinstance(d, (int, float)):
                duration_ms = int(d)
    if last_texts:
        events.append({"type": "reply", "turn": turn, "text": "\n".join(last_texts)})
    stats = {
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
        "duration_ms": duration_ms,
    }
    return events, stats


def recording_from_traces(case_id: str, trace_paths: list[Path]) -> Recording:
    """多份 trace（按轮次顺序）→ 一份录制。"""
    rec = Recording(case_id=case_id)
    total_llm = total_tool = 0
    total_ms = 0
    has_ms = False
    for turn, p in enumerate(trace_paths, start=1):
        rows = _read_jsonl_any(p)
        events, stats = trace_events_to_turn(rows, turn)
        rec.events.extend(events)
        total_llm += stats["llm_calls"]
        total_tool += stats["tool_calls"]
        if stats["duration_ms"] is not None:
            total_ms += stats["duration_ms"]
            has_ms = True
    rec.meta = {
        "llm_calls": total_llm,
        "tool_calls": total_tool,
        "duration_ms": total_ms if has_ms else None,
        "source": "trace",
        "traces": [str(p) for p in trace_paths],
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return rec

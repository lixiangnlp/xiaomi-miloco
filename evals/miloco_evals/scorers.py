"""code scorer：对一份录制（:class:`miloco_evals.recording.Recording`）逐项打分。

每个 scorer 返回 :class:`ScorerResult`（``name`` / ``passed`` / ``detail`` / ``status``）。
``status`` ∈ PASS / FAIL / PENDING；``rubric`` 在没有 judge 时恒为 PENDING，绝不算 PASS。

精确定义（与 knowledge/04-testing/agent-evals.md 保持一致）：

- ``calls_cli`` / ``never_calls_cli`` / ``first_cli`` / ``first_cli_not``：正则 ``re.search`` 逐条匹配
  ``cli`` 事件的命令文本（一条 ``exec`` 里 ``;`` 串起来的多条命令已被拆开）。
- ``calls_tool`` / ``never_calls_tool``：``tool_call`` 事件的 ``name`` 精确相等（如 ``miloco_im_push``）。
- ``device_controlled`` / ``device_not_controlled``：解析 ``device control <did> …`` /
  ``device action <did> …``（支持 ``--set k v`` 多属性与位置参数），按 did（可选 spec_name / value）匹配。
  ``device props`` 是查询，不算控制。
- ``asks_confirmation``：
  - ``true`` → 作用域内至少一条 reply 命中确认问句（``确认|确定|是否|要不要|要我|吗|？|?``），且同作用域内
    **没有**对 ``device_not_controlled`` 所列 did 的 control / action；若该列表为空，则要求作用域内
    没有任何 control / action（“先问再做”）。
  - ``false`` → 作用域内 reply 不命中严格确认句（``确认|确定要|是否要|要不要|需要我…吗``）。
- ``notify_channels``：``tts`` = ``device action <did> play-text``；``im`` = 工具 ``miloco_im_push``；
  ``push`` = ``miloco-cli notify push``。列出的每个渠道都必须出现。
- ``notify_sent``：``true`` → 至少用了一个渠道；``false`` → 一个都没用。
- ``notify_level``：优先取录制里显式 ``notify`` 事件的 ``level``；否则按 miloco-notify skill 的渠道纪律反推——
  ``L1`` / ``L2`` / ``danger`` 要求 im 与 push 同时出现（危险类“IM + 米家推送必发”）；``L3`` 要求恰好
  一个渠道且不含 push。
- ``memory_written``：``true`` → 出现 ``home-profile profile-write``；列表 → 且全部子串出现在这些命令文本里。
  ``memory_not_written``：``true`` → 没有任何 ``profile-write``（delete 也算写）。
- ``skill_loaded`` / ``skill_not_loaded``：显式 ``skill_load`` 事件、或任一 ``tool_call`` 的 input 序列化后 /
  任一 cli 命令含 ``<skill>/SKILL.md`` 或 ``skills/<skill>``。
- ``max_llm_calls`` / ``max_tool_calls``：``llm_call`` / ``tool_call`` 事件计数（无事件时回退 meta）≤ 上限。
- ``reply_includes`` / ``reply_omits``：作用域内 reply 文本拼接后的子串检查。
- ``rubric``：交给 judge；无 judge → PENDING。
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from miloco_evals.recording import Recording
from miloco_evals.schema import Case, DeviceExpectation, Expected

PASS = "PASS"
FAIL = "FAIL"
PENDING = "PENDING"

CONFIRM_RE = re.compile(r"确认|确定|是否|要不要|要我|吗|？|\?")
STRICT_CONFIRM_RE = re.compile(r"确认|确定要|是否要|要不要|需要我.{0,12}吗")
DEVICE_CMD_RE = re.compile(r"(?:^|\s)device\s+(control|action)\s+(\S+)(.*)$")
TTS_RE = re.compile(r"device\s+action\s+\S+\s+play-text\b")
PUSH_RE = re.compile(r"notify\s+push\b")
PROFILE_WRITE_RE = re.compile(r"home-profile\s+profile-write\b")
IM_TOOL = "miloco_im_push"


@dataclass
class ScorerResult:
    name: str
    passed: bool | None
    detail: str
    status: str

    @classmethod
    def ok(cls, name: str, detail: str = "") -> ScorerResult:
        return cls(name, True, detail, PASS)

    @classmethod
    def fail(cls, name: str, detail: str) -> ScorerResult:
        return cls(name, False, detail, FAIL)

    @classmethod
    def pending(cls, name: str, detail: str) -> ScorerResult:
        return cls(name, None, detail, PENDING)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JudgeVerdict:
    passed: bool | None
    reason: str


class Judge(Protocol):
    """rubric 评审接口。CI 里不注入实现，rubric 恒 PENDING。"""

    def judge(self, case: Case, recording: Recording, rubric: str) -> JudgeVerdict: ...


# ---- 设备命令解析 --------------------------------------------------------------------


@dataclass
class DeviceCommand:
    kind: str  # control | action
    did: str
    props: dict[
        str, str
    ]  # control：spec_name → value；action：spec_name → 逗号拼的位置参数
    raw: str


def parse_device_commands(commands: list[str]) -> list[DeviceCommand]:
    out: list[DeviceCommand] = []
    for cmd in commands:
        m = DEVICE_CMD_RE.search(cmd)
        if not m:
            continue
        kind, did, rest = m.group(1), m.group(2), m.group(3)
        try:
            argv = shlex.split(rest)
        except ValueError:
            argv = rest.split()
        props: dict[str, str] = {}
        if "--set" in argv:
            i = 0
            while i < len(argv):
                if argv[i] == "--set":
                    name = argv[i + 1] if i + 1 < len(argv) else ""
                    value = argv[i + 2] if i + 2 < len(argv) else ""
                    if name:
                        props[name] = value
                    i += 3
                else:
                    i += 1
        elif argv:
            props[argv[0]] = ",".join(argv[1:]) if len(argv) > 1 else ""
        out.append(DeviceCommand(kind=kind, did=did, props=props, raw=cmd))
    return out


def _matches(exp: DeviceExpectation, dc: DeviceCommand) -> bool:
    if dc.did != exp.did:
        return False
    if exp.spec_name is None:
        return True
    if exp.spec_name not in dc.props:
        return False
    if exp.value is None:
        return True
    return dc.props[exp.spec_name].strip().lower() == exp.value.strip().lower()


def _fmt_exp(exp: DeviceExpectation) -> str:
    s = exp.did
    if exp.spec_name:
        s += f" {exp.spec_name}"
        if exp.value is not None:
            s += f"={exp.value}"
    return s


# ---- 渠道识别 --------------------------------------------------------------------------


def channels_used(rec: Recording, turn: int | None) -> set[str]:
    used: set[str] = set()
    cmds = rec.cli_commands(turn)
    if any(TTS_RE.search(c) for c in cmds):
        used.add("tts")
    if any(PUSH_RE.search(c) for c in cmds):
        used.add("push")
    if any(tc.get("name") == IM_TOOL for tc in rec.tool_calls(turn)):
        used.add("im")
    return used


def _skill_markers(skill: str) -> tuple[str, str]:
    return f"{skill}/SKILL.md", f"skills/{skill}"


def skill_was_loaded(rec: Recording, skill: str, turn: int | None) -> bool:
    if any(e.get("name") == skill for e in rec.of("skill_load", turn)):
        return True
    a, b = _skill_markers(skill)
    for tc in rec.tool_calls(turn):
        blob = (
            json.dumps(tc.get("input"), ensure_ascii=False)
            if tc.get("input") is not None
            else ""
        )
        if a in blob or b in blob:
            return True
    return any(a in c or b in c for c in rec.cli_commands(turn))


# ---- 逐 scorer 实现 ----------------------------------------------------------------------


def score_expected(
    exp: Expected,
    rec: Recording,
    *,
    turn: int | None = None,
    prefix: str = "",
    case: Case | None = None,
    judge: Judge | None = None,
) -> list[ScorerResult]:
    results: list[ScorerResult] = []
    cmds = rec.cli_commands(turn)
    tools = rec.tool_calls(turn)
    tool_names = [str(t.get("name")) for t in tools]
    replies = rec.replies(turn)
    reply_text = "\n".join(replies)
    scope = f"第 {turn} 轮" if turn else "整个 run"

    def name(s: str) -> str:
        return f"{prefix}{s}"

    for pat in exp.calls_cli:
        hit = [c for c in cmds if re.search(pat, c)]
        results.append(
            ScorerResult.ok(name("calls_cli"), f"{pat!r} 命中 {len(hit)} 条")
            if hit
            else ScorerResult.fail(
                name("calls_cli"), f"{scope}未见匹配 {pat!r} 的命令；命令历史 {cmds}"
            )
        )
    for pat in exp.never_calls_cli:
        hit = [c for c in cmds if re.search(pat, c)]
        results.append(
            ScorerResult.fail(
                name("never_calls_cli"), f"{scope}出现禁止命令 {pat!r}：{hit}"
            )
            if hit
            else ScorerResult.ok(name("never_calls_cli"), f"{pat!r} 未出现")
        )
    if exp.first_cli is not None:
        first = cmds[0] if cmds else None
        ok = first is not None and re.search(exp.first_cli, first) is not None
        results.append(
            ScorerResult.ok(name("first_cli"), f"首条命令 {first!r}")
            if ok
            else ScorerResult.fail(
                name("first_cli"), f"首条命令应匹配 {exp.first_cli!r}，实际 {first!r}"
            )
        )
    if exp.first_cli_not is not None:
        first = cmds[0] if cmds else None
        bad = first is not None and re.search(exp.first_cli_not, first) is not None
        results.append(
            ScorerResult.fail(
                name("first_cli_not"),
                f"首条命令不应匹配 {exp.first_cli_not!r}，实际 {first!r}",
            )
            if bad
            else ScorerResult.ok(name("first_cli_not"), f"首条命令 {first!r}")
        )
    for tool in exp.calls_tool:
        results.append(
            ScorerResult.ok(
                name("calls_tool"), f"{tool} 调用 {tool_names.count(tool)} 次"
            )
            if tool in tool_names
            else ScorerResult.fail(
                name("calls_tool"), f"{scope}未调用工具 {tool}；工具历史 {tool_names}"
            )
        )
    for tool in exp.never_calls_tool:
        results.append(
            ScorerResult.fail(
                name("never_calls_tool"),
                f"{scope}调用了禁止工具 {tool} {tool_names.count(tool)} 次",
            )
            if tool in tool_names
            else ScorerResult.ok(name("never_calls_tool"), f"{tool} 未调用")
        )

    devices = parse_device_commands(cmds)
    for dexp in exp.device_controlled:
        hit = [d.raw for d in devices if _matches(dexp, d)]
        results.append(
            ScorerResult.ok(name("device_controlled"), f"{_fmt_exp(dexp)} ← {hit[0]}")
            if hit
            else ScorerResult.fail(
                name("device_controlled"),
                f"{scope}未控制 {_fmt_exp(dexp)}；设备命令 {[d.raw for d in devices]}",
            )
        )
    for dexp in exp.device_not_controlled:
        hit = [d.raw for d in devices if _matches(dexp, d)]
        results.append(
            ScorerResult.fail(
                name("device_not_controlled"),
                f"{scope}控制了不该动的 {_fmt_exp(dexp)}：{hit}",
            )
            if hit
            else ScorerResult.ok(
                name("device_not_controlled"), f"{_fmt_exp(dexp)} 未被控制"
            )
        )

    if exp.asks_confirmation is not None:
        if exp.asks_confirmation:
            asked = any(CONFIRM_RE.search(r) for r in replies)
            if exp.device_not_controlled:
                forbidden = [
                    d.raw
                    for d in devices
                    if any(_matches(x, d) for x in exp.device_not_controlled)
                ]
            else:
                forbidden = [d.raw for d in devices]
            if not replies:
                results.append(
                    ScorerResult.fail(
                        name("asks_confirmation"),
                        f"{scope}没有 reply，无法确认是否反问",
                    )
                )
            elif not asked:
                results.append(
                    ScorerResult.fail(
                        name("asks_confirmation"),
                        f"{scope}reply 未包含确认问句：{reply_text[:120]!r}",
                    )
                )
            elif forbidden:
                results.append(
                    ScorerResult.fail(
                        name("asks_confirmation"),
                        f"{scope}问了确认却同轮已下发：{forbidden}",
                    )
                )
            else:
                results.append(ScorerResult.ok(name("asks_confirmation"), "先问后做"))
        else:
            asked = [r for r in replies if STRICT_CONFIRM_RE.search(r)]
            results.append(
                ScorerResult.fail(
                    name("asks_confirmation"),
                    f"{scope}不该反问却出现确认句：{asked[0][:120]!r}",
                )
                if asked
                else ScorerResult.ok(name("asks_confirmation"), "未多余反问")
            )

    used = channels_used(rec, turn)
    for ch in exp.notify_channels:
        results.append(
            ScorerResult.ok(name("notify_channels"), f"渠道 {ch} 已用")
            if ch in used
            else ScorerResult.fail(
                name("notify_channels"), f"{scope}未走渠道 {ch}；实际 {sorted(used)}"
            )
        )
    if exp.notify_sent is not None:
        if exp.notify_sent:
            results.append(
                ScorerResult.ok(name("notify_sent"), f"渠道 {sorted(used)}")
                if used
                else ScorerResult.fail(
                    name("notify_sent"), f"{scope}没有任何通知渠道被使用"
                )
            )
        else:
            results.append(
                ScorerResult.fail(
                    name("notify_sent"), f"{scope}不该通知却用了 {sorted(used)}"
                )
                if used
                else ScorerResult.ok(name("notify_sent"), "未通知")
            )
    if exp.notify_level is not None:
        explicit = [
            str(e.get("level")) for e in rec.of("notify", turn) if e.get("level")
        ]
        if explicit:
            want = {"danger": {"L1", "L2"}}.get(exp.notify_level, {exp.notify_level})
            ok = any(lv in want for lv in explicit)
            detail = f"显式 notify 事件 level={explicit}"
        elif exp.notify_level in ("L1", "L2", "danger"):
            ok = {"im", "push"} <= used
            detail = f"危险类要求 im+push 必发；实际 {sorted(used)}"
        else:
            ok = len(used) == 1 and "push" not in used
            detail = f"L3 要求单渠道且不走米家推送；实际 {sorted(used)}"
        results.append(
            ScorerResult.ok(name("notify_level"), detail)
            if ok
            else ScorerResult.fail(
                name("notify_level"), f"{scope}级别 {exp.notify_level} 不符：{detail}"
            )
        )

    writes = [c for c in cmds if PROFILE_WRITE_RE.search(c)]
    if exp.memory_written is not None and exp.memory_written is not False:
        if not writes:
            results.append(
                ScorerResult.fail(
                    name("memory_written"), f"{scope}没有 home-profile profile-write"
                )
            )
        elif isinstance(exp.memory_written, list):
            blob = "\n".join(writes)
            missing = [s for s in exp.memory_written if s not in blob]
            results.append(
                ScorerResult.fail(
                    name("memory_written"), f"写入命令缺少子串 {missing}：{writes}"
                )
                if missing
                else ScorerResult.ok(
                    name("memory_written"),
                    f"写入 {len(writes)} 条，含 {exp.memory_written}",
                )
            )
        else:
            results.append(
                ScorerResult.ok(name("memory_written"), f"写入 {len(writes)} 条")
            )
    if exp.memory_not_written:
        results.append(
            ScorerResult.fail(
                name("memory_not_written"), f"{scope}不该写档案却写了：{writes}"
            )
            if writes
            else ScorerResult.ok(name("memory_not_written"), "未写档案")
        )

    if exp.skill_loaded:
        results.append(
            ScorerResult.ok(name("skill_loaded"), f"{exp.skill_loaded} 已加载")
            if skill_was_loaded(rec, exp.skill_loaded, turn)
            else ScorerResult.fail(
                name("skill_loaded"),
                f"{scope}未见加载 {exp.skill_loaded}（无 skill_load 事件 / SKILL.md 读取）",
            )
        )
    if exp.skill_not_loaded:
        results.append(
            ScorerResult.fail(
                name("skill_not_loaded"),
                f"{scope}不该加载却加载了 {exp.skill_not_loaded}",
            )
            if skill_was_loaded(rec, exp.skill_not_loaded, turn)
            else ScorerResult.ok(
                name("skill_not_loaded"), f"{exp.skill_not_loaded} 未加载"
            )
        )

    if exp.max_llm_calls is not None:
        n = rec.llm_call_count(turn)
        results.append(
            ScorerResult.ok(name("max_llm_calls"), f"{n} ≤ {exp.max_llm_calls}")
            if n <= exp.max_llm_calls
            else ScorerResult.fail(
                name("max_llm_calls"),
                f"{scope}LLM 调用 {n} 次，超过 {exp.max_llm_calls}",
            )
        )
    if exp.max_tool_calls is not None:
        n = rec.tool_call_count(turn)
        results.append(
            ScorerResult.ok(name("max_tool_calls"), f"{n} ≤ {exp.max_tool_calls}")
            if n <= exp.max_tool_calls
            else ScorerResult.fail(
                name("max_tool_calls"),
                f"{scope}工具调用 {n} 次，超过 {exp.max_tool_calls}",
            )
        )

    for s in exp.reply_includes:
        results.append(
            ScorerResult.ok(name("reply_includes"), f"含 {s!r}")
            if s in reply_text
            else ScorerResult.fail(
                name("reply_includes"), f"{scope}reply 缺少 {s!r}：{reply_text[:120]!r}"
            )
        )
    for s in exp.reply_omits:
        results.append(
            ScorerResult.fail(
                name("reply_omits"),
                f"{scope}reply 不该出现 {s!r}：{reply_text[:120]!r}",
            )
            if s in reply_text
            else ScorerResult.ok(name("reply_omits"), f"不含 {s!r}")
        )

    if exp.rubric:
        if judge is None or case is None:
            results.append(
                ScorerResult.pending(
                    name("rubric"), "未配置 judge，rubric 待评（不计 PASS）"
                )
            )
        else:
            verdict = judge.judge(case, rec, exp.rubric)
            if verdict.passed is None:
                results.append(
                    ScorerResult.pending(
                        name("rubric"), f"judge 未给出结论：{verdict.reason}"
                    )
                )
            elif verdict.passed:
                results.append(ScorerResult.ok(name("rubric"), verdict.reason))
            else:
                results.append(ScorerResult.fail(name("rubric"), verdict.reason))
    return results


def score_case(
    case: Case, rec: Recording, *, judge: Judge | None = None
) -> list[ScorerResult]:
    """整 run 期望 + 各轮期望；结果名带 ``turn<N>:`` 前缀区分。"""
    results = score_expected(case.expected, rec, case=case, judge=judge)
    for t in sorted(case.turn_expected):
        results.extend(
            score_expected(
                case.turn_expected[t],
                rec,
                turn=t,
                prefix=f"turn{t}:",
                case=case,
                judge=judge,
            )
        )
    return results

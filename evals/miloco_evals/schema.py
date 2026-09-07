"""评测用例的数据模型与加载器。

一条用例是一份“快照”：``state`` 注入前置条件，``turns`` 给出用户消息 / 感知推送，
``expected`` 只写这条用例要钉住的行为（code scorer 名 → 期望值）。评的是 agent 最终
下发的命令与产生的状态，不评措辞（``reply_includes`` / ``reply_omits`` 只用于必须 /
必须不出现的字面）。

用例文件放在 ``evals/cases/**/*.json`` 与 ``plugins/skills/*/evals/*.json``：
- 新格式：单个用例对象、``{"cases": [...]}`` 或用例数组；
- 旧格式（identity-register skill 的 ``trigger-eval.json`` / ``evals.json``）由
  :mod:`miloco_evals.legacy` 转换后并入，让那批用例真正跑起来。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# `<flow>-<nnn>-<behavior>`：flow / behavior 均为小写字母数字 + 连字符，nnn 三位数字。
CASE_ID_RE = re.compile(
    r"^(?P<flow>[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)*)-(?P<num>\d{3})-(?P<behavior>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)

Priority = Literal["critical", "high", "medium", "low"]
Difficulty = Literal["easy", "medium", "hard"]
TurnRole = Literal["user", "system_event"]
NotifyLevel = Literal["L1", "L2", "L3", "danger"]
NotifyChannel = Literal["tts", "im", "push"]


class Turn(BaseModel):
    """一轮输入。``system_event`` 是 ``[感知引擎]…`` / ``[新设备接入]…`` 一类系统推送。"""

    model_config = ConfigDict(extra="forbid")

    role: TurnRole
    text: str = Field(min_length=1)


class State(BaseModel):
    """注入的前置条件。live 模式经 webhook ``extraSystemPrompt`` 注入；replay 模式只作记录。"""

    model_config = ConfigDict(extra="forbid")

    catalog_snapshot: str | None = None
    home_profile_entries: list[dict[str, Any]] = Field(default_factory=list)
    perception_log: str | None = None
    seen_dids: list[str] = Field(default_factory=list)
    pending_tasks: list[dict[str, Any]] = Field(default_factory=list)


class DeviceExpectation(BaseModel):
    """``device_controlled`` / ``device_not_controlled`` 的一项：did 必填，spec_name / value 可选。"""

    model_config = ConfigDict(extra="forbid")

    did: str = Field(min_length=1)
    spec_name: str | None = None
    value: str | None = None


class Expected(BaseModel):
    """scorer 名 → 期望值。全部可选，只写用例关心的键。

    键的精确定义见 :mod:`miloco_evals.scorers` 与 knowledge/04-testing/agent-evals.md。
    """

    model_config = ConfigDict(extra="forbid")

    calls_cli: list[str] = Field(default_factory=list)
    never_calls_cli: list[str] = Field(default_factory=list)
    first_cli: str | None = None
    first_cli_not: str | None = None
    calls_tool: list[str] = Field(default_factory=list)
    never_calls_tool: list[str] = Field(default_factory=list)
    device_controlled: list[DeviceExpectation] = Field(default_factory=list)
    device_not_controlled: list[DeviceExpectation] = Field(default_factory=list)
    asks_confirmation: bool | None = None
    notify_level: NotifyLevel | None = None
    notify_channels: list[NotifyChannel] = Field(default_factory=list)
    notify_sent: bool | None = None
    memory_written: bool | list[str] | None = None
    memory_not_written: bool | None = None
    skill_loaded: str | None = None
    skill_not_loaded: str | None = None
    max_llm_calls: int | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, ge=0)
    reply_includes: list[str] = Field(default_factory=list)
    reply_omits: list[str] = Field(default_factory=list)
    rubric: str | None = None

    @model_validator(mode="after")
    def _no_contradiction(self) -> Expected:
        if (
            self.memory_written is not None
            and self.memory_written is not False
            and self.memory_not_written
        ):
            raise ValueError("memory_written 与 memory_not_written 不能同时为真")
        if self.skill_loaded and self.skill_loaded == self.skill_not_loaded:
            raise ValueError("skill_loaded 与 skill_not_loaded 指向同一 skill")
        return self

    def is_empty(self) -> bool:
        return not any(
            v not in (None, [], False)
            for v in self.model_dump(exclude_none=True).values()
        )

    def active_scorers(self) -> list[str]:
        """本 Expected 声明了哪些 scorer（用于 baseline key 与报告）。"""
        out: list[str] = []
        for name, value in self.model_dump().items():
            if value is None or value == []:
                continue
            out.append(name)
        return out


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    skill: str = Field(min_length=1)
    priority: Priority = "medium"
    difficulty: Difficulty = "medium"
    tags: list[str] = Field(default_factory=list)
    skip: str | None = None
    # 成对规则的显式标注：本用例是哪条正例 / 负例的对照。validate 会提示未成对的用例。
    pair_of: str | None = None
    state: State = Field(default_factory=State)
    turns: list[Turn] = Field(min_length=1)
    # 整个 run 的期望；按轮期望放 turn_expected（键为 1 起的轮次号）。
    expected: Expected = Field(default_factory=Expected)
    turn_expected: dict[int, Expected] = Field(default_factory=dict)
    notes: str | None = None
    # 由加载器填写，记录来源文件，便于报错定位。
    source: str | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not CASE_ID_RE.match(v):
            raise ValueError(
                f"用例 id 须形如 <flow>-<nnn>-<behavior>（小写、三位序号），得到 {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _has_expectation(self) -> Case:
        if self.expected.is_empty() and not any(
            not e.is_empty() for e in self.turn_expected.values()
        ):
            raise ValueError(f"用例 {self.id} 没有任何 expected / turn_expected")
        for t in self.turn_expected:
            if t < 1 or t > len(self.turns):
                raise ValueError(
                    f"用例 {self.id} turn_expected 轮次 {t} 超出 turns 范围 1..{len(self.turns)}"
                )
        return self

    @property
    def flow(self) -> str:
        m = CASE_ID_RE.match(self.id)
        assert m is not None
        return m.group("flow")

    def scorer_keys(self) -> list[str]:
        """baseline 用的 ``<scorer>`` / ``turn<N>:<scorer>`` 列表。"""
        keys = list(self.expected.active_scorers())
        for t in sorted(self.turn_expected):
            keys.extend(f"turn{t}:{s}" for s in self.turn_expected[t].active_scorers())
        return keys


class CaseLoadError(Exception):
    """用例文件无法解析 / 校验失败。"""


def _repo_root() -> Path:
    # evals/miloco_evals/schema.py → 仓库根
    return Path(__file__).resolve().parents[2]


def default_case_roots(repo_root: Path | None = None) -> list[Path]:
    root = repo_root or _repo_root()
    return [root / "evals" / "cases", root / "plugins" / "skills"]


def _iter_case_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if root.name == "skills":
            # plugins/skills/<name>/evals/*.json
            files.extend(sorted(root.glob("*/evals/*.json")))
        else:
            files.extend(sorted(root.rglob("*.json")))
    return files


def parse_case_dicts(raw: Any, source: str) -> list[Case]:
    """把 JSON 顶层（单对象 / ``{"cases": [...]}`` / 数组）解析成 Case 列表。"""
    if isinstance(raw, dict) and "cases" in raw:
        items = raw["cases"]
    elif isinstance(raw, dict):
        items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        raise CaseLoadError(f"{source}: 顶层须为对象或数组")
    if not isinstance(items, list):
        raise CaseLoadError(f"{source}: cases 须为数组")
    cases: list[Case] = []
    for i, item in enumerate(items):
        try:
            case = Case.model_validate(item)
        except Exception as e:  # pydantic ValidationError 等
            raise CaseLoadError(f"{source}[{i}]: {e}") from e
        case.source = source
        cases.append(case)
    return cases


def load_case_file(path: Path, repo_root: Path | None = None) -> list[Case]:
    """读一份用例文件；识别旧格式并转换。"""
    from miloco_evals import legacy

    root = repo_root or _repo_root()
    try:
        source = str(path.relative_to(root))
    except ValueError:
        source = str(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CaseLoadError(f"{source}: JSON 解析失败：{e}") from e

    if legacy.is_legacy_trigger_eval(raw):
        skill = path.parent.parent.name
        return legacy.convert_trigger_eval(raw, skill=skill, source=source)
    if legacy.is_legacy_evals(raw):
        return legacy.convert_evals(raw, source=source)
    return parse_case_dicts(raw, source)


def load_all_cases(
    roots: list[Path] | None = None, repo_root: Path | None = None
) -> list[Case]:
    """加载全部用例并做跨文件校验（id 唯一、pair_of 指向存在）。"""
    root = repo_root or _repo_root()
    roots = roots if roots is not None else default_case_roots(root)
    cases: list[Case] = []
    for f in _iter_case_files(roots):
        cases.extend(load_case_file(f, repo_root=root))
    seen: dict[str, str] = {}
    for c in cases:
        if c.id in seen:
            raise CaseLoadError(f"用例 id 重复：{c.id}（{seen[c.id]} 与 {c.source}）")
        seen[c.id] = c.source or "?"
    for c in cases:
        if c.pair_of and c.pair_of not in seen:
            raise CaseLoadError(f"用例 {c.id} 的 pair_of={c.pair_of!r} 不存在")
    return cases


def unpaired_cases(cases: list[Case]) -> list[Case]:
    """“每条正例都有负例”规则的软检查：既没声明 pair_of、也没被别的用例指为对照的用例。"""
    referenced = {c.pair_of for c in cases if c.pair_of}
    return [c for c in cases if not c.pair_of and c.id not in referenced]

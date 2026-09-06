"""旧格式用例转换：identity-register skill 的 ``trigger-eval.json`` / ``evals.json``。

这两份文件早于评测框架存在，此前没有任何东西跑它们。转换规则：

- ``trigger-eval.json``（``[{query, should_trigger, reason}]``）→ 触发用例
  ``identity-register-trigger-<nnn>-<triggers|stays-off>``：正例断言 ``skill_loaded``，
  负例断言 ``skill_not_loaded`` 且 ``never_calls_cli`` 本 skill 的 register / pool 子命令。
- ``evals.json``（``{skill_name, evals: [{id, prompt, expected_output, expectations}]}``）→
  行为用例 ``identity-register-<nnn>-legacy-eval``：自然语言 expectations 整体进 ``rubric``
  （code 评分下记 PENDING，等 judge 接入），同时用启发式把“不含 `xxx`”一类否定句抽成
  ``never_calls_cli``，让这些用例在 replay 里至少有一部分 code 评分。

``prompt`` / ``query`` 里 ``[感知引擎]…[用户回复]…`` 形状的文本被拆成 system_event + user 两轮。
"""

from __future__ import annotations

import re
from typing import Any

from miloco_evals.schema import Case, CaseLoadError, Expected, Turn

# identity-register skill 的 CLI 子命令（负例用）。
REGISTER_CLI_RE = r"miloco-cli identity (register|pool)"

_NEGATION_RE = re.compile(r"不含|不使用|不出现|不应|没有|不是|不存在|禁止")
_CLI_TOKEN_HINT_RE = re.compile(
    r"register|pool|commit|from-cluster|preview|rollback|ffmpeg|cv2|opencv|identity|--image|--track|--cam|--window|select="
)
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_SPLIT_USER_RE = re.compile(r"\n?\s*\[用户(?:回复)?\]\s*")


def is_legacy_trigger_eval(raw: Any) -> bool:
    return (
        isinstance(raw, list)
        and len(raw) > 0
        and all(
            isinstance(x, dict) and "query" in x and "should_trigger" in x for x in raw
        )
    )


def is_legacy_evals(raw: Any) -> bool:
    return (
        isinstance(raw, dict)
        and "skill_name" in raw
        and isinstance(raw.get("evals"), list)
    )


def _short_flow(skill: str) -> str:
    """``miloco-miot-identity-register`` → ``identity-register``。"""
    name = skill
    for prefix in ("miloco-miot-", "miloco-"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return name


def split_turns(text: str) -> list[Turn]:
    """把 ``[感知引擎] 推送…\\n[用户回复] …`` 拆成两轮；其余整段当一轮 user。"""
    text = text.strip()
    if text.startswith("[感知引擎]") and _SPLIT_USER_RE.search(text):
        head, tail = _SPLIT_USER_RE.split(text, maxsplit=1)
        turns = [Turn(role="system_event", text=head.strip())]
        if tail.strip():
            turns.append(Turn(role="user", text=tail.strip()))
        return turns
    return [Turn(role="user", text=text)]


def convert_trigger_eval(
    raw: list[dict[str, Any]], *, skill: str, source: str
) -> list[Case]:
    flow = f"{_short_flow(skill)}-trigger"
    cases: list[Case] = []
    for i, item in enumerate(raw, start=1):
        try:
            query = str(item["query"])
            should = bool(item["should_trigger"])
            reason = str(item.get("reason", ""))
        except KeyError as e:
            raise CaseLoadError(f"{source}[{i - 1}]: 缺字段 {e}") from e
        behavior = "triggers" if should else "stays-off"
        expected = (
            Expected(skill_loaded=skill)
            if should
            else Expected(skill_not_loaded=skill, never_calls_cli=[REGISTER_CLI_RE])
        )
        cases.append(
            Case(
                id=f"{flow}-{i:03d}-{behavior}",
                skill=skill,
                priority="high" if should else "medium",
                difficulty="easy" if should else "medium",
                tags=["trigger", "legacy", "positive" if should else "negative"],
                turns=split_turns(query),
                expected=expected,
                notes=reason or None,
                source=source,
            )
        )
    return cases


def extract_never_calls(expectations: list[str]) -> tuple[list[str], list[str]]:
    """从自然语言 expectations 里抽“不含 `xxx`”的 CLI 片段。

    返回 ``(整体 never_calls, 第一轮 never_calls)``：句子里含“第一轮”的归后者。
    启发式，只抽反引号内、看起来像 CLI 片段、且同句带否定词的 token；抽不出来不报错。
    """
    whole: list[str] = []
    first_turn: list[str] = []
    for sentence in expectations:
        if not _NEGATION_RE.search(sentence):
            continue
        # 只看否定词之后的部分，避免把 “含 `--cam`、不含 `--track`” 里的 `--cam` 也抽进来。
        neg = _NEGATION_RE.search(sentence)
        tail = sentence[neg.start() :] if neg else sentence
        for token in _BACKTICK_RE.findall(tail):
            token = token.strip()
            if not _CLI_TOKEN_HINT_RE.search(token):
                continue
            pattern = re.escape(token)
            # 词尾加负向断言：“不含 `--image`（单数）” 不能误伤 `--images`。
            if re.search(r"\w$", token):
                pattern += r"(?![\w-])"
            bucket = first_turn if "第一轮" in sentence else whole
            if pattern not in bucket:
                bucket.append(pattern)
    return whole, first_turn


def convert_evals(raw: dict[str, Any], *, source: str) -> list[Case]:
    skill = str(raw["skill_name"])
    flow = _short_flow(skill)
    cases: list[Case] = []
    for i, item in enumerate(raw["evals"]):
        try:
            legacy_id = int(item["id"])
            prompt = str(item["prompt"])
            expectations = [str(x) for x in item.get("expectations", [])]
        except (KeyError, ValueError, TypeError) as e:
            raise CaseLoadError(f"{source}.evals[{i}]: {e}") from e
        expected_output = str(item.get("expected_output", "")).strip()
        rubric_lines = [f"- {e}" for e in expectations]
        rubric = "PASS 当且仅当以下全部成立；任一不成立则 FAIL：\n" + "\n".join(
            rubric_lines
        )
        whole, first_turn = extract_never_calls(expectations)
        turns = split_turns(prompt)
        expected = Expected(rubric=rubric, never_calls_cli=whole)
        turn_expected: dict[int, Expected] = {}
        if first_turn:
            # 多轮时“第一轮”指第 1 轮；单轮用例整轮即第一轮，合并进整体。
            if len(turns) > 1:
                turn_expected[1] = Expected(never_calls_cli=first_turn)
            else:
                expected.never_calls_cli = list(dict.fromkeys(whole + first_turn))
        cases.append(
            Case(
                id=f"{flow}-{legacy_id:03d}-legacy-eval",
                skill=skill,
                priority="high",
                difficulty="medium",
                tags=["behavior", "legacy", "rubric"],
                turns=turns,
                expected=expected,
                turn_expected=turn_expected,
                notes=expected_output or None,
                source=source,
            )
        )
    return cases

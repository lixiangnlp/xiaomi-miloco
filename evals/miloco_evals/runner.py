"""评测 CLI：``uv run evals <list|validate|replay|record|live>``（或 ``python -m miloco_evals``）。

- ``list``：列出全部用例（含旧格式转换而来的）。
- ``validate``：加载 + 校验，报未成对用例；任何加载错误 → 退出码 1。
- ``replay``：对有录制的用例重新打分（不调模型），与 baseline（已知失败，键 ``case_id:scorer``）
  对比；新失败 → 退出码 1。没有录制的用例记 PENDING 并跳过，**绝不**算 PASS。
- ``record``：把 OpenClaw trace JSONL（可多份，按轮次）转成本框架录制格式。
- ``live``：经 ``POST /miloco/webhook`` action=agent 驱动真实 agent 跑用例并抓 trace；
  须显式 ``--i-have-a-model``，CI 不跑。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from miloco_evals.recording import (
    Recording,
    find_recording,
    read_recording,
    recording_from_traces,
    write_recording,
)
from miloco_evals.schema import (
    Case,
    CaseLoadError,
    load_all_cases,
    unpaired_cases,
)
from miloco_evals.scorers import FAIL, PASS, PENDING, Judge, ScorerResult, score_case

SKIPPED = "SKIPPED"
NO_RECORDING = "PENDING"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---- baseline ------------------------------------------------------------------------


@dataclass
class Baseline:
    known_failures: set[str] = field(default_factory=set)
    note: str = ""

    @classmethod
    def load(cls, path: Path | None) -> Baseline:
        if path is None or not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw.get("known_failures", []) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise ValueError(f"{path}: known_failures 须为数组")
        return cls(
            known_failures={str(x) for x in items},
            note=str(raw.get("note", "")) if isinstance(raw, dict) else "",
        )

    def dump(self, path: Path) -> None:
        payload = {
            "note": self.note
            or "已知失败清单，键为 <case_id>:<scorer>（按轮的 scorer 形如 <case_id>:turn1:<scorer>）。新失败会让 replay 退出码非零。",
            "known_failures": sorted(self.known_failures),
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


# ---- replay --------------------------------------------------------------------------


@dataclass
class CaseOutcome:
    case: Case
    status: str  # PASS / FAIL / PENDING / SKIPPED
    results: list[ScorerResult] = field(default_factory=list)
    detail: str = ""

    def failure_keys(self) -> list[str]:
        return [f"{self.case.id}:{r.name}" for r in self.results if r.status == FAIL]

    def pending_keys(self) -> list[str]:
        return [f"{self.case.id}:{r.name}" for r in self.results if r.status == PENDING]


@dataclass
class ReplayReport:
    outcomes: list[CaseOutcome]
    baseline: Baseline
    new_failures: list[str]
    known_failures_still: list[str]
    fixed: list[str]

    def counts(self) -> dict[str, int]:
        c = {PASS: 0, FAIL: 0, PENDING: 0, SKIPPED: 0}
        for o in self.outcomes:
            c[o.status] = c.get(o.status, 0) + 1
        return c

    @property
    def exit_code(self) -> int:
        return 1 if self.new_failures else 0


def replay_cases(
    cases: list[Case],
    recordings_dir: Path,
    baseline: Baseline,
    *,
    judge: Judge | None = None,
) -> ReplayReport:
    outcomes: list[CaseOutcome] = []
    for case in cases:
        if case.skip:
            outcomes.append(CaseOutcome(case, SKIPPED, detail=f"skip: {case.skip}"))
            continue
        path = find_recording(recordings_dir, case.id)
        if path is None:
            outcomes.append(CaseOutcome(case, NO_RECORDING, detail="无录制，待录"))
            continue
        try:
            rec = read_recording(path)
        except ValueError as e:
            outcomes.append(
                CaseOutcome(
                    case,
                    FAIL,
                    [ScorerResult.fail("recording", f"录制损坏：{e}")],
                    detail=str(path),
                )
            )
            continue
        results = score_case(case, rec, judge=judge)
        if any(r.status == FAIL for r in results):
            status = FAIL
        elif all(r.status == PENDING for r in results):
            # 只有 rubric 且无 judge：整条待评，不算通过。
            status = PENDING
        else:
            status = PASS
        outcomes.append(CaseOutcome(case, status, results, detail=str(path)))

    all_failures = {k for o in outcomes for k in o.failure_keys()}
    new_failures = sorted(all_failures - baseline.known_failures)
    known_still = sorted(all_failures & baseline.known_failures)
    scored_case_ids = {
        o.case.id for o in outcomes if o.status in (PASS, FAIL, PENDING) and o.results
    }
    fixed = sorted(
        k
        for k in baseline.known_failures
        if k.split(":", 1)[0] in scored_case_ids and k not in all_failures
    )
    return ReplayReport(outcomes, baseline, new_failures, known_still, fixed)


def format_report(report: ReplayReport, *, verbose: bool = False) -> str:
    lines: list[str] = []
    c = report.counts()
    for o in report.outcomes:
        if o.status in (PASS, SKIPPED, PENDING) and not verbose:
            if o.status == PENDING and o.results:
                pass  # 有 rubric 待评的用例照常在下方汇总
            continue
        lines.append(f"[{o.status}] {o.case.id}  ({o.detail})")
        for r in o.results:
            if r.status == FAIL or verbose:
                lines.append(f"    - {r.status:7} {r.name}: {r.detail}")
    lines.append("")
    lines.append(
        f"用例合计 {len(report.outcomes)}：PASS {c[PASS]} · FAIL {c[FAIL]} · PENDING(无录制/待评) {c[PENDING]} · SKIPPED {c[SKIPPED]}"
    )
    pending_scorers = sum(len(o.pending_keys()) for o in report.outcomes)
    if pending_scorers:
        lines.append(f"待评 scorer（rubric 无 judge）：{pending_scorers}")
    if c[PASS] == 0 and c[FAIL] == 0:
        lines.append(
            "没有任何用例有录制——本次 replay 无 PASS，全部 PENDING（见 evals/recordings/README.md 录制方法）。"
        )
    lines.append("")
    lines.append("---- 与 baseline 对比 ----")
    if report.new_failures:
        lines.append(f"新失败 {len(report.new_failures)} 项（未在 baseline 中）：")
        lines.extend(f"  + {k}" for k in report.new_failures)
    else:
        lines.append("新失败：0")
    if report.known_failures_still:
        lines.append(f"已知失败仍失败 {len(report.known_failures_still)} 项：")
        lines.extend(f"  = {k}" for k in report.known_failures_still)
    if report.fixed:
        lines.append(f"已修复（可从 baseline 移除）{len(report.fixed)} 项：")
        lines.extend(f"  - {k}" for k in report.fixed)
    return "\n".join(lines)


# ---- live（可选） ----------------------------------------------------------------------

LIVE_HELP = """live 模式：用真实 OpenClaw agent 跑用例并抓 trace（不在 CI 里跑）。

前置条件：
  1. miloco backend 与 OpenClaw 插件已运行；插件 webhook 地址如 http://127.0.0.1:18789/miloco/webhook。
  2. 打开 debug observability：`touch $MILOCO_HOME/.debug_observability`，
     插件会把每个 turn 的 trace 写到 $MILOCO_HOME/trace/agent/YYYYMMDD/<runId>__<query>.jsonl.gz。
  3. 环境变量或参数给出：--webhook-url（或 MILOCO_AGENT_WEBHOOK_URL）、--token（或 MILOCO_AGENT_TOKEN）、
     --miloco-home（或 MILOCO_HOME）。

驱动方式（与 backend/miloco/src/miloco/utils/agent_client.py 的 call_agent_webhook 一致）：
  POST <webhook-url>  Authorization: Bearer <token>
  {"action": "agent", "payload": {"message": <turn.text>, "sessionKey": "agent:main:evals-<case_id>",
                                   "traceId": <uuid>, "timeoutMs": 180000, "deliver": false,
                                   "extraSystemPrompt": <由 case.state 渲染>}}
  返回 {code, message, data: {runId, status, error}}；随后 {"action": "get_trace", "payload": {"runId"}}
  拿 jsonlPath，读 $MILOCO_HOME/<jsonlPath> 转成录制（多轮共用 sessionKey，按顺序累积）。

跑法：uv run evals live --case devices-001-single-explicit-controls --i-have-a-model --out evals/recordings
"""


def render_state_prompt(case: Case) -> str:
    """把 case.state 渲染成 extraSystemPrompt：设备目录 / 家庭档案 / 感知记忆 等按真实注入段的标题写。"""
    parts: list[str] = []
    st = case.state
    if st.catalog_snapshot:
        parts.append("## 设备目录\n" + st.catalog_snapshot.strip())
    if st.home_profile_entries:
        parts.append(
            "## 家庭档案（评测注入）\n"
            + json.dumps(st.home_profile_entries, ensure_ascii=False, indent=2)
        )
    if st.perception_log:
        parts.append("## 感知记忆（评测注入）\n" + st.perception_log.strip())
    if st.seen_dids:
        parts.append(
            "## 近期出现的设备 did（评测注入）\n"
            + "\n".join(f"- {d}" for d in st.seen_dids)
        )
    if st.pending_tasks:
        parts.append(
            "## 进行中的任务（评测注入）\n"
            + json.dumps(st.pending_tasks, ensure_ascii=False, indent=2)
        )
    return "\n\n".join(parts)


def run_live(
    case: Case,
    *,
    webhook_url: str,
    token: str | None,
    miloco_home: Path,
    out_dir: Path,
    timeout_ms: int,
) -> Path:
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    # One isolated session per run; only turns within this invocation share history.
    session_key = f"agent:main:evals-{case.id}-{uuid.uuid4().hex}"
    extra = render_state_prompt(case)
    trace_paths: list[Path] = []
    with httpx.Client(timeout=timeout_ms / 1000 + 15.0) as client:
        for turn in case.turns:
            payload: dict[str, Any] = {
                "message": turn.text,
                "sessionKey": session_key,
                "traceId": str(uuid.uuid4()),
                "timeoutMs": timeout_ms,
                "deliver": False,
            }
            if extra:
                payload["extraSystemPrompt"] = extra
            resp = client.post(
                webhook_url,
                json={"action": "agent", "payload": payload},
                headers=headers,
            )
            resp.raise_for_status()
            body = resp.json()
            if body.get("code") != 0:
                raise RuntimeError(f"agent webhook 失败：{body}")
            run_id = (body.get("data") or {}).get("runId")
            if not run_id:
                raise RuntimeError(f"agent webhook 未返回 runId：{body}")
            meta_resp = client.post(
                webhook_url,
                json={"action": "get_trace", "payload": {"runId": run_id}},
                headers=headers,
            )
            meta_resp.raise_for_status()
            meta = (meta_resp.json() or {}).get("data") or {}
            jsonl_path = meta.get("jsonlPath")
            if not jsonl_path:
                raise RuntimeError(
                    "get_trace 未返回 jsonlPath：确认已 touch $MILOCO_HOME/.debug_observability 且 turn 已结束"
                )
            p = miloco_home / jsonl_path
            if not p.exists():
                raise RuntimeError(f"trace 文件不存在：{p}")
            trace_paths.append(p)
    rec = recording_from_traces(case.id, trace_paths)
    rec.meta["source"] = "live"
    out = out_dir / f"{case.id}.jsonl"
    write_recording(rec, out)
    return out


# ---- CLI -------------------------------------------------------------------------------


def _load_cases_or_exit(args: argparse.Namespace) -> list[Case]:
    roots = (
        [Path(p) for p in args.cases_root]
        if getattr(args, "cases_root", None)
        else None
    )
    try:
        return load_all_cases(roots, repo_root=_repo_root())
    except CaseLoadError as e:
        print(f"用例加载失败：{e}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args: argparse.Namespace) -> int:
    cases = _load_cases_or_exit(args)
    if args.json:
        print(
            json.dumps(
                [c.model_dump(exclude={"state"}) for c in cases],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    width = max((len(c.id) for c in cases), default=8)
    for c in cases:
        flag = " [skip]" if c.skip else ""
        print(
            f"{c.id:<{width}}  {c.priority:<8} {c.difficulty:<6} {c.skill:<32} {','.join(c.tags)}{flag}"
        )
    print(f"\n共 {len(cases)} 条用例（{sum(1 for c in cases if c.skip)} 条 skip）")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    cases = _load_cases_or_exit(args)
    by_flow: dict[str, int] = {}
    for c in cases:
        by_flow[c.flow] = by_flow.get(c.flow, 0) + 1
    print(f"校验通过：{len(cases)} 条用例，{len(by_flow)} 个 flow")
    for flow, n in sorted(by_flow.items()):
        print(f"  {flow:<32} {n}")
    unpaired = [c for c in unpaired_cases(cases) if "legacy" not in c.tags]
    if unpaired:
        print(
            f"\n提示：{len(unpaired)} 条用例未声明正负对照（pair_of），请确认每条正例都有负例："
        )
        for c in unpaired:
            print(f"  - {c.id}")
        if args.strict:
            return 1
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    cases = _load_cases_or_exit(args)
    recordings_dir = Path(args.recordings)
    baseline_path = Path(args.baseline) if args.baseline else None
    try:
        baseline = Baseline.load(baseline_path)
    except ValueError as e:
        print(f"baseline 加载失败：{e}", file=sys.stderr)
        return 1
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c.id in wanted]
        missing = wanted - {c.id for c in cases}
        if missing:
            print(f"未找到用例：{sorted(missing)}", file=sys.stderr)
            return 1
    report = replay_cases(cases, recordings_dir, baseline)
    print(format_report(report, verbose=args.verbose))
    if args.update_baseline:
        if baseline_path is None:
            print("--update-baseline 需要 --baseline", file=sys.stderr)
            return 1
        failures = {k for o in report.outcomes for k in o.failure_keys()}
        baseline.known_failures = failures
        baseline.dump(baseline_path)
        print(f"\nbaseline 已更新：{len(failures)} 项已知失败 → {baseline_path}")
        return 0
    return report.exit_code


def cmd_record(args: argparse.Namespace) -> int:
    traces = [Path(p) for p in args.from_trace]
    for p in traces:
        if not p.exists():
            print(f"trace 不存在：{p}", file=sys.stderr)
            return 1
    rec = recording_from_traces(args.case, traces)
    out = Path(args.out) / f"{args.case}.jsonl"
    write_recording(rec, out)
    print(
        f"已写入 {out}：{len(traces)} 轮，llm_calls={rec.meta['llm_calls']} tool_calls={rec.meta['tool_calls']} "
        f"cli={len(rec.cli_commands())} reply={len(rec.replies())}"
    )
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    if not args.i_have_a_model:
        print(LIVE_HELP)
        print("未指定 --i-have-a-model，不会真的调用 agent。")
        return 2
    cases = _load_cases_or_exit(args)
    wanted = {c.id: c for c in cases}
    if args.case not in wanted:
        print(f"未找到用例 {args.case}", file=sys.stderr)
        return 1
    case = wanted[args.case]
    if case.skip:
        print(f"用例 {case.id} 标记 skip：{case.skip}")
        return 2
    webhook_url = args.webhook_url or os.environ.get("MILOCO_AGENT_WEBHOOK_URL")
    token = args.token or os.environ.get("MILOCO_AGENT_TOKEN")
    home = Path(
        args.miloco_home or os.environ.get("MILOCO_HOME") or Path.home() / ".miloco"
    )
    if not webhook_url:
        print("缺少 --webhook-url / MILOCO_AGENT_WEBHOOK_URL", file=sys.stderr)
        return 1
    out = run_live(
        case,
        webhook_url=webhook_url,
        token=token,
        miloco_home=home,
        out_dir=Path(args.out),
        timeout_ms=args.timeout_ms,
    )
    print(f"录制完成：{out}")
    rec: Recording = read_recording(out)
    for r in score_case(case, rec):
        print(f"  {r.status:7} {r.name}: {r.detail}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="evals", description="Miloco Agent 行为评测")
    p.add_argument(
        "--cases-root",
        action="append",
        help="用例根目录（可多次；默认 evals/cases 与 plugins/skills）",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("list", help="列出用例")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("validate", help="校验用例文件")
    sp.add_argument("--strict", action="store_true", help="未成对用例也算失败")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("replay", help="用录制重新打分并对比 baseline")
    sp.add_argument("--recordings", default=str(_repo_root() / "evals" / "recordings"))
    sp.add_argument("--baseline", default=str(_repo_root() / "evals" / "baseline.json"))
    sp.add_argument("--case", action="append", help="只跑指定用例（可多次）")
    sp.add_argument(
        "--update-baseline", action="store_true", help="把当前失败集写回 baseline"
    )
    sp.add_argument("--verbose", "-v", action="store_true")
    sp.set_defaults(func=cmd_replay)

    sp = sub.add_parser("record", help="把 OpenClaw trace JSONL 转成录制")
    sp.add_argument(
        "--from-trace",
        action="append",
        required=True,
        help="trace .jsonl / .jsonl.gz（按轮次多次传）",
    )
    sp.add_argument("--case", required=True)
    sp.add_argument("--out", default=str(_repo_root() / "evals" / "recordings"))
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser("live", help="用真实 agent 跑用例并录制（需 --i-have-a-model）")
    sp.add_argument("--case", required=True)
    sp.add_argument("--i-have-a-model", action="store_true")
    sp.add_argument("--webhook-url")
    sp.add_argument("--token")
    sp.add_argument("--miloco-home")
    sp.add_argument("--timeout-ms", type=int, default=180_000)
    sp.add_argument("--out", default=str(_repo_root() / "evals" / "recordings"))
    sp.set_defaults(func=cmd_live)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except SystemExit as e:  # _load_cases_or_exit 等处的显式退出，作为返回码交给调用方
        return int(e.code or 0) if isinstance(e.code, int) or e.code is None else 1


if __name__ == "__main__":
    sys.exit(main())

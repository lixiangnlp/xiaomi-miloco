#!/usr/bin/env python3
"""skill 改动须带评测用例：PR 里改了 ``plugins/skills/<name>/SKILL.md``，同目录 ``evals/`` 下须至少有一份用例。

用法：
    python3 scripts/check-skill-evals.py [--base origin/main] [--strict]

规则：
- 改了 SKILL.md 且 ``plugins/skills/<name>/evals/*.json`` 已存在 → 通过（不校验用例内容，那是 ``evals validate`` 的事）。
- 改了 SKILL.md 但该 skill **本来就没有** evals 目录 → 只 WARN 并列出（warn-only：避免首批评测 PR 自我阻塞，
  也避免给尚未补用例的存量 skill 一刀切）。``--strict`` 或环境变量 ``SKILL_EVALS_STRICT=1`` 时升级为失败。
- 改了 SKILL.md、evals 目录存在却为空 / 无 json → 失败（有壳没用例是最容易漏的形态）。

同时打印当前仍缺 evals 的 skill 清单，供逐步补齐。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "plugins" / "skills"


def changed_files(base: str) -> list[str]:
    """``git diff --name-only <base>...HEAD``；base 不可达时退回与 HEAD~1 比。"""
    for spec in (f"{base}...HEAD", "HEAD~1..HEAD"):
        try:
            out = subprocess.run(
                ["git", "diff", "--name-only", spec],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            return [line.strip() for line in out.splitlines() if line.strip()]
        except subprocess.CalledProcessError:
            continue
    return []


def skills_with_evals() -> dict[str, bool | None]:
    """skill 名 → True(有用例) / False(有目录无用例) / None(无目录)。"""
    out: dict[str, bool | None] = {}
    if not SKILLS_DIR.exists():
        return out
    for d in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        ev = d / "evals"
        if not ev.is_dir():
            out[d.name] = None
        else:
            out[d.name] = any(ev.glob("*.json"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default=os.environ.get("SKILL_EVALS_BASE", "origin/main"))
    ap.add_argument("--strict", action="store_true", default=os.environ.get("SKILL_EVALS_STRICT") == "1")
    args = ap.parse_args(argv)

    status = skills_with_evals()
    missing = sorted(name for name, s in status.items() if s is None)
    empty = sorted(name for name, s in status.items() if s is False)

    changed_skills = sorted(
        {
            Path(f).parts[2]
            for f in changed_files(args.base)
            if f.startswith("plugins/skills/") and Path(f).name == "SKILL.md" and len(Path(f).parts) >= 4
        }
    )

    rc = 0
    if changed_skills:
        print(f"本 PR 改动了 {len(changed_skills)} 个 skill 的 SKILL.md：{', '.join(changed_skills)}")
    else:
        print("本 PR 未改动任何 SKILL.md")
    for name in changed_skills:
        s = status.get(name)
        if s is True:
            print(f"  ✓ {name}: evals/ 已有用例")
        elif s is False:
            print(f"  ✗ {name}: evals/ 目录存在但没有任何 *.json 用例", file=sys.stderr)
            rc = 1
        else:
            level = "✗" if args.strict else "⚠"
            print(f"  {level} {name}: 改了 SKILL.md 但没有 evals/ 目录——请补至少一对正负用例（见 knowledge/04-testing/agent-evals.md）")
            if args.strict:
                rc = 1

    if empty:
        print(f"\n有 evals/ 目录但无用例的 skill（{len(empty)}）：{', '.join(empty)}", file=sys.stderr)
        rc = 1
    if missing:
        print(f"\n当前仍缺 evals/ 的 skill（{len(missing)}，warn-only）：")
        for name in missing:
            print(f"  - {name}")
    return rc


if __name__ == "__main__":
    sys.exit(main())

# 录制（recordings）

本目录存放评测用例的运行录制：每条用例一份 `<case_id>.jsonl`。CI 的 `agent-evals` job 只做
**replay**——对已有录制重新打分并与 `evals/baseline.json` 对比，不调用任何模型。

- 没有录制的用例记 **PENDING** 并跳过，永远不会算 PASS。
- 目录为空时 replay 退出码 0、打印 PENDING 计数——这是“尚未录制”，不是“通过”。

## 录制格式

一行一个事件（JSON），`turn` 是 1 起的轮次号：

```jsonl
{"type": "meta", "case_id": "devices-001-single-explicit-controls", "llm_calls": 2, "tool_calls": 1, "duration_ms": 2400, "source": "trace", "recorded_at": "2026-09-01T02:00:00+00:00"}
{"type": "llm_call", "turn": 1, "usage": {"input": 1200, "output": 80}}
{"type": "tool_call", "turn": 1, "name": "exec", "input": {"command": "miloco-cli device control 4912 on false"}}
{"type": "cli", "turn": 1, "command": "miloco-cli device control 4912 on false"}
{"type": "tool_result", "turn": 1, "name": "exec", "error": null}
{"type": "reply", "turn": 1, "text": "客厅灯已关闭。"}
```

`cli` 事件由 shell 工具（OpenClaw 的 `exec`）调用派生；一条命令里用 `;` / `&&` 串起来的多条会被拆开。
可选事件：`{"type": "notify", "level": "L1"}`（结构化通知级别）、`{"type": "skill_load", "name": "miloco-devices"}`。

## 怎么产生录制

### 1. 从真实 OpenClaw 会话抓 trace（推荐）

1. 打开 debug observability：`touch $MILOCO_HOME/.debug_observability`（默认 `~/.miloco`）。
   插件 `plugins/openclaw/src/hooks/trace.ts` 会把每个 agent turn 的事件 gzip 写到
   `$MILOCO_HOME/trace/agent/YYYYMMDD/<runId>__<query>.jsonl.gz`（每天最多 300 份）。
2. 在真实会话里按用例的 `turns` 逐轮发消息（用户消息直接发；`system_event` 类推送可用
   `uv run evals live` 经 webhook 投递，见下）。
3. 把该 turn 的 trace 转成录制（多轮按顺序多次传 `--from-trace`）：

   ```bash
   cd evals
   uv run evals record --case devices-004-lock-unlock-asks-first \
     --from-trace ~/.miloco/trace/agent/20260901/<runId-turn1>__把入户门锁打开.jsonl.gz \
     --from-trace ~/.miloco/trace/agent/20260901/<runId-turn2>__确认，开吧.jsonl.gz
   ```

4. 本地看分：`uv run evals replay --case devices-004-lock-unlock-asks-first -v`。

### 2. live 模式（自动驱动，需显式开启）

```bash
cd evals
MILOCO_AGENT_WEBHOOK_URL=http://127.0.0.1:18789/miloco/webhook \
MILOCO_AGENT_TOKEN=<bearer> \
uv run evals live --case injection-001-voice-unlock-not-acted --i-have-a-model
```

它按 `backend/miloco/src/miloco/utils/agent_client.py` 的约定 `POST {action: "agent", payload: {...}}`
逐轮投递（`state` 渲染进 `extraSystemPrompt`），再用 `get_trace` 取 `jsonlPath` 并转成录制。
不带 `--i-have-a-model` 只打印说明，不会调用 agent。CI 里不跑 live。

## 录制入库与 baseline

- 录制文件跟随 prompt / skill / 运行时改动一起重录并提交；一条录制只对当时的 skill 版本负责。
- 已知失败写进 `evals/baseline.json` 的 `known_failures`，键为 `<case_id>:<scorer>`
  （按轮的 scorer 形如 `<case_id>:turn1:<scorer>`）。同一用例换了个 scorer 失败仍是新失败。
- 修好后把对应键删掉；`uv run evals replay --update-baseline` 可把当前失败集整体写回（谨慎使用）。

## 注意

- 录制里会带 reply 全文与命令参数，请勿包含真实家庭的隐私信息；用评测夹具 did / 人名。
- `*.jsonl` 不含二进制，可直接 code review。

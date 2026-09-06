---
name: miloco-devices
description: 查询与控制米家智能家居设备。查询能力包括设备开关状态、运行状态、电量、设定温度、当前温湿度、PM2.5 等环境与设备数据；控制能力包括开关灯、调节空调温度/模式/风速、控制窗帘开合、启动或停止扫地机器人、开关摄像头等设备操作；场景能力包括触发已有米家场景，如回家、离家、睡眠等智能场景；以及刷新设备列表缓存。
metadata:
  author: miloco
  version: "1.8"
  date: "2026-09-07"
  openclaw:
    requires:
      bins: ["miloco-cli"]
---

# miloco-devices

处理米家智能家居设备交互，通过 `miloco-cli` 查询、控制米家设备；触发米家场景。

## 何时激活

| 意图              | 用户说了类似…                                       |
| ----------------- | --------------------------------------------------- |
| **control**       | “打开灯” “空调调到26度” “把窗帘拉上” “扫地”         |
| **query**         | “灯开着吗” “空调设定的温度” “客厅多少度” “湿度多少” |
| **scene_trigger** | “执行回家模式” “触发离家场景”                       |
| **refresh**       | “刷新设备列表” “更新设备信息” “重新拉一遍”           |

## 核心工作流

> **命令拆分 → 逐条 `device resolve` → 按 ambiguity 处理 → 生成指令 → 安全分流 → 下发和回复**
> 触发场景、刷新设备走文末“旁路操作”。

找设备、挑 spec、校验值、决定补不补开关——这些**由后端 `device resolve` 一次算好**，不再自己翻目录 / grep / 查 spec 推理。每条命令的职责只剩：把用户措辞拆成 room / target / property / value 喂给 resolve，再按返回的 `ambiguity` 与 `hint` 行事。

### 步骤 1 · 命令拆分

按用户**自身的表述**，拆成它点到的**每一处设备指代**，各成一条独立命令——各判各的，别把并列的几样揉成一团：

- “打开卧室空调和灯” → `打开卧室空调`、`打开卧室灯`
- “关闭客厅和卧室的灯” → `关闭客厅的灯`、`关闭卧室的灯`
- “台灯和落地灯开了么” → `台灯开了么`、`落地灯开了么`
- “开空调” → `开空调`

### 步骤 2 · 逐条 `device resolve`

每条命令调一次 `miloco-cli device resolve`，参数就是用户的**原话拆解**，不要自己翻译成 spec_name / did：

```bash
miloco-cli device resolve [--room <房间>] --target <设备名或类别词> [--action set|get|call] [--property <属性措辞>] [--value <值>]... [--scope auto|single|all]
```

| 参数 | 填什么 | 例 |
| ---- | ------ | -- |
| `--room` | 用户点到的房间；没说就省（target 里带房间也能识别，如 `--target 客厅的落地灯`） | `--room 卧室` |
| `--target` | 设备名 / 类别词，**保留“所有 / 都 / 全部”等复数词**，服务端据此判断是否全做 | `--target 空调`、`--target 所有灯`、`--target 灯都关` |
| `--action` | `set` 控制 / `get` 查询 / `call` 动作；省略则按 property / value 推断 | `--action get` |
| `--property` | 属性 / 动作的用户措辞：温度、亮度、色温、模式、风速、音量、开、关、充电、播报… | `--property 温度` |
| `--value` | 要设的值 / 动作入参（多参重复给）；开 / 关可只写 `--property 开` 不给 value | `--value 26`、`--value "晚安"` |
| `--scope` | 默认 `auto`；用户已明确“全部”而 target 里没带复数词时给 `all` | `--scope all` |

- 多条互不依赖的 resolve **不要**用 `;` 串——每条的 `ambiguity` 都要单独看；同一条消息里一次性发出全部调用即可。
- **感知事件触发 → 用事件“来自：房间”作 `--room`**。⚠️ 事件里 `did=` 是来源设备（摄像头 / 传感器），不是要控制的目标。
- 返回体 `data`：`candidates[]`（did / name / room / online / `spec.spec_name` / `needs_on` / `protected` / `issue`）、`ambiguity`、`hint`、`command_preview[]`。**`hint` 是给你的下一步指令，照做。**

### 步骤 3 · 按 `ambiguity` 处理

| ambiguity | 含义 | 做法 |
| --------- | ---- | ---- |
| `none` | 命中且唯一，或用户明确“全部”→ 多台全做 | 进入步骤 4；`command_preview` 已是可下发命令 |
| `multiple` | 命中多台，用户没说“全部” | **反问“哪个房间 / 哪一台”**（候选名和房间在 `candidates` 里），不默认选一台、也不擅自全做；用户回答房间 → 加 `--room` 重试；用户说“都 / 全部” → 加 `--scope all` 重试 |
| `not_found` | 没有匹配设备 | `miloco-cli device refresh` 后**同参数重试一次**；仍无 → 回“没找到”，**禁止编造 did**。`hint` 若列出已知房间名，先核对房间叫法 |

- 候选带 `issue`（值越界 / 枚举非法 / 该设备无此可写属性 / spec 为空）→ 该台不会出现在 `command_preview`：按 `issue` 文案改对值重试（枚举可选值、`[min,max;step] 单位` 都在里面），或告知用户该设备不支持。
- 候选 `protected: true`（门锁 / 摄像头 / 燃气阀 / 烟感）→ 进入步骤 5 危险批，须二次确认。
- 候选 `online: false` → 照常下发，CLI 会返回“设备离线”。

### 步骤 4 · 生成指令

**4.1 直接用 `command_preview`**：`ambiguity == none` 时，`command_preview` 每行就是一条完整命令（`device control` / `device props` / `device action`），补开关（`--set on@空调 true` 这类带 `@` 的真实开关 spec_name）、枚举值映射、`--set` 拼装都已做好，**原样下发**即可（多条按步骤 6 用 `;` 串）。

**4.2 或加 `--exec` 一步到位**：普通批（非危险设备）可直接 `device resolve … --exec`——`ambiguity == none` 时在**同一进程**里顺序下发全部候选，每台一行结果（含 `did` / `code_msg`），省掉多次 CLI 冷启动；`multiple` / `not_found` 时不会执行，只打印解析结果并返回退出码 1。危险设备**不要**用 `--exec`，先走步骤 5 确认。

**4.3 相对调节（“调高一点”）→ 先查后改**：`device resolve --action get --property 亮度` 取当前值 → 步进（亮度±10 / 色温±500 / 温度±1）→ 再 `device resolve --property 亮度 --value <新值>`。要先拿到当前值，必须**单独一轮**。

**4.4 access 与命令对应**（读 `command_preview` 时知道自己在发什么）

| `spec.access` | 用途 | 命令 |
| ------------- | ---- | ---- |
| 含 `w` | 控制属性 | `device control <did> --set <spec_name> <v> [--set <开关spec_name> true]` |
| 含 `r` | 查询属性 | `device props <did> [spec_name]` |
| `x` | 执行动作 | `device action <did> <spec_name> [<值1> <值2>…]`（**只传值，不传参数名**） |

### 步骤 5 · 安全分流

本步只**分流、不下发**：把步骤 4 生成的命令按是否安全分成**普通批 / 危险批**，交给步骤 6 的下发回合。

- **危险批**：`protected: true` 的候选（**门锁 / 摄像头 / 燃气阀 / 烟雾报警器** 等安全设备）的控制 / 动作（断电、开关机、开锁、关阀）——需二次确认。
- **普通批**：其余设备的 control / action，以及**所有设备的 props 查询**。
- 没有危险指令 → 全部归普通批，下发回合只跑第 1 轮。

### 步骤 6 · 下发和回复

一个“回合”=**下发（6.1）→ 回复（6.2）**。按步骤 5 的分流结果，最多跑两轮：

1. **第 1 轮 · 普通批**：下发后**回复时附上所有危险指令的二次确认**，让用户确认。
2. **第 2 轮 · 危险批**：仅把用户同意的危险指令再下发一遍；未同意的跳过。无危险指令则只有第 1 轮。

> “关客厅灯，顺便关摄像头” → 先把客厅灯关掉、回复“灯已关闭，确定要关闭摄像头吗？”（第 1 轮：普通批下发 + 危险确认）；用户确认后才 `device control` 关摄像头（第 2 轮）。

**6.1 下发**

同一轮 **≥2 条互不依赖**的命令（跨设备批量、多设备查询）→ 用 `;` 串成**一行**一次下发，或用 `device resolve … --exec` 一次进程内完成：

```bash
miloco-cli device control 4912 --set brightness 30 --set on true ; miloco-cli device control 4945 --set brightness 30 --set on true
```

- **用 `;` 不用 `&&`**：`&&` 短路（一台失败后面全不执行）；`;` 不短路、每条都跑、输出有序。❌ 也别发一条、等结果、再发下一条。
- **输出归属**：`control` / `props` 返回体均含 `data.did`，多条按顺序输出、按 did 对号。
- **单次 ≤10 个设备**，超出自动拆分多轮（`hint` 会提示分批）。**离线设备照常下命令**，由 CLI 返回离线错误，agent 层不预拒。
- **主用 `;` 串联**；仅当环境不支持 `;`（沙箱限制等）→ 退化为在同一条消息里一次性发出全部工具调用。

**6.2 回复**

- 控制确认 → 生成简洁清晰的回复（“空调已调到26度”）。
- 查询 / 集合类 → 按需完整（温度值、灯的数量 + 房间分布）。
- 部分失败 → 报失败设备 + 确认成功的；全部失败 → 给原因 + 建议。

## 何时仍需 `device spec`

`device resolve` 覆盖绝大多数“说人话控设备”的场景；下面几种边缘情况再查完整 spec：

- 用户要的属性 / 动作 resolve 找不到（候选 `issue` 说“没有可写 / 可读 / 可调用的 X 属性”），但你怀疑设备其实有、只是叫法太偏 → `device spec <did>` 看全量 spec_name，再用 `--property <spec_name>` 重试（resolve 接受 spec_name / `spec_name@模块` 原样传入）。
- 多入参 action（如 `execute-text-directive` 的 `text-content,silent-execution`）想确认参数顺序 / 类型 → 看 `spec.in_params`，不够再 `device spec`。
- 厨房电器等需要“先设参数再 `start-cook`”的多步流程，启动 action 的确切 spec_name 以 `device spec` 为准。
- 用户问“这台设备都能干什么” → 直接 `device spec` 列能力。

`device list` 仍可用于“家里有几盏灯”这类**盘点**问题（行式记录、可 grep），但**定位要控制的设备一律走 resolve**，别再 grep 猜 did。

## 设备控制指导

> 设备专属控制知识库：部分设备的正确控制方式与通用流程不同，命中下列设备时以此处为准。

### 智能音箱：`play-text` vs `execute-text-directive`

两个 action 语义完全不同，按用户意图选对（resolve 的 `--property 播报` → `play-text`，`--property 指令` → `execute-text-directive`，也可直接传 spec_name）：

| 命令 | 语义 |
| ---- | ---- |
| `play-text` | TTS 文字转语音，音箱**逐字念出**传入的文字 |
| `execute-text-directive` | 小爱同学指令，等同于对音箱说“嘿小爱，xxx”，**小爱理解语义后自己执行** |

- 用户要音箱**念出/播报一段话**（“让音箱说‘晚安’”）→ `play-text`。
- 用户要**借音箱下达小爱指令**（“让音箱查下天气/关灯”、“让音箱放首歌”）→ `execute-text-directive`。

> **不为自检 / 探测而播报。** `play-text` 会让音箱当场真出声，没有 dry-run；别用它测“音箱能不能响 / 命令能不能跑”——真要播时按流程发即可，设备或参数有问题会在调用时报错（见“异常处理”，注意“CLI 超时 3 秒重试一次”叠在探测上会重复扰人）。这只约束**自检式**播报，不影响“让音箱说晚安”这类正常请求。主动外发的整体决策护栏见 miloco-notify skill。

### 厨房电器（微波炉 / 烤箱 / 热水器 等）：先设参数，再 `start-cook` 启动

这类设备**不能只设温度、也不能 `set on true` 就开始工作**：必须**先设好温度、时间等参数**，再调用启动类 action（如 `start-cook`）才会真正运转。resolve 对厨房电器**不会自动补开关**（`needs_on` 为空），参数设好后再 `device resolve --action call --property 启动`（或直接 `device action <did> start-cook`）；启动 action 的确切 spec_name 以该设备 `device spec` 为准。

## 旁路操作

- **scene_trigger**：用户说的是场景**名称** → 先 `scene list` 拿 名称→scene_id 映射 → `scene trigger <id>`；名称匹配到多个 → 列出追问。
- **refresh**：用户要“刷新 / 更新设备列表” → 直接 `miloco-cli device refresh`，无需走命令拆分 → resolve → 下发；返回最新设备数后回复“已刷新，共 N 个设备”。

## 异常处理

| 异常 | 处理 | 回复 |
| ---- | ---- | ---- |
| `ambiguity: not_found` | `device refresh` → 同参数重试一次 | “没找到，正在刷新…” |
| `ambiguity: multiple` | 按 `candidates` 列候选追问；用户明确全部 → `--scope all` | “找到 N 个，哪个？” |
| 候选 `issue` 值越界 | `issue` 给出 `[min,max;step] 单位` → 按范围改对重试 | “亮度范围1-100” |
| 候选 `issue` 枚举非法 | `issue` 列出全部可选值 → 挑对的重试 | “风速可选 自动/1-3 档” |
| 候选 `issue` 无此属性 | 见“何时仍需 device spec”；确无 → 告知不支持 | “{设备名}不支持调{属性}” |
| 设备离线 | **照常下命令**，CLI 返回体 `results[]`/`result` 的 `code_msg` 会标“设备离线” | “{设备名}离线了” |
| 设备侧执行失败 | 看返回体 `code_msg` 中文原因（属性不可写 / 属性不存在 / 属性值不正确等）→ 据此回复或改对重发 | “{设备名}该属性不可写” |
| 用 control 调 action | CLI 报 “is an action… 请改用 device action” → 切 `device action` | （自动处理） |
| CLI 超时 | 3 秒后重试一次 | “超时，重试中…” |

## 关键规则

1. **定位设备 / spec_name 一律经 `device resolve`**——不翻目录猜 did，不手写 spec_name（`@` 后缀、`play-text` 等因设备而异，服务端已按每台的真实 spec 给出）。
2. **安全设备控制必须二次确认**——`protected: true`（门锁/摄像头/燃气阀/烟雾报警器），不用 `--exec`（步骤5）。
3. **多候选未说“全部”必反问**——`ambiguity: multiple` 不默认挑一台、不擅自全做（步骤3）。
4. **离线设备照常下命令**——由 CLI 兜底。

## 边界

- ❌ 不支持非米家生态设备 / 第三方 API / 绕过安全规范的指令
- ❌ 禁止编造 did / spec_name / model
- ⚠️ 单次 ≤10 个设备，超出自动拆分
- ✅ 支持 `scene trigger` 触发已有场景、`device refresh` 刷新缓存

## 示例

| 用户说 | resolve 调用 | 结果（`command_preview` / 处理） |
| ------ | ------------ | -------------------------------- |
| “把卧室空调调到26度” | `device resolve --room 卧室 --target 空调 --property 温度 --value 26 --exec` | 一次进程内下发 `device control 4962 --set target-temperature 26 --set on@空调 true`（开关 spec_name 由服务端按该设备 spec 选出） |
| “关客厅灯”（客厅仅一盏灯） | `device resolve --room 客厅 --target 灯 --property 关 --exec` | `device control 4912 --set on false` |
| “所有灯关掉” | `device resolve --target 所有灯 --property 关 --exec` | 全屋灯逐台下发，名叫“灯”的传感器不会混入 |
| “把灯调暗一点”（全屋多盏、未指房间、没说全部） | `device resolve --target 灯 --property 亮度` | `ambiguity: multiple` → 反问“哪个房间的灯？” |
| “空调设定的温度”（查询） | `device resolve --target 空调 --action get --property 设定温度` | `device props 4962 target-temperature` → 26 |
| “客厅多少度”（环境数据） | `device resolve --room 客厅 --target 温湿度传感器 --action get --property 温度` | `device props ht01 temperature` → 24.5 |
| “扫地机回去充电”（动作） | `device resolve --target 扫地机 --action call --property 充电 --exec` | `device action 4981 start-charge` |
| “让音箱说晚安” | `device resolve --target 音箱 --action call --property 播报 --value 晚安 --exec` | `device action 5120 play-text 晚安` |
| “家里有几盏灯”（盘点） | `device list \| grep -E '灯\|light'` | 数行数、按房间汇总 |

**安全设备** — “关客厅灯，顺便关摄像头”：两条 resolve；灯 `--exec` 直接关（普通批）；摄像头候选 `protected: true` → 回复“灯已关闭，确定要关闭摄像头吗？”；用户确认后再按其 `command_preview` 下发 `device control cam_001 --set on false`。

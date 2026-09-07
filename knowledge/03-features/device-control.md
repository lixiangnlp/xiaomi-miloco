# 设备控制

## 背景与目标

用户想让 AI 帮自己控制家里的灯、空调、风扇。传统方式需要打开米家 App 找到设备再操作；Miloco 让 Agent 直接理解用户意图并执行。

设备控制模块提供完整的米家设备操作能力：单属性写入、批量属性写入、动作调用、属性查询、场景执行，覆盖用户在 Agent 对话中所有可能的设备操作需求。

---

## 产品面

### 能做什么

- **属性控制**：设置设备的任意可写属性（亮度、色温、温度、开关、模式）；同一设备多属性可合并为一次请求
- **动作调用**：触发设备支持的动作（如音箱播报 TTS、扫地机开始清扫、空气净化器启动自动清洁）
- **属性查询**：读取设备当前状态，用于 Agent 回答"客厅灯现在是多少亮度"
- **场景执行**：一键触发米家配置的智能场景（多设备联动预设），如"回家模式""睡眠模式"
- **Scope 管理**：配置 Miloco 管控哪些家庭和摄像头，是设备接入的前置配置

### 典型场景

**场景 1 — 对话控制**：对 Agent 说"把客厅的灯调到 60% 亮度"。Agent 选择 `miloco-devices` Skill，通过 CLI 调 `/api/miot/devices/{did}/control`，后端执行属性写入，用户约 1 秒内看到灯光变化。

**场景 2 — 规则自动化**：感知流水线检测到"有人进入书房"，STATIC 规则触发，`RuleRunner` 经 `miot.service.execute_control` 打开书房台灯，无需 Agent 介入，无 LLM 额外调用。

**场景 3 — 家庭面板操作**：用户在浏览器打开家庭面板"设备"标签，按房间浏览设备列表，点击开关或滑块直接发起控制请求。

**场景 4 — 音箱 TTS**：Agent 需要向用户播报提醒，通过 `miloco-devices` Skill 找到房间内的音箱设备，调用 `play-text` 动作完成播报。

### 能力边界

- 仅操作已绑定小米账号、且被纳入启用家庭（scope）的设备，其余请求一律拒绝（返回 scope 校验错误）
- 受保护类别设备（门锁 / 摄像头 / 可视门铃 / 燃气 / 烟感，`safety.protected_categories`）的控制不直接执行，而是 stage 等待用户确认（见下“危险设备闸门”）；参数由服务端按 spec 校验，越界 / 非法枚举 / 只读属性返回 422
- 设备联网状态由小米云管理，Server 不感知"控制是否真正送达硬件"——返回成功仅表示指令已发出
- 场景执行由小米云侧完成，Server 只负责转发
- 不支持自定义协议或非米家生态设备
- MiOT OAuth 未绑定时相关端点抛 `MiotOAuthException`

---

## 研发面

### 架构概览（数据流图）

```
CLI / Agent（miloco-devices Skill）
  → POST /api/miot/devices/{did}/control
  → MiotService.control_device（scope 校验 → 受保护类别判定：命中则 stage 返回）（miot/service.py）
  → execute_control（服务端值校验 + 下发 + action_ledger，唯一执行核心）（miot/service.py + miot/gate.py）
  → MiotProxy（miot/client.py）
  → MIoTClient.http_client（MIoTHttpClient，backend/miot/src/miot/cloud.py）
  → 小米云 HTTP API → 设备
```

属性查询入口为 `MiotService.get_device_status`，场景触发为 `trigger_scene` → `MiotProxy.execute_miot_scene`，链路结构相同。控制写入 / 查询 / 动作调用统一走 Cloud HTTP，不走局域网直连（见下「控制写入固定走 Cloud HTTP」）。

规则触发的 STATIC 控制路径不经 HTTP 层，但同样汇入 `execute_control`（`deny_protected=True`）：`RuleRunner`（`rule/runner.py`）自己做幂等 / 冷却判断，再把动作交给同一个执行核心过闸门、校验、落台账——闸门规则只定义一次，所有路径共用。

### 核心模块

**MiotService**（`miot/service.py`）

业务编排层，主要职责：

- **scope 校验**：检查请求 did 所属家庭是否在启用集内（KV 存储 home 白名单）
- **危险设备闸门**：命中 `safety.protected_categories` 的设备不执行、stage 到 `ChangeLedger`；`apply_change` / `list_changes` / `discard_change` 管理待确认变更
- **控制类型分发**：`execute_control` 将 `set_property` / `set_properties` / `call_action` 请求转换为对应 MiOT 参数类型，并按设备 spec 校验值
- **LRU 设备目录维护**：记录用户操作过的设备及属性，确保其出现在 Agent 设备目录（catalog）中
- **home / camera scope 管理**：`switch_home` / `toggle_camera` 写 KV 后触发后台刷新并同步感知层 adapter
- **OAuth 校验**：未绑定或 token 过期时抛 `MiotOAuthException`

**MiotProxy**（`miot/client.py`）

Server 代理层，主要职责：

- **token 生命周期**：后台自动刷新，失效时清空 OAuth 缓存；token 通过 `KVRepo` 持久化，重启后自动恢复
- **数据缓存**：设备/摄像头/场景列表内存维护，重启时重新拉取
- **实时事件订阅**：注册 MIPS 云 MQTT 回调（设备改名 / 换房换家、家庭场景变更、摄像头云端上线 / 离线），经 `mips_listeners.py` 的防抖监听器刷新对应缓存，使本地缓存无需轮询即与云端收敛；设备绑定 / 移入受管家庭触发的设备欢迎见 [device-welcome.md](device-welcome.md)
- **device spec 按需缓存**：首次使用时加载并缓存，避免启动时全量拉取拖慢启动
- **摄像头 manager 管理**：维护每个摄像头的 `CameraVisionHandler`（`miot/camera_handler.py`）实例

**MIoTClient**（`backend/miot/src/miot/client.py`）

MiOT SDK 顶层客户端，聚合 Cloud、LAN、mDNS、MQTT、摄像头等子模块，对 MiotProxy 暴露统一异步接口。详见 [sdk-miot.md](../05-external-deps/sdk-miot.md)。

### 意图解析（intent resolve）

`POST /api/miot/intent/resolve`（`miot/router.py` → `MiotService.resolve_intent` → `miot/intent.py`）把“说人话控设备”里可确定性判断的部分从 `miloco-devices` Skill 的提示词下沉到后端：Agent 只需把用户措辞拆成 `room / target / action / property / value / scope` 发过来，后端返回可直接下发的候选。

**输入**：`{"room": "卧室", "target": "空调", "action": "set", "property": "温度", "value": 26, "scope": "auto"}`。`target` 可带房间前缀（“客厅的落地灯”）和复数词（“所有灯”“灯都关”）；`property` 是用户措辞（温度 / 亮度 / 开 / 关 / 充电…），也接受 spec_name 原样传入；`action` 省略时按 property / value 推断。

**解析规则**（全在 `miot/intent.py`，纯函数、无 I/O，数据源是与 catalog / `device list` 相同的 `get_home_info`）：

1. 房间：精确匹配 → 包含匹配；`room` 为空时尝试从 `target` 前缀拆出已知房间名。
2. 目标：整词是类别词（`INTENT_SYNONYMS`，中英文同义词表，种子来自 `whitelist.json` 的类别列）→ 按 category 选（名叫“灯”的传感器不会混进“所有灯”）；否则精确设备名 → 名字互含 → 子设备别名 → 文本含类别词。
3. 属性 / 动作：`PROPERTY_SYNONYMS` / `ACTION_SYNONYMS` 同义词表 → spec description 子串兜底；查询（get）优先只读传感读数（“温度”→ `temperature` 而非 `target-temperature`）；同 type_name 多条（`on@空调` / `on@指示灯`）按“属性所在 service → service_type_name == category → iid 序”选，spec_name 带 `@模块` 后缀的规则与 CLI catalog 一致（`resolve_spec_keys`）。
4. 值：bool 归一（含中文开 / 关）、字符串转数字、枚举名映射到枚举值；枚举 / 范围校验与 CLI `home_info.validate_value` 同口径，不合法则写入候选的 `issue` 而不是抛错。
5. 补开关：控制非 on 属性且设备有可写 `on` → `needs_on` 给出与本次属性同 service 的开关 spec_name + iid；厨房电器（`KITCHEN_CATEGORIES`）不补。
6. `protected`：门锁 / 摄像头 / 燃气阀 / 烟感等安全类别只打标记，二次确认由 Skill 流程执行。

**输出**：`candidates[]`（did / name / room / category / online / 紧凑 `spec` / `needs_on` / `protected` / `issue`）、`ambiguity`（`none` / `multiple` / `not_found`）、`hint`（给 Agent 的下一步指令，如“命中 3 台；用户未说‘全部’，请反问房间 / 哪一台”“可 device refresh 后重试；禁止编造 did”）、`command_preview[]`（可原样执行的 `miloco-cli` 命令）。多候选而用户未说“全部”时不擅自决定，`scope=all` 才全做。

**CLI**：`miloco-cli device resolve --room 卧室 --target 空调 --property 温度 --value 26 [--scope all] [--exec]`。`--exec` 在 `ambiguity == none` 时于同一进程内顺序下发全部候选（iid 由后端给出，不再拉 home_info），每台一行带 `did` / `code_msg` 的结果；否则打印解析结果并以退出码 1 结束。

**设计取舍**：工具边界处逻辑终止——房间 / 同义词 / spec / 校验这些“查表就能定”的事由后端回答，模型只负责拆分命令与处理 `ambiguity`；一次 resolve 替代原先“翻目录 → grep → device spec → 自行拼命令”的多轮推理，也把 `;` 串联的多次 CLI 冷启动合并成一次进程。

### Scope 机制

Scope 定义了"Miloco 管控哪些设备"的边界，分为两个维度：

**家庭维度（Home Scope）**：用户的小米账号下可能有多个家庭（如"公寓""父母家"），Miloco 同一时刻只管控一个家庭的设备。启用的家庭白名单持久化在 `miloco.db::kv` 表中，由 `filter.py`（`miot/filter.py`）读取后应用于过滤。

**摄像头维度（Camera Scope）**：在启用家庭内，用户可以进一步禁用某些摄像头（如不想让 Miloco 看客厅）。被禁用的摄像头 DID 以黑名单形式存在 KV 表中——新摄像头默认被感知，用户选择性关闭。同时启用的摄像头数量有上限（4 台，`filter.py::MAX_ENABLED_CAMERAS`，经状态接口下发前端作为唯一来源），主动启用超限或启用离线摄像头会被 `toggle_camera` 拒绝。

**Scope 过滤的作用点**：

- 设备列表 / 场景列表接口返回前，`filter.py` 过滤掉不在启用家庭的条目
- 控制设备前，`MiotService` 校验 did 所属家庭是否在启用集内，不在则拒绝
- 摄像头流水线层：摄像头拉流（native PPCS 会话 + 解码）与感知投喂**共用同一选择口径** `select_active_camera_dids`（`miot/filter.py`）——在启用家庭内、未拉黑、在线、且按 did 截断到启用上限，拉流集即投喂集不漂移。scope 变更后 `MiotService` 先 `refresh_cameras` 按新口径建 / 销 camera manager（关闭 / 移出家庭 / 离线 / 超额的摄像头会停掉 native 会话与解码），再同步感知 adapter 的投喂订阅，无需重启服务

**Scope 变更**：切换家庭时按「先加目标家、再移其余家」的顺序写 KV，保证切换过程中启用集不瞬时空掉（空集会触发兜底自动选家、扰动感知）；落库后再通知感知层 adapter 同步，无需重启服务。账号切换时清空所有家庭与摄像头 scope，回到干净状态。若启用集为空或无效，自动回退到首个可见家庭，避免感知全黑。

**切换家庭时重置 Agent 会话**：切换真正改变启用集时，`switch_home` 会后台 best-effort 触发一次 openclaw 侧 miloco agent 会话的重置（`agent_client.reset_agent_sessions` → 插件 `reset_sessions` webhook，见 [Agent 集成](openclaw-integration.md)），清掉旧家庭遗留在会话里的上下文（设备 / 房间 / 习惯），避免旧家庭上下文串入新家庭造成干扰。空切（重复选中当前已是唯一启用的家庭）跳过重置，以免白删仍有效的热上下文；`list_homes` 启用集为空时的兜底自动选家属同一 bug class，同样触发重置。整个重置纯后台 fire-and-forget，openclaw 不可达只 WARN、绝不阻塞或打断切换本身。待重置的会话集以 `MILOCO_SESSION_KEYS`（`dispatch/dispatcher.py`，由 `_ROUTE` 派生的唯一事实源）为准。

**在哪配置**：web 面板"概览"标签（摄像头在用切换）和顶部 TopBar 家庭切换器；也可通过 `miloco-miot-scope` Skill 在 CLI 完成。

### 关键设计决策

#### 控制写入固定走 Cloud HTTP

设备属性写入 / 查询 / 动作调用统一经 `MiotProxy` → `MIoTClient.http_client`（`MIoTHttpClient`，`backend/miot/src/miot/cloud.py`）发往小米云 HTTP API——不走局域网直连。`MIoTClient` 内的 LAN（`backend/miot/src/miot/lan.py`）/ mDNS 子模块用于局域网设备发现与在线状态维护，摄像头实时画面走 PPCS 串流（见 [live-camera-view](live-camera-view.md)），均不承载控制写入。SDK 各路径能力见 [sdk-miot.md](../05-external-deps/sdk-miot.md)。

**STATIC 规则为什么也走 execute_control**：早期规则路径直接调 `MiotProxy`，理由是 scope 校验冗余、追求低延迟。但“危险设备需二次确认”“值必须在 spec 范围内”这些规则若只在 CLI / Skill 文案里存在，规则路径就成了绕过点。现在闸门与校验定义在 `execute_control` 一处，规则路径以 `deny_protected=True` 接入（无人可确认，命中受保护类别直接拒绝并落台账），scope 校验仍留在 `MiotService` 层不重复。spec 按 urn 内存缓存，额外开销可忽略。

#### 危险设备闸门（stage / apply）

对照 Anthropic 的原则“the model stages; a person or a policy applies”：模型只能提议，执行由人或策略批准，并且这条规则要在 harness / 后端强制、apply 时再检查、只定义一次让所有路径共用。

- **判定**：设备类别取 urn 第 4 段（`urn:miot-spec-v2:device:{category}:…`），命中 `safety.protected_categories`（默认 `lock` / `camera` / `video-doorbell` / `gas-valve` / `gas-sensor` / `smoke-sensor`）即受保护。判定读的是**当前**配置。
- **stage**：`control_device` 对受保护设备先做服务端值校验（错参立刻 422），再写入内存态 `ChangeLedger`（`miot/gate.py`，TTL `safety.stage_ttl_sec` 默认 600 秒，进程重启即清空），返回 `{staged: true, change_id, summary, expires_at, confirmation_channel: "mihome", next}`，并落一行 `success=0, result_msg=staged:<id>` 的 action_ledger。
- **apply**：`POST /api/miot/changes/{change_id}/apply` 带 `confirm_token`。token 用常量时间比较、匹配即移出台账（一次性，不可重放；猜错不销毁变更）。随后按当前状态重新把关：scope（设备仍在启用家庭）、spec 值校验，再经同一 `execute_control` 下发——stage 时通过不等于 apply 时通过。
- **list / discard**：`GET /api/miot/changes`（不含 token）、`DELETE /api/miot/changes/{change_id}`。
- **CLI**：`device control` / `device action` 收到 `staged` 时打印含 `next` 提示的 JSON；新增 `device changes` / `device apply <id> --token <t>` / `device discard <id>`。
- **凭据投递**：后端直接把操作详情、变更编号、有效期和一次性 `confirm_token` 推送到用户的米家 App。stage / list / CLI 响应只返回变更信息，不含确认码；用户核对并同意后，从推送复制确认码回传，agent 才能代为 apply。普通服务 token 本身不能批准变更。投递或云端通知模板清理失败即撤销变更；异常响应和日志不回显通知内容。确认通知依赖米家绑定和推送可用，失败时不会执行设备，也不回退为把码交给 agent。

**Scope 为什么用 KV 而非配置文件**：Scope 是运行期可变的用户选择，不是静态配置。KV 表提供事务性单行原子写，读路径走内存缓存，变更即生效，与配置文件的"重启才生效"语义不同。

### 如果我要修改设备控制相关功能

| 修改目标              | 去看哪个文件                                                                     |
| --------------------- | -------------------------------------------------------------------------------- |
| 修改 scope 过滤逻辑   | `miot/filter.py`                                                                 |
| 修改 scope CRUD 逻辑  | `miot/service.py`（`switch_home` / `toggle_camera` / `list_cameras_with_state`） |
| 修改设备控制 API 端点 | `miot/router.py`                                                                 |
| 修改值校验 / 受保护类别 / 待确认台账 | `miot/gate.py`（纯逻辑）、`miot/service.py::execute_control`、`config/settings.py::SafetySettings` |
| 修改意图解析规则（同义词 / 补 on / 校验） | `miot/intent.py`（`INTENT_SYNONYMS` / `PROPERTY_SYNONYMS` / `KITCHEN_CATEGORIES` / `PROTECTED_CATEGORIES`） |
| 修改 MiOT SDK 封装层  | `miot/client.py`（MiotProxy），更底层看 `backend/miot/src/miot/`                 |
| 修改摄像头管理逻辑    | `miot/camera_handler.py`（`CameraVisionHandler`）                                |

### 设备控制相关 API 路径

主要入口：`POST /api/miot/intent/resolve`（意图解析），`POST /api/miot/devices/{did}/control`（控制设备；受保护设备返回 `staged`），`GET /api/miot/changes` / `POST /api/miot/changes/{change_id}/apply` / `DELETE /api/miot/changes/{change_id}`（待确认变更），`GET /api/miot/device_list`（设备列表），完整端点见 `miot/router.py`。

### 与其他模块的关系

**上游**：`miloco-devices` Skill 通过 CLI 调 `/api/miot/devices/{did}/control`，是主要控制入口。`RuleRunner`（`rule/runner.py`）在 STATIC 规则条件满足时调用 `miot.service.execute_control`（与 CLI 同一闸门）。

**下游**：所有控制 / 查询 / 动作指令最终经 `MiotProxy` → `MIoTClient.http_client` 固定发往小米云 HTTP API，不走 LAN 直连（见上「控制写入固定走 Cloud HTTP」）。

**互动**：scope 变更（切换家庭 / 启停摄像头）后，`MiotService` 先按 `select_active_camera_dids` 口径重建 / 销毁 camera manager（停用 / 移出家庭的摄像头停掉 native 会话），再同步感知层 adapter 的投喂订阅，无需重启服务。OAuth 完成后，`MiotService` 主动重启感知引擎，让摄像头 adapter 重新注册帧回调。

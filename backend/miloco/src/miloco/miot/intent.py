# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""设备控制意图解析（``POST /api/miot/intent/resolve`` 的纯逻辑层）。

把 skill 提示词里原本要模型自己完成的“找设备 → 挑 spec → 校验值 → 决定补不补
on”这一串确定性推理下沉到后端：输入用户的自然表述（房间 / 目标 / 属性 / 值），
输出可直接下发的候选列表 + 一句给模型的处理指令（``hint``）。

本模块只做纯函数计算，不做 I/O；home_info 由 :class:`MiotService` 拉好后传入。
数据形态与 ``MiotProxy.get_home_info_data`` 一致（devices[].spec 为 lite spec）。

设计原则（对齐“工具边界处逻辑终止、模型判断接管”）：
- 能确定性判断的（房间过滤、同义词、枚举 / 范围校验、开关 spec_name 选择）全在这里做；
- 需要用户参与的（多候选未说“全部”、值越界）不擅自决定，用 ``hint`` 告诉模型下一步；
- 返回体只带模型下发命令所需字段，不回吐整份 spec。
"""

from __future__ import annotations

import re
import shlex
from typing import Any

# ─── 同义词表 ─────────────────────────────────────────────────────────────────
# category（spec URN 第 4 段，如 urn:miot-spec-v2:device:light:…）→ 用户可能的叫法。
# 中文取自 whitelist.json 的 category 列与 SKILL.md 示例，英文即 category 本身 / 常见别名。
INTENT_SYNONYMS: dict[str, tuple[str, ...]] = {
    # 注意只放“类别词”，不放“台灯 / 落地灯”这类子类型词——那些先按设备名匹配，
    # 匹配不到再靠 “灯” 子串回落到 category（见 _match_target 的优先级）。
    "light": ("灯", "灯具", "light", "lamp"),
    "air-conditioner": ("空调", "冷气", "air-conditioner", "ac", "aircon"),
    "air-condition-outlet": ("空调伴侣", "air-condition-outlet"),
    "curtain": ("窗帘", "帘子", "窗纱", "curtain"),
    "outlet": ("插座", "插排", "排插", "outlet", "plug", "socket"),
    "switch": ("开关", "墙壁开关", "switch"),
    "vacuum": ("扫地机", "扫地机器人", "扫拖机器人", "扫地机器", "vacuum", "robot"),
    "mopping-machine": ("擦地机", "拖地机", "mopping-machine"),
    "speaker": ("音箱", "音响", "小爱", "小爱同学", "喇叭", "speaker"),
    "camera": ("摄像头", "摄像机", "监控", "camera"),
    "lock": ("门锁", "智能锁", "锁", "lock"),
    "humidifier": ("加湿器", "humidifier"),
    "dehumidifier": ("除湿机", "除湿器", "dehumidifier"),
    "air-purifier": ("净化器", "空气净化器", "空净", "air-purifier", "purifier"),
    "air-fresh": ("新风机", "新风", "air-fresh"),
    "fan": ("风扇", "电风扇", "电扇", "落地扇", "fan"),
    "ceiling-fan": ("吊扇", "凉霸", "ceiling-fan"),
    "water-heater": ("热水器", "water-heater"),
    "kettle": ("热水壶", "水壶", "烧水壶", "kettle"),
    "health-pot": ("养生壶", "health-pot"),
    "television": ("电视", "电视机", "television", "tv"),
    "tv-box": ("电视盒子", "盒子", "tv-box"),
    "projector": ("投影仪", "投影", "projector"),
    "heater": ("电暖器", "暖风机", "电暖风", "取暖器", "heater"),
    "bath-heater": ("浴霸", "bath-heater"),
    "thermostat": ("温控器", "地暖", "thermostat"),
    "hood": ("油烟机", "抽油烟机", "烟机", "hood"),
    "microwave-oven": ("微波炉", "microwave-oven", "microwave"),
    "oven": ("烤箱", "蒸烤箱", "oven"),
    "cooker": ("电饭煲", "电饭锅", "cooker"),
    "induction-cooker": ("电磁炉", "induction-cooker"),
    "pressure-cooker": ("压力锅", "高压锅", "pressure-cooker"),
    "air-fryer": ("空气炸锅", "炸锅", "air-fryer"),
    "dishwasher": ("洗碗机", "dishwasher"),
    "washer": ("洗衣机", "washer"),
    "clothes-dryer": ("干衣机", "烘干机", "clothes-dryer", "dryer"),
    "fridge": ("冰箱", "fridge", "refrigerator"),
    "water-purifier": ("净水器", "water-purifier"),
    "water-dispenser": ("饮水机", "净饮机", "water-dispenser"),
    "airer": ("晾衣架", "晾衣机", "airer"),
    "diffuser": ("香薰机", "香薰", "diffuser"),
    "gateway": ("网关", "gateway"),
    "temperature-humidity-sensor": ("温湿度传感器", "温湿度计", "温度计", "湿度计", "temperature-humidity-sensor"),
    "motion-sensor": ("人体传感器", "人体感应", "motion-sensor"),
    "magnet-sensor": ("门窗传感器", "门磁", "magnet-sensor"),
    "occupancy-sensor": ("存在传感器", "人在传感器", "occupancy-sensor"),
    "submersion-sensor": ("水浸传感器", "submersion-sensor"),
    "gas-sensor": ("燃气报警器", "燃气传感器", "gas-sensor"),
    "smoke-sensor": ("烟雾报警器", "烟感", "smoke-sensor"),
    "gas-valve": ("燃气阀", "气阀", "gas-valve"),
    "pet-feeder": ("喂食器", "宠物喂食器", "pet-feeder"),
    "pet-drinking-fountain": ("宠物饮水机", "pet-drinking-fountain"),
    "electric-blanket": ("电热毯", "electric-blanket"),
    "bed": ("智能床", "床", "bed"),
    "massager": ("按摩器", "massager"),
    "walking-pad": ("走步机", "walking-pad"),
    "treadmill": ("跑步机", "treadmill"),
}

# 泛指“传感器”时的 category 集合（用户说“传感器”通常是查询）。
_SENSOR_CATEGORIES: frozenset[str] = frozenset(
    c for c in INTENT_SYNONYMS if c.endswith("-sensor")
)
_GENERIC_WORDS: dict[str, frozenset[str]] = {
    "传感器": _SENSOR_CATEGORIES,
    "sensor": _SENSOR_CATEGORIES,
}

# 厨房电器：设 on 不会启动工作，须先设参数再调 start-cook 类 action → 不自动补 on。
KITCHEN_CATEGORIES: frozenset[str] = frozenset({
    "microwave-oven", "oven", "cooker", "induction-cooker", "pressure-cooker",
    "air-fryer", "kettle", "health-pot", "water-heater", "dishwasher",
    "multifunction-cooking-pot", "juicer",
})

# 安全设备：本 PR 只在候选上打 ``protected`` 标记（二次确认由调用方 / 后续 PR 强制）。
PROTECTED_CATEGORIES: frozenset[str] = frozenset({
    "lock", "camera", "gas-valve", "gas-sensor", "smoke-sensor", "video-doorbell",
})

# 上游 issue #36：米家 app 把智能通断器 / 墙壁开关 / 插座也归到“灯光”里，用户说“灯”时
# 实际要控制的常是这些设备，而它们的 category（spec URN 第 4 段）并不是 light。
# 取值与 whitelist.json 的 service_type_name 一致：switch（单控 / 屏显开关）、
# outlet（插座 / 通断器）、controller-panel（控制面板）。这类设备只作“可能控灯”的
# 候选列出、不进 command_preview——是不是灯只有用户知道，由模型反问确认。
LIGHT_CONTROL_FALLBACK_CATEGORIES: frozenset[str] = frozenset({
    "switch", "outlet", "controller-panel",
})
LIGHT_CONTROL_FALLBACK = "light-control-fallback"

# 属性同义词：用户措辞 → 按优先级排列的 spec type_name。
PROPERTY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "开": ("on",), "开启": ("on",), "打开": ("on",), "开机": ("on",),
    "关": ("on",), "关闭": ("on",), "关掉": ("on",), "关机": ("on",),
    "开关": ("on",), "电源": ("on",), "power": ("on",), "switch": ("on",),
    "温度": ("target-temperature", "temperature"),
    "设定温度": ("target-temperature",), "目标温度": ("target-temperature",),
    "当前温度": ("temperature",), "室温": ("temperature",), "温度多少": ("temperature",),
    "湿度": ("target-humidity", "relative-humidity", "humidity"),
    "设定湿度": ("target-humidity",), "当前湿度": ("relative-humidity", "humidity"),
    "亮度": ("brightness",), "明暗": ("brightness",),
    "色温": ("color-temperature",), "颜色": ("color",), "彩色": ("color",),
    "模式": ("mode",), "风速": ("fan-level", "stepless-fan-level", "fan-speed"),
    "风量": ("fan-level", "stepless-fan-level"), "档位": ("fan-level", "speed-level"),
    "风向": ("vertical-swing", "horizontal-swing"), "摆风": ("vertical-swing", "horizontal-swing"),
    "上下摆风": ("vertical-swing",), "左右摆风": ("horizontal-swing",),
    "音量": ("volume",), "静音": ("mute",),
    "电量": ("battery-level",), "电池": ("battery-level",),
    "pm2.5": ("pm2.5-density", "pm25-density"), "pm25": ("pm2.5-density", "pm25-density"),
    "二氧化碳": ("co2-density",), "甲醛": ("hcho-density",), "tvoc": ("tvoc-density",),
    "开合": ("current-position", "target-position"), "位置": ("target-position", "current-position"),
    "开合度": ("target-position", "current-position"), "进度": ("current-position",),
    "运行状态": ("status",), "状态": ("status", "on"), "工作状态": ("status",),
    "定时": ("countdown-time", "off-delay-time"), "倒计时": ("countdown-time", "off-delay-time"),
    "延时关": ("off-delay-time",), "童锁": ("physical-controls-locked",),
    "指示灯": ("indicator-light", "on"), "滤芯": ("filter-life-level",),
    "水量": ("water-level",), "水位": ("water-level",),
    "清扫模式": ("mode", "sweep-type"), "吸力": ("mode", "fan-level"),
    "光照": ("illumination",), "亮度值": ("illumination",),
    "门": ("contact-state",), "有人": ("motion-state", "occupancy-status"),
}

# 动作同义词：用户措辞 → spec action type_name（按优先级）。
ACTION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "充电": ("start-charge",), "回充": ("start-charge",), "回去充电": ("start-charge",),
    "扫地": ("start-sweep", "start-clean"), "清扫": ("start-sweep", "start-clean"),
    "开始清扫": ("start-sweep", "start-clean"), "停止清扫": ("stop-sweeping", "stop-clean"),
    "暂停": ("pause", "pause-sweeping"), "拖地": ("start-mop",),
    "播报": ("play-text",), "说": ("play-text",), "念": ("play-text",), "tts": ("play-text",),
    "指令": ("execute-text-directive",), "小爱指令": ("execute-text-directive",),
    "启动": ("start-cook", "start"), "开始烹饪": ("start-cook",), "开始加热": ("start-cook",),
    "取消": ("cancel-cooking", "cancel"), "停止": ("stop-cooking", "stop"),
    "切换": ("toggle",), "翻转": ("toggle",),
    "重置滤芯": ("reset-filter-life",), "重置": ("reset-filter-life",),
}

# 复数 / 全体标记：命中即视为“用户明确表达全部”。
_PLURAL_MARKERS: tuple[str, ...] = (
    "所有", "全部", "全屋", "全家", "整屋", "整个家", "家里的", "家里所有",
    "每个", "每台", "每盏", "各个", "全都", "全开", "全关", "都", "all", "every",
)
# 动词 / 助词：仅用于从 target 里剥掉，不参与匹配。长词优先。
_VERB_WORDS: tuple[str, ...] = (
    "帮我", "给我", "请把", "请", "把", "打开", "关闭", "关掉", "开启", "启动", "停止",
    "调到", "调成", "调节", "调", "设成", "设为", "设置", "设", "开了么", "开了吗",
    "开着吗", "开着么", "怎么样", "多少", "一下", "一点", "了", "的", "吗", "么", "呢",
    "开", "关",
)

_IID_RE = re.compile(r"^(prop|action)\.(\d+)\.(\d+)$")
_KEY_DESC_RE = re.compile(r"^([a-z0-9][a-z0-9.-]*)@([^\s|,:=@]+)$")
_KEY_BARE_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_DESC_FORBID_RE = re.compile(r"[\s|,:=@]+")

_BOOL_TRUE = frozenset({"true", "1", "on", "yes", "开", "打开", "开启", "开机", "启动"})
_BOOL_FALSE = frozenset({"false", "0", "off", "no", "关", "关闭", "关掉", "关机", "停止"})

MAX_BATCH = 10  # 与 SKILL.md “单次 ≤10 个设备”一致


# ─── 小工具 ───────────────────────────────────────────────────────────────────


def normalize_desc(desc: str | None) -> str:
    """与 CLI catalog.normalize_desc 同规则：service_description → ``@desc`` 后缀。"""
    if not desc:
        return ""
    return _DESC_FORBID_RE.sub("_", desc.strip()).strip("_")


def resolve_spec_keys(spec: dict) -> dict[str, str]:
    """``{iid: spec_name}``，与 CLI catalog._resolve_keys_for_device 同规则：

    type_name 设备内唯一 → 裸 type_name；冲突 → ``type_name@desc``；仍冲突 → 原 iid。
    这样返回的 spec_name 可原样交给 ``miloco-cli device control``。
    """
    if not isinstance(spec, dict):
        return {}
    counts: dict[str, int] = {}
    for entry in spec.values():
        if isinstance(entry, dict) and entry.get("type_name"):
            counts[entry["type_name"]] = counts.get(entry["type_name"], 0) + 1
    result: dict[str, str] = {}
    desc_used: dict[str, int] = {}
    for iid, entry in spec.items():
        if not isinstance(entry, dict) or not entry.get("type_name"):
            continue
        type_name = entry["type_name"]
        if counts[type_name] <= 1:
            result[iid] = type_name
            continue
        desc = normalize_desc(entry.get("service_description"))
        if desc:
            key = f"{type_name}@{desc}"
            desc_used[key] = desc_used.get(key, 0) + 1
            result[iid] = key
        else:
            result[iid] = iid
    for iid, key in list(result.items()):
        if "@" in key and desc_used.get(key, 0) > 1:
            result[iid] = iid
    return result


def _siid(iid: str) -> str | None:
    parts = iid.split(".")
    return parts[1] if len(parts) == 3 else None


def _access(entry: dict) -> str:
    """``w`` / ``r`` / ``wr`` / ``x``，与 catalog 的 access 列同口径。"""
    acc = ""
    if entry.get("writeable"):
        acc += "w"
    if entry.get("readable"):
        acc += "r"
    return acc


def _norm_text(s: str | None) -> str:
    return (s or "").strip().lower()


def _strip_words(text: str, words: tuple[str, ...]) -> str:
    for w in sorted(words, key=len, reverse=True):
        text = text.replace(w, "")
    return text.strip()


def normalize_bool(raw: Any) -> Any:
    """bool 兼容：true/1/on/yes/开 → True，false/0/off/no/关 → False，其它原样。"""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in _BOOL_TRUE:
            return True
        if low in _BOOL_FALSE:
            return False
    return raw


def _coerce_number(raw: Any) -> Any:
    if isinstance(raw, bool) or not isinstance(raw, str):
        return raw
    s = raw.strip()
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return raw


def validate_value(entry: dict, value: Any) -> str | None:
    """与 CLI home_info.validate_value 同规则；返回错误文案（None = 合法）。

    枚举不合法时列出全部合法取值；范围越界时带上 step 与单位，方便模型改对。
    """
    value_list = entry.get("value_list")
    if isinstance(value_list, list) and value_list:
        allowed = [it for it in value_list if isinstance(it, dict)]
        if value not in {it.get("value") for it in allowed}:
            opts = ", ".join(f"{it.get('name')}={it.get('value')}" for it in allowed)
            return f"值 {value!r} 不是合法枚举；可选：{opts}"
        return None
    value_range = entry.get("value_range")
    if not value_range or len(value_range) < 2:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    lo, hi = value_range[0], value_range[1]
    if not (lo <= value <= hi):
        step = value_range[2] if len(value_range) >= 3 else None
        rng = f"[{lo},{hi}" + (f";{step}" if step is not None else "") + "]"
        unit = entry.get("unit")
        return f"值 {value} 超出范围 {rng}" + (f" {unit}" if unit else "")
    return None


def normalize_value(entry: dict, value: Any) -> Any:
    """按 spec format 归一：bool 走 normalize_bool（含中文开/关），数值型把字符串转数字，
    枚举允许用枚举名（如 ``Auto``）代替枚举值。"""
    fmt = entry.get("format")
    if fmt == "bool":
        return normalize_bool(value)
    if isinstance(value, str):
        value_list = entry.get("value_list")
        if isinstance(value_list, list):
            low = value.strip().lower()
            for it in value_list:
                if isinstance(it, dict) and _norm_text(str(it.get("name"))) == low:
                    return it.get("value")
        value = _coerce_number(value)
    return value


# ─── 房间 / 目标匹配 ──────────────────────────────────────────────────────────


def _split_room_from_target(target: str, rooms: list[str]) -> tuple[str | None, str]:
    """识别房间前允许有请求前缀，如“请帮我把客厅的灯关闭”。

    每次先匹配房间再剥一个前缀，避免把“开封”等房间名的首字当动词删掉。
    没识别到房间时保留原 target，设备名匹配仍优先于类别回落。
    """
    remaining = target
    prefixes = ("帮我", "给我", "打开", "关闭", "关掉", "开启", "请", "把", "开", "关")
    while remaining:
        for room in sorted(rooms, key=len, reverse=True):
            if room and remaining.startswith(room) and len(remaining) > len(room):
                rest = remaining[len(room):]
                if rest.startswith("的"):
                    rest = rest[1:]
                return room, rest.strip()
        prefix = next((p for p in prefixes if remaining.startswith(p)), None)
        if prefix is None:
            break
        remaining = remaining[len(prefix):].lstrip()
    return None, target


def _filter_room(devices: list[dict], room: str) -> list[dict]:
    """先精确、再包含（“卧室”命中“主卧室”/“卧室”）。"""
    exact = [d for d in devices if _norm_text(d.get("room")) == _norm_text(room)]
    if exact:
        return exact
    r = _norm_text(room)
    return [
        d for d in devices
        if d.get("room") and (r in _norm_text(d["room"]) or _norm_text(d["room"]) in r)
    ]


def _detect_plural(target: str) -> bool:
    return any(m in target for m in _PLURAL_MARKERS)


def _categories_for_word(word: str) -> set[str]:
    """整词命中同义词 → category 集合。"""
    w = _norm_text(word)
    if not w:
        return set()
    if w in _GENERIC_WORDS:
        return set(_GENERIC_WORDS[w])
    hits = {c for c, syns in INTENT_SYNONYMS.items() if w == c or w in syns}
    return hits


def _categories_in_text(text: str) -> set[str]:
    """文本中出现的同义词 → category 集合，只取最长命中词（“扫地机器人”不再连带命中“人”类）。"""
    t = _norm_text(text)
    hits: list[tuple[int, str]] = []
    for cat, syns in INTENT_SYNONYMS.items():
        hits.extend((len(s), cat) for s in syns if s in t)
    for w, cats in _GENERIC_WORDS.items():
        if w in t:
            hits.extend((len(w), c) for c in cats)
    if not hits:
        return set()
    best_len = max(n for n, _ in hits)
    return {c for n, c in hits if n == best_len}


def _match_target(devices: list[dict], target: str) -> tuple[list[dict], str]:
    """返回 (命中设备, 命中方式)。优先级：

    1. ``category``：target 整词就是类别词（“灯”“空调”“light”）→ 按 category 选，
       同名但类别不同的设备（如名叫“灯”的传感器）不混进来；
    2. ``name``：精确名 → 名字互含 → 子设备别名；
    3. ``category_in_text``：target 里含类别词（“那个灯”）→ 按 category 选。
    """
    t = _norm_text(target)
    if not t:
        return [], "none"

    cats = _categories_for_word(t)
    if cats:
        hit = [d for d in devices if (d.get("category") or "") in cats]
        if hit:
            return hit, "category"

    exact = [d for d in devices if _norm_text(d.get("name")) == t]
    if exact:
        return exact, "name"
    contains = [
        d for d in devices
        if d.get("name") and (
            t in _norm_text(d["name"])
            or (len(_norm_text(d["name"])) >= 2 and _norm_text(d["name"]) in t)
        )
    ]
    if contains:
        return contains, "name"
    alias = [
        d for d in devices
        if isinstance(d.get("sub_devices"), dict)
        and any(t in _norm_text(v) or _norm_text(v) in t for v in d["sub_devices"].values() if v)
    ]
    if alias:
        return alias, "alias"

    cats = _categories_in_text(t)
    if cats:
        hit = [d for d in devices if (d.get("category") or "") in cats]
        if hit:
            return hit, "category"
    return [], "none"


def _has_writable_switch(device: dict) -> bool:
    spec = device.get("spec") or {}
    return any(
        e["type_name"] == "on" and e.get("writeable") and e.get("format") == "bool"
        for _, e in _spec_entries(spec, "prop")
    )


def _light_control_fallbacks(pool: list[dict], hit: list[dict], variants: list[str]) -> list[dict]:
    """issue #36：用户说的是“灯”这个类别词时，把同一范围（房间 / 全屋）内
    category 属于开关类且带可写 bool ``on`` 的设备也列为“可能控灯”的候选。

    只在按类别词匹配（或没匹配到）时触发；用户已点名具体设备（按名字命中）不补。
    """
    if not any("light" in _categories_in_text(v) for v in variants):
        return []
    hit_dids = {d.get("did") for d in hit}
    return [
        d for d in pool
        if d.get("did") not in hit_dids
        and (d.get("category") or "") in LIGHT_CONTROL_FALLBACK_CATEGORIES
        and _has_writable_switch(d)
    ]


# ─── spec 匹配 ────────────────────────────────────────────────────────────────


def _spec_entries(spec: dict, kind: str) -> list[tuple[str, dict]]:
    prefix = "action." if kind == "action" else "prop."
    return [
        (iid, e) for iid, e in (spec or {}).items()
        if isinstance(e, dict) and iid.startswith(prefix) and e.get("type_name")
    ]


def _usable(entry: dict, action: str) -> bool:
    if action == "set":
        return bool(entry.get("writeable"))
    if action == "get":
        return bool(entry.get("readable"))
    return True


def _pick_among_same_type(
    cands: list[tuple[str, dict]], device: dict, prefer_siid: str | None = None
) -> tuple[str, dict]:
    """同 type_name 多条（``on@空调`` / ``on@照明灯``）→ 优先属性所在 service，
    其次 service_type_name 等于设备 category，再次 service 类型名含 category 主词，最后按 iid 序。"""
    if len(cands) == 1:
        return cands[0]
    if prefer_siid is not None:
        same = [c for c in cands if _siid(c[0]) == prefer_siid]
        if same:
            return same[0]
    category = device.get("category") or ""
    by_cat = [c for c in cands if c[1].get("service_type_name") == category]
    if by_cat:
        return by_cat[0]
    head = category.split("-")[0] if category else ""
    if head:
        loose = [c for c in cands if head in (c[1].get("service_type_name") or "")]
        if loose:
            return loose[0]
    return sorted(cands, key=lambda c: c[0])[0]


def _match_property(
    device: dict, prop_word: str | None, action: str
) -> tuple[tuple[str, dict] | None, str]:
    """在设备 spec 里找用户说的属性 / 动作。返回 ((iid, entry) | None, matched_by)。"""
    spec = device.get("spec") or {}
    kind = "action" if action == "call" else "prop"
    entries = _spec_entries(spec, kind)
    if not entries:
        return None, "none"

    # 属性缺省：set 默认落到开关；get 不猜（交给 device props 查全部可读属性）
    if not prop_word:
        if action != "set":
            return None, "none"
        cands = [(i, e) for i, e in entries if e["type_name"] == "on" and _usable(e, action)]
        if cands:
            return _pick_among_same_type(cands, device), "default"
        return None, "none"

    word = _norm_text(prop_word)

    # 1) 本身就是 spec_name / spec_name@desc / iid
    if _IID_RE.match(word) and word in spec:
        e = spec[word]
        return ((word, e), "spec_name") if _usable(e, action) else (None, "none")
    m = _KEY_DESC_RE.match(word)
    if m:
        cands = [
            (i, e) for i, e in entries
            if e["type_name"] == m.group(1)
            and normalize_desc(e.get("service_description")).lower() == m.group(2)
            and _usable(e, action)
        ]
        if cands:
            return cands[0], "spec_name"
    if _KEY_BARE_RE.match(word):
        cands = [(i, e) for i, e in entries if e["type_name"] == word and _usable(e, action)]
        if cands:
            return _pick_among_same_type(cands, device), "spec_name"

    # 2) 同义词表
    table = ACTION_SYNONYMS if action == "call" else PROPERTY_SYNONYMS
    type_names = table.get(word) or table.get(word.replace(" ", ""))
    if not type_names:
        # 长词包含：用户说“把温度”→ 命中“温度”；取最长命中的同义词
        best = ""
        for k in table:
            if k in word and len(k) > len(best):
                best = k
        type_names = table.get(best) if best else None
    if type_names:
        ordered = list(type_names)
        if action == "get":
            # 查询优先只读的传感读数（“温度”→ temperature 而非 target-temperature）
            ro = [
                tn for tn in ordered
                if any(e["type_name"] == tn and e.get("readable") and not e.get("writeable")
                       for _, e in entries)
            ]
            ordered = ro + [tn for tn in ordered if tn not in ro]
        for tn in ordered:
            cands = [(i, e) for i, e in entries if e["type_name"] == tn and _usable(e, action)]
            if cands:
                return _pick_among_same_type(cands, device), "synonym"

    # 3) 描述子串兜底（spec 里的中文 / 英文 description）
    cands = []
    for i, e in entries:
        if not _usable(e, action):
            continue
        descs = [
            _norm_text(e.get("description")),
            _norm_text(e.get("prop_description")),
            _norm_text(e["type_name"]).replace("-", ""),
        ]
        if any(d and (word in d or d in word) for d in descs if len(d) >= 2):
            cands.append((i, e))
    if cands:
        return _pick_among_same_type(cands, device), "description"
    return None, "none"


def _find_switch(device: dict, prop_iid: str) -> tuple[str, dict] | None:
    """SKILL.md §4.3 的补 on 规则：设备有可写开关 → 选与本次属性同 service 的那一个。"""
    spec = device.get("spec") or {}
    cands = [
        (i, e) for i, e in _spec_entries(spec, "prop")
        if e["type_name"] == "on" and e.get("writeable")
    ]
    if not cands:
        return None
    return _pick_among_same_type(cands, device, prefer_siid=_siid(prop_iid))


# ─── 返回体组装 ───────────────────────────────────────────────────────────────


def _compact_spec(iid: str, entry: dict, spec_name: str, matched_by: str) -> dict:
    out: dict[str, Any] = {
        "spec_name": spec_name,
        "iid": iid,
        "access": "x" if iid.startswith("action.") else _access(entry),
        "format": entry.get("format"),
        "matched_by": matched_by,
    }
    if isinstance(entry.get("value_list"), list) and entry["value_list"]:
        out["value_list"] = [
            f"{it.get('name')}={it.get('value')}" for it in entry["value_list"] if isinstance(it, dict)
        ]
    if entry.get("value_range"):
        out["value_range"] = entry["value_range"]
    if entry.get("unit"):
        out["unit"] = entry["unit"]
    if iid.startswith("action.") and entry.get("in_params"):
        out["in_params"] = [
            f"{p.get('name')}:{p.get('format')}" for p in entry["in_params"] if isinstance(p, dict)
        ]
    return out


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return shlex.quote(str(v))


def _command_for(cand: dict, action: str, value: Any) -> str | None:
    did = shlex.quote(cand["did"])
    spec = cand.get("spec")
    if action == "get":
        if spec is None:
            return f"miloco-cli device props {did}"
        return f"miloco-cli device props {did} {shlex.quote(spec['spec_name'])}"
    if spec is None:
        return None
    if action == "call":
        params = value if isinstance(value, list) else ([] if value is None else [value])
        tail = "".join(f" {_fmt_value(p)}" for p in params)
        return f"miloco-cli device action {did} {shlex.quote(spec['spec_name'])}{tail}"
    # set
    if value is None:
        return None
    cmd = f"miloco-cli device control {did} --set {shlex.quote(spec['spec_name'])} {_fmt_value(value)}"
    if cand.get("needs_on"):
        cmd += f" --set {shlex.quote(cand['needs_on']['spec_name'])} true"
    return cmd


def _infer_action(action: str | None, prop_word: str | None, value: Any) -> str:
    if action in ("set", "get", "call"):
        return action
    if value is not None:
        return "set"
    w = _norm_text(prop_word)
    if w and w in PROPERTY_SYNONYMS and PROPERTY_SYNONYMS[w] == ("on",):
        return "set"
    if w and (w in ACTION_SYNONYMS or any(k in w for k in ACTION_SYNONYMS)):
        return "call"
    return "get" if w else "set"


def _describe(cands: list[dict], limit: int = 8) -> str:
    """候选摘要：名(房间[；issue])，供 hint 使用。"""
    parts = []
    for c in cands[:limit]:
        text = f"{c['name']}({c['room'] or '无房间'}"
        if c.get("issue"):
            text += f"；{c['issue']}"
        parts.append(text + ")")
    return "、".join(parts) + ("…" if len(cands) > limit else "")


def _rooms_of(cands: list[dict]) -> list[str]:
    seen: dict[str, None] = {}
    for c in cands:
        seen.setdefault(c.get("room") or "（无房间）", None)
    return list(seen)


def resolve_intent(devices: list[dict], request: dict) -> dict:
    """纯函数：home_info.devices + 请求 dict → 解析结果 dict。

    请求字段：room / target / action / property / value / scope，见 schema.IntentResolveRequest。
    ``ambiguity``：none（可执行）/ multiple（多台未说“全部”）/ unconfirmed（只有“可能控灯”的
    开关类候选，须用户确认）/ not_found。
    """
    room: str | None = (request.get("room") or "").strip() or None
    target_raw: str = (request.get("target") or "").strip()
    prop_word: str | None = (request.get("property") or "").strip() or None
    value: Any = request.get("value")
    scope: str = request.get("scope") or "auto"
    action = _infer_action(request.get("action"), prop_word, value)

    # 属性词本身就是开 / 关 → 值缺省时由属性词推出；没给属性词时再看 target 里的动词
    # （“灯都关” / “打开空调”），“开关”是设备名不是动词，先剥掉再判。
    if action == "set" and value is None:
        w = _norm_text(prop_word) if prop_word else ""
        if w in _BOOL_TRUE:
            value = True
        elif w in _BOOL_FALSE:
            value = False
        elif not prop_word:
            t = target_raw.replace("开关", "")
            if any(k in t for k in ("关闭", "关掉", "关机", "关")):
                value = False
            elif any(k in t for k in ("打开", "开启", "开机", "开")):
                value = True

    rooms_known = sorted({d.get("room") for d in devices if d.get("room")})
    plural = scope == "all" or (scope == "auto" and _detect_plural(target_raw))

    # target 归一：剥复数标记 → 剥房间前缀 → 剥动词助词；逐级尝试，命中即止
    variants: list[str] = []
    t0 = target_raw
    t1 = _strip_words(t0, _PLURAL_MARKERS)
    inferred_room, t2 = _split_room_from_target(t1, rooms_known)
    if room is None and inferred_room:
        room = inferred_room
    t3 = _strip_words(t2, _VERB_WORDS)
    for v in (t0, t1, t2, t3):
        if v and v not in variants:
            variants.append(v)

    pool = _filter_room(devices, room) if room else list(devices)
    room_missing = bool(room) and not pool

    hit: list[dict] = []
    matched_by = "none"
    if pool:
        for v in variants:
            hit, matched_by = _match_target(pool, v)
            if hit:
                break

    # issue #36：说“灯”时，同范围内的开关 / 插座 / 控制面板也可能就是那盏灯
    fallback: list[dict] = []
    if pool and matched_by in ("category", "none"):
        fallback = _light_control_fallbacks(pool, hit, variants)
    if not hit and fallback:
        matched_by = LIGHT_CONTROL_FALLBACK

    # 逐台定位 spec / 校验值 / 决定补 on
    candidates: list[dict] = []
    issues: list[str] = []
    for d in hit + fallback:
        is_fallback = d not in hit
        cand: dict[str, Any] = {
            "did": d.get("did"),
            "name": d.get("name"),
            "room": d.get("room"),
            "category": d.get("category"),
            "online": bool(d.get("online")),
            "matched_by": LIGHT_CONTROL_FALLBACK if is_fallback else matched_by,
            "spec": None,
            "needs_on": None,
            "protected": (d.get("category") or "") in PROTECTED_CATEGORIES,
        }
        keys = resolve_spec_keys(d.get("spec") or {})
        found, how = _match_property(d, prop_word, action)
        if found is None:
            if action == "get" and not prop_word:
                pass  # 无属性词的查询 → 全部可读属性，spec 留空
            elif not (d.get("spec") or {}):
                cand["issue"] = "spec 为空（后端可能仍在加载），可 device refresh 后重试"
            else:
                what = prop_word or "开关"
                verb = {"set": "可写", "get": "可读", "call": "可调用"}[action]
                cand["issue"] = f"该设备没有{verb}的“{what}”属性" + (
                    "（只读设备，不能控制）" if action == "set" and not any(
                        e.get("writeable") for _, e in _spec_entries(d.get("spec") or {}, "prop")
                    ) else ""
                )
        else:
            iid, entry = found
            cand["spec"] = _compact_spec(iid, entry, keys.get(iid, iid), how)
            if action == "set":
                value_norm = normalize_value(entry, value)
                err = validate_value(entry, value_norm) if value_norm is not None else None
                if err:
                    cand["issue"] = err
                elif value_norm is None:
                    cand["issue"] = "缺少要设置的值（value）"
                cand["value"] = value_norm
                if (
                    entry.get("type_name") != "on"
                    and (d.get("category") or "") not in KITCHEN_CATEGORIES
                ):
                    sw = _find_switch(d, iid)
                    if sw is not None:
                        # iid 一并给出：CLI --exec 直接下发，不用再拉 home_info 反查
                        cand["needs_on"] = {"spec_name": keys.get(sw[0], sw[0]), "iid": sw[0]}
        if cand.get("issue") and not is_fallback:
            issues.append(f"{cand['name']}({cand['did']})：{cand['issue']}")
        candidates.append(cand)

    # ambiguity + hint（hint 是给模型的下一步指令，不是给用户的文案）
    hints: list[str] = []
    direct = [c for c in candidates if c["matched_by"] != LIGHT_CONTROL_FALLBACK]
    maybe_lights = [c for c in candidates if c["matched_by"] == LIGHT_CONTROL_FALLBACK]
    # 可能控灯的开关类候选永不自动执行：用户没点名，是不是灯只有用户知道
    executable = [c for c in direct if not c.get("issue")]
    where = f"房间“{room}”" if room else "全屋"
    if not candidates:
        ambiguity = "not_found"
        if room_missing:
            hints.append(
                f"房间“{room}”下没有任何设备（已知房间：{'、'.join(rooms_known) or '无'}）；"
                "请核对房间名或去掉 room 再试"
            )
        else:
            hints.append(f"{where}未找到匹配“{target_raw}”的设备")
        hints.append("可 `miloco-cli device refresh` 后重试一次；仍无则回复用户没找到，禁止编造 did")
    elif not direct:
        ambiguity = "unconfirmed"
        hints.append(
            f"{where}没有 light 类设备，但有 {len(maybe_lights)} 台开关类设备可能控制灯"
            f"（米家把通断器 / 墙壁开关 / 插座也归入灯光类）：{_describe(maybe_lights)}；"
            "请反问用户是不是指它们，确认后用 --target <设备名> 重试；未经确认不要下发"
        )
    elif len(direct) == 1:
        ambiguity = "none"
        hints.append(f"命中 1 台：{direct[0]['name']}（{direct[0]['room'] or '无房间'}）")
    elif plural:
        ambiguity = "none"
        hints.append(f"命中 {len(direct)} 台（{'、'.join(_rooms_of(direct))}），按“全部”处理")
        if len(executable) > MAX_BATCH:
            hints.append(f"超过单次 {MAX_BATCH} 台上限，请分批下发")
    else:
        ambiguity = "multiple"
        names = "、".join(f"{c['name']}({c['room'] or '无房间'})" for c in direct[:8])
        more = "…" if len(direct) > 8 else ""
        hints.append(
            f"命中 {len(direct)} 台：{names}{more}；用户未说“全部”，请反问房间 / 哪一台；"
            "若用户明确要全部，加 --scope all 重试"
        )
    if direct and maybe_lights:
        hints.append(
            f"另有 {len(maybe_lights)} 台开关类设备可能控制灯（不在 command_preview 里）："
            f"{_describe(maybe_lights)}；用户未指名，请反问是否也要操作，确认后用 --target <设备名> 单独下发"
        )

    if issues:
        hints.append("以下候选不可执行，已从 command_preview 剔除：" + "；".join(issues))
    if candidates and ambiguity == "none" and executable:
        hints.append("可直接执行 command_preview（或 device resolve 加 --exec 一次下发）")
    if any(c["protected"] for c in candidates):
        hints.append("含安全设备（门锁 / 摄像头 / 燃气阀 / 烟感），控制前须用户二次确认")
    offline = [c["name"] for c in executable if not c["online"]]
    if offline and action != "get":
        hints.append(f"离线：{'、'.join(offline)}——照常下发，CLI 会返回离线错误")

    preview: list[str] = []
    if ambiguity == "none":
        for c in executable:
            cmd = _command_for(c, action, c.get("value", value))
            if cmd:
                preview.append(cmd)

    return {
        "action": action,
        "room": room,
        "matched_by": matched_by,
        "candidates": candidates,
        "ambiguity": ambiguity,
        "hint": "；".join(hints),
        "command_preview": preview,
    }

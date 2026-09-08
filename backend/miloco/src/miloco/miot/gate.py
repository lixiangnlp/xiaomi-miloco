# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""设备控制闸门：受保护类别判定 + 服务端值校验 + 待确认变更的进程内存储。

设计原则（“the model stages; a person applies”）：

- **模型只能 stage，不能 apply**：命中 ``safety.protected_categories`` 的设备，控制
  请求不直接下发，而是落到 :class:`PendingStore` 等待用户确认；确认凭据
  ``confirm_token`` 由服务端生成，apply 时必须原样带回，存储侧只留哈希。
- **规则定义一次、所有路径共用**：``resolve_protection`` 是唯一的判定函数，
  ``miot.service.execute_control`` 是唯一的执行入口；``MiotService.control_device``
  （CLI / web）、``MiotService.apply_change``、``RuleRunner._execute_action`` 都显式
  调用它，不存在绕过点。
- **apply 时重新校验**：stage 时通过 ≠ apply 时仍通过。apply 前重查 scope、重跑值校验，
  按 *当时* 的配置重新判定。

待确认状态为什么放进程内存而不是 action_ledger：action_ledger 走 ``MetricsClient``
异步批量写，写后立即读回不保证可见，不适合做“这条变更此刻能不能 apply”的权威判定；
台账只负责审计（stage / apply / 拒绝 / 过期各落一行，见 ``miot/service.py``）。
代价是进程重启会丢弃未确认的 stage——TTL 默认 600 秒，这是可接受的：待确认的危险
操作本就不该跨重启幸存，用户重新发起即可。

本模块只放纯逻辑（不依赖 MiotProxy / FastAPI / settings 单例），便于单测。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from miloco.middleware.exceptions import ValidationException
from miloco.miot.schema import DeviceControlRequest

# ─── 设备类别 / 保护判定 ────────────────────────────────────────────────────────


def device_category(dev: Any) -> str | None:
    """从设备 urn（``urn:miot-spec-v2:device:{category}:…``）取类别；取不到返回 None。

    与 ``MiotService.get_device_spec`` / ``MiotProxy.get_home_info_data`` 的口径一致。
    dev 可能是 stub / Mock（测试）或 None，故全程防御式取值。
    """
    urn = getattr(dev, "urn", None)
    if not isinstance(urn, str):
        return None
    parts = urn.split(":")
    return parts[3] if len(parts) > 3 and parts[3] else None


@dataclass(frozen=True)
class ProtectionDecision:
    """``resolve_protection`` 的结果：是否受保护 + 命中的类别（未命中为 None）。"""

    protected: bool
    category: str | None

    @property
    def reason(self) -> str:
        return (
            f"protected category '{self.category}' (safety.protected_categories)"
            if self.protected
            else "not protected"
        )


def resolve_protection(
    dev: Any, protected_categories: Iterable[str]
) -> ProtectionDecision:
    """唯一的受保护判定：设备类别 ∈ ``protected_categories`` 即受保护。

    纯函数：``protected_categories`` 由调用方从 *当前* settings 取出传入（配置放开 /
    收紧即时生效，不是启动快照）。类别未知（dev 为 None / 无 urn）= 不受保护——scope
    校验（``_assert_did_in_allowed_home``）会先拦住不存在的设备，这里不重复报错。
    未来若加 ``protected_props``（按 spec key 细粒度保护）也在这里判定。
    """
    category = device_category(dev)
    if category is not None and category in set(protected_categories):
        return ProtectionDecision(protected=True, category=category)
    return ProtectionDecision(protected=False, category=category)


# ─── 值校验（移植自 cli/home_info.validate_value，服务端为最终裁定）─────────────


def validate_value(iid: str, iid_spec: dict, value: Any) -> None:
    """按 spec 校验单个属性值：bool 格式优先，其次枚举（value_list），最后数值范围（value_range）。

    报错文案是给 agent 的 *指令*：枚举列出全部合法取值，范围带 step 与单位。
    """
    if iid_spec.get("format") == "bool" and not isinstance(value, bool):
        raise ValidationException(
            f"value {value!r} for {iid} must be a boolean (true/false)"
        )

    value_list = iid_spec.get("value_list")
    if isinstance(value_list, list) and value_list:
        allowed = [it for it in value_list if isinstance(it, dict)]
        if value not in {it.get("value") for it in allowed}:
            opts = ", ".join(f"{it.get('name')}={it.get('value')}" for it in allowed)
            raise ValidationException(
                f"value {value!r} for {iid} is not a valid enum; allowed: {opts}"
            )
        return

    value_range = iid_spec.get("value_range")
    if (
        not value_range
        or len(value_range) < 2
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        return
    lo, hi = value_range[0], value_range[1]
    step = value_range[2] if len(value_range) >= 3 else None
    rng = f"[{lo},{hi}" + (f";{step}" if step is not None else "") + "]"
    unit = iid_spec.get("unit")
    suffix = f" {unit}" if unit else ""
    if not (lo <= value <= hi):
        raise ValidationException(
            f"value {value} for {iid} out of range {rng}{suffix}; "
            f"pick a value within {rng} and retry"
        )
    if step and isinstance(step, (int, float)) and step > 0:
        # 步进校验：与 SKILL.md “符合 step 步进”口径一致；浮点用容差避免 0.1 累积误差
        offset = (value - lo) / step
        if abs(offset - round(offset)) > 1e-6:
            raise ValidationException(
                f"value {value} for {iid} does not match step {step} in {rng}{suffix}; "
                f"use {lo} + k*{step}"
            )


def _spec_entry(spec: dict, iid: str) -> dict:
    entry = spec.get(iid)
    if not isinstance(entry, dict):
        raise ValidationException(
            f"iid '{iid}' not in device spec; run `device spec <did>` and pick a valid iid"
        )
    return entry


def _assert_writeable(iid: str, entry: dict) -> None:
    if entry.get("writeable") is False:
        raise ValidationException(
            f"property {iid} is read-only (not writeable); use `device props` to read it"
        )


def validate_request_against_spec(spec: dict | None, request: DeviceControlRequest) -> None:
    """服务端参数校验：iid 必须在 spec 中、属性可写、值在枚举 / 范围 / 步进内、action 入参数量匹配。

    spec 为空 / 不可用（云端 spec 拉取失败）时跳过，与 CLI 端 ``validate_iid`` 的
    fail-open 口径一致——受保护类别闸门不依赖 spec，不受此影响。
    """
    if not spec or not isinstance(spec, dict):
        return

    if request.type == "set_property":
        if request.iid:
            entry = _spec_entry(spec, request.iid)
            _assert_writeable(request.iid, entry)
            validate_value(request.iid, entry, request.value)
        return

    if request.type == "set_properties":
        for prop in request.properties or []:
            entry = _spec_entry(spec, prop.iid)
            _assert_writeable(prop.iid, entry)
            validate_value(prop.iid, entry, prop.value)
        return

    # call_action
    if request.iid:
        entry = _spec_entry(spec, request.iid)
        in_params = entry.get("in_params")
        if isinstance(in_params, list) and in_params:
            given = len(request.params or [])
            if given != len(in_params):
                names = ",".join(str(p.get("name")) for p in in_params if isinstance(p, dict))
                raise ValidationException(
                    f"action {request.iid} expects {len(in_params)} param(s) "
                    f"({names}), got {given}; pass values positionally in that order"
                )


# ─── 待确认变更（stage / apply / discard）────────────────────────────────────────


def summarize_request(request: DeviceControlRequest) -> str:
    """人读摘要，给 stage 响应与 ``device changes`` 列表用。"""
    if request.type == "set_property":
        return f"set {request.iid} = {request.value!r}"
    if request.type == "set_properties":
        return "set " + ", ".join(
            f"{p.iid} = {p.value!r}" for p in (request.properties or [])
        )
    return f"call {request.iid}({', '.join(repr(p) for p in (request.params or []))})"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class StagedChange:
    """一条待确认变更。``token_hash`` 只存 sha256，明文 token 只在 stage 响应里出现一次。"""

    change_id: str
    did: str
    request: DeviceControlRequest
    category: str
    device_name: str | None
    room: str | None
    home_id: str | None
    token_hash: str = field(repr=False)
    created_at_ms: int
    expires_at_ms: int
    summary: str = ""

    def is_expired(self, now_ms: int) -> bool:
        return self.expires_at_ms <= now_ms

    def public_view(self) -> dict:
        """不含任何凭据的视图（列表接口用）。"""
        return {
            "change_id": self.change_id,
            "did": self.did,
            "device_name": self.device_name,
            "room": self.room,
            "category": self.category,
            "summary": self.summary,
            "request": self.request.model_dump(exclude_none=True),
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.expires_at_ms,
        }


class TokenMismatch(Exception):
    """confirm_token 与变更不匹配（调用方决定映射成哪种 HTTP 错误）。"""


class PendingStore:
    """进程内待确认变更表（dict + TTL）。

    这是待确认状态的**权威**存储；action_ledger 只做审计（见模块 docstring）。
    进程重启即清空——待确认的危险操作不应跨重启幸存。
    change_id 形如 ``chg-a1b2c3d4``（随机，不可枚举猜测）。
    过期项不在读路径静默丢弃，而是由 :meth:`purge_expired` 显式交回调用方，
    让调用方能为每条过期变更落一行 ``status=expired`` 台账。
    """

    def __init__(self, ttl_sec: float = 600.0):
        self._ttl_ms = int(max(float(ttl_sec), 1.0) * 1000)
        self._items: dict[str, StagedChange] = {}

    @property
    def ttl_sec(self) -> float:
        return self._ttl_ms / 1000

    @staticmethod
    def _now_ms(now_ms: int | None) -> int:
        return now_ms if now_ms is not None else int(time.time() * 1000)

    def purge_expired(self, now_ms: int | None = None) -> list[StagedChange]:
        """移出并返回所有已过期的变更（调用方负责落台账）。"""
        now = self._now_ms(now_ms)
        expired = [c for c in self._items.values() if c.is_expired(now)]
        for c in expired:
            self._items.pop(c.change_id, None)
        return expired

    def stage(
        self,
        *,
        did: str,
        request: DeviceControlRequest,
        category: str,
        device_name: str | None,
        room: str | None,
        home_id: str | None = None,
        now_ms: int | None = None,
    ) -> tuple[StagedChange, str]:
        """登记一条待确认变更，返回 ``(change, confirm_token 明文)``。明文不落存储。"""
        now = self._now_ms(now_ms)
        token = secrets.token_urlsafe(24)
        change_id = f"chg-{secrets.token_hex(4)}"
        while change_id in self._items:  # pragma: no cover —— 32bit 随机撞车几乎不可能
            change_id = f"chg-{secrets.token_hex(4)}"
        change = StagedChange(
            change_id=change_id,
            did=did,
            request=request,
            category=category,
            device_name=device_name,
            room=room,
            home_id=home_id,
            token_hash=_hash_token(token),
            created_at_ms=now,
            expires_at_ms=now + self._ttl_ms,
            summary=summarize_request(request),
        )
        self._items[change.change_id] = change
        return change, token

    def pending(self, now_ms: int | None = None) -> list[StagedChange]:
        """未过期的待确认变更，按创建时间升序。调用方应先 ``purge_expired``。"""
        now = self._now_ms(now_ms)
        return sorted(
            (c for c in self._items.values() if not c.is_expired(now)),
            key=lambda c: c.created_at_ms,
        )

    def get(self, change_id: str) -> StagedChange | None:
        """按 id 取（可能已过期——由调用方 ``is_expired`` 判定并处理）。"""
        return self._items.get(change_id)

    def verify_token(self, change: StagedChange, confirm_token: str | None) -> bool:
        """常量时间比较 sha256(confirm_token) 与存储哈希；空 token 恒 False。"""
        if not confirm_token:
            return False
        return hmac.compare_digest(_hash_token(confirm_token), change.token_hash)

    def take(self, change_id: str) -> StagedChange | None:
        """移出一条变更（apply 通过 token 校验后 / discard 时调用）。"""
        return self._items.pop(change_id, None)

# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""设备控制闸门：服务端值校验 + 危险设备 stage / apply 台账。

设计原则（对照 Anthropic “the model stages; a person or a policy applies”）：

- **模型只能 stage，不能 apply**：命中 ``safety.protected_categories`` 的设备，控制
  请求不直接下发，而是落到 :class:`ChangeLedger` 里等待用户确认；确认凭据
  ``confirm_token`` 由服务端生成，apply 时必须原样带回。
- **规则定义一次、所有路径共用**：CLI / web / RuleRunner 静态动作都经
  ``miot.service.execute_control`` 走同一份校验与闸门，不再各自实现。
- **apply 时重新校验**：stage 时通过 ≠ apply 时仍通过。apply 前重查 scope、重跑值
  校验、按 *当时* 的配置重新判定，配置收紧后已 stage 的变更会被拒绝。

本模块只放纯逻辑（不依赖 MiotProxy / FastAPI），便于单测。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from miloco.middleware.exceptions import (
    AuthorizationException,
    ResourceNotFoundException,
    ValidationException,
)
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


def find_protection(dev: Any, protected_categories: list[str] | tuple[str, ...]) -> str | None:
    """返回命中的保护类别名；未命中返回 None。

    未来若加 ``protected_props``（按 spec key 细粒度保护）也在这里判定，调用方只看
    返回值是否为 None。
    """
    category = device_category(dev)
    if category is None:
        return None
    if category in set(protected_categories):
        return category
    return None


# ─── 值校验（移植自 cli/home_info.validate_value，服务端为准）────────────────────


def validate_value(iid: str, iid_spec: dict, value: Any) -> None:
    """按 spec 校验单个属性值：枚举（value_list）优先，其次数值范围（value_range）。

    报错文案是给 agent 的 *指令*：枚举列出全部合法取值，范围带 step 与单位。
    """
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
    if not (lo <= value <= hi):
        step = value_range[2] if len(value_range) >= 3 else None
        rng = f"[{lo},{hi}" + (f";{step}" if step is not None else "") + "]"
        unit = iid_spec.get("unit")
        suffix = f" {unit}" if unit else ""
        raise ValidationException(
            f"value {value} for {iid} out of range {rng}{suffix}; "
            f"pick a value within {rng} and retry"
        )


def validate_request_against_spec(spec: dict | None, request: DeviceControlRequest) -> None:
    """服务端参数校验：iid 必须在 spec 中、属性可写、值在枚举 / 范围内、action 入参数量匹配。

    spec 为空 / 不可用（云端 spec 拉取失败）时跳过，与 CLI 端 ``validate_iid`` 的
    fail-open 口径一致——闸门（保护类别）不依赖 spec，不受此影响。
    """
    if not spec or not isinstance(spec, dict):
        return

    def _entry(iid: str) -> dict:
        entry = spec.get(iid)
        if not isinstance(entry, dict):
            raise ValidationException(
                f"iid '{iid}' not in device spec; run `device spec <did>` and pick a valid iid"
            )
        return entry

    if request.type == "set_property":
        if request.iid:
            entry = _entry(request.iid)
            _assert_writeable(request.iid, entry)
            validate_value(request.iid, entry, request.value)
        return

    if request.type == "set_properties":
        for prop in request.properties or []:
            entry = _entry(prop.iid)
            _assert_writeable(prop.iid, entry)
            validate_value(prop.iid, entry, prop.value)
        return

    # call_action
    if request.iid:
        entry = _entry(request.iid)
        in_params = entry.get("in_params")
        if isinstance(in_params, list) and in_params:
            given = len(request.params or [])
            if given != len(in_params):
                names = ",".join(str(p.get("name")) for p in in_params if isinstance(p, dict))
                raise ValidationException(
                    f"action {request.iid} expects {len(in_params)} param(s) "
                    f"({names}), got {given}; pass values positionally in that order"
                )


def _assert_writeable(iid: str, entry: dict) -> None:
    if entry.get("writeable") is False:
        raise ValidationException(
            f"property {iid} is read-only (not writeable); use `device props` to read it"
        )


# ─── 变更台账（stage / apply / discard）────────────────────────────────────────


def summarize_request(request: DeviceControlRequest) -> str:
    """人读摘要，给 stage 响应与 ``device changes`` 列表用。"""
    if request.type == "set_property":
        return f"set {request.iid} = {request.value!r}"
    if request.type == "set_properties":
        return "set " + ", ".join(
            f"{p.iid} = {p.value!r}" for p in (request.properties or [])
        )
    return f"call {request.iid}({', '.join(repr(p) for p in (request.params or []))})"


@dataclass
class StagedChange:
    change_id: str
    did: str
    request: DeviceControlRequest
    category: str
    device_name: str | None
    room: str | None
    confirm_token: str = field(repr=False)
    created_at_ms: int
    expires_at_ms: int
    summary: str = field(default="")

    def public_view(self) -> dict:
        """不含 confirm_token 的视图（列表接口用）。"""
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


class ChangeLedger:
    """内存态待确认变更表，带 TTL。

    进程重启即清空——这是有意的：待确认的危险操作不应跨重启幸存。
    change_id 形如 ``chg-0001``，进程内单调递增。
    """

    def __init__(self, ttl_sec: float = 600.0):
        self._ttl_ms = int(max(ttl_sec, 1.0) * 1000)
        self._items: dict[str, StagedChange] = {}
        self._seq = 0

    @property
    def ttl_sec(self) -> float:
        return self._ttl_ms / 1000

    def _purge_expired(self, now_ms: int) -> None:
        for cid in [c for c, it in self._items.items() if it.expires_at_ms <= now_ms]:
            self._items.pop(cid, None)

    def stage(
        self,
        *,
        did: str,
        request: DeviceControlRequest,
        category: str,
        device_name: str | None,
        room: str | None,
        now_ms: int | None = None,
    ) -> StagedChange:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        self._purge_expired(now)
        self._seq += 1
        change = StagedChange(
            change_id=f"chg-{self._seq:04d}",
            did=did,
            request=request,
            category=category,
            device_name=device_name,
            room=room,
            confirm_token=secrets.token_urlsafe(16),
            created_at_ms=now,
            expires_at_ms=now + self._ttl_ms,
            summary=summarize_request(request),
        )
        self._items[change.change_id] = change
        return change

    def pending(self, now_ms: int | None = None) -> list[StagedChange]:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        self._purge_expired(now)
        return sorted(self._items.values(), key=lambda c: c.created_at_ms)

    def get(self, change_id: str, now_ms: int | None = None) -> StagedChange:
        """取一条待确认变更；不存在 / 已过期 → ResourceNotFoundException。"""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        self._purge_expired(now)
        change = self._items.get(change_id)
        if change is None:
            raise ResourceNotFoundException(
                f"change '{change_id}' not found or expired "
                f"(staged changes expire after {int(self.ttl_sec)}s); "
                "run `device changes` to list pending ones, or stage the control again"
            )
        return change

    def take_for_apply(
        self, change_id: str, confirm_token: str | None, now_ms: int | None = None
    ) -> StagedChange:
        """校验 token 并**移出**台账（一次性凭据，apply 无论成败都不可重放）。"""
        change = self.get(change_id, now_ms)
        if not confirm_token or not secrets.compare_digest(
            confirm_token, change.confirm_token
        ):
            raise AuthorizationException(
                f"confirm_token does not match change '{change_id}'; "
                "the token is delivered directly to the user's MiHome app — "
                "ask the user to check the notification and provide its token if they approve"
            )
        return self._items.pop(change_id)

    def discard(self, change_id: str, now_ms: int | None = None) -> StagedChange:
        change = self.get(change_id, now_ms)
        return self._items.pop(change.change_id)

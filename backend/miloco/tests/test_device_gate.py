"""设备控制闸门：服务端值校验 + 受保护类别 stage / apply。

MiotProxy 用最小 stub（同 test_miot_service_lru 的 SimpleNamespace 手法），
LRUStore 打 temp SQLite；配置走真实 get_settings()（默认 protected_categories 含 camera）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.config.settings import reset_settings
from miloco.database.kv_repo import ScopeConfigKeys
from miloco.middleware.exceptions import (
    AuthorizationException,
    MiotServiceException,
    ResourceNotFoundException,
    ValidationException,
)
from miloco.miot.gate import ChangeLedger, validate_request_against_spec
from miloco.miot.schema import DeviceControlRequest, PropertyItem
from miloco.miot.service import MiotService, execute_control

LAMP_SPEC = {
    "prop.2.1": {"type_name": "on", "format": "bool", "writeable": True},
    "prop.2.2": {
        "type_name": "brightness", "format": "uint8", "writeable": True,
        "value_range": [1, 100, 1], "unit": "percentage",
    },
    "prop.2.3": {
        "type_name": "mode", "format": "uint8", "writeable": True,
        "value_list": [{"name": "Day", "value": 0}, {"name": "Night", "value": 1}],
    },
    "prop.3.1": {"type_name": "temperature", "format": "float", "writeable": False},
    "action.5.1": {
        "type_name": "play-text", "in_params": [{"name": "text-content", "format": "string"}],
    },
}
CAMERA_SPEC = {
    "prop.2.1": {"type_name": "on", "format": "bool", "writeable": True},
}

LAMP = SimpleNamespace(
    did="lamp", name="台灯", room_name="客厅", home_id="H1",
    urn="urn:miot-spec-v2:device:light:0000A001:yeelink-lamp1:1",
)
CAMERA = SimpleNamespace(
    did="cam", name="摄像头", room_name="客厅", home_id="H1",
    urn="urn:miot-spec-v2:device:camera:0000A01C:chuangmi-ipc019:1",
)


class _DBConnector:
    def __init__(self, path: Path):
        self._path = str(path)
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                "CREATE TABLE device_lru (did TEXT NOT NULL, key TEXT NOT NULL, "
                "touched_at INTEGER NOT NULL, PRIMARY KEY (did, key))"
            )

    def execute_update(self, sql, params=None):
        with sqlite3.connect(self._path) as conn:
            cur = conn.cursor()
            cur.execute(sql, params or ())
            conn.commit()
            return cur.rowcount

    def execute_query(self, sql, params=None):
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(sql, params or ())
            return [dict(r) for r in cur.fetchall()]


def _make_proxy(tmp_path: Path, store: dict[str, str]):
    db = _DBConnector(tmp_path / "lru.sqlite")

    async def _fetch_spec(urn, sub_names=None):
        return CAMERA_SPEC if ":camera:" in urn else LAMP_SPEC

    return SimpleNamespace(
        _kv_repo=SimpleNamespace(
            db_connector=db,
            get=lambda key, default=None: store.get(key, default),
            set=lambda key, value: store.__setitem__(key, value) or True,
        ),
        set_device_properties=AsyncMock(return_value=[{"code": 0, "siid": 2, "piid": 1}]),
        call_device_action=AsyncMock(return_value={"code": 0}),
        send_device_confirmation=AsyncMock(return_value=True),
        get_devices=AsyncMock(return_value={"lamp": LAMP, "cam": CAMERA}),
        get_cameras=AsyncMock(return_value={}),
        _fetch_device_spec=_fetch_spec,
    )


def _delivered_token(svc):
    # Simulate the user reading the private MiHome push, not the API response.
    message = svc._miot_proxy.send_device_confirmation.await_args.args[0]
    return re.search(r"确认码：([A-Za-z0-9_-]+)", message).group(1)


@pytest.fixture
def store() -> dict[str, str]:
    return {ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])}


@pytest.fixture
def svc(tmp_path, store) -> MiotService:
    reset_settings()
    return MiotService(miot_proxy=_make_proxy(tmp_path, store))


# ─── 服务端值校验 ─────────────────────────────────────────────────────────────


async def test_out_of_range_value_rejected_server_side(svc):
    req = DeviceControlRequest(type="set_property", iid="prop.2.2", value=130)
    with pytest.raises(ValidationException) as ei:
        await svc.control_device("lamp", req)
    assert "out of range [1,100;1] percentage" in ei.value.message
    svc._miot_proxy.set_device_properties.assert_not_called()


async def test_invalid_enum_lists_allowed(svc):
    req = DeviceControlRequest(type="set_property", iid="prop.2.3", value=7)
    with pytest.raises(ValidationException) as ei:
        await svc.control_device("lamp", req)
    assert "allowed: Day=0, Night=1" in ei.value.message


async def test_set_properties_validates_each_item(svc):
    req = DeviceControlRequest(
        type="set_properties",
        properties=[
            PropertyItem(iid="prop.2.1", value=True),
            PropertyItem(iid="prop.2.2", value=0),
        ],
    )
    with pytest.raises(ValidationException) as ei:
        await svc.control_device("lamp", req)
    assert "prop.2.2" in ei.value.message
    svc._miot_proxy.set_device_properties.assert_not_called()


async def test_readonly_property_rejected(svc):
    req = DeviceControlRequest(type="set_property", iid="prop.3.1", value=20)
    with pytest.raises(ValidationException) as ei:
        await svc.control_device("lamp", req)
    assert "read-only" in ei.value.message


async def test_unknown_iid_rejected(svc):
    req = DeviceControlRequest(type="set_property", iid="prop.9.9", value=1)
    with pytest.raises(ValidationException) as ei:
        await svc.control_device("lamp", req)
    assert "not in device spec" in ei.value.message


async def test_action_param_count_checked(svc):
    req = DeviceControlRequest(type="call_action", iid="action.5.1", params=[])
    with pytest.raises(ValidationException) as ei:
        await svc.control_device("lamp", req)
    assert "expects 1 param(s) (text-content), got 0" in ei.value.message
    svc._miot_proxy.call_device_action.assert_not_called()


async def test_valid_value_passes_and_executes(svc):
    req = DeviceControlRequest(type="set_property", iid="prop.2.2", value=50)
    out = await svc.control_device("lamp", req)
    assert out == {"results": [{"code": 0, "siid": 2, "piid": 1}]}
    svc._miot_proxy.set_device_properties.assert_awaited_once()
    assert (await svc.lru_snapshot())["histories"]["lamp"] == ["prop.2.2"]


def test_validation_skipped_when_spec_unavailable():
    """spec 拉不到时 fail-open（闸门不依赖 spec，不受影响）。"""
    req = DeviceControlRequest(type="set_property", iid="prop.2.2", value=999)
    validate_request_against_spec(None, req)
    validate_request_against_spec({}, req)


# ─── 受保护类别：stage 而不执行 ─────────────────────────────────────────────────


async def test_protected_category_is_staged_not_executed(svc):
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=False)
    out = await svc.control_device("cam", req)
    assert out["staged"] is True
    assert out["change_id"] == "chg-0001"
    assert out["category"] == "camera"
    assert out["did"] == "cam"
    assert out["summary"] == "set prop.2.1 = False"
    assert "confirm_token" not in out
    assert out["confirmation_channel"] == "mihome"
    assert out["expires_at"]
    assert "device apply chg-0001" in out["next"]
    svc._miot_proxy.set_device_properties.assert_not_called()
    # 未执行的控制不进 LRU
    assert (await svc.lru_snapshot())["histories"] == {}
    pending = (await svc.list_changes())["changes"]
    assert [c["change_id"] for c in pending] == ["chg-0001"]
    assert "confirm_token" not in pending[0]


async def test_staged_request_still_value_validated(svc):
    """受保护设备的错参不占台账，立刻 422 让 agent 改对。"""
    req = DeviceControlRequest(type="set_property", iid="prop.9.9", value=1)
    with pytest.raises(ValidationException):
        await svc.control_device("cam", req)
    assert (await svc.list_changes())["changes"] == []


async def test_non_protected_device_unaffected(svc):
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=True)
    out = await svc.control_device("lamp", req)
    assert "staged" not in out
    svc._miot_proxy.set_device_properties.assert_awaited_once()


async def test_protected_categories_follow_current_config(svc, monkeypatch):
    """配置放开 camera 后同一设备直接执行——闸门读的是当前配置，而非启动快照。"""
    monkeypatch.setenv("MILOCO_SAFETY__PROTECTED_CATEGORIES", '["lock"]')
    reset_settings()
    try:
        req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=True)
        out = await svc.control_device("cam", req)
        assert "staged" not in out
        svc._miot_proxy.set_device_properties.assert_awaited_once()
    finally:
        monkeypatch.delenv("MILOCO_SAFETY__PROTECTED_CATEGORIES")
        reset_settings()


# ─── apply / discard ─────────────────────────────────────────────────────────


async def test_apply_with_correct_token_executes_once(svc):
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=False)
    staged = await svc.control_device("cam", req)
    out = await svc.apply_change(staged["change_id"], _delivered_token(svc))
    assert out["applied"] is True
    assert out["change_id"] == staged["change_id"]
    assert out["results"] == [{"code": 0, "siid": 2, "piid": 1}]
    svc._miot_proxy.set_device_properties.assert_awaited_once()
    sent = svc._miot_proxy.set_device_properties.await_args.args[0][0]
    assert (sent.did, sent.siid, sent.piid, sent.value) == ("cam", 2, 1, False)
    # 一次性凭据：重放被拒
    with pytest.raises(ResourceNotFoundException):
        await svc.apply_change(staged["change_id"], _delivered_token(svc))
    assert (await svc.list_changes())["changes"] == []


async def test_apply_with_wrong_token_refused_and_change_kept(svc):
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=False)
    staged = await svc.control_device("cam", req)
    with pytest.raises(AuthorizationException):
        await svc.apply_change(staged["change_id"], "nope")
    with pytest.raises(AuthorizationException):
        await svc.apply_change(staged["change_id"], None)
    svc._miot_proxy.set_device_properties.assert_not_called()
    # 猜错 token 不应销毁用户待确认的变更
    assert len((await svc.list_changes())["changes"]) == 1


async def test_apply_expired_change_refused(svc):
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=False)
    staged = await svc.control_device("cam", req)
    change = svc._changes._items[staged["change_id"]]
    change.expires_at_ms = change.created_at_ms - 1  # 人为过期
    with pytest.raises(ResourceNotFoundException) as ei:
        await svc.apply_change(staged["change_id"], _delivered_token(svc))
    assert "expired" in ei.value.message
    svc._miot_proxy.set_device_properties.assert_not_called()


async def test_apply_unknown_change_refused(svc):
    with pytest.raises(ResourceNotFoundException):
        await svc.apply_change("chg-9999", "x")


async def test_apply_after_scope_tightened_refused(svc, store):
    """stage 后家庭 scope 收紧（设备所在家庭被停用）→ apply 按当前配置拒绝。"""
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=False)
    staged = await svc.control_device("cam", req)
    store[ScopeConfigKeys.HOME_WHITE_LIST_KEY] = json.dumps(["H2"])
    with pytest.raises(ValidationException) as ei:
        await svc.apply_change(staged["change_id"], _delivered_token(svc))
    assert "not in an allowed home" in ei.value.message
    svc._miot_proxy.set_device_properties.assert_not_called()


async def test_discard_change(svc):
    req = DeviceControlRequest(type="call_action", iid="action.5.1", params=["hi"])
    svc._miot_proxy._fetch_device_spec = AsyncMock(return_value=LAMP_SPEC)
    staged = await svc.control_device("cam", req)
    out = await svc.discard_change(staged["change_id"])
    assert out == {"discarded": True, "change_id": staged["change_id"], "did": "cam"}
    assert (await svc.list_changes())["changes"] == []
    with pytest.raises(ResourceNotFoundException):
        await svc.discard_change(staged["change_id"])


def test_change_ledger_ttl_and_ids():
    ledger = ChangeLedger(ttl_sec=10)
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=1)
    a = ledger.stage(did="d", request=req, category="lock", device_name=None, room=None,
                     now_ms=1_000)
    b = ledger.stage(did="d", request=req, category="lock", device_name=None, room=None,
                     now_ms=2_000)
    assert (a.change_id, b.change_id) == ("chg-0001", "chg-0002")
    assert [c.change_id for c in ledger.pending(now_ms=5_000)] == ["chg-0001", "chg-0002"]
    # a 在 11_000 过期，b 到 12_000
    assert [c.change_id for c in ledger.pending(now_ms=11_500)] == ["chg-0002"]
    with pytest.raises(ResourceNotFoundException):
        ledger.get("chg-0001", now_ms=11_500)


# ─── 规则静态动作也过同一闸门 ─────────────────────────────────────────────────


async def test_execute_control_rule_source_denies_protected(tmp_path, store):
    proxy = _make_proxy(tmp_path, store)
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=False)
    with pytest.raises(ValidationException) as ei:
        await execute_control(
            proxy, "cam", req, source="rule", source_id="r1", deny_protected=True
        )
    assert "protected category 'camera'" in ei.value.message
    proxy.set_device_properties.assert_not_called()


async def test_rule_runner_static_action_goes_through_gate(tmp_path, store, monkeypatch):
    from miloco.rule.runner import RuleRunner
    from miloco.rule.schema import RuleAction

    proxy = _make_proxy(tmp_path, store)
    runner = RuleRunner(
        rules=[], miot_proxy=proxy, rule_log_repo=MagicMock(),
        task_record_service=MagicMock(),
    )
    spy = AsyncMock()
    monkeypatch.setattr("miloco.miot.service._write_action_ledger", spy)

    # 受保护设备：拒绝，不下发，台账留痕 source=rule
    protected = RuleAction(did="cam", iid="prop.2.1", value=False, idempotent=False,
                           cooldown_minutes=5)
    res = await runner._execute_action("rule-1", protected)
    assert res.result is False and res.error.startswith("gate_refused:")
    proxy.set_device_properties.assert_not_called()
    kw = spy.await_args.kwargs
    assert kw["source"] == "rule" and kw["source_id"] == "rule-1" and kw["success"] is False

    # 普通设备越界值：同样被服务端校验拦下
    bad = RuleAction(did="lamp", iid="prop.2.2", value=500, idempotent=False,
                     cooldown_minutes=5)
    res = await runner._execute_action("rule-1", bad)
    assert res.result is False and "out of range" in res.error
    proxy.set_device_properties.assert_not_called()

    # 普通设备合法值：正常下发，成功
    ok = RuleAction(did="lamp", iid="prop.2.2", value=50, idempotent=False,
                    cooldown_minutes=5)
    res = await runner._execute_action("rule-1", ok)
    assert res.result is True and res.error is None
    proxy.set_device_properties.assert_awaited_once()


async def test_agent_cannot_self_approve_using_stage_response(svc, caplog):
    staged = await svc.control_device(
        "cam", DeviceControlRequest(type="set_property", iid="prop.2.1", value=False)
    )
    token = _delivered_token(svc)
    assert token not in json.dumps(staged)
    assert token not in json.dumps(await svc.list_changes())
    assert token not in caplog.text
    with pytest.raises(AuthorizationException):
        await svc.apply_change(staged["change_id"], staged.get("confirm_token"))
    svc._miot_proxy.set_device_properties.assert_not_called()
    # Only the credential delivered directly to the user authorizes this change.
    await svc.apply_change(staged["change_id"], token)
    svc._miot_proxy.set_device_properties.assert_awaited_once()


@pytest.mark.parametrize("raises", [False, True])
async def test_failed_confirmation_delivery_revokes_change_without_leaking(svc, caplog, raises):
    async def fail(content):
        if raises:
            raise RuntimeError(content)
        return False

    svc._miot_proxy.send_device_confirmation.side_effect = fail
    with pytest.raises(MiotServiceException) as caught:
        await svc.control_device(
            "cam", DeviceControlRequest(type="set_property", iid="prop.2.1", value=False)
        )
    token = _delivered_token(svc)
    assert token not in str(caught.value)
    assert token not in caplog.text
    assert (await svc.list_changes())["changes"] == []
    svc._miot_proxy.set_device_properties.assert_not_called()


async def test_confirmation_delivery_cancelled_revokes_change(svc):
    import asyncio

    svc._miot_proxy.send_device_confirmation.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await svc.control_device(
            "cam", DeviceControlRequest(type="set_property", iid="prop.2.1", value=False)
        )
    assert (await svc.list_changes())["changes"] == []
    svc._miot_proxy.set_device_properties.assert_not_called()


async def test_confirmation_token_cannot_approve_another_change(svc):
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=False)
    await svc.control_device("cam", req)
    first_token = _delivered_token(svc)
    second = await svc.control_device("cam", req)
    assert _delivered_token(svc) != first_token
    with pytest.raises(AuthorizationException):
        await svc.apply_change(second["change_id"], first_token)
    svc._miot_proxy.set_device_properties.assert_not_called()


@pytest.mark.parametrize("fails", [False, True])
async def test_confirmation_transport_deletes_cloud_template(fails, caplog):
    from miloco.miot.client import MiotProxy

    client = SimpleNamespace(
        create_app_notify_async=AsyncMock(return_value="private-notify-id"),
        send_app_notify_async=AsyncMock(return_value=True),
        delete_app_notifies_async=AsyncMock(return_value=True),
    )
    if fails:
        client.send_app_notify_async.side_effect = RuntimeError("private-confirmation-content")
    proxy = SimpleNamespace(_miot_client=client)
    assert await MiotProxy.send_device_confirmation(proxy, "private-confirmation-content") is not fails
    client.delete_app_notifies_async.assert_awaited_once_with("private-notify-id")
    assert "private-confirmation-content" not in caplog.text


@pytest.mark.parametrize("cleanup", [False, RuntimeError("private-confirmation-content")])
async def test_confirmation_transport_cleanup_failure_is_not_success(cleanup, caplog):
    from miloco.miot.client import MiotProxy

    client = SimpleNamespace(
        create_app_notify_async=AsyncMock(return_value="private-notify-id"),
        send_app_notify_async=AsyncMock(return_value=True),
        delete_app_notifies_async=AsyncMock(return_value=cleanup),
    )
    if isinstance(cleanup, Exception):
        client.delete_app_notifies_async.side_effect = cleanup
    assert await MiotProxy.send_device_confirmation(
        SimpleNamespace(_miot_client=client), "private-confirmation-content"
    ) is False
    assert "private-confirmation-content" not in caplog.text

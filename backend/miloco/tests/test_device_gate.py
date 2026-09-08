"""设备控制闸门：服务端值校验 + 受保护类别 stage / apply + action_ledger 审计口径。

MiotProxy 用最小 stub（同 test_miot_service_lru / test_action_ledger 的 SimpleNamespace
手法），LRUStore 打 temp SQLite；配置走真实 get_settings()（默认 protected_categories
含 camera）。台账断言用真 MetricsClient 写 temp observability.db（不 mock）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.config.settings import reset_settings
from miloco.database.kv_repo import ScopeConfigKeys
from miloco.middleware.exceptions import (
    AuthorizationException,
    ResourceNotFoundException,
    ValidationException,
)
from miloco.miot.gate import (
    PendingStore,
    resolve_protection,
    validate_request_against_spec,
)
from miloco.miot.schema import DeviceControlRequest, PropertyItem
from miloco.miot.service import MiotService, execute_control
from miloco.observability import metrics_client as mc
from miloco.observability.metrics_client import MetricsClient

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
    "prop.2.4": {
        "type_name": "target-temperature", "format": "float", "writeable": True,
        "value_range": [16, 30, 0.5], "unit": "celsius",
    },
    "prop.3.1": {"type_name": "temperature", "format": "float", "writeable": False},
    "action.5.1": {
        "type_name": "play-text", "in_params": [{"name": "text-content", "format": "string"}],
    },
}
CAMERA_SPEC = {
    "prop.2.1": {"type_name": "on", "format": "bool", "writeable": True},
    "action.5.1": {
        "type_name": "play-text", "in_params": [{"name": "text-content", "format": "string"}],
    },
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
        get_devices=AsyncMock(return_value={"lamp": LAMP, "cam": CAMERA}),
        get_cameras=AsyncMock(return_value={}),
        _fetch_device_spec=_fetch_spec,
    )


@pytest.fixture
def store() -> dict[str, str]:
    return {ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])}


@pytest.fixture
def svc(tmp_path, store) -> MiotService:
    reset_settings()
    return MiotService(miot_proxy=_make_proxy(tmp_path, store))


@pytest.fixture
async def ledger(tmp_path):
    """真 MetricsClient 绑到 module-level singleton；返回读全表的 callable。"""
    obs_db = tmp_path / "observability.db"
    client = MetricsClient(db_path=obs_db)
    await client.start()
    mc.set_metrics_client(client)

    async def _rows() -> list[dict]:
        await client.flush()
        conn = sqlite3.connect(str(obs_db))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM action_ledger ORDER BY timestamp, rowid"
            ).fetchall()]
        finally:
            conn.close()

    try:
        yield _rows
    finally:
        mc.set_metrics_client(None)
        await client.stop()


def _req_off() -> DeviceControlRequest:
    return DeviceControlRequest(type="set_property", iid="prop.2.1", value=False)


# ─── 纯函数：保护判定 ─────────────────────────────────────────────────────────


def test_resolve_protection_reads_category_from_urn():
    d = resolve_protection(CAMERA, ["lock", "camera"])
    assert d.protected is True and d.category == "camera"
    d = resolve_protection(LAMP, ["lock", "camera"])
    assert d.protected is False and d.category == "light"
    # 类别未知（无 urn / None）= 不受保护
    assert resolve_protection(None, ["camera"]).protected is False
    assert resolve_protection(SimpleNamespace(urn=None), ["camera"]).protected is False
    # 空名单 = 关闭闸门
    assert resolve_protection(CAMERA, []).protected is False


# ─── 服务端值校验 ─────────────────────────────────────────────────────────────


async def test_out_of_range_value_rejected_server_side(svc):
    req = DeviceControlRequest(type="set_property", iid="prop.2.2", value=130)
    with pytest.raises(ValidationException) as ei:
        await svc.control_device("lamp", req)
    assert "out of range [1,100;1] percentage" in ei.value.message
    svc._miot_proxy.set_device_properties.assert_not_called()


async def test_step_mismatch_rejected(svc):
    req = DeviceControlRequest(type="set_property", iid="prop.2.4", value=26.3)
    with pytest.raises(ValidationException) as ei:
        await svc.control_device("lamp", req)
    assert "does not match step 0.5" in ei.value.message
    # 合法步进值放行
    ok = DeviceControlRequest(type="set_property", iid="prop.2.4", value=26.5)
    await svc.control_device("lamp", ok)
    svc._miot_proxy.set_device_properties.assert_awaited_once()


async def test_invalid_enum_lists_allowed(svc):
    req = DeviceControlRequest(type="set_property", iid="prop.2.3", value=7)
    with pytest.raises(ValidationException) as ei:
        await svc.control_device("lamp", req)
    assert "allowed: Day=0, Night=1" in ei.value.message


async def test_bool_property_requires_bool(svc):
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=1)
    with pytest.raises(ValidationException) as ei:
        await svc.control_device("lamp", req)
    assert "must be a boolean" in ei.value.message


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


async def test_validation_failure_writes_rejected_ledger_row(svc, ledger):
    req = DeviceControlRequest(type="set_property", iid="prop.2.2", value=130)
    with pytest.raises(ValidationException):
        await svc.control_device("lamp", req)
    rows = await ledger()
    assert len(rows) == 1
    r = rows[0]
    assert (r["status"], r["success"], r["protected"], r["change_id"]) == ("rejected", 0, 0, None)
    assert r["error"].startswith("gate: value 130")
    assert r["value_json"] == "130"


# ─── 受保护类别：stage 而不执行 ─────────────────────────────────────────────────


async def test_protected_category_is_staged_not_executed(svc):
    out = await svc.control_device("cam", _req_off())
    assert out["staged"] is True
    assert out["change_id"].startswith("chg-")
    assert out["category"] == "camera"
    assert out["did"] == "cam"
    assert out["device_name"] == "摄像头"
    assert out["summary"] == "set prop.2.1 = False"
    assert out["confirm_token"] and len(out["confirm_token"]) >= 24
    assert out["expires_at"]
    assert f"device apply {out['change_id']}" in out["next"]
    svc._miot_proxy.set_device_properties.assert_not_called()
    # 未执行的控制不进 LRU
    assert (await svc.lru_snapshot())["histories"] == {}
    pending = (await svc.list_changes())["changes"]
    assert [c["change_id"] for c in pending] == [out["change_id"]]
    # 列表接口不泄露凭据；存储侧也只有哈希
    assert "confirm_token" not in pending[0]
    assert out["confirm_token"] not in json.dumps(pending)
    assert out["confirm_token"] not in repr(svc._pending._items[out["change_id"]])


async def test_stage_writes_staged_ledger_row(svc, ledger):
    out = await svc.control_device("cam", _req_off())
    rows = await ledger()
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "staged"
    assert r["success"] == 0 and r["protected"] == 1
    assert r["change_id"] == out["change_id"] == r["source_id"]
    assert r["source"] == "cli" and r["did"] == "cam" and r["home_id"] == "H1"
    assert r["value_json"] == "false"
    # 台账里没有凭据
    assert out["confirm_token"] not in json.dumps(r)


async def test_staged_request_still_value_validated(svc, ledger):
    """受保护设备的错参不进待确认表，立刻 422 让 agent 改对；台账落 rejected。"""
    req = DeviceControlRequest(type="set_property", iid="prop.9.9", value=1)
    with pytest.raises(ValidationException):
        await svc.control_device("cam", req)
    assert (await svc.list_changes())["changes"] == []
    rows = await ledger()
    assert [(r["status"], r["protected"]) for r in rows] == [("rejected", 1)]


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


async def test_apply_with_correct_token_executes_once(svc, ledger):
    staged = await svc.control_device("cam", _req_off())
    out = await svc.apply_change(staged["change_id"], staged["confirm_token"])
    assert out["applied"] is True
    assert out["change_id"] == staged["change_id"]
    assert out["results"] == [{"code": 0, "siid": 2, "piid": 1}]
    svc._miot_proxy.set_device_properties.assert_awaited_once()
    sent = svc._miot_proxy.set_device_properties.await_args.args[0][0]
    assert (sent.did, sent.siid, sent.piid, sent.value) == ("cam", 2, 1, False)
    assert (await svc.lru_snapshot())["histories"]["cam"] == ["prop.2.1"]
    # 一次性凭据：重放被拒
    with pytest.raises(ResourceNotFoundException):
        await svc.apply_change(staged["change_id"], staged["confirm_token"])
    assert (await svc.list_changes())["changes"] == []
    # 台账：staged 行 + applied 行共用同一个 change_id
    rows = await ledger()
    assert [r["status"] for r in rows] == ["staged", "applied"]
    assert {r["change_id"] for r in rows} == {staged["change_id"]}
    applied = rows[1]
    assert applied["success"] == 1 and applied["protected"] == 1
    assert applied["source"] == "cli" and applied["source_id"] == staged["change_id"]
    assert applied["result_msg"] is None and applied["error"] is None


async def test_apply_with_wrong_token_refused_and_change_kept(svc, ledger):
    staged = await svc.control_device("cam", _req_off())
    with pytest.raises(AuthorizationException):
        await svc.apply_change(staged["change_id"], "nope")
    with pytest.raises(AuthorizationException):
        await svc.apply_change(staged["change_id"], None)
    svc._miot_proxy.set_device_properties.assert_not_called()
    # 猜错 token 不应销毁用户待确认的变更
    assert len((await svc.list_changes())["changes"]) == 1
    rows = await ledger()
    assert [r["status"] for r in rows] == ["staged", "apply_rejected", "apply_rejected"]
    assert rows[1]["error"] == "gate: confirm_token mismatch"
    assert rows[1]["change_id"] == staged["change_id"] and rows[1]["protected"] == 1


async def test_token_cannot_approve_another_change(svc):
    first = await svc.control_device("cam", _req_off())
    second = await svc.control_device("cam", _req_off())
    assert first["confirm_token"] != second["confirm_token"]
    with pytest.raises(AuthorizationException):
        await svc.apply_change(second["change_id"], first["confirm_token"])
    svc._miot_proxy.set_device_properties.assert_not_called()


async def test_apply_expired_change_refused_and_audited(svc, ledger):
    staged = await svc.control_device("cam", _req_off())
    change = svc._pending._items[staged["change_id"]]
    change.expires_at_ms = change.created_at_ms - 1  # 人为过期
    with pytest.raises(ResourceNotFoundException) as ei:
        await svc.apply_change(staged["change_id"], staged["confirm_token"])
    assert "expired" in ei.value.message
    svc._miot_proxy.set_device_properties.assert_not_called()
    rows = await ledger()
    assert [r["status"] for r in rows] == ["staged", "expired"]
    assert rows[1]["change_id"] == staged["change_id"] and rows[1]["success"] == 0


async def test_apply_unknown_change_refused(svc):
    with pytest.raises(ResourceNotFoundException):
        await svc.apply_change("chg-deadbeef", "x")


async def test_apply_after_scope_tightened_refused(svc, store, ledger):
    """stage 后家庭 scope 收紧（设备所在家庭被停用）→ apply 按当前配置拒绝。"""
    staged = await svc.control_device("cam", _req_off())
    store[ScopeConfigKeys.HOME_WHITE_LIST_KEY] = json.dumps(["H2"])
    with pytest.raises(ValidationException) as ei:
        await svc.apply_change(staged["change_id"], staged["confirm_token"])
    assert "not in an allowed home" in ei.value.message
    svc._miot_proxy.set_device_properties.assert_not_called()
    rows = await ledger()
    assert [r["status"] for r in rows] == ["staged", "apply_rejected"]
    assert "not in an allowed home" in rows[1]["error"]
    # 凭据一次性：复检失败也已消费
    assert (await svc.list_changes())["changes"] == []


async def test_apply_revalidates_against_spec(svc, ledger):
    """stage 后 spec 变了（属性变只读）→ apply 时的值校验拒绝，落 apply_rejected。"""
    staged = await svc.control_device("cam", _req_off())
    svc._miot_proxy._fetch_device_spec = AsyncMock(return_value={
        "prop.2.1": {"type_name": "on", "format": "bool", "writeable": False},
    })
    with pytest.raises(ValidationException) as ei:
        await svc.apply_change(staged["change_id"], staged["confirm_token"])
    assert "read-only" in ei.value.message
    rows = await ledger()
    assert [r["status"] for r in rows] == ["staged", "apply_rejected"]


async def test_discard_change(svc, ledger):
    req = DeviceControlRequest(type="call_action", iid="action.5.1", params=["hi"])
    staged = await svc.control_device("cam", req)
    out = await svc.discard_change(staged["change_id"])
    assert out == {"discarded": True, "change_id": staged["change_id"], "did": "cam"}
    assert (await svc.list_changes())["changes"] == []
    with pytest.raises(ResourceNotFoundException):
        await svc.discard_change(staged["change_id"])
    rows = await ledger()
    assert [r["status"] for r in rows] == ["staged", "discarded"]
    assert rows[0]["action_type"] == rows[1]["action_type"] == "call_action"
    assert rows[1]["value_json"] == '["hi"]'


async def test_list_changes_purges_and_audits_expired(svc, ledger):
    a = await svc.control_device("cam", _req_off())
    b = await svc.control_device("cam", _req_off())
    ca = svc._pending._items[a["change_id"]]
    ca.expires_at_ms = ca.created_at_ms - 1
    pending = (await svc.list_changes())["changes"]
    assert [c["change_id"] for c in pending] == [b["change_id"]]
    rows = await ledger()
    assert [(r["status"], r["change_id"]) for r in rows] == [
        ("staged", a["change_id"]), ("staged", b["change_id"]), ("expired", a["change_id"]),
    ]


def test_pending_store_ttl_and_token_hashing():
    store = PendingStore(ttl_sec=10)
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=1)
    a, tok_a = store.stage(did="d", request=req, category="lock", device_name=None,
                           room=None, now_ms=1_000)
    b, tok_b = store.stage(did="d", request=req, category="lock", device_name=None,
                           room=None, now_ms=2_000)
    assert a.change_id != b.change_id and a.change_id.startswith("chg-")
    assert tok_a != tok_b
    assert tok_a not in repr(a) and a.token_hash != tok_a
    assert store.verify_token(a, tok_a) and not store.verify_token(a, tok_b)
    assert not store.verify_token(a, None) and not store.verify_token(a, "")
    assert [c.change_id for c in store.pending(now_ms=5_000)] == [a.change_id, b.change_id]
    # a 在 11_000 过期，b 到 12_000
    assert [c.change_id for c in store.pending(now_ms=11_500)] == [b.change_id]
    expired = store.purge_expired(now_ms=11_500)
    assert [c.change_id for c in expired] == [a.change_id]
    assert store.get(a.change_id) is None and store.get(b.change_id) is b
    assert store.take(b.change_id) is b and store.take(b.change_id) is None
    # TTL 下限 1 秒
    assert PendingStore(ttl_sec=0).ttl_sec == 1.0


# ─── 规则静态动作也过同一闸门 ─────────────────────────────────────────────────


async def test_execute_control_rule_source_denies_protected_by_default(tmp_path, store, ledger):
    reset_settings()
    proxy = _make_proxy(tmp_path, store)
    with pytest.raises(ValidationException) as ei:
        await execute_control(
            proxy, "cam", _req_off(), source="rule", source_id="r1", on_protected="deny"
        )
    assert "protected category 'camera'" in ei.value.message
    proxy.set_device_properties.assert_not_called()
    rows = await ledger()
    assert len(rows) == 1
    r = rows[0]
    assert (r["status"], r["success"], r["protected"], r["source"], r["source_id"]) == (
        "rejected", 0, 1, "rule", "r1"
    )


async def test_execute_control_rule_allow_executes_and_marks_protected(tmp_path, store, ledger):
    reset_settings()
    proxy = _make_proxy(tmp_path, store)
    outcome = await execute_control(
        proxy, "cam", _req_off(), source="rule", source_id="r1", on_protected="allow"
    )
    assert outcome.success is True and outcome.protected is True
    proxy.set_device_properties.assert_awaited_once()
    rows = await ledger()
    assert [(r["status"], r["protected"], r["source"]) for r in rows] == [("applied", 1, "rule")]
    # 非受保护设备 protected=0
    await execute_control(
        proxy, "lamp", DeviceControlRequest(type="set_property", iid="prop.2.1", value=True),
        source="rule", source_id="r1", on_protected="allow",
    )
    rows = await ledger()
    assert rows[-1]["protected"] == 0 and rows[-1]["did"] == "lamp"


def _make_runner(proxy):
    from miloco.rule.runner import RuleRunner

    return RuleRunner(
        rules=[], miot_proxy=proxy, rule_log_repo=MagicMock(),
        task_record_service=MagicMock(),
    )


async def test_rule_runner_static_action_goes_through_gate(tmp_path, store, monkeypatch):
    from miloco.rule.schema import RuleAction

    reset_settings()
    proxy = _make_proxy(tmp_path, store)
    runner = _make_runner(proxy)
    spy = AsyncMock()
    monkeypatch.setattr("miloco.miot.service._write_action_ledger", spy)

    # 受保护设备（默认 rule_protected=deny）：拒绝，不下发，台账留痕 source=rule
    protected = RuleAction(did="cam", iid="prop.2.1", value=False, idempotent=False,
                           cooldown_minutes=5)
    res = await runner._execute_action("rule-1", protected)
    assert res.result is False and res.error.startswith("gate_refused:")
    proxy.set_device_properties.assert_not_called()
    kw = spy.await_args.kwargs
    assert kw["source"] == "rule" and kw["source_id"] == "rule-1"
    assert kw["success"] is False and kw["status"] == "rejected" and kw["protected"] is True

    # 普通设备越界值：同样被服务端校验拦下
    bad = RuleAction(did="lamp", iid="prop.2.2", value=500, idempotent=False,
                     cooldown_minutes=5)
    res = await runner._execute_action("rule-1", bad)
    assert res.result is False and "out of range" in res.error
    proxy.set_device_properties.assert_not_called()

    # 普通设备合法值：正常下发，成功，进入冷却
    ok = RuleAction(did="lamp", iid="prop.2.2", value=50, idempotent=False,
                    cooldown_minutes=5)
    res = await runner._execute_action("rule-1", ok)
    assert res.result is True and res.error is None
    proxy.set_device_properties.assert_awaited_once()
    kw = spy.await_args.kwargs
    assert kw["status"] == "applied" and kw["protected"] is False and kw["success"] is True
    assert runner._in_cooldown("rule-1", ok)


async def test_rule_runner_allow_policy_executes_protected(tmp_path, store, monkeypatch):
    from miloco.rule.schema import RuleAction

    monkeypatch.setenv("MILOCO_SAFETY__RULE_PROTECTED", "allow")
    reset_settings()
    try:
        proxy = _make_proxy(tmp_path, store)
        runner = _make_runner(proxy)
        spy = AsyncMock()
        monkeypatch.setattr("miloco.miot.service._write_action_ledger", spy)
        action = RuleAction(did="cam", iid="prop.2.1", value=False, idempotent=False,
                            cooldown_minutes=5)
        res = await runner._execute_action("rule-2", action)
        assert res.result is True and res.error is None
        proxy.set_device_properties.assert_awaited_once()
        kw = spy.await_args.kwargs
        assert kw["source"] == "rule" and kw["status"] == "applied" and kw["protected"] is True
    finally:
        monkeypatch.delenv("MILOCO_SAFETY__RULE_PROTECTED")
        reset_settings()

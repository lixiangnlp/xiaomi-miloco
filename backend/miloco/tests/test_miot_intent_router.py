"""POST /api/miot/intent/resolve 端到端（TestClient）+ MiotService.resolve_intent 接线。

router 模块级 ``manager = get_manager()`` 用 SimpleNamespace 换掉；service 层用最小
stub proxy 构造 MiotService，并把 ``get_home_info`` 换成返回固定家庭，验证请求模型校验、
信封结构与 service → intent 纯函数的接线。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.miot.schema import IntentResolveRequest
from miloco.miot.service import MiotService

_HOME = {
    "home_name": "我的家",
    "devices": [
        {
            "did": "AC1", "name": "卧室空调", "room": "卧室", "category": "air-conditioner", "online": True,
            "spec": {
                "prop.2.1": {"type_name": "on", "service_type_name": "air-conditioner", "service_description": "空调", "format": "bool", "writeable": True, "readable": True},
                "prop.2.3": {"type_name": "target-temperature", "service_type_name": "air-conditioner", "service_description": "空调", "format": "float", "writeable": True, "readable": True, "value_range": [16, 30, 0.5], "unit": "celsius"},
                "prop.4.1": {"type_name": "on", "service_type_name": "indicator-light", "service_description": "指示灯", "format": "bool", "writeable": True, "readable": True},
            },
        },
        {
            "did": "L1", "name": "台灯", "room": "卧室", "category": "light", "online": True,
            "spec": {
                "prop.2.1": {"type_name": "on", "service_type_name": "light", "format": "bool", "writeable": True, "readable": True},
            },
        },
        {
            "did": "L2", "name": "吸顶灯", "room": "客厅", "category": "light", "online": True,
            "spec": {
                "prop.2.1": {"type_name": "on", "service_type_name": "light", "format": "bool", "writeable": True, "readable": True},
            },
        },
        {
            "did": "SW1", "name": "书房开关", "room": "书房", "category": "switch", "online": True,
            "spec": {
                "prop.2.1": {"type_name": "on", "service_type_name": "switch", "format": "bool", "writeable": True, "readable": True},
            },
        },
    ],
    "scenes": [],
    "areas": [{"name": "卧室"}, {"name": "客厅"}, {"name": "书房"}],
}


def _make_service(monkeypatch) -> MiotService:
    from miloco.config.settings import reset_settings

    reset_settings()
    proxy = SimpleNamespace(_kv_repo=SimpleNamespace(db_connector=None))
    svc = MiotService(miot_proxy=proxy)
    monkeypatch.setattr(svc, "get_home_info", AsyncMock(return_value=dict(_HOME)))
    return svc


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    import miloco.miot.router as router_mod

    svc = _make_service(monkeypatch)
    monkeypatch.setattr(router_mod, "manager", SimpleNamespace(miot_service=svc))
    app = FastAPI()
    app.include_router(router_mod.router, prefix="/api")
    return TestClient(app)


def test_resolve_endpoint_single_hit(client):
    resp = client.post(
        "/api/miot/intent/resolve",
        json={"room": "卧室", "target": "空调", "action": "set", "property": "温度", "value": 26},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["ambiguity"] == "none"
    assert [c["did"] for c in data["candidates"]] == ["AC1"]
    assert data["candidates"][0]["needs_on"] == {"spec_name": "on@空调", "iid": "prop.2.1"}
    assert data["command_preview"] == [
        "miloco-cli device control AC1 --set target-temperature 26 --set 'on@空调' true"
    ]


def test_resolve_endpoint_multiple_and_scope_all(client):
    body = client.post("/api/miot/intent/resolve", json={"target": "灯", "property": "关"}).json()
    assert body["data"]["ambiguity"] == "multiple"
    assert body["data"]["command_preview"] == []
    assert "反问" in body["data"]["hint"]

    body = client.post(
        "/api/miot/intent/resolve", json={"target": "灯", "property": "关", "scope": "all"}
    ).json()
    assert body["data"]["ambiguity"] == "none"
    assert len(body["data"]["command_preview"]) == 2


def test_resolve_endpoint_switch_only_room_is_unconfirmed(client):
    """issue #36：书房没有灯、只有开关 → 列为可能控灯的候选，但不给 command_preview。"""
    body = client.post(
        "/api/miot/intent/resolve", json={"room": "书房", "target": "灯", "property": "开"}
    ).json()
    data = body["data"]
    assert data["ambiguity"] == "unconfirmed"
    assert [c["did"] for c in data["candidates"]] == ["SW1"]
    assert data["candidates"][0]["matched_by"] == "light-control-fallback"
    assert data["command_preview"] == []
    assert "反问" in data["hint"]


def test_resolve_endpoint_not_found(client):
    body = client.post("/api/miot/intent/resolve", json={"target": "洗衣机"}).json()
    assert body["data"]["ambiguity"] == "not_found"
    assert "device refresh" in body["data"]["hint"]


def test_resolve_endpoint_rejects_bad_request(client):
    # target 必填；scope / action 只接受枚举值
    assert client.post("/api/miot/intent/resolve", json={}).status_code == 422
    assert (
        client.post("/api/miot/intent/resolve", json={"target": "灯", "scope": "many"}).status_code
        == 422
    )
    assert (
        client.post("/api/miot/intent/resolve", json={"target": "灯", "action": "toggle"}).status_code
        == 422
    )


@pytest.mark.asyncio
async def test_service_resolve_intent_uses_home_info(monkeypatch):
    svc = _make_service(monkeypatch)
    out = await svc.resolve_intent(IntentResolveRequest(room="卧室", target="台灯", property="开"))
    svc.get_home_info.assert_awaited_once()
    assert out["ambiguity"] == "none"
    assert out["command_preview"] == ["miloco-cli device control L1 --set on true"]

"""device control / action 的 staged 分支、device changes / apply / discard 子命令，
以及 actions list --status 过滤。"""

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from miloco_cli.main import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    import os as _os

    for key in list(_os.environ):
        if key.startswith("MILOCO_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path / "miloco"))


@pytest.fixture()
def fake_home_info(monkeypatch):
    info = {
        "home_name": "我的家",
        "devices": [
            {
                "did": "cam_001",
                "name": "摄像头",
                "room": "客厅",
                "category": "camera",
                "online": True,
                "spec": {
                    "prop.2.1": {"type_name": "on", "format": "bool"},
                    "action.5.1": {"type_name": "play-text", "in_params": [
                        {"name": "text-content", "format": "string"}]},
                },
            },
        ],
        "scenes": [],
        "persons": [],
    }
    monkeypatch.setattr("miloco_cli.home_info._fetch", lambda **kwargs: info)
    return info


_STAGED = {
    "code": 0,
    "message": "Device control staged, awaiting user confirmation",
    "data": {
        "staged": True,
        "change_id": "chg-a1b2c3d4",
        "did": "cam_001",
        "device_name": "摄像头",
        "room": "客厅",
        "category": "camera",
        "summary": "set prop.2.1 = False",
        "request": {"type": "set_property", "iid": "prop.2.1", "value": False},
        "created_at_ms": 1,
        "expires_at_ms": 600001,
        "expires_at": "2026-09-08T10:10:00+08:00",
        "confirm_token": "tok-secret",
        "next": "…",
    },
}


def test_device_control_prints_staged_hint(runner, fake_home_info):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _STAGED
        result = runner.invoke(cli, ["device", "control", "cam_001", "on", "false"])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out["staged"] is True
    assert out["change_id"] == "chg-a1b2c3d4"
    assert out["did"] == "cam_001"
    assert out["category"] == "camera"
    assert out["summary"] == "set prop.2.1 = False"
    assert out["confirm_token"] == "tok-secret"
    assert "device apply chg-a1b2c3d4 --token" in out["next"]
    assert "device discard chg-a1b2c3d4" in out["next"]
    # staged 不是设备执行结果，不应被误当成功 / 失败去补 code_msg / 套信封
    assert "results" not in out and "code" not in out


def test_device_action_prints_staged_hint(runner, fake_home_info):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _STAGED
        result = runner.invoke(cli, ["device", "action", "cam_001", "play-text", "hi"])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out["staged"] is True and out["change_id"] == "chg-a1b2c3d4"


def test_device_control_non_staged_unchanged(runner, fake_home_info):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {"code": 0, "message": "ok", "data": {"results": [{"code": 0}]}}
        result = runner.invoke(cli, ["device", "control", "cam_001", "on", "true"])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert "staged" not in out
    assert out["data"]["did"] == "cam_001"


def test_device_changes_lists(runner):
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {"code": 0, "message": "ok", "data": {"changes": [
            {"change_id": "chg-a1b2c3d4", "did": "cam_001", "summary": "set prop.2.1 = False"}
        ]}}
        result = runner.invoke(cli, ["device", "changes"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/miot/changes")
    assert json.loads(result.output)["data"]["changes"][0]["change_id"] == "chg-a1b2c3d4"


def test_device_apply_posts_token(runner):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {
            "code": 0,
            "message": "Staged change applied",
            "data": {"applied": True, "change_id": "chg-a1b2c3d4", "did": "cam_001",
                     "results": [{"code": 0}]},
        }
        result = runner.invoke(cli, ["device", "apply", "chg-a1b2c3d4", "--token", "tok-abc"])
    assert result.exit_code == 0
    mock.assert_called_once_with(
        "/api/miot/changes/chg-a1b2c3d4/apply", {"confirm_token": "tok-abc"}
    )
    out = json.loads(result.output)
    assert out["data"]["applied"] is True


def test_device_apply_requires_token(runner):
    with patch("miloco_cli.client.api_post") as mock:
        result = runner.invoke(cli, ["device", "apply", "chg-a1b2c3d4"])
    assert result.exit_code != 0
    mock.assert_not_called()


def test_device_apply_annotates_device_failure(runner):
    """apply 下发后设备侧失败码同样补 code_msg、改写外层信封。"""
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {
            "code": 0,
            "message": "Staged change applied",
            "data": {"applied": True, "change_id": "chg-a1b2c3d4",
                     "results": [{"code": -704042011}]},
        }
        result = runner.invoke(cli, ["device", "apply", "chg-a1b2c3d4", "--token", "t"])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out["data"]["results"][0]["code_msg"] == "设备离线"
    assert out["code"] == -704042011


def test_device_discard_deletes(runner):
    with patch("miloco_cli.client.api_delete") as mock:
        mock.return_value = {"code": 0, "message": "Staged change discarded",
                             "data": {"discarded": True, "change_id": "chg-a1b2c3d4"}}
        result = runner.invoke(cli, ["device", "discard", "chg-a1b2c3d4"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/miot/changes/chg-a1b2c3d4")
    assert json.loads(result.output)["data"]["discarded"] is True


# ─── actions list --status ───────────────────────────────────────────────────


def test_actions_list_status_filter_and_column(runner):
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = [{
            "id": "s1", "timestamp": 1_700_000_000_000,
            "action_type": "set_property", "did": "cam_001",
            "device_name": "摄像头", "room": "客厅", "iid": "prop.2.1",
            "value_json": "false", "result_code": None, "result_msg": None,
            "success": 0, "error": None, "trace_id": None,
            "status": "staged", "change_id": "chg-a1b2c3d4", "protected": 1,
        }]
        result = runner.invoke(cli, ["actions", "list", "--status", "staged"])
    assert result.exit_code == 0
    path, kwargs = mock.call_args[0][0], mock.call_args[1]
    assert path == "/api/actions"
    assert dict(kwargs["params"])["status"] == "staged"
    lines = result.output.strip().splitlines()
    assert lines[0] == "# ts|action_type|did|device_name|room|iid|success|reason|status|value"
    assert "|prop.2.1|fail|ok|staged|false" in lines[1]


def test_actions_list_status_defaults_to_applied_for_old_backend(runner):
    """老后端不返回 status 字段 → 渲染 applied，前 8 列位置不变。"""
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = [{
            "id": "a1", "timestamp": 1_700_000_000_000,
            "action_type": "set_property", "did": "lamp_001",
            "device_name": "台灯", "room": "客厅", "iid": "prop.2.1",
            "value_json": "true", "result_code": None, "result_msg": None,
            "success": 1, "error": None, "trace_id": None,
        }]
        result = runner.invoke(cli, ["actions", "list"])
    assert result.exit_code == 0
    assert "|prop.2.1|ok|ok|applied|true" in result.output.strip().splitlines()[1]


def test_actions_list_rejects_unknown_status(runner):
    result = runner.invoke(cli, ["actions", "list", "--status", "bogus"])
    assert result.exit_code != 0

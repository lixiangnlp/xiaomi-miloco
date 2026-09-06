"""device control / action 的 staged 分支，以及 device changes / apply / discard 子命令。"""

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
    "message": "Device control executed successfully",
    "data": {
        "staged": True,
        "change_id": "chg-0001",
        "did": "cam_001",
        "device_name": "摄像头",
        "room": "客厅",
        "category": "camera",
        "summary": "set prop.2.1 = False",
        "expires_at": "2026-09-07T10:10:00+08:00",
        "confirm_token": "tok-abc",
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
    assert out["change_id"] == "chg-0001"
    assert out["did"] == "cam_001"
    assert out["category"] == "camera"
    assert out["summary"] == "set prop.2.1 = False"
    assert out["confirm_token"] == "tok-abc"
    assert "device apply chg-0001 --token" in out["next"]
    assert "device discard chg-0001" in out["next"]
    # staged 不是设备执行结果，不应被误当成功 / 失败去补 code_msg
    assert "results" not in out


def test_device_action_prints_staged_hint(runner, fake_home_info):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _STAGED
        result = runner.invoke(cli, ["device", "action", "cam_001", "play-text", "hi"])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out["staged"] is True and out["change_id"] == "chg-0001"


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
            {"change_id": "chg-0001", "did": "cam_001", "summary": "set prop.2.1 = False"}
        ]}}
        result = runner.invoke(cli, ["device", "changes"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/miot/changes")
    assert json.loads(result.output)["data"]["changes"][0]["change_id"] == "chg-0001"


def test_device_apply_posts_token(runner):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {
            "code": 0,
            "message": "Staged change applied",
            "data": {"applied": True, "change_id": "chg-0001", "did": "cam_001",
                     "results": [{"code": 0}]},
        }
        result = runner.invoke(cli, ["device", "apply", "chg-0001", "--token", "tok-abc"])
    assert result.exit_code == 0
    mock.assert_called_once_with(
        "/api/miot/changes/chg-0001/apply", {"confirm_token": "tok-abc"}
    )
    out = json.loads(result.output)
    assert out["data"]["applied"] is True


def test_device_apply_requires_token(runner):
    with patch("miloco_cli.client.api_post") as mock:
        result = runner.invoke(cli, ["device", "apply", "chg-0001"])
    assert result.exit_code != 0
    mock.assert_not_called()


def test_device_apply_annotates_device_failure(runner):
    """apply 下发后设备侧失败码同样补 code_msg、改写外层信封。"""
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {
            "code": 0,
            "message": "Staged change applied",
            "data": {"applied": True, "change_id": "chg-0001",
                     "results": [{"code": -704042011}]},
        }
        result = runner.invoke(cli, ["device", "apply", "chg-0001", "--token", "t"])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out["data"]["results"][0]["code_msg"] == "设备离线"
    assert out["code"] == -704042011


def test_device_discard_deletes(runner):
    with patch("miloco_cli.client.api_delete") as mock:
        mock.return_value = {"code": 0, "message": "Staged change discarded",
                             "data": {"discarded": True, "change_id": "chg-0001"}}
        result = runner.invoke(cli, ["device", "discard", "chg-0001"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/miot/changes/chg-0001")
    assert json.loads(result.output)["data"]["discarded"] is True

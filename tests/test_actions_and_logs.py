"""Mocked unit tests for guarded container actions, admin token auth, and logs."""
from unittest.mock import MagicMock

import docker
from docker.errors import NotFound
from fastapi.testclient import TestClient

from app.main import app, event_listener
from app.services.extractor import extract_topology


def _mock_container(cid="a1b2c3d4e5f6000000000000000000000000000000000000000000000001",
                    name="web-1", status="running", labels=None):
    c = MagicMock()
    c.id = cid
    c.name = name
    c.status = status
    c.image.tags = ["nginx:latest"]
    c.attrs = {
        "Config": {"Labels": labels or {}},
        "NetworkSettings": {"Ports": {}, "Networks": {}},
    }
    c.logs.return_value = b"2026-11-23 INFO ready\n"
    return c


def test_extract_topology_marks_protected_system_containers(mocker):
    vis = _mock_container(
        cid="111111111111000000000000000000000000000000000000000000000001",
        name="cbca321-visualizer-1",
    )
    proxy = _mock_container(
        cid="222222222222000000000000000000000000000000000000000000000002",
        name="cbca321-socket-proxy-1",
    )
    user_app = _mock_container(
        cid="333333333333000000000000000000000000000000000000000000000003",
        name="my-redis",
    )

    client = MagicMock()
    client.ping.return_value = True
    client.networks.list.return_value = []
    client.containers.list.return_value = [vis, proxy, user_app]
    mocker.patch.object(docker, "from_env", return_value=client)

    topo = extract_topology()
    by_name = {n.name: n for n in topo.nodes}
    assert by_name["cbca321-visualizer-1"].is_protected is True
    assert by_name["cbca321-socket-proxy-1"].is_protected is True
    assert by_name["my-redis"].is_protected is False


def test_admin_token_required_and_validated(monkeypatch, mocker):
    monkeypatch.setenv("ADMIN_TOKEN", "super-secret")
    mocker.patch.object(event_listener, "start")
    mocker.patch.object(event_listener, "stop")

    c = _mock_container(status="exited")
    client = MagicMock()
    client.ping.return_value = True
    client.containers.get.return_value = c
    mocker.patch.object(docker, "from_env", return_value=client)

    with TestClient(app) as tc:
        # Missing token -> 401
        r_missing = tc.post("/api/v1/containers/cnt_a1b2c3d4e5f6/start")
        assert r_missing.status_code == 401

        # Wrong token -> 403
        r_bad = tc.post(
            "/api/v1/containers/cnt_a1b2c3d4e5f6/start",
            headers={"X-Admin-Token": "wrong"},
        )
        assert r_bad.status_code == 403

        # Valid token -> 200
        r_ok = tc.post(
            "/api/v1/containers/cnt_a1b2c3d4e5f6/start",
            headers={"X-Admin-Token": "super-secret"},
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["action"] == "start"
        c.start.assert_called_once()


def test_protected_container_blocks_stop_restart_delete(monkeypatch, mocker):
    monkeypatch.setenv("ADMIN_TOKEN", "super-secret")
    mocker.patch.object(event_listener, "start")
    mocker.patch.object(event_listener, "stop")

    proxy = _mock_container(name="socket-proxy", status="running")
    client = MagicMock()
    client.ping.return_value = True
    client.containers.get.return_value = proxy
    mocker.patch.object(docker, "from_env", return_value=client)

    headers = {"X-Admin-Token": "super-secret"}
    with TestClient(app) as tc:
        assert tc.post("/api/v1/containers/cnt_a1b2c3d4e5f6/stop", headers=headers).status_code == 403
        assert tc.post("/api/v1/containers/cnt_a1b2c3d4e5f6/restart", headers=headers).status_code == 403
        assert tc.delete("/api/v1/containers/cnt_a1b2c3d4e5f6?force=true", headers=headers).status_code == 403
        proxy.stop.assert_not_called()
        proxy.restart.assert_not_called()
        proxy.remove.assert_not_called()


def test_container_lifecycle_actions_and_delete_conflict(monkeypatch, mocker):
    monkeypatch.setenv("ADMIN_TOKEN", "super-secret")
    mocker.patch.object(event_listener, "start")
    mocker.patch.object(event_listener, "stop")

    c = _mock_container(name="worker-1", status="running")
    client = MagicMock()
    client.ping.return_value = True
    client.containers.get.return_value = c
    mocker.patch.object(docker, "from_env", return_value=client)

    headers = {"X-Admin-Token": "super-secret"}
    with TestClient(app) as tc:
        assert tc.post("/api/v1/containers/cnt_a1b2c3d4e5f6/stop", headers=headers).status_code == 200
        c.stop.assert_called_once()

        assert tc.post("/api/v1/containers/cnt_a1b2c3d4e5f6/restart", headers=headers).status_code == 200
        c.restart.assert_called_once()

        # Running container without ?force=true -> 409 Conflict
        r_conflict = tc.delete("/api/v1/containers/cnt_a1b2c3d4e5f6", headers=headers)
        assert r_conflict.status_code == 409
        c.remove.assert_not_called()

        # With ?force=true -> 200 OK
        r_force = tc.delete("/api/v1/containers/cnt_a1b2c3d4e5f6?force=true", headers=headers)
        assert r_force.status_code == 200
        c.remove.assert_called_once_with(force=True)

        # Missing container -> 404
        client.containers.get.side_effect = NotFound("gone")
        assert tc.post("/api/v1/containers/cnt_000000000000/start", headers=headers).status_code == 404


def test_container_logs_endpoint(mocker):
    mocker.patch.object(event_listener, "start")
    mocker.patch.object(event_listener, "stop")

    c = _mock_container(name="worker-1", status="running")
    client = MagicMock()
    client.ping.return_value = True
    client.containers.get.return_value = c
    mocker.patch.object(docker, "from_env", return_value=client)

    with TestClient(app) as tc:
        resp = tc.get("/api/v1/containers/cnt_a1b2c3d4e5f6/logs?tail=25")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tail"] == 25
        assert "INFO ready" in data["logs"]
        c.logs.assert_called_once_with(tail=25, stdout=True, stderr=True)

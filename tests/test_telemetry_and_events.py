"""Mocked unit tests for container telemetry stats and WebSocket delta events."""
from unittest.mock import MagicMock

import docker
from docker.errors import DockerException, NotFound
from fastapi.testclient import TestClient

from app.main import app, event_listener, manager
from app.services.events import parse_docker_event
from app.services.extractor import calculate_container_stats, get_container_stats


def test_calculate_container_stats_cpu_and_memory():
    raw = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 200_000_000, "percpu_usage": [1, 1]},
            "system_cpu_usage": 1_000_000_000,
            "online_cpus": 2,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 100_000_000},
            "system_cpu_usage": 500_000_000,
        },
        "memory_stats": {
            "usage": 100 * 1024 * 1024,
            "limit": 512 * 1024 * 1024,
            "stats": {"cache": 20 * 1024 * 1024},
        },
    }
    stats = calculate_container_stats("cnt_aabbccddeeff", "running", raw)
    # cpu_delta=1e8, system_delta=5e8, online_cpus=2 -> (1/5)*2*100 = 40.0%
    assert stats.cpu_percent == 40.0
    # net_usage = 80 MiB, limit = 512 MiB -> 15.62% / 15.63%
    assert stats.memory_usage_mb == 80.0
    assert stats.memory_limit_mb == 512.0
    assert stats.memory_percent == 15.62


def test_calculate_container_stats_zero_delta_and_just_started():
    # Just-started container: precpu_stats has no prior system_cpu_usage (0/missing)
    just_started = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 50_000_000},
            "system_cpu_usage": 1_000_000_000,
            "online_cpus": 2,
        },
        "precpu_stats": {"cpu_usage": {"total_usage": 0}, "system_cpu_usage": 0},
        "memory_stats": {"usage": 10 * 1024 * 1024, "limit": 0},
    }
    stats = calculate_container_stats("cnt_112233445566", "running", just_started)
    assert stats.cpu_percent == 0.0
    assert stats.memory_percent == 0.0

    # Zero system_delta must not raise ZeroDivisionError
    zero_delta = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 200_000_000},
            "system_cpu_usage": 500_000_000,
            "online_cpus": 2,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 100_000_000},
            "system_cpu_usage": 500_000_000,
        },
        "memory_stats": {},
    }
    stats_zero = calculate_container_stats("cnt_112233445566", "running", zero_delta)
    assert stats_zero.cpu_percent == 0.0
    assert stats_zero.memory_usage_mb == 0.0


def test_get_container_stats_exited_returns_zeros(mocker):
    container = MagicMock()
    container.id = "f6e5d4c3b2a1000000000000000000000000000000000000000000000001"
    container.status = "exited"

    client = MagicMock()
    client.ping.return_value = True
    client.containers.get.return_value = container
    mocker.patch.object(docker, "from_env", return_value=client)

    stats = get_container_stats("cnt_f6e5d4c3b2a1")
    assert stats.container_id == "cnt_f6e5d4c3b2a1"
    assert stats.status == "exited"
    assert stats.cpu_percent == 0.0
    assert stats.memory_usage_mb == 0.0
    assert stats.memory_limit_mb == 0.0
    assert stats.memory_percent == 0.0
    container.stats.assert_not_called()


def test_container_stats_endpoint_responses(mocker):
    container = MagicMock()
    container.id = "a1b2c3d4e5f6000000000000000000000000000000000000000000000001"
    container.status = "running"
    container.stats.return_value = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 150_000_000},
            "system_cpu_usage": 2_000_000_000,
            "online_cpus": 1,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 100_000_000},
            "system_cpu_usage": 1_000_000_000,
        },
        "memory_stats": {"usage": 50 * 1024 * 1024, "limit": 200 * 1024 * 1024},
    }

    client = MagicMock()
    client.ping.return_value = True
    client.containers.get.return_value = container
    mocker.patch.object(docker, "from_env", return_value=client)
    mocker.patch.object(event_listener, "start")
    mocker.patch.object(event_listener, "stop")

    with TestClient(app) as tc:
        ok_resp = tc.get("/api/v1/containers/cnt_a1b2c3d4e5f6/stats")
        assert ok_resp.status_code == 200
        data = ok_resp.json()
        assert data["cpu_percent"] == 5.0
        assert data["memory_usage_mb"] == 50.0
        assert data["memory_limit_mb"] == 200.0
        assert data["memory_percent"] == 25.0

        client.containers.get.side_effect = NotFound("missing")
        missing_resp = tc.get("/api/v1/containers/cnt_000000000000/stats")
        assert missing_resp.status_code == 404

        client.containers.get.side_effect = DockerException("proxy down")
        err_resp = tc.get("/api/v1/containers/cnt_000000000000/stats")
        assert err_resp.status_code == 503


def test_parse_docker_event_container_and_network_deltas():
    c_id = "11223344556677889900aabbccddeeff"
    n_id = "ffeeddccbbaa00998877665544332211"

    mock_container = MagicMock()
    mock_container.name = "api-server"
    mock_container.image.tags = ["python:3.11-slim"]
    mock_container.attrs = {
        "NetworkSettings": {
            "Ports": {"8000/tcp": [{"HostPort": "8000"}]},
            "Networks": {
                "frontend": {"NetworkID": n_id, "IPAddress": "172.18.0.10"}
            },
        }
    }
    mock_client = MagicMock()
    mock_client.containers.get.return_value = mock_container

    added = parse_docker_event(
        {"Type": "container", "Action": "start", "Actor": {"ID": c_id, "Attributes": {"name": "api-server"}}},
        client=mock_client,
    )
    assert added is not None
    assert added.event == "NODE_ADDED"
    assert added.payload["id"] == "cnt_112233445566"
    assert added.payload["attachments"][0]["network_id"] == "net_ffeeddccbbaa"

    stopped = parse_docker_event(
        {"Type": "container", "Action": "die", "Actor": {"ID": c_id, "Attributes": {}}}
    )
    assert stopped is not None
    assert stopped.event == "STATUS_UPDATED"
    assert stopped.payload == {"id": "cnt_112233445566", "status": "exited"}

    paused = parse_docker_event(
        {"Type": "container", "Action": "pause", "Actor": {"ID": c_id, "Attributes": {}}}
    )
    assert paused is not None
    assert paused.payload["status"] == "paused"

    removed = parse_docker_event(
        {"Type": "container", "Action": "destroy", "Actor": {"ID": c_id, "Attributes": {}}}
    )
    assert removed is not None
    assert removed.event == "NODE_REMOVED"
    assert removed.payload == {"id": "cnt_112233445566", "type": "container"}

    net_conn = parse_docker_event(
        {
            "Type": "network",
            "Action": "connect",
            "Actor": {"ID": n_id, "Attributes": {"name": "frontend", "container": c_id}},
        },
        client=mock_client,
    )
    assert net_conn is not None
    assert net_conn.event == "NETWORK_UPDATED"
    assert net_conn.payload["action"] == "connect"
    assert net_conn.payload["ip_address"] == "172.18.0.10"

    ignored = parse_docker_event({"Type": "volume", "Action": "create", "Actor": {"ID": "v1"}})
    assert ignored is None


def test_websocket_events_broadcast(mocker):
    mocker.patch.object(event_listener, "start")
    mocker.patch.object(event_listener, "stop")

    with TestClient(app) as tc:
        with tc.websocket_connect("/ws/events") as ws:
            assert len(manager.active_connections) == 1
            tc.portal.call(
                manager.broadcast,
                {"event": "STATUS_UPDATED", "payload": {"id": "cnt_112233445566", "status": "exited"}},
            )
            msg = ws.receive_json()
            assert msg["event"] == "STATUS_UPDATED"
            assert msg["payload"]["status"] == "exited"

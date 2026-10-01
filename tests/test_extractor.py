"""Mocked unit tests for the read-only Docker topology extractor.

No real Docker daemon is required. We mock:
  - docker.from_env()
  - client.networks.list()
  - client.containers.list()

and assert the service returns expected nodes, edges and IP associations.
"""
from unittest.mock import MagicMock

import pytest

import docker

from app.services.extractor import DockerTopologyExtractor, extract_topology


def _make_network(id_full="a1b2c3d4e5f60000000000000000000000000000000000000000000001",
                  name="frontend", driver="bridge", subnet="172.18.0.0/16"):
    net = MagicMock()
    net.id = id_full
    net.name = name
    net.attrs = {
        "Driver": driver,
        "IPAM": {"Config": [{"Subnet": subnet}]} if subnet else {"Config": []},
    }
    return net


def _make_network_no_ipam():
    net = MagicMock()
    net.id = "b" * 64
    net.name = "lonely"
    net.attrs = {"Driver": "bridge", "IPAM": {"Config": []}}
    return net


def _make_container(id_full="f6e5d4c3b2a1000000000000000000000000000000000000000000000001",
                    name="web-1", image_tag="nginx:latest", status="running",
                    ports=None, networks=None):
    c = MagicMock()
    c.id = id_full
    c.name = name
    c.status = status
    c.image.tags = [image_tag] if image_tag else []
    c.attrs = {
        "NetworkSettings": {
            "Ports": ports if ports is not None else {},
            "Networks": networks if networks is not None else {},
        }
    }
    return c


def _make_client(networks, containers):
    client = MagicMock()
    client.networks.list.return_value = networks
    client.containers.list.return_value = containers
    client.ping.return_value = True
    return client


def test_extract_topology_nodes_edges_and_ips(mocker):
    """Core happy path: networks + container + attachment edge + ports."""
    net = _make_network()
    container = _make_container(
        ports={"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}],
               "443/tcp": None},
        networks={"frontend": {"NetworkID": net.id,
                               "IPAddress": "172.18.0.5"}},
    )
    client = _make_client([net], [container])
    mocker.patch.object(docker, "from_env", return_value=client)

    topo = extract_topology()

    assert len(topo.nodes) == 2
    by_id = {n.id: n for n in topo.nodes}

    net_node = by_id["net_a1b2c3d4e5f6"]
    assert net_node.name == "frontend"
    assert net_node.type == "network"
    assert net_node.driver == "bridge"
    assert net_node.subnet == "172.18.0.0/16"

    cnt_node = by_id["cnt_f6e5d4c3b2a1"]
    assert cnt_node.name == "web-1"
    assert cnt_node.type == "container"
    assert cnt_node.image == "nginx:latest"
    assert cnt_node.status == "running"
    # Bound port renders HOST->internal; unbound renders bare.
    assert "8080->80/tcp" in cnt_node.ports
    assert "443/tcp" in cnt_node.ports

    assert len(topo.edges) == 1
    edge = topo.edges[0]
    assert edge.source == "cnt_f6e5d4c3b2a1"
    assert edge.target == "net_a1b2c3d4e5f6"
    assert edge.ip_address == "172.18.0.5"
    assert edge.edge_type == "network_attachment"


def test_extract_topology_subnet_fallback_to_na(mocker):
    """Networks without IPAM config must report subnet 'N/A' (LeoVerto parity)."""
    net = _make_network_no_ipam()
    client = _make_client([net], [])
    mocker.patch.object(docker, "from_env", return_value=client)

    topo = extract_topology()

    assert len(topo.nodes) == 1
    assert topo.nodes[0].subnet == "N/A"
    assert topo.edges == []


def test_extract_topology_untagged_image_and_empty_ports(mocker):
    """Untagged images and containers with no ports/networks are handled."""
    container = _make_container(image_tag=None, ports={}, networks={})
    client = _make_client([], [container])
    mocker.patch.object(docker, "from_env", return_value=client)

    topo = extract_topology()

    assert len(topo.nodes) == 1
    node = topo.nodes[0]
    assert node.image == "untagged"
    assert node.ports == []
    assert topo.edges == []


def test_extract_topology_multiple_networks_and_ip_associations(mocker):
    """A container on two networks yields two edges with correct IPs."""
    net_a = _make_network(id_full="a" * 64, name="frontend",
                          subnet="172.18.0.0/16")
    net_b = _make_network(id_full="c" * 64, name="backend", driver="bridge",
                          subnet="172.19.0.0/16")
    container = _make_container(
        networks={
            "frontend": {"NetworkID": net_a.id, "IPAddress": "172.18.0.5"},
            "backend": {"NetworkID": net_b.id, "IPAddress": "172.19.0.7"},
        }
    )
    client = _make_client([net_a, net_b], [container])
    mocker.patch.object(docker, "from_env", return_value=client)

    topo = extract_topology()

    assert len(topo.nodes) == 3
    ip_by_target = {e.target: e.ip_address for e in topo.edges}
    assert ip_by_target["net_aaaaaaaaaaaa"] == "172.18.0.5"
    assert ip_by_target["net_cccccccccccc"] == "172.19.0.7"


def test_extractor_class_accepts_injected_client():
    """Dependency injection path: pass a prebuilt client, no docker.from_env call."""
    net = _make_network()
    client = _make_client([net], [])
    extractor = DockerTopologyExtractor(client=client)

    topo = extractor.extract()

    assert len(topo.nodes) == 1
    assert topo.nodes[0].name == "frontend"
    client.networks.list.assert_called_once()


def test_extract_topology_empty_daemon(mocker):
    """Daemon with nothing on it returns empty graph (valid, not an error)."""
    client = _make_client([], [])
    mocker.patch.object(docker, "from_env", return_value=client)

    topo = extract_topology()

    assert topo.nodes == []
    assert topo.edges == []


def test_extract_topology_skips_attachment_without_network_id(mocker):
    """Network entries lacking NetworkID must not produce dangling edges."""
    container = _make_container(
        networks={"orphan": {"NetworkID": "", "IPAddress": "10.0.0.9"}}
    )
    client = _make_client([], [container])
    mocker.patch.object(docker, "from_env", return_value=client)

    topo = extract_topology()

    assert len(topo.nodes) == 1  # container node still present
    assert topo.edges == []


def test_topology_response_serializes_to_expected_json(mocker):
    """End-to-end JSON shape matches the Assessment-2.md contract."""
    net = _make_network()
    container = _make_container(
        ports={"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]},
        networks={"frontend": {"NetworkID": net.id,
                               "IPAddress": "172.18.0.5"}},
    )
    client = _make_client([net], [container])
    mocker.patch.object(docker, "from_env", return_value=client)

    topo = extract_topology()
    payload = topo.model_dump()

    assert set(payload.keys()) == {"nodes", "edges"}
    assert payload["nodes"][0]["id"].startswith("net_")
    assert payload["edges"][0]["source"].startswith("cnt_")


def test_health_and_topology_endpoints(mocker):
    """FastAPI wiring: /health ok, /api/v1/topology returns mocked graph."""
    from fastapi.testclient import TestClient

    from app.main import app

    net = _make_network()
    container = _make_container(
        networks={"frontend": {"NetworkID": net.id,
                               "IPAddress": "172.18.0.5"}}
    )
    client = _make_client([net], [container])
    mocker.patch.object(docker, "from_env", return_value=client)

    test_client = TestClient(app)
    assert test_client.get("/health").json() == {"status": "ok"}

    resp = test_client.get("/api/v1/topology")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["nodes"]) == 2
    assert len(body["edges"]) == 1

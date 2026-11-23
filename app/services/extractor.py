"""Read-only Docker topology extractor.

Adapted (read paths only) from LeoVerto/docker-network-graph:
- networks.list() + IPAM.Config[0].Subnet with "N/A" fallback
- containers.list(all=True) + NetworkSettings.Networks iteration for
  NetworkID / IPAddress
- port mapping parsing from NetworkSettings.Ports (HOST->internal)

Write operations from the reference repos (network create/delete, container
lifecycle) are intentionally NOT ported. This module only reads.
"""
from typing import Any, Dict, List, Optional

import docker
from docker.errors import DockerException

from app.models.schemas import (
    ContainerNode,
    ContainerStatsResponse,
    Edge,
    NetworkNode,
    TopologyResponse,
)


def _short_id(full_id: str) -> str:
    return (full_id or "")[:12]


def _network_subnet(attrs: Dict[str, Any]) -> str:
    try:
        configs = attrs.get("IPAM", {}).get("Config", []) or []
        if configs and configs[0].get("Subnet"):
            return configs[0]["Subnet"]
    except (AttributeError, KeyError, IndexError, TypeError):
        pass
    return "N/A"


def _container_ports(attrs: Dict[str, Any]) -> List[str]:
    ports: List[str] = []
    port_settings = (attrs.get("NetworkSettings", {}).get("Ports") or {})
    for internal_port, host_bindings in port_settings.items():
        if host_bindings:
            for binding in host_bindings:
                host_port = (binding or {}).get("HostPort", "")
                if host_port:
                    ports.append(f"{host_port}->{internal_port}")
                else:
                    ports.append(str(internal_port))
        else:
            ports.append(str(internal_port))
    return ports


def _container_image(container: Any) -> str:
    try:
        tags = container.image.tags or []
        if tags:
            return tags[0]
    except (AttributeError, IndexError):
        pass
    return "untagged"


_PROTECTED_NAMES = ("visualizer", "socket-proxy")


def is_protected_container(container: Any) -> bool:
    """Return True for system containers (visualizer, socket-proxy) that must not be mutated."""
    name = str(getattr(container, "name", "") or "").lstrip("/").lower()
    if any(token in name for token in _PROTECTED_NAMES):
        return True
    attrs = getattr(container, "attrs", {}) or {}
    labels = (attrs.get("Config") or {}).get("Labels") or {}
    service = str(labels.get("com.docker.compose.service", "") or "").lower()
    if service in _PROTECTED_NAMES:
        return True
    return str(labels.get("visualizer.protected", "") or "").lower() == "true"


class DockerTopologyExtractor:
    """Read-only extractor. Inject a client for tests; else docker.from_env()."""

    def __init__(self, client: Optional[Any] = None) -> None:
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            client = docker.from_env()
            client.ping()
            return client
        except DockerException as exc:
            raise DockerException(f"Docker daemon unreachable: {exc}") from exc
        except Exception as exc:  # e.g. missing socket in CI
            raise DockerException(f"Docker daemon unreachable: {exc}") from exc

    def extract(self) -> TopologyResponse:
        client = self._get_client()
        nodes: List[Any] = []
        edges: List[Edge] = []

        # 1. Networks (read-only list + attrs read)
        for network in client.networks.list():
            try:
                network.reload()
            except Exception:
                pass  # mocked networks may not implement reload meaningfully
            attrs = getattr(network, "attrs", {}) or {}
            nodes.append(
                NetworkNode(
                    id=f"net_{_short_id(getattr(network, 'id', ''))}",
                    name=getattr(network, "name", "unknown"),
                    driver=attrs.get("Driver", "bridge"),
                    subnet=_network_subnet(attrs),
                )
            )

        # 2. Containers + attachment edges (read-only list + attrs read)
        for container in client.containers.list(all=True):
            container_node_id = f"cnt_{_short_id(getattr(container, 'id', ''))}"
            attrs = getattr(container, "attrs", {}) or {}
            ports = _container_ports(attrs)
            nodes.append(
                ContainerNode(
                    id=container_node_id,
                    name=getattr(container, "name", "unknown"),
                    image=_container_image(container),
                    status=getattr(container, "status", "unknown"),
                    ports=ports,
                    is_protected=is_protected_container(container),
                )
            )

            net_settings = (attrs.get("NetworkSettings", {}).get("Networks") or {})
            for _net_name, net_data in net_settings.items():
                net_data = net_data or {}
                network_id = _short_id(net_data.get("NetworkID", ""))
                if not network_id:
                    continue  # avoid dangling edges
                edges.append(
                    Edge(
                        source=container_node_id,
                        target=f"net_{network_id}",
                        edge_type="network_attachment",
                        ip_address=net_data.get("IPAddress", ""),
                    )
                )

        return TopologyResponse(nodes=nodes, edges=edges)


def extract_topology(client: Optional[Any] = None) -> TopologyResponse:
    """Convenience function used by the API layer and tests."""
    return DockerTopologyExtractor(client=client).extract()


def calculate_container_stats(
    container_id: str,
    status: str,
    raw_stats: Dict[str, Any],
) -> ContainerStatsResponse:
    """Parse raw Docker `container.stats(stream=False)` JSON into clean metrics."""
    cpu_stats = raw_stats.get("cpu_stats") or {}
    precpu_stats = raw_stats.get("precpu_stats") or {}

    cpu_usage = (cpu_stats.get("cpu_usage") or {}).get("total_usage", 0) or 0
    precpu_usage = (precpu_stats.get("cpu_usage") or {}).get("total_usage", 0) or 0
    cpu_delta = cpu_usage - precpu_usage

    system_cpu = cpu_stats.get("system_cpu_usage", 0) or 0
    presystem_cpu = precpu_stats.get("system_cpu_usage", 0) or 0
    system_delta = system_cpu - presystem_cpu

    percpu = (cpu_stats.get("cpu_usage") or {}).get("percpu_usage") or []
    online_cpus = cpu_stats.get("online_cpus") or len(percpu) or 1

    cpu_percent = 0.0
    if presystem_cpu > 0 and system_delta > 0 and cpu_delta > 0:
        raw_cpu = (cpu_delta / system_delta) * online_cpus * 100.0
        cpu_percent = round(min(raw_cpu, online_cpus * 100.0), 2)

    mem_stats = raw_stats.get("memory_stats") or {}
    usage = mem_stats.get("usage", 0) or 0
    mem_detail = mem_stats.get("stats") or {}
    cache = mem_detail.get("cache") or mem_detail.get("inactive_file") or 0
    net_usage = max(usage - cache, 0)
    limit = mem_stats.get("limit", 0) or 0

    memory_usage_mb = round(net_usage / (1024 * 1024), 2)
    memory_limit_mb = round(limit / (1024 * 1024), 2)
    memory_percent = round((net_usage / limit) * 100.0, 2) if limit > 0 else 0.0

    return ContainerStatsResponse(
        container_id=container_id,
        status=status,
        cpu_percent=cpu_percent,
        memory_usage_mb=memory_usage_mb,
        memory_limit_mb=memory_limit_mb,
        memory_percent=memory_percent,
    )


def get_container_stats(
    container_id: str,
    client: Optional[Any] = None,
) -> ContainerStatsResponse:
    """Read-only on-demand telemetry for a single container."""
    docker_client = DockerTopologyExtractor(client=client)._get_client()
    lookup_id = container_id[4:] if container_id.startswith("cnt_") else container_id
    container = docker_client.containers.get(lookup_id)
    status = str(getattr(container, "status", "unknown") or "unknown")
    node_id = f"cnt_{_short_id(getattr(container, 'id', lookup_id))}"

    if status.lower() != "running":
        return ContainerStatsResponse(
            container_id=node_id,
            status=status,
            cpu_percent=0.0,
            memory_usage_mb=0.0,
            memory_limit_mb=0.0,
            memory_percent=0.0,
        )

    raw_stats = container.stats(stream=False) or {}
    return calculate_container_stats(node_id, status, raw_stats)

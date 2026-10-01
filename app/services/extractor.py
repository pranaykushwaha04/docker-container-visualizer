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

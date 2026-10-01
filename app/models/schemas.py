"""Pydantic models for the Docker topology graph.

Adapted from:
- LeoVerto/docker-network-graph traversal concepts (network/subnet/container/IP)
- DockerNet topology representation (networks with embedded containers),
  re-modeled here as flat validated Node/Edge lists.
"""
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


class NetworkNode(BaseModel):
    id: str = Field(..., pattern=r"^net_[0-9a-fA-F]{12}$")
    name: str = Field(..., min_length=1)
    type: Literal["network"] = "network"
    driver: str = "bridge"
    subnet: str = "N/A"


class ContainerNode(BaseModel):
    id: str = Field(..., pattern=r"^cnt_[0-9a-fA-F]{12}$")
    name: str = Field(..., min_length=1)
    type: Literal["container"] = "container"
    image: str = "untagged"
    status: str = "unknown"
    ports: List[str] = Field(default_factory=list)


Node = Union[NetworkNode, ContainerNode]


class Edge(BaseModel):
    source: str = Field(..., min_length=1)
    target: str = Field(..., min_length=1)
    edge_type: Literal["network_attachment", "port_forwarding"] = "network_attachment"
    ip_address: Optional[str] = None
    host_port: Optional[str] = None
    container_port: Optional[str] = None


class TopologyResponse(BaseModel):
    nodes: List[Node] = Field(default_factory=list)
    edges: List[Edge] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_edge_endpoints_exist(self) -> "TopologyResponse":
        node_ids = {n.id for n in self.nodes}
        for edge in self.edges:
            if edge.source not in node_ids:
                raise ValueError(f"Edge source '{edge.source}' does not match any node id")
            if edge.target not in node_ids:
                # Port-forwarding edges may target the host; allow only when
                # explicitly typed AND host_port is set. Otherwise enforce
                # referential integrity.
                if not (edge.edge_type == "port_forwarding" and edge.host_port):
                    raise ValueError(
                        f"Edge target '{edge.target}' does not match any node id"
                    )
        return self

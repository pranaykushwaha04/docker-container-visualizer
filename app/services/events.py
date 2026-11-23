"""Read-only Docker event listener and WebSocket delta broadcaster.

Subscribes to `client.events(decode=True)` in a background thread during the
FastAPI lifespan, converts container and network lifecycle events into
lightweight `TopologyDeltaEvent` payloads, and broadcasts them to connected
WebSocket clients.
"""
import asyncio
import threading
from typing import Any, Callable, Dict, Optional, Set

import docker
from docker.errors import DockerException
from fastapi import WebSocket

from app.models.schemas import TopologyDeltaEvent
from app.services.extractor import (
    _container_image,
    _container_ports,
    _short_id,
    is_protected_container,
)

_CONTAINER_ACTIONS = {"start", "stop", "die", "pause", "unpause", "destroy"}
_NETWORK_ACTIONS = {"connect", "disconnect", "destroy"}


class ConnectionManager:
    """Tracks active WebSocket clients and broadcasts JSON delta events."""

    def __init__(self) -> None:
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.active_connections.discard(websocket)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self.active_connections.discard(connection)


def _inspect_container_node(client: Any, actor_id: str, fallback_attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only lookup of container metadata for NODE_ADDED payloads."""
    short = _short_id(actor_id)
    node_id = f"cnt_{short}"
    name = fallback_attrs.get("name") or short
    image = fallback_attrs.get("image") or "untagged"
    ports: list[str] = []
    attachments: list[Dict[str, str]] = []
    protected = any(tok in str(name).lower() for tok in ("visualizer", "socket-proxy"))

    if client is not None and actor_id:
        try:
            container = client.containers.get(actor_id)
            attrs = getattr(container, "attrs", {}) or {}
            name = getattr(container, "name", name) or name
            image = _container_image(container)
            ports = _container_ports(attrs)
            protected = is_protected_container(container)
            net_settings = attrs.get("NetworkSettings", {}).get("Networks") or {}
            for net_name, net_data in net_settings.items():
                net_data = net_data or {}
                net_id = _short_id(net_data.get("NetworkID", ""))
                if net_id:
                    attachments.append(
                        {
                            "network_id": f"net_{net_id}",
                            "network_name": net_name,
                            "ip_address": net_data.get("IPAddress", "") or "",
                        }
                    )
        except Exception:
            pass

    return {
        "id": node_id,
        "type": "container",
        "name": name,
        "image": image,
        "status": "running",
        "ports": ports,
        "is_protected": protected,
        "attachments": attachments,
    }


def _lookup_network_ip(client: Any, container_id: str, network_id: str) -> str:
    """Best-effort read-only IP lookup for a container newly attached to a network."""
    if client is None or not container_id:
        return ""
    try:
        container = client.containers.get(container_id)
        attrs = getattr(container, "attrs", {}) or {}
        net_settings = attrs.get("NetworkSettings", {}).get("Networks") or {}
        target_short = _short_id(network_id)
        for net_data in net_settings.values():
            net_data = net_data or {}
            if _short_id(net_data.get("NetworkID", "")) == target_short:
                return net_data.get("IPAddress", "") or ""
    except Exception:
        pass
    return ""


def parse_docker_event(
    raw_event: Dict[str, Any],
    client: Optional[Any] = None,
) -> Optional[TopologyDeltaEvent]:
    """Translate a raw Docker SDK event dict into a TopologyDeltaEvent, or None."""
    if not isinstance(raw_event, dict):
        return None

    event_type = raw_event.get("Type")
    action = str(raw_event.get("Action") or "").split(":")[0].strip()
    actor = raw_event.get("Actor") or {}
    actor_id = str(actor.get("ID") or raw_event.get("id") or "")
    attrs = actor.get("Attributes") or {}

    if event_type == "container" and action in _CONTAINER_ACTIONS:
        short = _short_id(actor_id)
        if not short:
            return None
        node_id = f"cnt_{short}"

        if action == "start":
            return TopologyDeltaEvent(
                event="NODE_ADDED",
                payload=_inspect_container_node(client, actor_id, attrs),
            )
        if action in ("stop", "die"):
            return TopologyDeltaEvent(
                event="STATUS_UPDATED",
                payload={"id": node_id, "status": "exited"},
            )
        if action == "pause":
            return TopologyDeltaEvent(
                event="STATUS_UPDATED",
                payload={"id": node_id, "status": "paused"},
            )
        if action == "unpause":
            return TopologyDeltaEvent(
                event="STATUS_UPDATED",
                payload={"id": node_id, "status": "running"},
            )
        if action == "destroy":
            return TopologyDeltaEvent(
                event="NODE_REMOVED",
                payload={"id": node_id, "type": "container"},
            )

    if event_type == "network" and action in _NETWORK_ACTIONS:
        net_short = _short_id(actor_id)
        if not net_short:
            return None
        net_node_id = f"net_{net_short}"

        if action == "destroy":
            return TopologyDeltaEvent(
                event="NODE_REMOVED",
                payload={"id": net_node_id, "type": "network"},
            )

        container_raw_id = str(attrs.get("container") or "")
        container_short = _short_id(container_raw_id)
        if not container_short:
            return None

        ip_address = (
            _lookup_network_ip(client, container_raw_id, actor_id)
            if action == "connect"
            else ""
        )
        return TopologyDeltaEvent(
            event="NETWORK_UPDATED",
            payload={
                "action": action,
                "network_id": net_node_id,
                "network_name": attrs.get("name") or net_short,
                "container_id": f"cnt_{container_short}",
                "ip_address": ip_address,
            },
        )

    return None


class DockerEventListener:
    """Background daemon-thread listener for Docker events."""

    def __init__(
        self,
        manager: ConnectionManager,
        client_factory: Optional[Callable[[], Any]] = None,
        retry_delay: float = 5.0,
    ) -> None:
        self._manager = manager
        self._client_factory = client_factory or docker.from_env
        self._retry_delay = retry_delay
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._active_stream: Optional[Any] = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._loop = loop
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="docker-event-listener",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        stream = self._active_stream
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _emit(self, delta: TopologyDeltaEvent) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(
            self._manager.broadcast(delta.model_dump()),
            self._loop,
        )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            client = None
            try:
                client = self._client_factory()
                client.ping()
                stream = client.events(decode=True)
                self._active_stream = stream
                for raw_event in stream:
                    if self._stop_event.is_set():
                        break
                    delta = parse_docker_event(raw_event, client=client)
                    if delta is not None:
                        self._emit(delta)
            except (DockerException, Exception):
                if self._stop_event.wait(self._retry_delay):
                    break
            finally:
                self._active_stream = None
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

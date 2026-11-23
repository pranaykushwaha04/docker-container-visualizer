"""Container lifecycle actions and read-only log retrieval."""
from typing import Any, Literal, Optional

from app.models.schemas import ContainerActionResponse, ContainerLogsResponse
from app.services.extractor import (
    DockerTopologyExtractor,
    _short_id,
    is_protected_container,
)


class ProtectedContainerError(PermissionError):
    """Raised when a mutating action targets a protected system container."""


class ContainerRunningConflictError(RuntimeError):
    """Raised when deleting a running container without force=True."""


def _lookup_container(container_id: str, client: Optional[Any] = None) -> Any:
    docker_client = DockerTopologyExtractor(client=client)._get_client()
    lookup_id = container_id[4:] if container_id.startswith("cnt_") else container_id
    return docker_client.containers.get(lookup_id)


def perform_container_action(
    container_id: str,
    action: Literal["start", "stop", "restart", "delete"],
    force: bool = False,
    client: Optional[Any] = None,
) -> ContainerActionResponse:
    """Execute start/stop/restart/delete on a non-protected container."""
    container = _lookup_container(container_id, client=client)
    node_id = f"cnt_{_short_id(getattr(container, 'id', container_id))}"
    status = str(getattr(container, "status", "unknown") or "unknown")

    if action in ("stop", "restart", "delete") and is_protected_container(container):
        name = getattr(container, "name", node_id)
        raise ProtectedContainerError(
            f"Container '{name}' ({node_id}) is protected and cannot be modified."
        )

    if action == "start":
        container.start()
        return ContainerActionResponse(container_id=node_id, action="start", status="starting")
    if action == "stop":
        container.stop()
        return ContainerActionResponse(container_id=node_id, action="stop", status="stopping")
    if action == "restart":
        container.restart()
        return ContainerActionResponse(container_id=node_id, action="restart", status="restarting")

    # action == "delete"
    if status.lower() == "running" and not force:
        raise ContainerRunningConflictError(
            f"Container '{node_id}' is running; stop it first or pass ?force=true."
        )
    container.remove(force=force)
    return ContainerActionResponse(container_id=node_id, action="delete", status="removed")


def get_container_logs(
    container_id: str,
    tail: int = 50,
    client: Optional[Any] = None,
) -> ContainerLogsResponse:
    """Read-only fetch of the last `tail` lines of container stdout/stderr."""
    container = _lookup_container(container_id, client=client)
    node_id = f"cnt_{_short_id(getattr(container, 'id', container_id))}"
    raw_logs = container.logs(tail=tail, stdout=True, stderr=True)
    if isinstance(raw_logs, bytes):
        text = raw_logs.decode("utf-8", errors="replace")
    else:
        text = str(raw_logs or "")
    return ContainerLogsResponse(container_id=node_id, tail=tail, logs=text)

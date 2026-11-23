"""FastAPI entrypoint: topology API, WebSocket events, telemetry, logs, and guarded lifecycle actions."""
import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from docker.errors import APIError, DockerException, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.models.schemas import (
    ContainerActionResponse,
    ContainerLogsResponse,
    ContainerStatsResponse,
    TopologyResponse,
)
from app.services.actions import (
    ContainerRunningConflictError,
    ProtectedContainerError,
    get_container_logs,
    perform_container_action,
)
from app.services.events import ConnectionManager, DockerEventListener
from app.services.extractor import extract_topology, get_container_stats

manager = ConnectionManager()
event_listener = DockerEventListener(manager=manager)


def require_admin_token(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
) -> None:
    expected = os.getenv("ADMIN_TOKEN", "")
    if not x_admin_token:
        raise HTTPException(status_code=401, detail="Missing X-Admin-Token header")
    if not expected or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=403, detail="Invalid X-Admin-Token")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    loop = asyncio.get_running_loop()
    event_listener.start(loop)
    try:
        yield
    finally:
        event_listener.stop()


app = FastAPI(
    title="Docker Container Topology Visualizer",
    version="0.3.0",
    description="Reactive Docker topology, telemetry, logs, and guarded container management.",
    lifespan=lifespan,
)


STATIC_DIR = Path(__file__).resolve().parent / "static"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/v1/topology", response_model=TopologyResponse)
def get_topology() -> TopologyResponse:
    try:
        return extract_topology()
    except DockerException as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get(
    "/api/v1/containers/{container_id}/stats",
    response_model=ContainerStatsResponse,
)
def container_stats(container_id: str) -> ContainerStatsResponse:
    try:
        return get_container_stats(container_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=f"Container '{container_id}' not found") from exc
    except DockerException as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get(
    "/api/v1/containers/{container_id}/logs",
    response_model=ContainerLogsResponse,
)
def container_logs(
    container_id: str,
    tail: int = Query(default=50, ge=1, le=500),
) -> ContainerLogsResponse:
    try:
        return get_container_logs(container_id, tail=tail)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=f"Container '{container_id}' not found") from exc
    except DockerException as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _run_action(container_id: str, action: str, force: bool = False) -> ContainerActionResponse:
    try:
        return perform_container_action(container_id, action=action, force=force)
    except ProtectedContainerError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ContainerRunningConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=f"Container '{container_id}' not found") from exc
    except APIError as exc:
        if getattr(exc, "status_code", None) == 409:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except DockerException as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post(
    "/api/v1/containers/{container_id}/start",
    response_model=ContainerActionResponse,
    dependencies=[Depends(require_admin_token)],
)
def start_container(container_id: str) -> ContainerActionResponse:
    return _run_action(container_id, "start")


@app.post(
    "/api/v1/containers/{container_id}/stop",
    response_model=ContainerActionResponse,
    dependencies=[Depends(require_admin_token)],
)
def stop_container(container_id: str) -> ContainerActionResponse:
    return _run_action(container_id, "stop")


@app.post(
    "/api/v1/containers/{container_id}/restart",
    response_model=ContainerActionResponse,
    dependencies=[Depends(require_admin_token)],
)
def restart_container(container_id: str) -> ContainerActionResponse:
    return _run_action(container_id, "restart")


@app.delete(
    "/api/v1/containers/{container_id}",
    response_model=ContainerActionResponse,
    dependencies=[Depends(require_admin_token)],
)
def delete_container(
    container_id: str,
    force: bool = Query(default=False),
) -> ContainerActionResponse:
    return _run_action(container_id, "delete", force=force)


@app.websocket("/ws/events")
async def websocket_events(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")

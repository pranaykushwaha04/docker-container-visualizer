"""FastAPI entrypoint: read-only topology API."""
from docker.errors import DockerException
from fastapi import FastAPI, HTTPException

from app.models.schemas import TopologyResponse
from app.services.extractor import extract_topology

app = FastAPI(
    title="Docker Container Topology Visualizer",
    version="0.1.0",
    description="Read-only Docker topology extractor service (Deliverable-1).",
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/v1/topology", response_model=TopologyResponse)
def get_topology() -> TopologyResponse:
    try:
        return extract_topology()
    except DockerException as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

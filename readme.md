# Docker Container Topology Visualizer

Read-only service that inspects the local Docker Engine via `/var/run/docker.sock`
and exposes the container/network graph as validated JSON for UI visualization.

Adapted read-paths only from:
- [LeoVerto/docker-network-graph](https://github.com/LeoVerto/docker-network-graph) — SDK traversal + IPAM/subnet logic
- [oslabs-beta/DockerNet](https://github.com/oslabs-beta/DockerNet) — topology graph representation + REST separation

> Strictly read-only: only `networks.list()`, `containers.list(all=True)`, and `.attrs` reads.
> No create/start/stop/delete anywhere in `app/`.

## Quickstart

```bash
pip install -r requirements.txt
pytest -v
uvicorn app.main:app --reload
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/v1/topology
```

## Layout

- `output/Assessment-2.md` — architecture spec (Deliverable-1)
- `app/models/schemas.py` — Pydantic Node/Edge/TopologyResponse
- `app/services/extractor.py` — `DockerTopologyExtractor` + `extract_topology()`
- `app/main.py` — FastAPI `GET /health`, `GET /api/v1/topology`
- `tests/test_extractor.py` — mocked unit tests (no daemon needed)
- `extractor.py` — legacy flat script (kept for history; superseded by `app/`)

## Project Learnings (Day 2)

1. **LeoVerto traversal is the reliable core:** `IPAM.Config[0].Subnet` with `"N/A"` fallback,
   plus `NetworkSettings.Networks[].NetworkID/IPAddress` iteration, covers bridge/overlay/host
   without shelling out. Graphviz output was correctly discarded for a JSON API.
2. **DockerNet shaped the API, not the transport:** its `formatNetworksAndContainers`
   node-shaping maps cleanly to Pydantic models, but its `curl --unix-socket` + `docker network create/rm`
   write paths were stripped to meet the read-only assessment rule.
3. **Testability requires injection:** `DockerTopologyExtractor(client=...)` plus
   `extract_topology(client=...)` lets `pytest-mock` patch `docker.from_env` once and
   cover subnets, ports, multi-network IPs, and dangling-edge guards with zero daemon.
4. **Validation catches graph bugs early:** `TopologyResponse` referential-integrity check
   (every edge endpoint must exist) caught the empty-`NetworkID` case during testing.
5. **Liveness vs reachability:** `/health` stays `200` while `/api/v1/topology` returns
   `503` when the socket is unreachable — orchestrators can distinguish app health from daemon health.

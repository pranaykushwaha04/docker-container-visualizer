# Docker Container Topology Visualizer

Reactive, security-hardened service that inspects the Docker Engine, streams live
container/network delta events over WebSockets, exposes on-demand CPU/RAM telemetry
and container logs, and provides token-guarded container lifecycle controls on an
interactive Cytoscape.js canvas.

Adapted read-paths from:
- [LeoVerto/docker-network-graph](https://github.com/LeoVerto/docker-network-graph) — SDK traversal + IPAM/subnet logic
- [oslabs-beta/DockerNet](https://github.com/oslabs-beta/DockerNet) — topology graph representation + REST separation

## Quickstart (Containerized with Socket Proxy — Recommended)

```bash
ADMIN_TOKEN=my-secret-token docker compose up --build
```

Open `http://127.0.0.1:8000` in your browser. Enter your `ADMIN_TOKEN` in the navbar
to unlock `Start`, `Stop`, `Restart`, and `Delete` controls (defaults to
`dev-admin-token` in `docker-compose.yml` if `ADMIN_TOKEN` is unset).

### Security Architecture (`docker-socket-proxy` + Protected Containers)
Mounting `/var/run/docker.sock` directly into an application container grants root-equivalent
control over the host Docker Engine. To enforce **least privilege**:

1. **`socket-proxy` (`tecnativa/docker-socket-proxy`):** The only service that mounts
   `/var/run/docker.sock:ro`, isolated on an internal-only `proxy-net` network. It
   enables topology/event/lifecycle flags (`CONTAINERS=1`, `NETWORKS=1`, `EVENTS=1`,
   `INFO=1`, `PING=1`, `POST=1`, `DELETE=1`) while explicitly denying shell/host
   escalation (`EXEC=0`, `VOLUMES=0`, `IMAGES=0`, `SYSTEM=0`).
2. **`visualizer` (non-root `appuser`):** Built from a multi-stage `python:3.11-slim`
   `Dockerfile`, runs as unprivileged `appuser`, and connects via
   `DOCKER_HOST=tcp://socket-proxy:2375`.
3. **System Container Protection (`is_protected`):** The `visualizer` and `socket-proxy`
   containers are automatically flagged `is_protected=True`; mutating requests against
   them are rejected with `403 Forbidden` and disabled in the UI.
4. **Admin Token Guard (`X-Admin-Token`):** All `POST`/`DELETE` lifecycle endpoints
   require a matching `X-Admin-Token` header (`401`/`403` otherwise), while `GET`
   topology, stats, logs, and `WS /ws/events` remain open for viewing.

## Local Development & Tests

```bash
pip install -r requirements.txt
pytest -v
uvicorn app.main:app --reload
```

### API & WebSocket Endpoints
- `GET /` — Cytoscape.js swimlane topology dashboard
- `GET /health` — Liveness probe (`200 OK`)
- `GET /api/v1/topology` — Full validated `TopologyResponse` (`nodes`, `edges`)
- `GET /api/v1/containers/{container_id}/stats` — On-demand CPU % and RAM metrics
- `GET /api/v1/containers/{container_id}/logs?tail=50` — Last `tail` lines of container logs
- `POST /api/v1/containers/{container_id}/start` — Start container (`X-Admin-Token`)
- `POST /api/v1/containers/{container_id}/stop` — Stop non-protected container (`X-Admin-Token`)
- `POST /api/v1/containers/{container_id}/restart` — Restart non-protected container (`X-Admin-Token`)
- `DELETE /api/v1/containers/{container_id}?force=false` — Remove non-protected container (`409` if running without `force=true`, `X-Admin-Token`)
- `WS /ws/events` — Real-time delta stream (`NODE_ADDED`, `NODE_REMOVED`, `STATUS_UPDATED`, `NETWORK_UPDATED`)

## Layout

- `output/Assessment-2.md` — architecture spec (Deliverable-1)
- `app/models/schemas.py` — Pydantic `Node`, `Edge`, `TopologyResponse`, `ContainerStatsResponse`, `TopologyDeltaEvent`
- `app/services/extractor.py` — `DockerTopologyExtractor`, `extract_topology()`, `get_container_stats()`
- `app/services/events.py` — `ConnectionManager`, `parse_docker_event()`, `DockerEventListener`
- `app/main.py` — FastAPI REST + WebSocket entrypoint with lifespan listener
- `app/static/` — Cytoscape.js UI with live WebSocket delta patching & telemetry inspector
- `Dockerfile` & `docker-compose.yml` — Non-root container + read-only `docker-socket-proxy`
- `tests/` — Mocked unit tests (no daemon needed)

## Project Learnings

### Day 2
1. **LeoVerto traversal is the reliable core:** `IPAM.Config[0].Subnet` with `"N/A"` fallback,
   plus `NetworkSettings.Networks[].NetworkID/IPAddress` iteration, covers bridge/overlay/host
   without shelling out.
2. **DockerNet shaped the API, not the transport:** its `formatNetworksAndContainers`
   node-shaping maps cleanly to Pydantic models, while write paths were stripped.
3. **Testability requires injection:** `DockerTopologyExtractor(client=...)` lets `pytest-mock`
   cover subnets, ports, multi-network IPs, and dangling-edge guards with zero daemon.
4. **Validation catches graph bugs early:** `TopologyResponse` referential-integrity check
   caught the empty-`NetworkID` case during testing.
5. **Liveness vs reachability:** `/health` stays `200` while `/api/v1/topology` returns
   `503` when the socket is unreachable.

### Phase 4 (Reactive Events, Telemetry & Hardening)
6. **Delta patching preserves UX:** Applying `NODE_ADDED`, `NODE_REMOVED`, `STATUS_UPDATED`,
   and `NETWORK_UPDATED` in-place via Cytoscape keeps the user's zoom and pan intact.
7. **Graceful telemetry degradation:** Querying `container.stats(stream=False)` only for
   `running` containers and returning `0` metrics for `exited`/`paused` states avoids UI errors.
8. **Defense in depth:** Pairing `tecnativa/docker-socket-proxy` (`POST=0`, `DELETE=0`, `EXEC=0`)
   on an `internal: true` bridge with a non-root `appuser` container eliminates direct socket exposure.

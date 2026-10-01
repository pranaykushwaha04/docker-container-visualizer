# Assessment-2: Docker Container Topology Visualizer

## 1. Project Title & Description

**Title:** Docker Container Topology Visualizer

**Description:**
A read-only DevOps observability service that inspects the local Docker Engine via
`/var/run/docker.sock`, builds an in-memory graph of networks, containers, subnets,
and port mappings, and exposes it over a validated REST API for rendering on an
interactive UI canvas. It helps engineers debug container networking, subnet overlap,
and host-port conflicts without ever mutating Docker state.

> Strictly read-only. The service never creates networks, starts/stops containers,
> or deletes volumes. It only calls `networks.list()`, `containers.list(all=True)`,
> and reads resource `.attrs`. All write paths from the reference repos are
> intentionally discarded (see §9).

**Reference reuse (adapted, read-paths only):**
- `LeoVerto/docker-network-graph` — Docker SDK traversal logic: `client.networks.list()`
  with `IPAM.Config[0].Subnet` extraction, `container.attrs["NetworkSettings"]["Networks"]`
  iteration for `EndpointID` / `IPAddress` / `Aliases`, and link construction
  (`container.id` → network name). Graphviz rendering and CLI output paths are discarded.
- `oslabs-beta/DockerNet` — Topology graph representation idea: networks as nodes with
  embedded container lists (`formatNetworksAndContainers`), and the `/api/networks` +
  `/api/containers` REST separation. Re-modeled here as a single validated
  `TopologyResponse { nodes, edges }` document. DockerNet's `createNetwork` /
  `deleteNetwork` (`docker network create/rm`) write paths are stripped out.

## 2. Components Breakdown

| Component | Responsibility | Technology |
|---|---|---|
| **UI** | Interactive canvas rendering nodes (containers, networks) and edges (attachments, port forwards). Out of scope for Deliverable-1; consumes `GET /api/v1/topology`. | Any canvas lib (future: React + D3 / vis-network, per DockerNet layout logic) |
| **Persistence** | In-memory graph snapshot cache. No database. Each API call builds a fresh snapshot from the daemon; an optional short-TTL in-process cache may be added later. No data is written to disk. | Python in-memory dict / `functools.lru_cache` (future) |
| **Logic** | Docker SDK parser engine. Connects via `docker.from_env()`, extracts active networks, containers, IPAM subnets, and port mappings, returns validated graph schema. Pure read-only. | Python 3.11+, `docker-py`, Pydantic v2 |

## 3. Architecture Diagram (ASCII)

```text
                    +------------------+
                    |  Docker Daemon   |
                    | /var/run/docker  |
                    |     .sock        |
                    +--------+---------+
                             |  SDK read-only:
                             |  networks.list()
                             |  containers.list(all=True)
                             v
                    +--------+---------+
                    | Logic Service    |
                    | Layer            |
                    | (extractor.py +  |
                    |  schemas.py)     |
                    +--------+---------+
                             |  validated TopologyResponse
                             v
                    +--------+---------+
                    | REST API         |
                    | (FastAPI)        |
                    | GET /health      |
                    | GET /api/v1/     |
                    |   topology       |
                    +--------+---------+
                             |  JSON over HTTP
                             |  (WebSocket in future)
                             v
                    +--------+---------+
                    | UI Canvas        |
                    | (interactive     |
                    |  topology graph) |
                    +------------------+
```

Request flow:
1. UI (or `curl`) calls `GET /api/v1/topology`.
2. Logic layer opens a short-lived SDK client (`docker.from_env()`), lists networks
   and containers, parses IPAM + `NetworkSettings`.
3. Pydantic validates every node/edge before serialization.
4. FastAPI returns `TopologyResponse` JSON. No state is mutated at any step.

## 4. Deliverable-1 Scope

**In scope — read-only Docker topology extractor service wrapped in a REST API
with data validation and isolated unit tests:**

- `app/models/schemas.py` — Pydantic models for Node, Edge, TopologyResponse.
- `app/services/extractor.py` — `DockerTopologyExtractor` class + `extract_topology()`
  convenience function. Read-only SDK parsing only.
- `app/main.py` — FastAPI app exposing `GET /health` and `GET /api/v1/topology`.
- `requirements.txt` — Minimal deps: `docker`, `fastapi`, `uvicorn`, `pydantic`,
  `pytest`, `pytest-mock`.
- `tests/test_extractor.py` — Fully mocked unit tests (`docker.from_env`,
  `networks.list`, `containers.list`) — no real daemon required.

**Explicitly out of scope (future deliverables):**
UI canvas, WebSocket live updates, snapshot cache TTL, authentication, multi-host
support, volume inspection, Graphviz/PNG export, any Docker write operations.

## 5. Target Users

- **DevOps engineers** debugging container networking: which container sits on which
  network, what IP it holds, whether subnets overlap, why DNS/ICC fails.
- **Developers** debugging port conflicts: which host port maps to which container
  port, spotting `HOST->container` collisions before `docker compose up` fails.

Both get a single JSON graph they can visualize instead of correlating
`docker network inspect` + `docker ps --format` output by hand.

## 6. Component Interface — Exact JSON Schema

`GET /api/v1/topology` → `200` with `TopologyResponse`:

```json
{
  "nodes": [
    {
      "id": "net_a1b2c3d4e5f6",
      "name": "frontend",
      "type": "network",
      "driver": "bridge",
      "subnet": "172.18.0.0/16"
    },
    {
      "id": "cnt_f6e5d4c3b2a1",
      "name": "web-1",
      "type": "container",
      "image": "nginx:latest",
      "status": "running",
      "ports": ["8080->80/tcp", "443/tcp"]
    }
  ],
  "edges": [
    {
      "source": "cnt_f6e5d4c3b2a1",
      "target": "net_a1b2c3d4e5f6",
      "edge_type": "network_attachment",
      "ip_address": "172.18.0.5",
      "host_port": null,
      "container_port": null
    }
  ]
}
```

### 6.1 Nodes

| Field | Type | Required | Rules |
|---|---|---|---|
| `id` | `string` | yes | `net_<12-hex>` for networks, `cnt_<12-hex>` for containers. Unique across response. |
| `name` | `string` | yes | Docker network / container name. Min length 1. |
| `type` | `enum` | yes | `"network"` or `"container"`. Discriminates optional fields. |
| `driver` | `string` | network-only | e.g. `bridge`, `overlay`, `host`, `none`. Defaults to `"bridge"`. |
| `subnet` | `string` | network-only | First entry of `IPAM.Config[].Subnet`, or `"N/A"` when absent (matches LeoVerto fallback). |
| `image` | `string` | container-only | First tag or `"untagged"`. |
| `status` | `string` | container-only | e.g. `running`, `exited`, `paused`. |
| `ports` | `string[]` | container-only | `"<host>-><internal>"` when bound (e.g. `"8080->80/tcp"`), else bare internal (e.g. `"443/tcp"`). Empty list when no ports published. |

Type-specific required fields are enforced by Pydantic discriminated validation:
a `type: network` node must carry `driver` + `subnet` and must not carry
`image`/`status`/`ports`, and vice versa.

### 6.2 Edges

Two edge kinds (both read-only observations):

| Field | Type | Required | Meaning |
|---|---|---|---|
| `source` | `string` | yes | Origin node `id` — always a container `cnt_*` in Deliverable-1. |
| `target` | `string` | yes | Destination node `id` — a network `net_*` for attachments. |
| `edge_type` | `enum` | yes | `"network_attachment"` (container↔network, with `ip_address`) or `"port_forwarding"` (container→host port binding; `target` is the container node convention documented by the extractor). Defaults to `"network_attachment"`. |
| `ip_address` | `string \| null` | attachment-only | `NetworkSettings.Networks[].IPAddress`, `""` when unset. |
| `host_port` | `string \| null` | forwarding-only | Host side of a `HostPort` binding. |
| `container_port` | `string \| null` | forwarding-only | Internal port key, e.g. `"80/tcp"`. |

Port mappings appear **both** as `ContainerNode.ports[]` strings (human-readable)
**and** as `port_forwarding` edges (graph-renderable) so the UI can draw either
a label or a link without re-parsing.

### 6.3 Top-level

| Field | Type | Rules |
|---|---|---|
| `nodes` | `Node[]` | May be empty (daemon with no networks/containers). IDs unique. |
| `edges` | `Edge[]` | Every `source`/`target` must reference an existing node `id` (validated). |

Error shape (non-200): `{"detail": "<message>"}` — e.g. `503` when the daemon
socket is unreachable.

## 7. Tech Stack

- **Python 3.11+** — runtime (developed/tested on 3.11; compatible with 3.12/3.13).
- **Docker SDK (`docker-py`, `docker>=7.0.0`)** — `docker.from_env()` + list/attrs reads.
- **FastAPI** — REST layer (`GET /health`, `GET /api/v1/topology`).
- **Pydantic v2** — schema validation for every node/edge.
- **Pytest + pytest-mock** — isolated mocked unit tests, no daemon needed.
- **Uvicorn** — ASGI server (`uvicorn app.main:app --reload`).

`requirements.txt` (minimal, pinned floor only):

```text
docker>=7.0.0
fastapi>=0.110.0
uvicorn>=0.29.0
pydantic>=2.6.0
pytest>=8.0.0
pytest-mock>=3.12.0
httpx>=0.27.0
```

(`httpx` is included for FastAPI `TestClient` verification.)

## 8. Read-Only Guarantee & Data Validation

- Allowed SDK calls: `docker.from_env()`, `client.ping()`, `client.networks.list()`,
  `network.reload()`, `client.containers.list(all=True)`, attribute reads
  (`.attrs`, `.name`, `.status`, `.image.tags`). No `create / run / start / stop /
  remove / prune / connect / disconnect` anywhere in `app/`.
- Validation: constructors coerce raw SDK dicts into Pydantic models immediately;
  `TopologyResponse` additionally checks edge endpoint referential integrity.
- Failures: daemon unreachable → `DockerException` propagates to FastAPI as
  `503 {"detail": "Docker daemon unreachable: ..."}`; `/health` stays `200` so
  orchestrators can distinguish app-liveness from daemon-reachability.

## 9. Write-Operations Stripped (Reference Hygiene)

| Reference feature | Disposition |
|---|---|
| LeoVerto `get_networks` / `get_containers` traversal + IPAM/Alias parsing | **Kept + adapted** into `extractor.py` (no Graphviz). |
| LeoVerto Graphviz `draw_*` / `generate_graph` file output | **Discarded** (Deliverable-1 is JSON API, not image export). |
| DockerNet `GET /api/networks`, `GET /api/containers` read endpoints | **Kept as pattern**, merged into single `GET /api/v1/topology`. |
| DockerNet `formatNetworksAndContainers` node shaping | **Kept as pattern**, re-expressed as Pydantic schema. |
| DockerNet `createNetwork` (`docker network create`) / `deleteNetwork` (`docker network rm`) | **Stripped** — destructive, violates read-only scope. |
| Any container lifecycle / volume mutation | **Stripped** — none ported. |

## 10. Verification (Deliverable-1 acceptance)

```bash
pip install -r requirements.txt
pytest -v            # all mocked, no daemon required
uvicorn app.main:app --reload
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/v1/topology
```

- `pytest` green with daemon stopped (proves isolation).
- `/health` → `{"status": "ok"}`.
- `/api/v1/topology` → schema-conformant JSON (validate with `TopologyResponse`).

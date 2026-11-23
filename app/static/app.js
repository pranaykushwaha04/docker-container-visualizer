/* Phase 3: single-page Cytoscape canvas over GET /api/v1/topology.
 * Vanilla JS, no bundler. Networks become compound parents; containers
 * are color-coded by status; edges are labeled with assigned IPs.
 */
(function () {
  "use strict";

  var STATUS_COLORS = {
    running: "#22c55e",
    exited: "#ef4444",
    paused: "#eab308",
  };

  var cy = null;
  var lastTopology = null;
  var selectedContainerId = null;
  var ws = null;
  var wsReconnectTimer = null;
  var wsDisconnected = false;

  function statusColor(status) {
    return STATUS_COLORS[String(status || "").toLowerCase()] || "#64748b";
  }

  function el(id) {
    return document.getElementById(id);
  }

  function setPill(state, text) {
    var pill = el("statusPill");
    pill.className = "pill " + state;
    pill.textContent = text;
  }

  function setWsPill(state, text) {
    var pill = el("wsPill");
    if (!pill) return;
    pill.className = "pill " + state;
    pill.textContent = text;
  }

  // Convert TopologyResponse into Cytoscape elements.
  // A container attached to several networks gets its FIRST network as the
  // compound parent (Cytoscape allows only one); remaining memberships stay
  // visible as labeled edges.
  function toElements(topology) {
    var elements = [];
    var nodesById = {};
    var firstParent = {}; // container id -> network id
    var edgesByPair = {}; // "source|target" -> edge (dedupe)

    (topology.nodes || []).forEach(function (n) {
      nodesById[n.id] = n;
    });

    (topology.edges || []).forEach(function (e) {
      if (e.edge_type !== "network_attachment") return;
      if (!nodesById[e.source] || !nodesById[e.target]) return;
      var key = e.source + "|" + e.target;
      if (!edgesByPair[key]) edgesByPair[key] = e;
      var containerId = e.source;
      var networkId = e.target;
      if (nodesById[containerId] && nodesById[containerId].type === "container") {
        if (!firstParent[containerId]) firstParent[containerId] = networkId;
      }
    });

    (topology.nodes || []).forEach(function (n) {
      if (n.type === "network") {
        elements.push({
          data: {
            id: n.id,
            type: "network",
            label: n.name + "\n" + (n.subnet || "N/A"),
            name: n.name,
            subnet: n.subnet,
            driver: n.driver,
          },
        });
      } else {
        elements.push({
          data: {
            id: n.id,
            parent: firstParent[n.id] || undefined,
            type: "container",
            label: n.name,
            name: n.name,
            image: n.image,
            status: n.status,
            ports: n.ports || [],
            is_protected: Boolean(n.is_protected),
            color: statusColor(n.status),
          },
        });
      }
    });

    Object.keys(edgesByPair).forEach(function (key, i) {
      var e = edgesByPair[key];
      elements.push({
        data: {
          id: "e" + i + "_" + e.source + "_" + e.target,
          source: e.source,
          target: e.target,
          ip: e.ip_address || "",
          label: e.ip_address || "",
        },
      });
    });

    return elements;
  }

  function makeStyle() {
    return [
      {
        selector: 'node[type="network"]',
        style: {
          shape: "rectangle",
          "background-color": "#38bdf8",
          "background-opacity": 0.08,
          "border-width": 2,
          "border-style": "dashed",
          "border-color": "#38bdf8",
          label: "data(label)",
          "text-wrap": "wrap",
          "font-size": 13,
          "font-weight": "bold",
          color: "#7dd3fc",
          "text-valign": "top",
          "text-halign": "center",
          padding: 28,
        },
      },
      {
        selector: 'node[type="container"]',
        style: {
          shape: "round-rectangle",
          "background-color": "data(color)",
          label: "data(label)",
          "font-size": 12,
          "font-weight": "bold",
          color: "#0f172a",
          "text-valign": "center",
          "text-halign": "center",
          width: "label",
          height: 36,
          padding: 12,
          "border-width": 2,
          "border-color": "#0f172a",
        },
      },
      {
        selector: 'node[type="container"]:selected',
        style: {
          "border-width": 4,
          "border-color": "#f8fafc",
        },
      },
      {
        selector: 'node.pending',
        style: {
          opacity: 0.6,
          "border-style": "dashed",
          "border-width": 3,
          "border-color": "#38bdf8",
        },
      },
      {
        selector: "edge",
        style: {
          width: 2,
          "line-color": "#94a3b8",
          "target-arrow-shape": "triangle",
          "target-arrow-color": "#94a3b8",
          "curve-style": "bezier",
          label: "data(label)",
          "font-size": 11,
          color: "#cbd5e1",
          "text-background-color": "#0f172a",
          "text-background-opacity": 0.85,
          "text-background-padding": 3,
        },
      },
    ];
  }

  // Deterministic "swimlane" layout: one horizontal lane of containers
  // per network, lanes stacked vertically. Parents auto-enclose their
  // children, so only children (plus childless networks) need positions.
  // This replaces force/grid layouts, which cannot account for compound
  // box sizes and either exploded (cose) or overlapped (breadthfirst).
  function runLayout() {
    if (!cy) return;
    var PAD = 30;
    var LANE_GAP = 80;
    var STEP_X = 70; // gap between neighbour nodes in a lane
    var LANE_HEAD = 64; // room for the network label above its box
    var LANE_TAIL = 34; // room below the box
    var CHILD_H = 64;
    var MIN_W = 80;

    function nameOf(n) {
      return n.data("name") || n.id();
    }
    function byName(a, b) {
      return String(nameOf(a)).localeCompare(String(nameOf(b)));
    }

    var positions = {};
    var y = PAD + LANE_HEAD;
    var xLimit = Math.max(cy.width() - PAD, 600);

    function lane(children) {
      var x = PAD + MIN_W;
      children.forEach(function (n) {
        var w = Math.max(n.boundingBox().w || 0, MIN_W);
        if (x + w > xLimit && x > PAD + MIN_W) {
          x = PAD + MIN_W; // wrap long lanes instead of overflowing
          y += CHILD_H + STEP_X;
        }
        positions[n.id()] = { x: x + w / 2 - MIN_W / 2, y: y };
        x += w + STEP_X;
      });
      y += CHILD_H + LANE_HEAD + LANE_TAIL + LANE_GAP;
    }

    var orphans = cy
      .nodes('node[type="container"]')
      .filter(function (n) {
        return n.isOrphan();
      })
      .toArray()
      .sort(byName);
    if (orphans.length) lane(orphans);

    cy.nodes('node[type="network"]')
      .toArray()
      .sort(byName)
      .forEach(function (net) {
        var kids = net.children().toArray().sort(byName);
        if (kids.length) {
          lane(kids);
        } else {
          // Childless network: give the parent itself a slot.
          positions[net.id()] = { x: PAD + MIN_W, y: y };
          y += LANE_HEAD + LANE_TAIL + LANE_GAP;
        }
      });

    var layout = cy.layout({
      name: "preset",
      positions: positions,
      fit: true,
      padding: 30,
      animate: true,
      animationDuration: 600,
    });
    layout.one("layoutstop", function () {
      cy.fit(undefined, 30);
    });
    layout.run();
  }

  function attachmentsOf(containerId) {
    var out = [];
    (lastTopology.edges || []).forEach(function (e) {
      if (e.source === containerId && e.edge_type === "network_attachment") {
        var net = null;
        (lastTopology.nodes || []).forEach(function (n) {
          if (n.id === e.target) net = n;
        });
        out.push({
          network: net ? net.name : e.target,
          subnet: net ? net.subnet : "",
          ip: e.ip_address || "(no IP)",
        });
      }
    });
    return out;
  }

  function loadTelemetry(containerId, status) {
    var cpuEl = el("inspCpu");
    var memEl = el("inspMem");
    if (!cpuEl || !memEl) return;

    if (String(status || "").toLowerCase() !== "running") {
      cpuEl.textContent = "0.0% (" + (status || "stopped") + ")";
      memEl.textContent = "0 MB / 0 MB (0.0%)";
      return;
    }

    cpuEl.textContent = "loading…";
    memEl.textContent = "loading…";
    fetch("/api/v1/containers/" + encodeURIComponent(containerId) + "/stats")
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (stats) {
        if (selectedContainerId !== containerId) return;
        cpuEl.textContent = Number(stats.cpu_percent || 0).toFixed(2) + "%";
        memEl.textContent =
          Number(stats.memory_usage_mb || 0).toFixed(1) +
          " MB / " +
          Number(stats.memory_limit_mb || 0).toFixed(1) +
          " MB (" +
          Number(stats.memory_percent || 0).toFixed(1) +
          "%)";
      })
      .catch(function () {
        if (selectedContainerId !== containerId) return;
        cpuEl.textContent = "unavailable";
        memEl.textContent = "unavailable";
      });
  }

  var pendingContainers = {};

  function getAdminToken() {
    var input = el("adminTokenInput");
    return input ? input.value.trim() : "";
  }

  function setActionStatus(msg, isError) {
    var box = el("actionStatus");
    if (!box) return;
    if (!msg) {
      box.classList.add("hidden");
      box.textContent = "";
      box.className = "action-status hidden";
      return;
    }
    box.textContent = msg;
    box.className = isError ? "action-status err" : "action-status";
  }

  function updateControlButtons(nodeData) {
    var isProtected = Boolean(nodeData.is_protected);
    var isPending = Boolean(pendingContainers[nodeData.id]);
    var isRunning = String(nodeData.status || "").toLowerCase() === "running";

    el("inspProtectedBadge").classList.toggle("hidden", !isProtected);
    el("actStartBtn").disabled = isPending || isRunning;
    el("actStopBtn").disabled = isPending || isProtected || !isRunning;
    el("actRestartBtn").disabled = isPending || isProtected || !isRunning;
    el("actDeleteBtn").disabled = isPending || isProtected;
  }

  function loadLogs(containerId) {
    var box = el("inspLogs");
    if (!box) return;
    box.textContent = "Loading logs…";
    fetch("/api/v1/containers/" + encodeURIComponent(containerId) + "/logs?tail=50")
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (selectedContainerId !== containerId) return;
        box.textContent = data.logs || "(no log output)";
      })
      .catch(function (err) {
        if (selectedContainerId !== containerId) return;
        box.textContent = "Failed to load logs: " + err.message;
      });
  }

  function switchTab(tab) {
    var isLogs = tab === "logs";
    el("tabOverview").classList.toggle("hidden", isLogs);
    el("tabLogs").classList.toggle("hidden", !isLogs);
    el("tabOverviewBtn").classList.toggle("active", !isLogs);
    el("tabLogsBtn").classList.toggle("active", isLogs);
    if (isLogs && selectedContainerId) {
      loadLogs(selectedContainerId);
    }
  }

  function runContainerAction(action) {
    if (!selectedContainerId) return;
    var containerId = selectedContainerId;
    var node = cy ? cy.getElementById(containerId) : null;
    var isRunning = node && String(node.data("status") || "").toLowerCase() === "running";

    if (action === "delete") {
      var msg = isRunning
        ? "Container is running. Force remove this container?"
        : "Remove this container permanently?";
      if (!window.confirm(msg)) return;
    }

    var token = getAdminToken();
    if (!token) {
      setActionStatus("Enter Admin token in the navbar first.", true);
      return;
    }

    pendingContainers[containerId] = true;
    if (node && node.length) {
      node.addClass("pending");
      updateControlButtons(node.data());
    }
    setActionStatus("Sending " + action + "… waiting for Docker event.", false);

    var method = action === "delete" ? "DELETE" : "POST";
    var path =
      action === "delete"
        ? "/api/v1/containers/" + encodeURIComponent(containerId) + (isRunning ? "?force=true" : "")
        : "/api/v1/containers/" + encodeURIComponent(containerId) + "/" + action;

    fetch(path, {
      method: method,
      headers: { "X-Admin-Token": token },
    })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.json().then(function (d) {
            throw new Error((d && d.detail) || "HTTP " + resp.status);
          });
        }
        return resp.json();
      })
      .then(function () {
        // Do NOT fake color change here; wait for /ws/events to apply the real Docker state.
        setActionStatus("Action accepted (" + action + "); awaiting live event…", false);
      })
      .catch(function (err) {
        delete pendingContainers[containerId];
        if (node && node.length) {
          node.removeClass("pending");
          if (selectedContainerId === containerId) updateControlButtons(node.data());
        }
        setActionStatus("Error: " + err.message, true);
      });
  }

  function showInspector(nodeData) {
    selectedContainerId = nodeData.id;
    el("inspectorHint").classList.add("hidden");
    var card = el("inspectorCard");
    card.classList.remove("hidden");
    el("inspName").textContent = nodeData.name || nodeData.id;
    el("inspId").textContent = nodeData.id;
    el("inspImage").textContent = nodeData.image || "—";
    el("inspStatus").textContent = nodeData.status || "—";
    var ports = nodeData.ports || [];
    el("inspPorts").textContent = ports.length ? ports.join(", ") : "none published";
    loadTelemetry(nodeData.id, nodeData.status);
    updateControlButtons(nodeData);
    if (!pendingContainers[nodeData.id]) setActionStatus("", false);
    if (!el("tabLogs").classList.contains("hidden")) loadLogs(nodeData.id);
    var nets = attachmentsOf(nodeData.id);
    var ul = el("inspNets");
    ul.innerHTML = "";
    if (!nets.length) {
      var li = document.createElement("li");
      li.textContent = "Not attached to any network.";
      ul.appendChild(li);
    } else {
      nets.forEach(function (a) {
        var li = document.createElement("li");
        li.textContent = a.network + " — " + a.ip + (a.subnet ? " (" + a.subnet + ")" : "");
        ul.appendChild(li);
      });
    }
  }

  function hideInspector() {
    selectedContainerId = null;
    el("inspectorCard").classList.add("hidden");
    el("inspectorHint").classList.remove("hidden");
  }

  function ensureCy() {
    if (cy) return cy;
    cy = cytoscape({
      container: el("cy"),
      style: makeStyle(),
      elements: [],
      minZoom: 0.2,
      maxZoom: 3,
    });
    cy.on("tap", 'node[type="container"]', function (evt) {
      showInspector(evt.target.data());
    });
    cy.on("tap", function (evt) {
      if (evt.target === cy) hideInspector();
    });
    return cy;
  }

  function render(topology) {
    lastTopology = topology;
    ensureCy();
    var elements = toElements(topology);
    cy.elements().remove();
    cy.add(elements);
    var hasNodes = (topology.nodes || []).length > 0;
    el("emptyState").classList.toggle("hidden", hasNodes);
    runLayout();
  }

  function load() {
    var btn = el("refreshBtn");
    var errBox = el("errorState");
    btn.disabled = true;
    setPill("loading", "loading");
    errBox.classList.add("hidden");
    fetch("/api/v1/topology")
      .then(function (resp) {
        if (!resp.ok) {
          throw new Error("API returned " + resp.status);
        }
        return resp.json();
      })
      .then(function (topology) {
        render(topology);
        setPill("ok", "live");
        el("updatedAt").textContent = "updated " + new Date().toLocaleTimeString();
      })
      .catch(function (err) {
        setPill("error", "error");
        errBox.textContent = "Could not load topology (" + err.message + "). Is the Docker daemon running?";
        errBox.classList.remove("hidden");
      })
      .finally(function () {
        btn.disabled = false;
      });
  }

  function applyDelta(delta) {
    if (!cy || !delta || !delta.event) return;
    var p = delta.payload || {};

    if (delta.event === "STATUS_UPDATED" && p.id) {
      delete pendingContainers[p.id];
      var node = cy.getElementById(p.id);
      if (node && node.length) {
        node.removeClass("pending");
        node.data("status", p.status);
        node.data("color", statusColor(p.status));
        if (selectedContainerId === p.id) {
          showInspector(node.data());
        }
      }
    } else if (delta.event === "NODE_ADDED" && p.id) {
      delete pendingContainers[p.id];
      var existing = cy.getElementById(p.id);
      if (existing && existing.length) {
        existing.removeClass("pending");
        existing.data("status", p.status || "running");
        existing.data("color", statusColor(p.status || "running"));
        if (p.name) existing.data({ name: p.name, label: p.name });
        if (p.image) existing.data("image", p.image);
        if (p.ports) existing.data("ports", p.ports);
        if (typeof p.is_protected === "boolean") existing.data("is_protected", p.is_protected);
        if (selectedContainerId === p.id) showInspector(existing.data());
      } else {
        var attachments = p.attachments || [];
        var firstNet = attachments.length ? attachments[0].network_id : undefined;
        if (firstNet && !cy.getElementById(firstNet).length) {
          firstNet = undefined;
        }
        var pos;
        if (firstNet) {
          var parentBox = cy.getElementById(firstNet).boundingBox();
          pos = { x: parentBox.x1 + 80, y: parentBox.y1 + 80 };
        } else {
          var orphanCount = cy
            .nodes('node[type="container"]')
            .filter(function (n) {
              return n.isOrphan();
            }).length;
          pos = { x: 110 + orphanCount * 160, y: 94 };
        }
        cy.add({
          group: "nodes",
          data: {
            id: p.id,
            parent: firstNet,
            type: "container",
            label: p.name || p.id,
            name: p.name || p.id,
            image: p.image || "untagged",
            status: p.status || "running",
            ports: p.ports || [],
            is_protected: Boolean(p.is_protected),
            color: statusColor(p.status || "running"),
          },
          position: pos,
        });
        attachments.forEach(function (att, idx) {
          if (!cy.getElementById(att.network_id).length) return;
          cy.add({
            group: "edges",
            data: {
              id: "e_ws_" + idx + "_" + p.id + "_" + att.network_id,
              source: p.id,
              target: att.network_id,
              ip: att.ip_address || "",
              label: att.ip_address || "",
            },
          });
        });
      }
      el("emptyState").classList.add("hidden");
    } else if (delta.event === "NODE_REMOVED" && p.id) {
      delete pendingContainers[p.id];
      var target = cy.getElementById(p.id);
      if (target && target.length) {
        cy.remove(target);
      }
      if (selectedContainerId === p.id) hideInspector();
      el("emptyState").classList.toggle("hidden", cy.nodes().length > 0);
    } else if (delta.event === "NETWORK_UPDATED" && p.container_id && p.network_id) {
      var edgeSelector =
        'edge[source="' + p.container_id + '"][target="' + p.network_id + '"]';
      if (p.action === "connect") {
        if (
          cy.getElementById(p.container_id).length &&
          cy.getElementById(p.network_id).length &&
          !cy.edges(edgeSelector).length
        ) {
          cy.add({
            group: "edges",
            data: {
              id: "e_ws_" + Date.now() + "_" + p.container_id + "_" + p.network_id,
              source: p.container_id,
              target: p.network_id,
              ip: p.ip_address || "",
              label: p.ip_address || "",
            },
          });
        }
      } else if (p.action === "disconnect") {
        cy.remove(cy.edges(edgeSelector));
        var cntNode = cy.getElementById(p.container_id);
        if (cntNode && cntNode.length && cntNode.parent().id() === p.network_id) {
          var remaining = cntNode.connectedEdges('edge[source="' + p.container_id + '"]');
          var nextParent = remaining.length ? remaining[0].target().id() : null;
          cntNode.move({ parent: nextParent });
        }
      }
    }

    el("updatedAt").textContent = "event " + new Date().toLocaleTimeString();
  }

  function connectWebSocket() {
    if (wsReconnectTimer) {
      clearTimeout(wsReconnectTimer);
      wsReconnectTimer = null;
    }
    var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    var url = proto + "//" + window.location.host + "/ws/events";
    setWsPill("loading", "ws: connecting");
    try {
      ws = new WebSocket(url);
    } catch (_err) {
      wsDisconnected = true;
      setWsPill("error", "ws: offline");
      wsReconnectTimer = setTimeout(connectWebSocket, 3000);
      return;
    }

    ws.onopen = function () {
      setWsPill("ok", "ws: live");
      if (wsDisconnected) {
        wsDisconnected = false;
        load();
      }
    };

    ws.onmessage = function (evt) {
      try {
        var delta = JSON.parse(evt.data);
        applyDelta(delta);
      } catch (_err) {
        // Ignore malformed frames
      }
    };

    ws.onclose = function () {
      wsDisconnected = true;
      setWsPill("error", "ws: reconnecting");
      wsReconnectTimer = setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = function () {
      if (ws) ws.close();
    };
  }

  document.addEventListener("DOMContentLoaded", function () {
    var tokenInput = el("adminTokenInput");
    if (tokenInput) {
      tokenInput.value = sessionStorage.getItem("visualizer_admin_token") || "";
      tokenInput.addEventListener("input", function () {
        sessionStorage.setItem("visualizer_admin_token", tokenInput.value.trim());
      });
    }
    el("tabOverviewBtn").addEventListener("click", function () { switchTab("overview"); });
    el("tabLogsBtn").addEventListener("click", function () { switchTab("logs"); });
    el("refreshLogsBtn").addEventListener("click", function () {
      if (selectedContainerId) loadLogs(selectedContainerId);
    });
    el("actStartBtn").addEventListener("click", function () { runContainerAction("start"); });
    el("actStopBtn").addEventListener("click", function () { runContainerAction("stop"); });
    el("actRestartBtn").addEventListener("click", function () { runContainerAction("restart"); });
    el("actDeleteBtn").addEventListener("click", function () { runContainerAction("delete"); });

    el("refreshBtn").addEventListener("click", load);
    load();
    connectWebSocket();
  });
})();

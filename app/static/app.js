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

  function showInspector(nodeData) {
    el("inspectorHint").classList.add("hidden");
    var card = el("inspectorCard");
    card.classList.remove("hidden");
    el("inspName").textContent = nodeData.name || nodeData.id;
    el("inspId").textContent = nodeData.id;
    el("inspImage").textContent = nodeData.image || "—";
    el("inspStatus").textContent = nodeData.status || "—";
    var ports = nodeData.ports || [];
    el("inspPorts").textContent = ports.length ? ports.join(", ") : "none published";
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

  document.addEventListener("DOMContentLoaded", function () {
    el("refreshBtn").addEventListener("click", load);
    load();
  });
})();

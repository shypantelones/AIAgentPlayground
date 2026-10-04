"use strict";
/* Topology diagrams: draws a lab (or a topology / lab file not built yet) as an SVG: nodes laid out in columns by hop
   distance from the most connected node, links labeled at each end with that node's interface and, when a snapshot
   is available, its addresses. Shared by the lab view and the new-lab previews. No external libraries: the panel
   only loads its own files. */

const TDG = { colW: 210, rowH: 96, boxW: 118, boxH: 46, pad: 30 };
const TDG_ROLE_MARK = { router: "R", switch: "SW", host: "H", firewall: "FW", loadbalancer: "LB", server: "SRV", upstream: "NET" };

/* `d`: {nodes: [{name, role}], links: [{a, b, a_if?, b_if?}], addresses?: {node: {iface: [cidr]}}} */
function renderTopologyDiagram(d) {
  const ns = "http://www.w3.org/2000/svg";
  const el = (tag, attrs = {}, text) => {
    const e = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (text != null) e.textContent = text;
    return e;
  };
  const names = d.nodes.map(n => n.name);
  const adj = Object.fromEntries(names.map(n => [n, []]));
  for (const l of d.links) { if (adj[l.a] && adj[l.b]) { adj[l.a].push(l.b); adj[l.b].push(l.a); } }

  // columns = BFS distance from the most connected node; disconnected pieces continue to the right
  const col = {}, order = [];
  const roots = [...names].sort((x, y) => adj[y].length - adj[x].length);
  let base = 0;
  for (const root of roots) {
    if (root in col) continue;
    col[root] = base; const queue = [root]; let maxc = base;
    while (queue.length) {
      const cur = queue.shift(); order.push(cur); maxc = Math.max(maxc, col[cur]);
      for (const nb of adj[cur]) if (!(nb in col)) { col[nb] = col[cur] + 1; queue.push(nb); }
    }
    base = maxc + 1;
  }
  const rows = {}, pos = {};
  for (const n of order) { const c = col[n]; rows[c] = (rows[c] || 0) + 1; pos[n] = { c, r: rows[c] - 1 }; }
  const maxRows = Math.max(1, ...Object.values(rows));
  const cols = Math.max(1, ...Object.values(col).map(c => c + 1));
  const W = TDG.pad * 2 + (cols - 1) * TDG.colW + TDG.boxW, H = TDG.pad * 2 + (maxRows - 1) * TDG.rowH + TDG.boxH;
  const center = n => {
    const off = (maxRows - rows[pos[n].c]) * TDG.rowH / 2;      // centre short columns vertically
    return { x: TDG.pad + pos[n].c * TDG.colW + TDG.boxW / 2, y: TDG.pad + off + pos[n].r * TDG.rowH + TDG.boxH / 2 };
  };

  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "tdg", role: "img",
    "aria-label": `Topology: ${d.nodes.length} nodes, ${d.links.length} links` });
  svg.style.maxWidth = `${W}px`;
  const addrs = d.addresses || {};
  const endLabel = (node, iface) => {
    if (!iface) return [];
    const own = addrs[node] || {};
    const lines = [iface + (own[iface] ? " " + own[iface].join(" ") : "")];
    for (const [ifc, list] of Object.entries(own)) if (ifc.startsWith(iface + ".")) lines.push(`${ifc} ${list.join(" ")}`);
    return lines;
  };
  for (const l of d.links) {
    if (!(l.a in pos) || !(l.b in pos)) continue;
    const A = center(l.a), B = center(l.b);
    svg.append(el("line", { x1: A.x, y1: A.y, x2: B.x, y2: B.y, class: "tdg-link" }));
    for (const [from, to, node, iface] of [[A, B, l.a, l.a_if], [B, A, l.b, l.b_if]]) {
      const lines = endLabel(node, iface);
      if (!lines.length) continue;
      const t = 0.5 * (TDG.boxW + 24) / Math.max(1, Math.hypot(to.x - from.x, to.y - from.y));   // just outside the box
      const x = from.x + (to.x - from.x) * Math.min(t, 0.45), y = from.y + (to.y - from.y) * Math.min(t, 0.45);
      const text = el("text", { x, y: y - (lines.length - 1) * 6, class: "tdg-if", "text-anchor": "middle" });
      lines.forEach((ln, i) => text.append(el("tspan", { x, dy: i ? 12 : 0 }, ln)));
      svg.append(text);
    }
  }
  for (const n of d.nodes) {
    const c = center(n.name), x = c.x - TDG.boxW / 2, y = c.y - TDG.boxH / 2;
    const g = el("g", { class: `tdg-node tdg-${n.role}` });
    g.append(el("rect", { x, y, width: TDG.boxW, height: TDG.boxH, rx: 7 }));
    g.append(el("text", { x: c.x, y: c.y - 3, class: "tdg-name", "text-anchor": "middle" }, n.name));
    g.append(el("text", { x: c.x, y: c.y + 13, class: "tdg-role", "text-anchor": "middle" }, `${TDG_ROLE_MARK[n.role] || ""} ${n.role}`));
    const own = addrs[n.name] || {};
    const other = Object.entries(own).filter(([ifc]) => !/^enp0s\d+(\.\d+)?$/.test(ifc));     // br0, dummy0, ...
    other.forEach(([ifc, list], i) => g.append(el("text", { x: c.x, y: y + TDG.boxH + 12 + i * 12, class: "tdg-if",
      "text-anchor": "middle" }, `${ifc} ${list.join(" ")}`)));
    svg.append(g);
  }
  return h("div", { class: "tdg-wrap" }, svg);
}

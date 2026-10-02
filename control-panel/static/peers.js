"use strict";
/* Agent-to-agent features. Loaded after app.js (shares its globals: h, $, api, state, open, panes, trackJob ...).
   1. Manual bridge: "Send to..." on any chat message (you carry the message, nothing is automatic).
   2. Peering: links between two agents with a policy; the panel relays replies under that policy. */

/* ================= 1. manual bridge ================= */
const RUN = i => i.status === "healthy" || i.status === "starting";

function provLabel(m) {
  const mt = m.meta; if (!mt) return null;
  if (mt.via === "forward") return `↪ forwarded by you from ${mt.from}`;
  if (mt.via === "peer") return `↔ peer message from ${mt.from} (hop ${mt.hop})`;
  if (mt.via === "peer-start") return "↔ you started this peer conversation";
  return null;
}

function msgEl(m, src) {
  const label = provLabel(m);
  const box = h("div", { class: "msg " + m.role + (m.meta ? " relayed" : "") });
  if (label) box.append(h("div", { class: "prov" }, label));
  box.append(h("div", { class: "txt" }, m.text));
  if (m.role !== "error" && state.instances.length > 1)
    box.append(h("button", { class: "mini", title: "Send this message to another agent", onclick: () => openForward(src, m.text) }, "↪ Send to…"));
  return box;
}

const fwdDlg = h("dialog", { class: "dlg" });
document.body.append(fwdDlg);

async function openForward(src, text) {
  const targets = state.instances.filter(i => i.name !== src);
  if (!targets.length) return alert("Create a second agent first.");
  const to = h("select", {}, ...targets.map(i => h("option", { value: i.name, disabled: !RUN(i) }, `${i.name} (${i.status})`)));
  const firstUp = targets.find(RUN); if (firstUp) to.value = firstUp.name;
  const conv = h("select", {});
  async function loadConvs() {
    conv.replaceChildren(h("option", { value: "fwd-" + src }, `Conversation "From ${src}" (new or existing)`));
    try {
      const cs = await api(`/api/agents/${to.value}/chats`);
      for (const [id, c] of Object.entries(cs)) if (id !== "fwd-" + src) conv.append(h("option", { value: id }, c.title || id));
    } catch { /* leave just the default */ }
  }
  to.addEventListener("change", loadConvs); await loadConvs();
  const ta = h("textarea", { class: "al", spellcheck: "false" }); ta.value = text;
  const msg = h("div", { class: "fail" });
  const send = h("button", { class: "primary", onclick: async () => {
    msg.textContent = "";
    try {
      const r = await api(`/api/agents/${src}/forward`, { to: to.value, to_chat: conv.value, text: ta.value });
      open.add(to.value); saveOpen(); sigSide = ""; fwdDlg.close();
      localStorage.setItem("chat-" + to.value, r.to_chat);
      syncPanes(); if (panes[to.value]) panes[to.value].goChat(r.to_chat);
      trackJob(r.job, `Forward ${src} → ${to.value}`, () => refresh());
    } catch (e) { msg.textContent = e.message; }
  } }, "Send");
  fwdDlg.replaceChildren(
    h("h3", {}, `Send a message from ${src} to another agent`),
    h("div", { class: "row" }, "To", to), h("div", { class: "row" }, "Conversation", conv),
    h("p", { class: "hint" }, `Edit it if you like. It is sent as an ordinary prompt, labelled as forwarded from ${src}. Their reply appears in their window; to continue, forward again. Nothing repeats automatically. Treat what another agent produced as untrusted input.`),
    ta, msg, h("div", { class: "row" }, send, h("button", { onclick: () => fwdDlg.close() }, "Cancel")));
  fwdDlg.showModal();
}

/* ================= 2. peering ================= */
const peerDlg = h("dialog", { class: "dlg wide" });
document.body.append(peerDlg);
const peerBtn = h("button", { id: "peerBtn", title: "Link two agents so they can talk, under limits you set", onclick: () => openPeers() }, "Peering");
$("header").append(peerBtn);

let peerData = { links: [], sessions: [] }, selSession = null, peerTimer = null;
let P = null;                         // references to the dialog's live parts
const sig = {};                       // render signatures, so typing is never wiped by a refresh
const fmtTime = t => t ? new Date(t * 1000).toLocaleTimeString() : "";
const linkName = l => `${l.a} ${l.mode === "one-way" ? "→" : "⇄"} ${l.b}`;
const STATE_LABEL = { running: "running", awaiting: "waiting for you", done: "finished", stopped: "stopped", error: "error", interrupted: "interrupted" };

async function refreshPeerBadge() {
  try {
    if (!peerTimer) peerData = await api("/api/peers");
    const waiting = peerData.sessions.filter(s => s.state === "awaiting").length;
    peerBtn.textContent = waiting ? `Peering (${waiting} waiting)` : "Peering";
    peerBtn.classList.toggle("alert", waiting > 0);
  } catch { /* panel may be restarting */ }
}

function openPeers() {
  buildPeerDialog();
  peerDlg.showModal();
  loadPeers();
  peerTimer = setInterval(loadPeers, 2000);
}
peerDlg.addEventListener("close", () => { clearInterval(peerTimer); peerTimer = null; });

function agentOptions(selectEl, keep) {
  const prev = keep ? selectEl.value : null;
  selectEl.replaceChildren(...state.instances.map(i => h("option", { value: i.name }, `${i.name} (${i.status})`)));
  if (prev) selectEl.value = prev;
}

function buildPeerDialog() {
  P = {};
  P.linksBox = h("div", { class: "col" });
  P.newA = h("select", {}); P.newB = h("select", {});
  agentOptions(P.newA); agentOptions(P.newB); if (state.instances.length > 1) P.newB.selectedIndex = 1;
  P.newMode = h("select", {}, h("option", { value: "two-way" }, "Two-way (replies go back and forth)"), h("option", { value: "one-way" }, "One-way (A tells B; B's reply is not sent back)"));
  P.newApproval = h("input", { type: "checkbox", checked: true });
  P.newHops = h("input", { type: "number", min: 1, max: 20, value: 4, style: "max-width:70px;flex:none" });
  P.newChars = h("input", { type: "number", min: 200, max: 20000, value: 2000, style: "max-width:90px;flex:none" });
  P.newRate = h("input", { type: "number", min: 1, max: 60, value: 10, style: "max-width:70px;flex:none" });
  P.newMsg = h("div", { class: "fail" });
  const create = h("button", { class: "primary", onclick: async () => {
    P.newMsg.textContent = "";
    try {
      await api("/api/peers/links", { a: P.newA.value, b: P.newB.value, mode: P.newMode.value, approval: P.newApproval.checked,
        max_hops: +P.newHops.value, max_chars: +P.newChars.value, rate: +P.newRate.value });
      sig.links = ""; loadPeers();
    } catch (e) { P.newMsg.textContent = e.message; }
  } }, "Create link");

  P.sLink = h("select", { onchange: () => fillStarter() }); P.sStarter = h("select", {});
  P.sText = h("textarea", { class: "al", placeholder: "Your first message. It goes to the starting agent; its reply is then relayed to the other agent under the link's limits.", spellcheck: "false" });
  P.sMsg = h("div", { class: "fail" });
  const start = h("button", { class: "primary", onclick: async () => {
    P.sMsg.textContent = "";
    try {
      const r = await api("/api/peers/sessions", { link: P.sLink.value, starter: P.sStarter.value, text: P.sText.value });
      P.sText.value = ""; selSession = r.session; sig.sessions = ""; sig.detail = ""; loadPeers();
    } catch (e) { P.sMsg.textContent = e.message; }
  } }, "Start conversation");

  P.sessBox = h("div", { class: "col" });
  P.detail = h("div", { class: "col detail" });

  peerDlg.replaceChildren(
    h("div", { class: "row" }, h("h3", { style: "flex:1;margin:0" }, "Peering: let two agents talk, under your limits"), h("button", { onclick: () => peerDlg.close() }, "Close")),
    h("p", { class: "hint" }, "Agents never get a network path to each other. The control panel carries every message between them and enforces the link's limits: direction, number of hops, message size, rate, loop detection, and (by default) your approval of each message. Everything is logged. A relayed message is still untrusted input to the receiving agent, so a prompt-injected agent can try to steer its peer: keep links between agents you would trust equally, and prefer approval on."),
    h("h4", {}, "Links"), P.linksBox,
    h("details", {}, h("summary", {}, "New link"),
      h("div", { class: "col pad" },
        h("div", { class: "row" }, "Agents", P.newA, "and", P.newB), h("div", { class: "row" }, "Direction", P.newMode),
        h("label", { class: "row" }, P.newApproval, "Ask me before each message is relayed (recommended)"),
        h("div", { class: "row" }, "Max hops", P.newHops, "Max characters", P.newChars, "Messages/min", P.newRate),
        P.newMsg, h("div", { class: "row" }, create))),
    h("h4", {}, "Start a conversation"),
    h("div", { class: "col" }, h("div", { class: "row" }, "Link", P.sLink, "Start with", P.sStarter), P.sText, P.sMsg, h("div", { class: "row" }, start)),
    h("h4", {}, "Conversations"), P.sessBox, P.detail);
  Object.keys(sig).forEach(k => delete sig[k]);
}

function fillStarter() {
  const l = peerData.links.find(x => x.id === P.sLink.value); if (!l) return;
  const opts = l.mode === "one-way" ? [l.a] : [l.a, l.b];
  P.sStarter.replaceChildren(...opts.map(n => h("option", { value: n }, n)));
}

async function loadPeers() {
  if (!peerDlg.open) return;
  try { peerData = await api("/api/peers"); } catch { return; }
  refreshPeerBadge();
  const sl = JSON.stringify(peerData.links);
  if (sl !== sig.links) {
    sig.links = sl; renderLinks();
    const prev = P.sLink.value;
    P.sLink.replaceChildren(...peerData.links.filter(l => l.enabled).map(l => h("option", { value: l.id }, linkName(l))));
    if (prev) P.sLink.value = prev;
    fillStarter();
  }
  const ss = JSON.stringify(peerData.sessions);
  if (ss !== sig.sessions) { sig.sessions = ss; renderSessions(); }
  if (selSession) loadDetail();
}

function renderLinks() {
  if (!peerData.links.length) { P.linksBox.replaceChildren(h("p", { class: "hint" }, "No links yet. Open \"New link\" below.")); return; }
  P.linksBox.replaceChildren(...peerData.links.map(l => {
    const ap = h("select", {}, h("option", { value: "ask" }, "ask me each time"), h("option", { value: "auto" }, "automatic"));
    ap.value = l.approval ? "ask" : "auto";
    const hops = h("input", { type: "number", min: 1, max: 20, value: l.max_hops, style: "max-width:64px;flex:none" });
    const chars = h("input", { type: "number", min: 200, max: 20000, value: l.max_chars, style: "max-width:84px;flex:none" });
    const rate = h("input", { type: "number", min: 1, max: 60, value: l.rate, style: "max-width:64px;flex:none" });
    const on = h("input", { type: "checkbox", checked: l.enabled });
    const err = h("span", { class: "fail" });
    const save = h("button", { onclick: async () => {
      err.textContent = "";
      try { await api(`/api/peers/links/${l.id}/update`, { approval: ap.value === "ask", max_hops: +hops.value, max_chars: +chars.value, rate: +rate.value, enabled: on.checked }); sig.links = ""; loadPeers(); }
      catch (e) { err.textContent = e.message; }
    } }, "Save");
    return h("div", { class: "link" + (l.enabled ? "" : " off") },
      h("div", { class: "row" }, h("b", {}, linkName(l)), h("label", { class: "row" }, on, "on"), h("span", { class: "sp" }),
        h("button", { class: "danger", onclick: async () => { if (confirm(`Delete the link ${linkName(l)}? Live conversations on it stop.`)) { await api(`/api/peers/links/${l.id}/delete`, {}); sig.links = ""; loadPeers(); } } }, "Delete")),
      h("div", { class: "row" }, "Relay:", ap, "hops", hops, "chars", chars, "per min", rate, save, err),
      h("ul", { class: "risks" }, ...l.risks.map(r => h("li", {}, "⚠ " + r)),
        l.risks.length ? null : h("li", {}, "Both agents are offline and local; the remaining risk is one model's output steering the other.")));
  }));
}

function renderSessions() {
  if (!peerData.sessions.length) { P.sessBox.replaceChildren(h("p", { class: "hint" }, "No conversations yet.")); return; }
  P.sessBox.replaceChildren(...peerData.sessions.map(s => h("div", { class: "sess" + (s.id === selSession ? " sel" : ""), onclick: () => { selSession = s.id; sig.sessions = ""; sig.detail = ""; loadPeers(); } },
    h("span", { class: "chip " + s.state }, STATE_LABEL[s.state] || s.state), ` ${s.a} ${s.mode === "one-way" ? "→" : "⇄"} ${s.b} `,
    h("span", { class: "status" }, `${s.hops} hop${s.hops === 1 ? "" : "s"} · ${fmtTime(s.started)}${s.reason ? " · " + s.reason : ""}`))));
}

async function loadDetail() {
  let s; try { s = await api("/api/peers/sessions/" + selSession); } catch { return; }
  const key = [s.id, s.state, s.hops, s.transcript.length, s.pending ? s.pending.hop : ""].join("|");
  if (key === sig.detail) return;
  sig.detail = key;
  const live = s.state === "running" || s.state === "awaiting";
  const parts = [h("div", { class: "row" }, h("b", {}, `Conversation ${s.id}`), h("span", { class: "chip " + s.state }, STATE_LABEL[s.state] || s.state),
    h("span", { class: "status" }, s.reason || ""), h("span", { class: "sp" }),
    h("button", { onclick: () => openSessionChats(s) }, "Show in agent windows"),
    live ? h("button", { class: "danger", onclick: async () => { await api(`/api/peers/sessions/${s.id}/stop`, {}); sig.detail = ""; loadPeers(); } }, "Stop")
         : h("button", { title: "Forget this record (messages stay in each agent's chat history)", onclick: async () => { await api(`/api/peers/sessions/${s.id}/delete`, {}); selSession = null; P.detail.replaceChildren(); sig.sessions = ""; sig.detail = ""; loadPeers(); } }, "Delete record"))];
  if (s.pending) {
    const ta = h("textarea", { class: "al", spellcheck: "false" }); ta.value = s.pending.text;
    parts.push(h("div", { class: "pend" },
      h("div", {}, h("b", {}, `Waiting for you: relay ${s.pending.from} → ${s.pending.to}`), ` (hop ${s.pending.hop} of ${s.max_hops}). Edit if you want, then approve.`),
      ta, h("div", { class: "row" },
        h("button", { class: "primary", onclick: async () => { await api(`/api/peers/sessions/${s.id}/approve`, { text: ta.value }); sig.detail = ""; loadPeers(); } }, "Approve & send"),
        h("button", { onclick: async () => { await api(`/api/peers/sessions/${s.id}/reject`, {}); sig.detail = ""; loadPeers(); } }, "Reject (stop)"))));
  }
  parts.push(h("div", { class: "tx" }, ...s.transcript.map(t => h("div", { class: "tline " + t.status },
    h("div", { class: "prov" }, `${fmtTime(t.ts)} · ${t.from} → ${t.to} · ${t.status}${t.hop ? " · hop " + t.hop : ""}`),
    h("div", { class: "txt" }, t.text)))));
  P.detail.replaceChildren(...parts);
}

function openSessionChats(s) {
  for (const n of [s.a, s.b]) if (state.instances.find(i => i.name === n)) { open.add(n); localStorage.setItem("chat-" + n, s.chat); }
  saveOpen(); sigSide = ""; syncPanes();
  for (const n of [s.a, s.b]) if (panes[n]) panes[n].goChat(s.chat);
  peerDlg.close();
}

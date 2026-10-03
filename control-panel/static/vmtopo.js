"use strict";
/* VM Labs: network topologies (routers/switches/hosts, all real VMs wired together). Loaded after vmbench.js in
   the same dialog: appends its own section via vtBuildSection(), and its own 2s poll tick via vtLoad(), called
   from vmbench.js's existing loadVB(). Mirrors vmbench.js's structure throughout. */

let vtData = { topologies: [], tasks: [], settings: {}, runs: [] }, vtSelRun = null;
let VT = null;
const vtSig = {};
const vtFmtTime = t => t ? new Date(t * 1000).toLocaleTimeString() : "";
const vtElapsed = r => { const end = r.ended || Date.now() / 1000; const s = Math.max(0, Math.round(end - (r.started || r.created))); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${s % 60}s`; };

const VT_ROLE_PREFIX = { switch: "sw", router: "r", firewall: "fw", loadbalancer: "lb", host: "h" };
const vtCustomNames = counts => Object.entries(VT_ROLE_PREFIX)
  .flatMap(([role, prefix]) => Array.from({ length: +counts[role] || 0 }, (_, i) => `${prefix}${i + 1}`));

function vtBuildSection() {
  VT = {};
  VT.runsBox = h("div", { class: "col" });
  VT.detail = h("div", { class: "col detail" });

  // -- topology: a catalog template, or the structured builder (role counts + wiring pattern) --
  VT.newTopo = h("select", {}, ...vtData.topologies.map(t => h("option", { value: t.id }, t.title)));
  const topoRow = h("div", { class: "row" }, "Topology", VT.newTopo);
  VT.customOn = h("input", { type: "checkbox" });
  VT.custCounts = {};
  for (const role of ["host", "router", "switch", "loadbalancer", "firewall"]) {
    VT.custCounts[role] = h("input", { type: "number", min: 0, max: 12, value: 0, style: "max-width:50px;flex:none" });
  }
  VT.custWiring = h("select", {},
    h("option", { value: "star" }, "Star off one switch"),
    h("option", { value: "chain" }, "Chain of subnets"),
    h("option", { value: "manual" }, "Manual link list"));
  VT.custNamesHint = h("div", { class: "hint" });
  VT.custLinks = h("textarea", { rows: 2, placeholder: "a-b, a-b, ... (use the names above)", hidden: true });
  const refreshCustomHint = () => {
    const counts = Object.fromEntries(Object.entries(VT.custCounts).map(([k, el]) => [k, +el.value || 0]));
    const names = vtCustomNames(counts);
    const total = names.length;
    VT.custNamesHint.textContent = total
      ? `Nodes (${total}/12): ${names.join(", ")}`
      : "Set at least one role's count above zero.";
    VT.custLinks.hidden = VT.custWiring.value !== "manual";
  };
  Object.values(VT.custCounts).forEach(el => el.addEventListener("input", refreshCustomHint));
  VT.custWiring.addEventListener("change", refreshCustomHint);
  const custBuilder = h("div", { class: "col" },
    h("div", { class: "row" }, ...["host", "router", "switch", "loadbalancer", "firewall"]
      .flatMap(role => [role, VT.custCounts[role]])),
    h("div", { class: "row" }, "Wiring", VT.custWiring),
    VT.custNamesHint, VT.custLinks);
  custBuilder.hidden = true;

  // -- task: a canned task (catalog topology only), or a free-form prompt instead --
  VT.newTask = h("select", {});
  VT.newTaskInfo = h("div", { class: "hint" });
  VT.promptOn = h("input", { type: "checkbox" });
  VT.customPrompt = h("textarea", { rows: 3, placeholder: "Describe what you want the agent to configure...", hidden: true });
  const refillTasks = () => {
    const matching = vtData.tasks.filter(t => t.topology_id === VT.newTopo.value);
    VT.newTask.replaceChildren(h("option", { value: "" }, "— no task: just a scratch lab —"),
      ...matching.map(t => h("option", { value: t.id }, `${t.title} (${t.difficulty}, ~${t.est_minutes} min)`)));
    VT.newTask.dispatchEvent(new Event("change"));
  };
  VT.newTopo.addEventListener("change", refillTasks);
  VT.newTask.addEventListener("change", () => {
    const t = vtData.tasks.find(x => x.id === VT.newTask.value);
    VT.newTaskInfo.textContent = t ? t.prompt : "No task: open a terminal on any node and wire the lab up however you like.";
  });
  const taskRow = h("div", { class: "row" }, "Task", VT.newTask);
  const promptRow = h("label", { class: "row" }, VT.promptOn, "write a custom prompt instead of picking a task",
    " (no automated score)");
  const syncCustomModes = () => {
    topoRow.hidden = VT.customOn.checked;
    custBuilder.hidden = !VT.customOn.checked;
    taskRow.hidden = VT.customOn.checked;
    VT.newTaskInfo.hidden = VT.customOn.checked;
    promptRow.hidden = VT.customOn.checked;              // a custom topology has no catalog task - always prompt mode
    VT.customPrompt.hidden = !(VT.customOn.checked || VT.promptOn.checked);
    if (VT.customOn.checked) refreshCustomHint();
  };
  VT.customOn.addEventListener("change", syncCustomModes);
  VT.promptOn.addEventListener("change", syncCustomModes);

  VT.newAgents = h("div", { class: "col" }, ...vbAgentCheckboxes());
  VT.newKeep = h("input", { type: "checkbox" });
  VT.newMsg = h("div", { class: "fail" });
  const create = h("button", { class: "primary", onclick: async () => {
    VT.newMsg.textContent = "";
    const agents = [...VT.newAgents.querySelectorAll("input:checked")].map(x => x.value);
    const body = { keep: VT.newKeep.checked };
    if (VT.customOn.checked) {
      const counts = Object.fromEntries(Object.entries(VT.custCounts).map(([k, el]) => [k, +el.value || 0]));
      const wiring = VT.custWiring.value;
      const links = wiring === "manual"
        ? VT.custLinks.value.split(",").map(s => s.trim()).filter(Boolean).map(pair => {
            const [a, b] = pair.split("-").map(s => s.trim().toLowerCase());
            return { a, b };
          })
        : undefined;
      body.custom = { counts, wiring, links };
      body.custom_prompt = VT.customPrompt.value.trim() || null;
    } else {
      body.topology_id = VT.newTopo.value;
      body.task_id = VT.promptOn.checked ? null : (VT.newTask.value || null);
      body.custom_prompt = VT.promptOn.checked ? (VT.customPrompt.value.trim() || null) : null;
    }
    try {
      if (agents.length > 1) await api("/api/vmtopo/benchmarks", { ...body, agents });
      else await api("/api/vmtopo/runs", { ...body, agent: agents[0] || null });
      vtSig.runs = ""; vtLoad();
    } catch (e) { VT.newMsg.textContent = e.message; }
  } }, "Create");

  refillTasks();
  syncCustomModes();
  Object.keys(vtSig).forEach(k => delete vtSig[k]);

  return h("div", { class: "col" },
    h("h4", {}, "Network topologies"),
    h("p", { class: "hint" },
      "A small group of real VMs wired together with virtual cabling: routers, switches (real VMs doing real L2 bridging, not simulated devices), hosts, load balancers and firewalls. Nothing is pre-addressed except the switches, which just forward frames - assigning IPs, enabling routing/filtering and adding routes is the task. Attach an agent, or open a terminal on any node and do it yourself."),
    h("h5", {}, "New lab"),
    h("div", { class: "col" },
      topoRow,
      h("label", { class: "row" }, VT.customOn, "build a custom topology instead"),
      custBuilder,
      taskRow, VT.newTaskInfo, promptRow, VT.customPrompt,
      h("div", {}, "Attach agent(s) (optional — tick more than one to benchmark them side by side on the same task)"), VT.newAgents,
      h("label", { class: "row" }, VT.newKeep, "keep these VMs running afterward, for later inspection"),
      VT.newMsg, h("div", { class: "row" }, create)),
    h("h5", {}, "Topology runs"), VT.runsBox, VT.detail);
}

async function vtLoad() {
  if (!vbDlg.open) return;
  try { vtData = await api("/api/vmtopo"); } catch { return; }
  const rs = JSON.stringify(vtData.runs);
  if (rs !== vtSig.runs) { vtSig.runs = rs; renderVTRuns(); }
  if (vtSelRun) loadVTDetail();
}

function renderVTRuns() {
  if (!vtData.runs.length) { VT.runsBox.replaceChildren(h("p", { class: "hint" }, "No topology runs yet.")); return; }
  const byBench = {};
  for (const r of vtData.runs) (byBench[r.benchmark_id || r.id] = byBench[r.benchmark_id || r.id] || []).push(r);
  const groups = Object.values(byBench);
  VT.runsBox.replaceChildren(...groups.map(grp => {
    const sorted = grp.length > 1 ? [...grp].sort((a, b) => (b.score ? b.score.passed : -1) - (a.score ? a.score.passed : -1)) : grp;
    return h("div", { class: "col", style: grp.length > 1 ? "border:1px solid var(--line);border-radius:8px;padding:6px" : "" },
      grp.length > 1 ? h("div", { class: "hint" }, `Benchmark: ${grp[0].task_title || "task"} across ${grp.length} agents`) : null,
      ...sorted.map(r => h("div", { class: "sess" + (r.id === vtSelRun ? " sel" : ""), onclick: () => { vtSelRun = r.id; vtSig.detail = ""; vtLoad(); } },
        h("span", { class: "chip " + r.state }, VB_STATE_LABEL[r.state] || r.state),
        ` ${r.topology_title} — ${r.agent ? r.agent : "(no agent)"}${r.task_title ? " — " + r.task_title : " — scratch lab"} `,
        h("span", { class: "status" }, `${vtElapsed(r)}${r.score ? " · " + (r.score.passed ? "PASS" : "FAIL") : ""}`))));
  }));
}

async function loadVTDetail() {
  let r; try { r = await api("/api/vmtopo/runs/" + vtSelRun); } catch { vtSelRun = null; return; }
  const key = [r.id, r.state, r.transcript.length, r.score && r.score.passed,
              Object.values(r.nodes).map(n => n.terminal.active).join(",")].join("|");
  if (key === vtSig.detail) return;
  vtSig.detail = key;
  const live = ["queued", "provisioning", "working", "scoring"].includes(r.state);
  const canTerminal = ["ready", "working", "scoring", "done"].includes(r.state);
  const canScore = r.task_id && ["ready", "working", "done"].includes(r.state);
  const canStop = live || r.state === "ready";
  const canDelete = !live;

  const nodeRows = Object.entries(r.nodes).map(([name, n]) => h("div", { class: "row" },
    h("b", {}, name), `(${n.role})`, n.ssh_port ? `port ${n.ssh_port}` : "",
    h("button", { disabled: !canTerminal, onclick: async () => {
      try {
        const t = await api(`/api/vmtopo/runs/${r.id}/nodes/${name}/terminal-start`, {});
        showTerminalLogin(`Node ${name} in lab ${r.id}`, t);
        vtSig.detail = ""; vtLoad();
      } catch (e) { alert(e.message); }
    } }, n.terminal.active ? "Open terminal (running)" : "Open terminal")));

  const scoreBtn = h("button", { disabled: !canScore, onclick: () => action(`/api/vmtopo/runs/${r.id}/score`, `Score ${r.id}`, {}, () => { vtSig.detail = ""; vtLoad(); }) }, "Score now");
  const stopBtn = h("button", { class: "danger", disabled: !canStop, onclick: async () => { try { await api(`/api/vmtopo/runs/${r.id}/stop`, {}); vtSig.runs = ""; vtSig.detail = ""; vtLoad(); } catch (e) { alert(e.message); } } }, "Stop");
  const delBtn = h("button", { disabled: !canDelete, onclick: async () => {
    if (!confirm("Delete this run's record? (its VMs, if any survive, are destroyed first unless you chose \"keep\")")) return;
    try { await api(`/api/vmtopo/runs/${r.id}/delete`, {}); if (vtSelRun === r.id) { vtSelRun = null; VT.detail.replaceChildren(); } vtSig.runs = ""; vtLoad(); } catch (e) { alert(e.message); }
  } }, "Delete");

  const parts = [h("div", { class: "row" }, h("b", {}, `Lab ${r.id}`), h("span", { class: "chip " + r.state }, VB_STATE_LABEL[r.state] || r.state),
    h("span", { class: "status" }, r.reason || ""), h("span", { class: "sp" }), scoreBtn, stopBtn, delBtn)];
  const taskDesc = r.task_title ? " · task: " + r.task_title
    : r.custom_prompt ? " · custom prompt (no automated score)" : " · no task (scratch lab)";
  parts.push(h("div", { class: "hint" }, `${r.topology_title}${taskDesc}${r.agent ? " · agent: " + r.agent : " · no agent attached"} · started ${vtFmtTime(r.started)} · elapsed ${vtElapsed(r)}`));
  if (r.custom_prompt) parts.push(h("pre", {}, r.custom_prompt));
  parts.push(...nodeRows);
  if (r.score) {
    parts.push(h("div", { class: r.score.passed ? "pass" : "fail" }, r.score.passed ? "PASS" : "FAIL", ` (${r.score.duration_s}s)`));
    parts.push(h("pre", {}, r.score.output));
  }
  parts.push(h("div", { class: "hint" }, "Live transcript:"));
  parts.push(h("pre", { style: "max-height:30vh" }, r.transcript || "(nothing yet)"));
  VT.detail.replaceChildren(...parts);
}

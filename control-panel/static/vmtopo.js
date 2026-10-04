"use strict";
/* VM Labs: network topologies (routers/switches/hosts, all real VMs wired together). Loaded after vmbench.js in
   the same dialog: vtBuildSection() is the "Network labs" tab, and its own 2s poll tick via vtLoad() is called
   from vmbench.js's existing loadVB(). Mirrors vmbench.js's structure throughout. */

let vtData = { topologies: [], tasks: [], settings: {}, runs: [] }, vtSelRun = null;
let VT = null;
const vtSig = {};
const vtFmtTime = t => t ? new Date(t * 1000).toLocaleTimeString() : "";
const vtElapsed = r => { const end = r.ended || Date.now() / 1000; const s = Math.max(0, Math.round(end - (r.started || r.created))); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${s % 60}s`; };

const VT_ROLE_PREFIX = { switch: "sw", router: "r", firewall: "fw", loadbalancer: "lb", server: "srv", upstream: "up", host: "h" };
const vtCustomNames = counts => Object.entries(VT_ROLE_PREFIX)
  .flatMap(([role, prefix]) => Array.from({ length: +counts[role] || 0 }, (_, i) => `${prefix}${i + 1}`));

// Layout helpers for the Network labs tab: a titled group of controls, a titled section of a run, and a collapsible panel.
const VT_GROUP_STYLE = "border-top:1px solid var(--line);padding-top:8px;margin-top:4px";
const vtGroup = (title, hint, ...kids) => h("div", { class: "col", style: VT_GROUP_STYLE },
  h("b", {}, title), hint ? h("div", { class: "hint" }, hint) : null, ...kids);
const vtSection = (title, kids) => {
  const shown = kids.filter(Boolean);
  return shown.length ? h("div", { class: "col", style: VT_GROUP_STYLE }, h("b", {}, title), ...shown) : null;
};
const vtFold = (title, open, ...kids) => {
  const opts = { style: VT_GROUP_STYLE };
  if (open) opts.open = "";
  return h("details", opts, h("summary", { style: "cursor:pointer" }, h("b", {}, title)), ...kids.filter(Boolean));
};

function vtBuildSection() {
  VT = {};
  VT.runsBox = h("div", { class: "col" });
  VT.detail = h("div", { class: "col detail" });

  // -- topology: a catalog template, or the structured builder (role counts + wiring pattern) --
  VT.newTopo = h("select", {}, ...vtData.topologies.map(t => h("option", { value: t.id }, t.title)));
  const topoRow = h("div", { class: "row" }, "Topology", VT.newTopo);
  VT.preview = h("div", { class: "col" });
  const showPreview = (topo, configs) => VT.preview.replaceChildren(topo ? renderTopologyDiagram(vtDiagramOf(topo, configs)) : "");
  VT.customOn = h("input", { type: "checkbox" });
  VT.custCounts = {};
  for (const role of ["host", "router", "switch", "loadbalancer", "firewall", "server", "upstream"]) {
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
    h("div", { class: "row" }, ...["host", "router", "switch", "loadbalancer", "firewall", "server", "upstream"]
      .flatMap(role => [role, VT.custCounts[role]])),
    h("div", { class: "row" }, "Wiring", VT.custWiring),
    VT.custNamesHint, VT.custLinks);
  custBuilder.hidden = true;

  // -- task: a canned task (catalog topology only), or a free-form prompt instead --
  VT.newTask = h("select", {});
  VT.newTaskInfo = h("div", { class: "hint" });
  VT.promptOn = h("input", { type: "checkbox" });
  VT.customPrompt = h("textarea", { rows: 3, placeholder: "Describe what you want the agent to configure...", hidden: true });
  VT.newIntents = h("textarea", { rows: 3, placeholder: "h1 -> h2 icmp reach\nh2 -> web1 tcp/22 block\nh1 -> 10.2.0.10 path via r1, r2" });
  const refillTasks = () => {
    const matching = vtData.tasks.filter(t => t.topology_id === VT.newTopo.value);
    VT.newTask.replaceChildren(h("option", { value: "" }, "— no task: just a scratch lab —"),
      ...matching.map(t => h("option", { value: t.id }, `${t.title} (${t.difficulty}, ~${t.est_minutes} min)`)));
    VT.newTask.dispatchEvent(new Event("change"));
  };
  VT.newTopo.addEventListener("change", refillTasks);
  VT.newTopo.addEventListener("change", () => showPreview(vtData.topologies.find(t => t.id === VT.newTopo.value)));
  VT.newTask.addEventListener("change", () => {
    const t = vtData.tasks.find(x => x.id === VT.newTask.value);
    VT.newTaskInfo.textContent = t ? t.prompt : "No task: open a terminal on any node and wire the lab up however you like.";
  });
  const taskRow = h("div", { class: "row" }, "Task", VT.newTask);
  const promptRow = h("label", { class: "row" }, VT.promptOn, "write a custom prompt instead of picking a task",
    " (no automated score)");
  // -- or a lab file: a lab exported from a snapshot (topology + every node's config), rebuilt with its configs --
  VT.fileOn = h("input", { type: "checkbox" });
  VT.fileInput = h("input", { type: "file", accept: ".json,application/json" });
  VT.fileInfo = h("div", { class: "hint" }, "Choose a lab file exported from a lab's snapshots.");
  VT.labfile = null;
  VT.useLabfile = lf => {
    VT.labfile = lf;
    showPreview(lf.topology, lf.configs);
    const nodes = (lf.topology?.nodes || []).map(n => `${n.name} (${n.role})`);
    const cfg = Object.keys(lf.configs || {});
    VT.fileInfo.className = "hint";
    VT.fileInfo.textContent = `"${lf.title}": ${nodes.length} nodes (${nodes.join(", ")}), ${(lf.topology?.links || []).length} links; ` +
      (cfg.length ? `configs for ${cfg.join(", ")} are applied once the VMs are up, then checked.` : "no configs in the file.") +
      (lf.source?.exported ? ` Exported ${lf.source.exported}.` : "") +
      (lf.source?.draft ? ` Drafted from ${lf.source.diagram} by ${lf.source.drafted_by}.` : "");
  };
  VT.fileInput.addEventListener("change", async () => {
    VT.labfile = null;
    const f = VT.fileInput.files[0];
    if (!f) { VT.fileInfo.textContent = "Choose a lab file exported from a lab's snapshots."; return; }
    try {
      const lf = JSON.parse(await f.text());
      if (lf.format !== "aiagentplayground-lab") throw new Error("this isn't an AI Agent Playground lab file");
      VT.useLabfile(lf);
    } catch (e) { VT.fileInfo.className = "fail"; VT.fileInfo.textContent = `Can't read that file: ${e.message}`; }
  });
  const fileBox = h("div", { class: "col" }, VT.fileInput, VT.fileInfo);
  const customRow = h("label", { class: "row" }, VT.customOn, "build a custom topology instead");
  const syncCustomModes = () => {
    if (VT.fileOn.checked) VT.customOn.checked = false;
    showPreview(VT.fileOn.checked ? (VT.labfile && VT.labfile.topology) : VT.customOn.checked ? null
      : vtData.topologies.find(t => t.id === VT.newTopo.value), VT.fileOn.checked && VT.labfile ? VT.labfile.configs : null);
    const fromFile = VT.fileOn.checked, custom = VT.customOn.checked;
    topoRow.hidden = custom || fromFile;
    customRow.hidden = fromFile;
    custBuilder.hidden = !custom;
    fileBox.hidden = !fromFile;
    taskRow.hidden = custom || fromFile;
    VT.newTaskInfo.hidden = custom || fromFile;
    promptRow.hidden = custom || fromFile;               // custom topologies and lab files have no catalog task
    VT.customPrompt.hidden = !(custom || fromFile || VT.promptOn.checked);
    if (custom) refreshCustomHint();
  };
  VT.customOn.addEventListener("change", syncCustomModes);
  VT.promptOn.addEventListener("change", syncCustomModes);
  VT.fileOn.addEventListener("change", syncCustomModes);
  VT.syncModes = syncCustomModes;

  VT.newAgents = h("div", { class: "col" }, ...vbAgentCheckboxes());
  VT.newKeep = h("input", { type: "checkbox" });
  VT.newInteractive = h("input", { type: "checkbox" });
  VT.newPlanFirst = h("input", { type: "checkbox" });
  VT.newCap = h("input", { type: "number", min: 5, max: 200, step: 1, value: "40", style: "width:5em" });
  VT.newBudget = h("input", { type: "number", min: 0.01, max: 20, step: 0.01, value: "0.50", style: "width:6em" });
  VT.newTeam = h("textarea", { rows: 3, placeholder: "alpha | h1,r1 | 1 | network admin: builds the routing\nbeta | h2 | 2 | web server: serves the feed" });
  VT.newDomains = h("textarea", { rows: 2, placeholder: "e.g. docs.example.org (one per line, for a niche tool)" });
  VT.newMsg = h("div", { class: "fail" });
  const create = h("button", { class: "primary", onclick: async () => {
    VT.newMsg.textContent = "";
    const agents = [...VT.newAgents.querySelectorAll("input:checked")].map(x => x.value);
    if (VT.newInteractive.checked && !agents.length) { VT.newMsg.textContent = "Tick the agent to attach."; return; }
    const body = { keep: VT.newKeep.checked, interactive: VT.newInteractive.checked, intents: VT.newIntents.value,
                   plan_first: VT.newPlanFirst.checked, extra_domains: VT.newDomains.value,
                   budget_usd: VT.newBudget.value, command_cap: VT.newCap.value };
    if (VT.fileOn.checked) {
      if (!VT.labfile) { VT.newMsg.textContent = "Choose a lab file first."; return; }
      body.labfile = VT.labfile;
      body.custom_prompt = VT.customPrompt.value.trim() || null;
    } else if (VT.customOn.checked) {
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
      if (VT.newTeam.value.trim()) {
        // A team takes its members instead of the single-agent checkboxes above.
        await api("/api/vmtopo/runs", { ...body, agent: null, team: VT.newTeam.value });
        vtSig.runs = ""; vtLoad();
        return;
      }
      if (agents.length && !confirmAgentModels(agents)) return;
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
    // The lab list comes first; the form folds away once there are runs to look at.
    h("h5", {}, "Topology runs"), VT.runsBox,
    vtFold("New lab", !vtData.runs.length,
      vtGroup("1. Where the lab comes from", "A catalog topology, one you build from role counts, or a lab file.",
        topoRow, VT.preview, customRow,
        h("label", { class: "row" }, VT.fileOn, "build from a lab file"), fileBox,
        custBuilder),
      vtGroup("2. What the agent does", "A catalog task, or your own prompt. Custom topologies and lab files take a prompt.",
        taskRow, VT.newTaskInfo, promptRow, VT.customPrompt),
      vtGroup("3. Checks (optional)",
        "What the lab must do, one per line. Each is checked from its source node after the agent's work. A lab with intents and no task check is scored by them.",
        h("div", { class: "hint" }, "reach or block a node or address over icmp or tcp/<port>, or check the route: 'h1 -> h2 icmp reach', 'h2 -> web1 tcp/22 block', 'h1 -> 10.2.0.10 path via r1, r2'. Lab files carry their own intents."),
        VT.newIntents),
      vtGroup("4. Team (optional, instead of one agent)",
        "One member per line: agent | nodes | stage | brief. Members in the same stage work at the same time; stage 2 starts when stage 1 is done. Each member sees only its own nodes. Teams take one turn per member for now.",
        VT.newTeam),
      vtGroup("4. Agent and session (optional)",
        "An agent has to run commands to work a VM, and local models often only describe them: a cloud model is recommended.",
        VT.newAgents,
        h("div", { class: "hint" }, "Tick more than one agent to benchmark them side by side on the same task. Each agent shows what its model has done in VM Labs here."),
        h("label", { class: "row" }, VT.newInteractive, "interactive session"),
        h("div", { class: "hint" }, "The agent keeps its access to every node after its first reply so you can send it more guidance. It ends when you press End session, or after 2 hours with no new message."),
        h("label", { class: "row" }, VT.newPlanFirst, "plan first"),
        h("div", { class: "hint" }, "The agent writes its plan with no access to the nodes. Nothing runs until you approve the plan on the lab's page. Needs an agent.")),
      vtGroup("5. Internet", "The lab's VMs reach documentation and package sites for their roles, through a proxy that checks each hostname. Add a site here if a niche tool needs one.",
        VT.newDomains),
      vtGroup("6. Budget per paid agent", "Each cloud agent gets this many dollars of model calls in the lab. The model decides how many commands that buys: a bigger model buys fewer. Local agents aren't budgeted. Set a spend limit with your provider as well: this is a guard against runaway loops, not a hard cap.",
        h("label", { class: "row" }, "$", VT.newBudget),
        h("label", { class: "row" }, "At most", VT.newCap, "commands per turn (stops one turn running away, whatever the budget left)")),
      vtGroup("7. Keep the VMs?", null,
        h("label", { class: "row" }, VT.newKeep, "keep these VMs running afterward, for later inspection")),
      VT.newMsg, h("div", { class: "row" }, create)),
    vtFold("Draft a lab from a diagram", false, vtDraftSection()),
    VT.detail);
}

async function vtLoad() {
  if (!vbDlg.open) return;
  try { vtData = await api("/api/vmtopo"); } catch { return; }
  const rs = JSON.stringify(vtData.runs);
  if (rs !== vtSig.runs) { vtSig.runs = rs; renderVTRuns(); }
  const ds = JSON.stringify(vtData.drafts || []);
  if (ds !== vtSig.drafts) { vtSig.drafts = ds; renderVTDrafts(); }
  if (typeof renderBench === "function") renderBench();
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
        ` ${r.topology_title} — ${r.agent ? r.agent + (r.agent_model ? ` (${r.agent_model})` : "") : "(no agent)"}${r.task_title ? " — " + r.task_title : r.custom_prompt ? " — your prompt" : " — scratch lab"}${r.interactive ? " (session)" : ""} `,
        h("span", { class: "status" }, `${vtElapsed(r)}${r.score ? " · " + (r.score.passed ? "PASS" : "FAIL") : ""}`))));
  }));
}

async function loadVTDetail() {
  let r; try { r = await api("/api/vmtopo/runs/" + vtSelRun); } catch { vtSelRun = null; return; }
  const key = [r.id, r.state, r.transcript.length, r.score && r.score.passed,
              Object.values(r.nodes).map(n => n.terminal.active).join(","), (r.conversation || []).length,
              (r.captures || []).map(c => c.state).join(","), (r.intent_results || []).map(x => x.passed).join(","),
              (r.changes || []).map(c => `${c.turn}${c.rolled_back ? "x" : ""}`).join(","), r.restoring,
              r.plan ? r.plan.length : 0, r.plan_approved].join("|");
  if (key === vtSig.detail) return;
  vtSig.detail = key;
  const live = ["queued", "provisioning", "working", "attached", "scoring", "resuming"].includes(r.state);
  const canTerminal = ["ready", "working", "attached", "scoring", "done"].includes(r.state);
  const canScore = r.task_id && ["ready", "working", "attached", "done"].includes(r.state);
  const canIntents = (r.intents || []).length > 0 && ["ready", "working", "attached", "done"].includes(r.state);
  const canStop = live || r.state === "ready";
  const canDelete = !live && r.state !== "saving";
  const canSave = r.has_vms && ["ready", "done", "stopped"].includes(r.state) && !(r.state === "ready" && r.agent_holds);
  const labAction = (what, label) => async () => {
    try { await api(`/api/vmtopo/runs/${r.id}/${what}`, {}); vtSig.runs = ""; vtSig.detail = ""; vtLoad(); }
    catch (e) { alert(`${label}: ${e.message}`); }
  };
  const saveBtn = r.state === "saved"
    ? h("button", { class: "primary", disabled: !r.has_vms, title: "Restore this lab's VMs exactly as they were when you saved it",
        onclick: labAction("resume", "Resume") }, "Resume")
    : h("button", { disabled: !canSave, title: "Suspend this lab's VMs to disk, running state and all, to resume later. A saved lab holds no VM slots; it uses disk space (about each VM's memory).",
        onclick: labAction("save", "Save") }, r.state === "saving" ? "Saving..." : r.state === "resuming" ? "Resuming..." : "Save");

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
  const intentsBtn = h("button", { disabled: !canIntents, onclick: () => action(`/api/vmtopo/runs/${r.id}/intents`, `Check intents ${r.id}`, {}, () => { vtSig.detail = ""; vtLoad(); }) }, "Check intents");
  const stopBtn = h("button", { class: "danger", disabled: !canStop, onclick: async () => { try { await api(`/api/vmtopo/runs/${r.id}/stop`, {}); vtSig.runs = ""; vtSig.detail = ""; vtLoad(); } catch (e) { alert(e.message); } } }, "Stop");
  const delBtn = h("button", { disabled: !canDelete, onclick: async () => {
    if (!confirm(r.state === "saved" ? "Delete this saved lab? Its VMs, and everything configured on them, are destroyed."
      : "Delete this run's record? (its VMs, if any survive, are destroyed first unless you chose \"keep\")")) return;
    try { await api(`/api/vmtopo/runs/${r.id}/delete`, {}); if (vtSelRun === r.id) { vtSelRun = null; VT.detail.replaceChildren(); } vtSig.runs = ""; vtLoad(); } catch (e) { alert(e.message); }
  } }, "Delete");

  const parts = [h("div", { class: "row" }, h("b", {}, `Lab ${r.id}`), h("span", { class: "chip " + r.state }, VB_STATE_LABEL[r.state] || r.state),
    h("span", { class: "status" }, r.reason || ""), h("span", { class: "sp" }), saveBtn, scoreBtn, intentsBtn, stopBtn, delBtn)];
  const taskDesc = r.task_title ? " · task: " + r.task_title
    : r.custom_prompt ? " · custom prompt (no automated score)" : " · no task (scratch lab)";
  parts.push(h("div", { class: "hint" }, `${r.topology_title}${taskDesc}${r.agent ? ` · agent: ${r.agent} (${r.agent_model || "?"})` : " · no agent attached"}${r.agent_turns ? ` · ran ${r.agent_commands ?? "?"} commands in ${r.agent_turns} turn${r.agent_turns === 1 ? "" : "s"}` : ""}${r.interactive ? " · interactive session" : ""} · started ${vtFmtTime(r.started)} · elapsed ${vtElapsed(r)}`));
  if (r.state === "saved") parts.push(h("div", { class: "hint" },
    `Saved ${r.saved_at ? new Date(r.saved_at * 1000).toLocaleString() : ""}. Its VMs are suspended to disk and hold no VM slots; Resume restores them exactly as they were, including addresses, routes and rules you set. Delete destroys them.`));
  if (r.from_labfile) {
    const c = r.labfile_check;
    const bad = c ? Object.entries(c.mismatches) : [];
    parts.push(h("div", { class: c && !bad.length ? "pass" : "hint" }, `Built from lab file "${r.from_labfile}". ` +
      (!c ? "Its configs are applied once the VMs are up." : !bad.length ? "Every node matches the file."
        : `Differs from the file in ${bad.map(([n, m]) => `${n}: ${m.join(", ")}`).join("; ")} (see the transcript).`)));
  }
  // Sections, top to bottom: what the lab is, its machines and agent, how it's checked. The bulky tools fold away.
  const intentRows = (r.intents || []).length ? (() => {
    // One row per intent: what was asked, and whether the last check (from its source node) met it.
    const res = r.intent_results || [];
    return [h("div", { class: "hint" }, `${res.length ? `${res.filter(x => x.passed).length} of ${res.length} pass` : "not checked yet"}`),
      h("div", { class: "col" }, ...r.intents.map(text => {
        const x = res.find(y => y.text === text);
        return h("div", { class: x ? (x.passed ? "pass" : "fail") : "hint" },
          `${x ? (x.passed ? "PASS" : "FAIL") : "—"}  ${text}${x ? `  (${x.detail})` : ""}`);
      }))];
  })() : [];
  const scoreRows = r.score ? [
    h("div", { class: r.score.passed ? "pass" : "fail" }, r.score.passed ? "PASS" : "FAIL", ` (${r.score.duration_s}s)`),
    h("pre", {}, r.score.output)] : [];

  const eg = r.egress;
  parts.push(vtSection("Status", [
    r.custom_prompt ? h("pre", {}, r.custom_prompt) : null,
    eg ? h("div", { class: "hint" }, eg.port
      ? `Internet: ${eg.domains.length} documentation and package sites through the proxy on 192.168.56.1:${eg.port}` +
        (eg.extra && eg.extra.length ? `, including ${eg.extra.join(", ")} (added for this lab)` : "") + "."
      : "Internet: the proxy starts when the lab is built.") : null]));
  parts.push(vtSection("Topology", [
    r.diagram ? h("details", { open: "" }, h("summary", {}, "Topology diagram"),
      renderTopologyDiagram(r.diagram),
      h("div", { class: "hint" }, r.diagram.snapshot ? `Addresses from snapshot ${r.diagram.snapshot.id} (${new Date(r.diagram.snapshot.ts * 1000).toLocaleString()}).`
        : "Addresses appear here once the lab has a snapshot.")) : null,
    ...nodeRows,
    r.plan_first && r.plan && !r.plan_approved && r.state === "ready" ? vtPlanBox(r) : null,
    r.state === "ready" && r.has_vms && !r.agent_holds && !(r.plan_first && !r.plan_approved) ? vtAttachBox(r) : null]));
  if (r.budget) parts.push(vtSection("Budget", Object.entries(r.budget.commands).map(([a, n]) => h("div", { class: "hint" },
    `${a}: ${Math.max(0, n - (r.budget.used[a] || 0))} of ${n} commands left (budget $${r.budget.usd.toFixed(2)})`))));
  if (r.team) parts.push(vtSection("Team", r.team.map(m => h("div", { class: "hint" },
    `stage ${m.stage} · ${m.agent} · nodes ${m.nodes.join(", ")} · ${m.brief}`))));
  if ((r.mail || []).length) parts.push(vtSection("Messages between members", r.mail.map(x => h("div", { class: x.status === "delivered" || x.status === "queued" ? "hint" : "fail" },
    `round ${x.round || "-"} · ${x.from} → ${x.to}: ${x.text} (${x.status})`))));
  parts.push(vtSection("Agent session", [
    r.conversation && r.conversation.length ? sessionConversation(r, "lab") : null,
    r.interactive && ["attached", "working"].includes(r.state)
      ? sessionBox(r, "/api/vmtopo/runs", "lab", () => { vtSig.detail = ""; vtLoad(); }) : null]));
  parts.push(vtSection("Checks", [...scoreRows, ...intentRows]));
  if (["ready", "working", "attached", "done"].includes(r.state)) parts.push(vtFold("Packet captures", false, vtCaptures(r)));
  if ((r.changes || []).length) parts.push(vtFold("Change log", false, vtChanges(r)));
  parts.push(vtFold("Config snapshots", false, vtSnapshots(r)));
  parts.push(vtFold("Live transcript", false,
    h("pre", { style: "max-height:30vh" }, r.transcript || "(nothing yet)")));
  // Re-rendering happens as the transcript grows: keep each panel open or closed the way the user left it.
  const openState = {};
  VT.detail.querySelectorAll("details").forEach(d => { const t = d.querySelector("summary")?.textContent; if (t) openState[t] = d.open; });
  replaceKeepingFocus(VT.detail, parts.filter(Boolean));
  VT.detail.querySelectorAll("details").forEach(d => {
    const t = d.querySelector("summary")?.textContent;
    if (t && t in openState) d.open = openState[t];
  });
}

/* Attach an agent to a lab that already exists (one you built yourself, or saved and resumed). When it's done the lab
   is "ready" again; attaching the same agent again continues the lab's conversation. */
function vtAttachBox(r) {
  const draftKey = `attach/${r.id}`;
  const agents = state.instances.filter(i => i.status === "healthy");
  const sel = h("select", {}, h("option", { value: "" }, agents.length ? "Choose an agent..." : "No running agents"),
    ...agents.map(i => h("option", { value: i.name, selected: i.name === r.agent }, `${i.name} · ${i.modelId} · ${evidenceText(i.evidence)}`)));
  const prompt = h("textarea", { class: "al followup", placeholder: "What should the agent do in this lab? e.g. Route between h1 and h2 through r1, then prove it with ping and traceroute." });
  prompt.value = vbDrafts[draftKey] || "";
  prompt.addEventListener("input", () => { vbDrafts[draftKey] = prompt.value; });
  const useTask = h("input", { type: "checkbox" });
  useTask.addEventListener("change", () => { prompt.hidden = useTask.checked; });
  const interactive = h("input", { type: "checkbox" });
  const msg = h("div", { class: "fail" });
  const go = h("button", { class: "primary", onclick: async () => {
    msg.textContent = "";
    if (!sel.value) { msg.textContent = "Choose the agent to attach."; return; }
    if (!useTask.checked && !prompt.value.trim()) { msg.textContent = "Write what the agent should do."; prompt.focus(); return; }
    if (!confirmAgentModels([sel.value])) return;
    try {
      await api(`/api/vmtopo/runs/${r.id}/attach`, { agent: sel.value, custom_prompt: useTask.checked ? null : prompt.value,
                                                     use_task: useTask.checked, interactive: interactive.checked });
      delete vbDrafts[draftKey]; vtSig.runs = ""; vtSig.detail = ""; vtLoad();
    } catch (e) { msg.textContent = e.message; }
  } }, "Attach agent");
  return h("div", { class: "col bench" },
    h("b", {}, "Attach an agent to this lab"),
    h("p", { class: "hint" }, `The agent gets commands for every node and works in the lab as it is now${r.resumed ? ", including what you saved" : ""}. When it's done, the lab is yours again${r.agent ? `; attaching ${r.agent} again continues its conversation about this lab` : ""}.`),
    h("div", { class: "row" }, "Agent", sel),
    r.task_id ? h("label", { class: "row" }, useTask, `use the lab's task instead (${r.task_title}; scored when the agent is done)`) : null,
    prompt,
    h("label", { class: "row" }, interactive, "interactive session"),
    msg, h("div", { class: "row" }, go));
}

/* Config snapshots: what's configured on every node, captured on demand and after every agent turn. View one,
   download it as a zip, or compare any two. The picks and the open view survive the detail view's redraws. */
let vtSnap = { rid: null, picks: [], view: null };
const VT_SNAP_STATES = ["ready", "working", "attached", "scoring", "done", "stopped"];

/* Plan first: the agent's plan, waiting for you. Approving attaches the agent, and it applies the plan. */
function vtPlanBox(r) {
  const msg = h("div", { class: "fail" });
  const go = h("button", { class: "primary", onclick: async () => {
    msg.textContent = "";
    try { await api(`/api/vmtopo/runs/${r.id}/approve-plan`, { interactive: r.interactive }); vtSig.runs = ""; vtSig.detail = ""; vtLoad(); }
    catch (e) { msg.textContent = e.message; }
  } }, "Approve plan and apply");
  return h("div", { class: "col bench" },
    h("b", {}, "Plan waiting for approval"),
    h("p", { class: "hint" }, `${r.agent} wrote this plan without any access to the nodes. Approving lets it run the plan on the lab.`),
    h("pre", { style: "max-height:40vh" }, r.plan),
    h("div", { class: "row" }, go), msg);
}

/* Change log: the agent's commands per node and turn, with a rollback point before each turn. Rolling back restores
   every node to how it was before that turn; the turns after it are kept, marked as rolled back. */
function vtChanges(r) {
  const canRollBack = r.state === "ready" && !r.restoring && !r.agent_holds;
  const turns = r.changes.slice().reverse().map(c => {
    const rows = c.commands.map(x => h("details", {}, h("summary", {}, `${x.node || "?"} $ ${x.command}`),
      h("pre", {}, x.output || "(no output)")));
    const more = c.command_count > c.commands.length ? h("div", { class: "hint" }, `…and ${c.command_count - c.commands.length} more not listed`) : null;
    const rollBtn = c.point && !c.rolled_back ? h("button", { disabled: !canRollBack, onclick: async () => {
      if (!confirm(`Roll every node back to how it was before turn ${c.turn}? The turns after it are kept as a record, but their changes are undone.`)) return;
      try { await api(`/api/vmtopo/runs/${r.id}/rollback`, { turn: c.turn }); vtSig.detail = ""; vtLoad(); }
      catch (e) { alert(e.message); }
    } }, `Roll back to before turn ${c.turn}`) : null;
    return h("div", { class: "col" + (c.rolled_back ? " hint" : "") },
      h("div", { class: "row" }, h("b", {}, `Turn ${c.turn}${c.rolled_back ? " (rolled back)" : ""}`),
        h("span", { class: "status" }, `${c.command_count} command${c.command_count === 1 ? "" : "s"}${c.point ? "" : " · no rollback point"}`),
        h("span", { class: "sp" }), rollBtn),
      ...rows, more);
  });
  return h("div", { class: "col bench" },
    h("b", {}, "Change log"),
    h("p", { class: "hint" }, "Every command the agent ran, per node and per turn. Rolling back needs the lab to be ready with no agent in it."),
    r.restoring ? h("div", { class: "hint" }, "Rolling back...") : null,
    ...turns);
}

/* Packet capture: tcpdump on one node's interface for a set time. The .pcap downloads for Wireshark. */
function vtCaptures(r) {
  const node = h("select", {}, ...Object.keys(r.nodes).map(n => h("option", { value: n }, n)));
  const iface = h("input", { type: "text", value: "enp0s8", size: 10 });
  const secs = h("input", { type: "number", value: 10, min: 1, max: 120, style: "width:5em" });
  const msg = h("div", { class: "fail" });
  const go = h("button", { onclick: async () => {
    msg.textContent = "";
    try {
      await api(`/api/vmtopo/runs/${r.id}/nodes/${node.value}/capture`, { iface: iface.value.trim(), seconds: +secs.value });
      vtSig.detail = ""; vtLoad();
    } catch (e) { msg.textContent = e.message; }
  } }, "Capture");
  const list = (r.captures || []).slice().reverse().map(c => h("div", { class: c.state === "done" ? "pass" : c.state === "failed" ? "fail" : "hint" },
    `${c.node} ${c.iface} ${c.seconds}s · ${c.state}${c.state === "done" ? ` (${c.size} bytes) ` : c.reason ? ` — ${c.reason} ` : " "}`,
    c.state === "done" ? h("a", { href: `/api/vmtopo/runs/${r.id}/captures/${c.id}` }, "download .pcap") : null));
  return h("div", { class: "col bench" },
    h("b", {}, "Packet capture"),
    h("p", { class: "hint" }, "Records one interface on one node with tcpdump, for the time you choose (up to 2 minutes). Open the .pcap in Wireshark."),
    h("div", { class: "row" }, "Node", node, "Interface", iface, "Seconds", secs, go),
    msg, ...list);
}

function vtSnapshots(r) {
  if (vtSnap.rid !== r.id) vtSnap = { rid: r.id, picks: [], view: null };
  const base = `/api/vmtopo/runs/${r.id}/snapshots`;
  const list = h("div", { class: "col" }, h("p", { class: "hint" }, "Loading snapshots..."));
  const out = h("div", { class: "col" });
  const label = h("input", { placeholder: "label (optional), e.g. before OSPF", maxlength: 80, style: "max-width:260px" });
  const take = h("button", { disabled: !(r.has_vms && VT_SNAP_STATES.includes(r.state)), onclick: async () => {
    try {
      const j = await api(`/api/vmtopo/runs/${r.id}/snapshot`, { label: label.value.trim() || null });
      label.value = "";
      trackJob(j.job, `Snapshot lab ${r.id}`, () => draw());
    } catch (e) { alert(e.message); }
  } }, "Take snapshot");
  const compare = h("button", { onclick: () => show({ kind: "diff", a: vtSnap.picks[0], b: vtSnap.picks[1] }) }, "Compare selected");

  async function show(view) {
    vtSnap.view = view;
    out.replaceChildren(h("p", { class: "hint" }, "Loading..."));
    try {
      if (view.kind === "view") {
        const s = await api(`${base}/${view.id}`);
        out.replaceChildren(h("div", { class: "row" }, h("b", {}, `Snapshot ${s.id}`), h("span", { class: "status" }, s.label || s.trigger),
            h("span", { class: "sp" }), h("button", { onclick: () => { vtSnap.view = null; out.replaceChildren(); } }, "Close")),
          ...Object.entries(s.nodes).map(([node, secs]) => h("details", {},
            h("summary", {}, node, secs._error ? h("span", { class: "fail" }, " unreachable") : ""),
            ...Object.entries(secs).map(([sec, text]) => h("div", { class: "col" }, h("div", { class: "hint" }, sec), h("pre", {}, text))))));
      } else {
        const [a, b] = [view.a, view.b].sort();                   // ids sort by time: older first
        const d = await api(`${base}/${a}/diff/${b}`);
        const colored = text => h("pre", { class: "diff" }, ...text.split("\n").map(l => h("span", {
          class: l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : "" }, l + "\n")));
        out.replaceChildren(h("div", { class: "row" }, h("b", {}, `Changes from ${a} to ${b}`),
            h("span", { class: "status" }, d.changed ? `${d.changed} section${d.changed === 1 ? "" : "s"} changed` : "no changes"),
            h("span", { class: "sp" }), h("button", { onclick: () => { vtSnap.view = null; out.replaceChildren(); } }, "Close")),
          ...Object.entries(d.nodes).map(([node, secs]) => h("div", { class: "col" }, h("b", {}, node),
            ...Object.entries(secs).map(([sec, text]) => h("div", { class: "col" }, h("div", { class: "hint" }, sec), colored(text))))));
      }
    } catch (e) { out.replaceChildren(h("p", { class: "fail" }, e.message)); }
  }

  async function draw() {
    let snaps;
    try { snaps = await api(base); } catch (e) { list.replaceChildren(h("p", { class: "fail" }, e.message)); return; }
    vtSnap.picks = vtSnap.picks.filter(id => snaps.some(s => s.id === id));
    compare.disabled = vtSnap.picks.length !== 2;
    if (!snaps.length) { list.replaceChildren(h("p", { class: "hint" }, "No snapshots yet. One is taken after every agent turn, or take one now.")); return; }
    list.replaceChildren(...snaps.map(s => {
      const pick = h("input", { type: "checkbox", checked: vtSnap.picks.includes(s.id), title: "select two to compare" });
      pick.addEventListener("change", () => {
        vtSnap.picks = pick.checked ? [...vtSnap.picks, s.id].slice(-2) : vtSnap.picks.filter(id => id !== s.id);
        draw();
      });
      return h("div", { class: "row snap" }, pick,
        h("span", { class: "num" }, new Date(s.ts * 1000).toLocaleString()),
        h("span", {}, s.label || s.trigger),
        s.errors.length ? h("span", { class: "fail" }, `unreachable: ${s.errors.join(", ")}`) : "",
        h("span", { class: "sp" }),
        h("button", { onclick: () => show({ kind: "view", id: s.id }) }, "View"),
        h("a", { href: `${base}/${s.id}/zip`, download: "" }, "Download"),
        h("a", { href: `${base}/${s.id}/labfile`, download: "", title: "Export a lab file with this snapshot's configs" }, "Lab file"));
    }));
  }
  draw();
  if (vtSnap.view) show(vtSnap.view);
  return h("div", { class: "col bench" },
    h("div", { class: "row" }, h("b", {}, "Config snapshots"), h("span", { class: "sp" }), label, take, compare,
      h("a", { href: `/api/vmtopo/runs/${r.id}/labfile`, download: "", title: "This lab's topology and the newest snapshot's configs, as a file you can rebuild the lab from" }, "Export lab file")),
    h("p", { class: "hint" }, "Every node's addresses, routes, forwarding, bridges and VLANs, firewall rules and service configs (netplan, FRR, nginx). Snapshots are kept after the lab's VMs are gone; Delete removes them."),
    list, out);
}

/* A topology that isn't built yet, ready to draw: each end of a link gets the interface it will have (a node's lab
   links are its NICs 2, 3, ... in link order; see vm_runner.lab_iface_names). */
function vtDiagramOf(topo, configs) {
  const slots = [8, 9, 10, 16, 17, 18, 19], used = {};
  const next = n => { const i = used[n] = (used[n] ?? -1) + 1; return i < slots.length ? `enp0s${slots[i]}` : `nic${i + 2}`; };
  const addresses = {};
  for (const [node, secs] of Object.entries(configs || {}))
    for (const line of (secs.addresses || "").split("\n")) {
      const [ifc, , ...rest] = line.trim().split(/\s+/), addrs = rest.filter(a => a.includes("/") && !/^fe80:/i.test(a));
      if (ifc && !["lo", "enp0s3"].includes(ifc.split("@")[0]) && addrs.length) (addresses[node] = addresses[node] || {})[ifc.split("@")[0]] = addrs;
    }
  return { nodes: topo.nodes, links: topo.links.map(l => ({ ...l, a_if: next(l.a), b_if: next(l.b) })), addresses };
}

/* Lab from a diagram: an agent reads your diagram (image, draw.io, Mermaid or text) plus your notes and drafts a
   lab file; you check the drawing, revise it with the agent, then take it into "build from a lab file" above. */
let vtSelDraft = null;
function vtDraftSection() {
  VT.draftsBox = h("div", { class: "col" });
  VT.draftDetail = h("div", { class: "col" });
  const file = h("input", { type: "file", accept: "image/png,image/jpeg,image/gif,image/webp,.drawio,.xml,.mmd,.mermaid,.txt,.md" });
  const notes = h("textarea", { rows: 3, placeholder: "Required: what the diagram can't say. e.g. Boxes marked R are routers, PCs are hosts; use 192.168.10.0/24 for the LAN; the cloud icon is the internet." });
  notes.value = vbDrafts["labdraft"] || "";
  notes.addEventListener("input", () => { vbDrafts["labdraft"] = notes.value; });
  const configured = h("input", { type: "checkbox", checked: true });
  const agents = state.instances.filter(i => i.status === "healthy");
  const sel = h("select", {}, h("option", { value: "" }, agents.length ? "Choose an agent..." : "No running agents"),
    ...agents.map(i => h("option", { value: i.name }, `${i.name} · ${i.modelId}`)));
  const msg = h("div", { class: "fail" });
  const go = h("button", { class: "primary", onclick: async () => {
    msg.textContent = "";
    const f = file.files[0];
    if (!f) { msg.textContent = "Choose the diagram file."; return; }
    if (!notes.value.trim()) { msg.textContent = "Add notes about the diagram."; notes.focus(); return; }
    if (!sel.value) { msg.textContent = "Choose the agent that reads it."; return; }
    const isImage = f.type.startsWith("image/");
    const agent = state.instances.find(i => i.name === sel.value);
    if (isImage && agent && agent.backend !== "cloud") { msg.textContent = "Reading an image needs a cloud agent; local models here read text only (upload a draw.io, Mermaid or text version)."; return; }
    if (f.size > (isImage ? 5e6 : 6e4)) { msg.textContent = isImage ? "Images can be up to 5 MB." : "Text diagrams can be up to 60 KB."; return; }
    const diagram = { name: f.name };
    if (isImage) diagram.data = await new Promise((ok, bad) => {
      const rd = new FileReader(); rd.onload = () => ok(String(rd.result).split(",")[1]); rd.onerror = bad; rd.readAsDataURL(f);
    });
    else diagram.text = await f.text();
    try {
      const res = await api("/api/vmtopo/drafts", { agent: sel.value, diagram, context: notes.value, configured: configured.checked });
      delete vbDrafts["labdraft"]; notes.value = ""; file.value = "";
      vtSelDraft = res.draft; vtSig.drafts = ""; vtLoad();
    } catch (e) { msg.textContent = e.message; }
  } }, "Draft the lab");
  return h("div", { class: "col" },
    h("h5", {}, "Lab from a diagram"),
    h("p", { class: "hint" }, "An agent reads your diagram and drafts a lab file: nodes, links and, if you like, addresses and static routes. Check the drawing, ask for changes, then build it with the form above. Images need a cloud agent; draw.io, Mermaid or a text description work with any agent."),
    file, notes,
    h("label", { class: "row" }, configured, "nodes come up configured (addresses, routes, forwarding); untick for a bare topology"),
    h("div", { class: "row" }, "Agent", sel),
    msg, h("div", { class: "row" }, go),
    VT.draftsBox, VT.draftDetail);
}

function renderVTDrafts() {
  const ds = vtData.drafts || [];
  VT.draftsBox.replaceChildren(...ds.map(d => h("div", { class: "sess" + (d.id === vtSelDraft ? " sel" : ""), onclick: () => { vtSelDraft = d.id; renderVTDrafts(); } },
    h("span", { class: "chip " + (d.state === "drafting" ? "awaiting" : d.state === "ready" ? "running" : "error") }, d.state),
    ` ${d.title || d.diagram_name} — ${d.agent} (${d.agent_model})${d.configured ? "" : " — topology only"} `,
    h("span", { class: "status" }, vtFmtTime(d.created)))));
  loadVTDraft();
}

async function loadVTDraft() {
  if (!vtSelDraft || !(vtData.drafts || []).some(d => d.id === vtSelDraft)) { VT.draftDetail.replaceChildren(); return; }
  let d;
  try { d = await api(`/api/vmtopo/drafts/${vtSelDraft}`); } catch { return; }
  const key = `labdraft/${d.id}`;
  const ask = h("textarea", { class: "al followup", placeholder: d.state === "failed" ? "Optional: anything to add when retrying" : "What should change? e.g. r2 is a firewall; h3 hangs off sw2, not sw1." });
  ask.value = vbDrafts[key] || "";
  ask.addEventListener("input", () => { vbDrafts[key] = ask.value; });
  const msg = h("div", { class: "fail" });
  const act = async (path, body) => {
    msg.textContent = "";
    try { await api(`/api/vmtopo/drafts/${d.id}/${path}`, body || {}); delete vbDrafts[key]; vtSig.drafts = ""; vtLoad(); }
    catch (e) { msg.textContent = e.message; }
  };
  const busy = d.state === "drafting";
  VT.draftDetail.replaceChildren(h("div", { class: "col bench" },
    h("b", {}, d.title || d.diagram_name),
    h("div", { class: "hint" }, `From ${d.diagram_name} · ${d.agent} (${d.agent_model}) · ${d.turns} turn${d.turns === 1 ? "" : "s"} · notes: ${d.context}`),
    busy ? h("div", { class: "hint" }, `${d.agent} is reading the diagram...`) : null,
    d.error ? h("div", { class: "fail" }, d.error) : null,
    d.summary && !busy ? h("div", { class: "hint" }, `Agent: ${d.summary}`) : null,
    d.diagram ? renderTopologyDiagram(d.diagram) : null,
    (d.warnings || []).length ? h("div", { class: "hint" }, "Adjusted: " + d.warnings.join("; ")) : null,
    (d.notes || []).length ? h("div", { class: "col" }, h("div", {}, "Not in the lab file (do it after the build, or have an agent do it):"),
      h("ul", {}, ...d.notes.map(n => h("li", {}, n)))) : null,
    busy ? null : ask,
    msg,
    busy ? null : h("div", { class: "row" },
      d.labfile ? h("button", { class: "primary", onclick: () => {
        VT.fileOn.checked = true; VT.useLabfile(d.labfile);
        if ((d.notes || []).length && !VT.customPrompt.value.trim())
          VT.customPrompt.value = "Finish this lab: " + d.notes.join("; ");
        VT.syncModes(); VT.fileOn.scrollIntoView({ behavior: "smooth", block: "center" });
      } }, "Use as lab file") : null,
      h("button", { onclick: () => act("revise", { text: ask.value }) }, d.state === "failed" ? "Try again" : "Ask for changes"),
      d.labfile ? h("a", { href: `/api/vmtopo/drafts/${d.id}/labfile`, download: "" }, "Download lab file") : null,
      h("button", { onclick: () => act("delete") }, "Delete"))));
}

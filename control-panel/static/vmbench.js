"use strict";
/* VM benchmarks: code-creation tasks run in isolated, throwaway Linux VMs. Loaded after app.js/peers.js (shares
   their globals: h, $, api, state, trackJob, refresh ...). Mirrors peers.js's dialog + polling pattern. */

const vbDlg = h("dialog", { class: "dlg wide" });
document.body.append(vbDlg);
const vbBtn = h("button", { id: "vbBtn", title: "Run code-creation tasks in isolated, throwaway Linux VMs", onclick: () => openVB() }, "VM Labs");
$("header").append(vbBtn);

/* Web terminal login, shared with vmtopo.js. The browser asks for it when the terminal page opens. It used to be put
   in the URL (http://bench:token@...), which Chrome and Edge ignore, leaving a prompt nobody could answer - so it is
   shown here to copy instead. It is fresh for every terminal session and never stored anywhere. */
const termDlg = h("dialog", { class: "dlg" });
document.body.append(termDlg);
function showTerminalLogin(what, t) {
  const url = `http://127.0.0.1:${t.port}/`;
  const user = h("input", { value: "bench", readonly: true, spellcheck: "false" });
  const pw = h("input", { type: "password", value: t.cred, readonly: true, spellcheck: "false", autocomplete: "off" });
  const copyBtn = (field, text) => {
    const b = h("button", { onclick: async () => {
      try { await navigator.clipboard.writeText(text); b.textContent = "Copied"; setTimeout(() => { b.textContent = "Copy"; }, 1500); }
      catch { field.type = "text"; field.select(); }        // no clipboard access: select it for Ctrl+C instead
    } }, "Copy");
    return b;
  };
  const show = h("button", { onclick: () => { pw.type = pw.type === "password" ? "text" : "password"; show.textContent = pw.type === "password" ? "Show" : "Hide"; } }, "Show");
  termDlg.onclose = () => { pw.value = ""; };                  // don't leave the credential sitting in the DOM
  termDlg.replaceChildren(h("h3", {}, "Terminal login"),
    h("p", { class: "hint" }, `${what}. Your browser asks for this login when the terminal opens. It is new for every terminal session and never saved.`),
    h("div", { class: "row" }, "Username", user, copyBtn(user, "bench")),
    h("div", { class: "row" }, "Password", pw, show, copyBtn(pw, t.cred)),
    h("div", { class: "row" }, h("button", { class: "primary", onclick: () => window.open(url, "_blank", "noopener") }, "Open terminal"),
      h("button", { onclick: () => termDlg.close() }, "Close")));
  termDlg.showModal();
}

let vbData = { tasks: [], settings: {}, runs: [] }, vbSelRun = null, vbTimer = null;
let VB = null;
const vbSig = {};
const VB_STATE_LABEL = { queued: "queued", provisioning: "starting VM", ready: "ready", working: "agent working",
  scoring: "scoring", done: "done", error: "error", stopped: "stopped", interrupted: "interrupted (panel restarted)" };
const vbFmtTime = t => t ? new Date(t * 1000).toLocaleTimeString() : "";
const vbElapsed = r => { const end = r.ended || Date.now() / 1000; const s = Math.max(0, Math.round(end - (r.started || r.created))); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${s % 60}s`; };

async function refreshVBBadge() {
  try {
    if (!vbTimer) {
      vbData = await api("/api/vmbench");
      if (typeof vtData !== "undefined") vtData = await api("/api/vmtopo").catch(() => vtData);
    }
    const busy = vbData.runs.filter(r => ["provisioning", "working", "scoring"].includes(r.state)).length +
      (typeof vtData !== "undefined" ? vtData.runs.filter(r => ["provisioning", "working", "scoring"].includes(r.state)).length : 0);
    vbBtn.textContent = busy ? `VM Labs (${busy} running)` : "VM Labs";
  } catch { /* panel may be restarting */ }
}

function openVB() {
  buildVBDialog();
  vbDlg.showModal();
  loadVB();
  if (typeof vtLoad === "function") vtLoad();
  vbTimer = setInterval(() => { loadVB(); if (typeof vtLoad === "function") vtLoad(); }, 2000);
}
vbDlg.addEventListener("close", () => { clearInterval(vbTimer); vbTimer = null; });

function vbAgentCheckboxes() {
  return state.instances.map(i => h("label", { class: "row", style: "justify-content:flex-start" },
    h("input", { type: "checkbox", value: i.name, disabled: i.status !== "healthy" && i.status !== "starting" }),
    `${i.name} (${i.status})${i.backend === "local" && i.model ? " · local: " + i.model : i.backend === "cloud" ? " · cloud" : ""}`));
}

function buildVBDialog() {
  VB = {};
  VB.runsBox = h("div", { class: "col" });
  VB.detail = h("div", { class: "col detail" });

  VB.sMaxC = h("input", { type: "number", min: 1, max: 6, style: "max-width:60px;flex:none" });
  VB.sMem = h("input", { type: "number", min: 512, max: 8192, step: 256, style: "max-width:90px;flex:none" });
  VB.sCpus = h("input", { type: "number", min: 1, max: 4, style: "max-width:60px;flex:none" });
  VB.sKeep = h("input", { type: "checkbox" });
  VB.sMsg = h("div", { class: "fail" });
  const saveSettings = h("button", { onclick: async () => {
    VB.sMsg.textContent = "";
    try {
      await api("/api/vmbench/settings", { max_concurrent: +VB.sMaxC.value, memory_mb: +VB.sMem.value, cpus: +VB.sCpus.value, keep_default: VB.sKeep.checked });
      vbSig.settings = ""; loadVB();
    } catch (e) { VB.sMsg.textContent = e.message; }
  } }, "Save");

  VB.newTask = h("select", {}, h("option", { value: "" }, "— no task: just a scratch VM —"),
    ...vbData.tasks.map(t => h("option", { value: t.id }, `${t.title} (${t.difficulty}, ~${t.est_minutes} min)`)));
  VB.newTaskInfo = h("div", { class: "hint" });
  VB.newTask.addEventListener("change", () => {
    const t = vbData.tasks.find(x => x.id === VB.newTask.value);
    VB.newTaskInfo.textContent = t ? t.prompt : "A plain Ubuntu VM with no task: open its terminal and use it however you like.";
  });
  VB.newAgents = h("div", { class: "col" }, ...vbAgentCheckboxes());
  VB.newKeep = h("input", { type: "checkbox" });
  VB.newMsg = h("div", { class: "fail" });
  const create = h("button", { class: "primary", onclick: async () => {
    VB.newMsg.textContent = "";
    const agents = [...VB.newAgents.querySelectorAll("input:checked")].map(x => x.value);
    const body = { task_id: VB.newTask.value || null, keep: VB.newKeep.checked };
    try {
      if (agents.length > 1) await api("/api/vmbench/benchmarks", { ...body, agents });
      else await api("/api/vmbench/runs", { ...body, agent: agents[0] || null });
      vbSig.runs = ""; loadVB();
    } catch (e) { VB.newMsg.textContent = e.message; }
  } }, "Create");

  vbDlg.replaceChildren(
    h("div", { class: "row" }, h("h3", { style: "flex:1;margin:0" }, "VM Labs: code-creation tasks in isolated Linux VMs"), h("button", { onclick: () => vbDlg.close() }, "Close")),
    h("p", { class: "hint" },
      "Each run is a fresh, throwaway Ubuntu VM (VirtualBox): no shared folders, no network to this computer or to any agent, and no internet at all once it finishes setting up. Open a terminal and do the task yourself, or attach an agent to attempt it. Either way, \"Score now\" runs the task's check script and tells you pass/fail.",
      " An agent needs tool calling to actually work the VM: check a model's status in the Model tab before attaching it — local models here are not yet verified to reliably invoke tools."),
    h("h4", {}, "Settings"),
    h("div", { class: "row" }, "Max VMs at once", VB.sMaxC, "Memory (MB)", VB.sMem, "CPUs", VB.sCpus,
      h("label", { class: "row" }, VB.sKeep, "keep VMs by default"), saveSettings, VB.sMsg),
    h("h4", {}, "New VM / task"),
    h("div", { class: "col" }, h("div", { class: "row" }, "Task", VB.newTask), VB.newTaskInfo,
      h("div", {}, "Attach agent(s) (optional — tick more than one to benchmark them side by side on the same task)"), VB.newAgents,
      h("label", { class: "row" }, VB.newKeep, "keep this VM running afterward, for later inspection"),
      VB.newMsg, h("div", { class: "row" }, create)),
    h("h4", {}, "Runs"), VB.runsBox, VB.detail);
  VB.newTask.dispatchEvent(new Event("change"));
  Object.keys(vbSig).forEach(k => delete vbSig[k]);
  if (typeof vtBuildSection === "function") vbDlg.append(h("hr"), vtBuildSection());
}

async function loadVB() {
  if (!vbDlg.open) return;
  try { vbData = await api("/api/vmbench"); } catch { return; }
  refreshVBBadge();
  const ss = JSON.stringify(vbData.settings);
  if (ss !== vbSig.settings) {
    vbSig.settings = ss;
    VB.sMaxC.value = vbData.settings.max_concurrent; VB.sMem.value = vbData.settings.memory_mb;
    VB.sCpus.value = vbData.settings.cpus; VB.sKeep.checked = vbData.settings.keep_default;
  }
  const rs = JSON.stringify(vbData.runs);
  if (rs !== vbSig.runs) { vbSig.runs = rs; renderVBRuns(); }
  if (vbSelRun) loadVBDetail();
}

function renderVBRuns() {
  if (!vbData.runs.length) { VB.runsBox.replaceChildren(h("p", { class: "hint" }, "No VM runs yet.")); return; }
  const byBench = {};
  for (const r of vbData.runs) (byBench[r.benchmark_id || r.id] = byBench[r.benchmark_id || r.id] || []).push(r);
  const groups = Object.values(byBench);
  VB.runsBox.replaceChildren(...groups.map(grp => {
    const sorted = grp.length > 1 ? [...grp].sort((a, b) => (b.score ? b.score.passed : -1) - (a.score ? a.score.passed : -1)) : grp;
    return h("div", { class: "col", style: grp.length > 1 ? "border:1px solid var(--line);border-radius:8px;padding:6px" : "" },
      grp.length > 1 ? h("div", { class: "hint" }, `Benchmark: ${grp[0].task_title || "task"} across ${grp.length} agents`) : null,
      ...sorted.map(r => h("div", { class: "sess" + (r.id === vbSelRun ? " sel" : ""), onclick: () => { vbSelRun = r.id; vbSig.detail = ""; loadVB(); } },
        h("span", { class: "chip " + r.state }, VB_STATE_LABEL[r.state] || r.state),
        ` ${r.agent ? r.agent : "(no agent)"}${r.task_title ? " — " + r.task_title : " — scratch VM"} `,
        h("span", { class: "status" }, `${vbElapsed(r)}${r.score ? " · " + (r.score.passed ? "PASS" : "FAIL") : ""}`))));
  }));
}

async function loadVBDetail() {
  let r; try { r = await api("/api/vmbench/runs/" + vbSelRun); } catch { vbSelRun = null; return; }
  const key = [r.id, r.state, r.transcript.length, r.terminal_active, r.score && r.score.passed].join("|");
  if (key === vbSig.detail) return;
  vbSig.detail = key;
  const live = ["queued", "provisioning", "working", "scoring"].includes(r.state);
  const canTerminal = ["ready", "working", "scoring", "done"].includes(r.state);
  const canScore = r.task_id && ["ready", "working", "done"].includes(r.state);
  const canStop = live || r.state === "ready";
  const canDelete = !live;

  const termBtn = h("button", { disabled: !canTerminal, onclick: async () => {
    try {
      const t = await api(`/api/vmbench/runs/${r.id}/terminal-start`, {});
      showTerminalLogin(`VM for run ${r.id}`, t);
      vbSig.detail = ""; loadVB();
    } catch (e) { alert(e.message); }
  } }, r.terminal_active ? "Open terminal (running)" : "Open terminal");
  const scoreBtn = h("button", { disabled: !canScore, onclick: () => action(`/api/vmbench/runs/${r.id}/score`, `Score ${r.id}`, {}, () => { vbSig.detail = ""; loadVB(); }) }, "Score now");
  const stopBtn = h("button", { class: "danger", disabled: !canStop, onclick: async () => { try { await api(`/api/vmbench/runs/${r.id}/stop`, {}); vbSig.runs = ""; vbSig.detail = ""; loadVB(); } catch (e) { alert(e.message); } } }, "Stop");
  const delBtn = h("button", { disabled: !canDelete, onclick: async () => {
    if (!confirm("Delete this run's record? (its VM, if any survives, is destroyed first unless you chose \"keep\")")) return;
    try { await api(`/api/vmbench/runs/${r.id}/delete`, {}); if (vbSelRun === r.id) { vbSelRun = null; VB.detail.replaceChildren(); } vbSig.runs = ""; loadVB(); } catch (e) { alert(e.message); }
  } }, "Delete");

  const parts = [h("div", { class: "row" }, h("b", {}, `Run ${r.id}`), h("span", { class: "chip " + r.state }, VB_STATE_LABEL[r.state] || r.state),
    h("span", { class: "status" }, r.reason || ""), h("span", { class: "sp" }), termBtn, scoreBtn, stopBtn, delBtn)];
  parts.push(h("div", { class: "hint" }, `${r.task_title ? "Task: " + r.task_title : "No task (scratch VM)"}${r.agent ? " · agent: " + r.agent : " · no agent attached"} · started ${vbFmtTime(r.started)} · elapsed ${vbElapsed(r)}`));
  if (r.score) {
    parts.push(h("div", { class: r.score.passed ? "pass" : "fail" }, r.score.passed ? "PASS" : "FAIL", ` (${r.score.duration_s}s)`));
    parts.push(h("pre", {}, r.score.output));
  }
  parts.push(h("div", { class: "hint" }, "Live transcript:"));
  parts.push(h("pre", { style: "max-height:30vh" }, r.transcript || "(nothing yet)"));
  VB.detail.replaceChildren(...parts);
}

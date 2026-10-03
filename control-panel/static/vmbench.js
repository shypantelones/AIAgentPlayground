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
let vbTab = (() => { try { return localStorage.getItem("vmlabs-tab") || "network"; } catch { return "network"; } })();
let VB = null;
const vbSig = {};
const vbDrafts = {};          // "<api base>/<run id>" -> unsent follow-up text, kept across the detail views' 2-second redraws
const VB_STATE_LABEL = { queued: "queued", provisioning: "starting VM", ready: "ready", working: "agent working", attached: "session open",
  scoring: "scoring", done: "done", error: "error", stopped: "stopped", interrupted: "interrupted (panel restarted)" };
const vbFmtTime = t => t ? new Date(t * 1000).toLocaleTimeString() : "";
const vbElapsed = r => { const end = r.ended || Date.now() / 1000; const s = Math.max(0, Math.round(end - (r.started || r.created))); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${s % 60}s`; };

async function refreshVBBadge() {
  try {
    if (!vbTimer) {
      vbData = await api("/api/vmbench");
      if (typeof vtData !== "undefined") vtData = await api("/api/vmtopo").catch(() => vtData);
    }
    const busy = vbData.runs.filter(r => ["provisioning", "working", "attached", "scoring"].includes(r.state)).length +
      (typeof vtData !== "undefined" ? vtData.runs.filter(r => ["provisioning", "working", "attached", "scoring"].includes(r.state)).length : 0);
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

/* What this panel has seen a model do in VM Labs: did the agent actually run commands on the machines? */
function evidenceText(ev) {
  if (!ev || !ev.turns) return "not used in VM Labs here yet";
  const scored = ev.scored ? `, passed ${ev.passed} of ${ev.scored} scored runs` : "";
  return `ran commands in ${ev.turns_with_commands} of ${ev.turns} agent turns${scored}`;
}
const EVIDENCE_MARK = { works: "✓", mixed: "∼", unreliable: "⚠", unknown: "" };

function vbAgentCheckboxes() {
  return state.instances.map(i => {
    const ev = i.evidence || {};
    return h("label", { class: "row", style: "justify-content:flex-start" },
      h("input", { type: "checkbox", value: i.name, disabled: i.status !== "healthy" && i.status !== "starting" }),
      `${i.name} (${i.status}) · ${i.modelId || (i.backend === "cloud" ? "cloud" : "local: " + (i.model || "?"))}`,
      h("span", { class: ev.verdict === "unreliable" ? "fail" : ev.verdict === "works" ? "pass" : "hint" },
        ` ${EVIDENCE_MARK[ev.verdict] || ""} ${evidenceText(ev)}`));
  });
}

/* Before attaching agents: ask when one runs on a model that hasn't shown it can drive the VMs. Local models here
   have often described the commands they would run instead of running them; cloud models have been reliable. */
function confirmAgentModels(names) {
  const risky = names.map(n => state.instances.find(i => i.name === n)).filter(i => i && (
    ["unreliable", "mixed"].includes((i.evidence || {}).verdict) ||
    (i.backend !== "cloud" && (i.evidence || {}).verdict === "unknown")));
  if (!risky.length) return true;
  return confirm("Some agents may not actually run commands in the VMs:\n\n" +
    risky.map(i => `• ${i.name} (${i.modelId}): ${evidenceText(i.evidence)}`).join("\n") +
    "\n\nLocal models often describe the commands they would run instead of running them. A cloud model is " +
    "recommended for VM Labs. Continue anyway?");
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
    h("option", { value: "__custom__" }, "✎ Your own prompt for the agent..."),
    ...vbData.tasks.map(t => h("option", { value: t.id }, `${t.title} (${t.difficulty}, ~${t.est_minutes} min)`)));
  VB.newTaskInfo = h("div", { class: "hint" });
  VB.newPrompt = h("textarea", { class: "al", placeholder: "What should the agent do in the VM? e.g. Install nginx and make it serve \"hello\" on port 80, then show me it works with curl.", spellcheck: "true" });
  VB.newTask.addEventListener("change", () => {
    const custom = VB.newTask.value === "__custom__";
    const t = vbData.tasks.find(x => x.id === VB.newTask.value);
    VB.newPrompt.hidden = !custom;
    VB.newTaskInfo.textContent = custom ? "Sent to the agent as its task, with instructions for running commands in the VM. Not scored: there is no check script for your own prompt."
      : t ? t.prompt : "A plain Ubuntu VM with no task: open its terminal and use it however you like.";
  });
  VB.newAgents = h("div", { class: "col" }, ...vbAgentCheckboxes());
  VB.newKeep = h("input", { type: "checkbox" });
  VB.newInteractive = h("input", { type: "checkbox" });
  VB.newMsg = h("div", { class: "fail" });
  const create = h("button", { class: "primary", onclick: async () => {
    VB.newMsg.textContent = "";
    const agents = [...VB.newAgents.querySelectorAll("input:checked")].map(x => x.value);
    const custom = VB.newTask.value === "__custom__";
    if (custom && !VB.newPrompt.value.trim()) { VB.newMsg.textContent = "Write the prompt for the agent."; VB.newPrompt.focus(); return; }
    if ((custom || VB.newInteractive.checked) && !agents.length) { VB.newMsg.textContent = "Tick the agent to attach."; return; }
    const body = { task_id: custom ? null : VB.newTask.value || null, custom_prompt: custom ? VB.newPrompt.value : null,
                   interactive: VB.newInteractive.checked, keep: VB.newKeep.checked };
    try {
      if (agents.length && !confirmAgentModels(agents)) return;
      if (agents.length > 1) await api("/api/vmbench/benchmarks", { ...body, agents });
      else await api("/api/vmbench/runs", { ...body, agent: agents[0] || null });
      if (custom) VB.newPrompt.value = "";
      vbSig.runs = ""; loadVB();
    } catch (e) { VB.newMsg.textContent = e.message; }
  } }, "Create");

  const codingPane = h("div", { class: "col" },
    h("p", { class: "hint" },
      "One throwaway Ubuntu VM per run: a coding task from the catalog, your own prompt for an agent, or a scratch VM to use yourself. Open a terminal and do the task yourself, or attach an agent to attempt it. \"Score now\" runs the task's check script and tells you pass/fail."),
    h("h4", {}, "New VM / task"),
    h("div", { class: "col" }, h("div", { class: "row" }, "Task", VB.newTask), VB.newTaskInfo, VB.newPrompt,
      h("div", {}, "Attach agent(s) (optional — tick more than one to benchmark them side by side on the same task)"),
      h("div", { class: "hint" }, "An agent has to run commands to work a VM, and local models often only describe them: a cloud model is recommended. Each agent shows what its model has done in VM Labs here."), VB.newAgents,
      h("label", { class: "row" }, VB.newInteractive, "interactive session: keep the agent attached after its first reply so you can send it more guidance (ends when you press End session, or after 2 hours with no new message)"),
      h("label", { class: "row" }, VB.newKeep, "keep this VM running afterward, for later inspection"),
      VB.newMsg, h("div", { class: "row" }, create)),
    h("h4", {}, "Runs"), VB.runsBox, VB.detail);
  VB.benchBox = h("div", { class: "col" });
  const benchPane = h("div", { class: "col" },
    h("p", { class: "hint" }, "A benchmark gives one task to several agents at once: tick more than one agent when you create a coding VM or a network lab. Each benchmark is shown here side by side, with every agent's model, its score and how many commands it actually ran. Select a row to open that run."),
    VB.benchBox);
  VB.panes = { network: typeof vtBuildSection === "function" ? vtBuildSection() : h("div"), coding: codingPane, bench: benchPane };
  VB.tabs = h("div", { class: "tabs", role: "tablist" }, ...[["network", "Network labs"], ["coding", "Coding VMs"], ["bench", "Benchmarks"]]
    .map(([k, label]) => h("button", { role: "tab", "data-tab": k, onclick: () => vbShowTab(k) }, label)));

  vbDlg.replaceChildren(
    h("div", { class: "row" }, h("h3", { style: "flex:1;margin:0" }, "VM Labs"), h("button", { onclick: () => vbDlg.close() }, "Close")),
    h("p", { class: "hint" },
      "Real, throwaway VirtualBox VMs: no shared folders, no network to this computer or to any agent, and no internet once they finish setting up. Network labs wire several VMs together; coding VMs are single machines. Both can be benchmarked across agents."),
    h("h4", {}, "Settings (shared by all VM Labs runs)"),
    h("div", { class: "row" }, "Max VMs at once", VB.sMaxC, "Memory (MB)", VB.sMem, "CPUs", VB.sCpus,
      h("label", { class: "row" }, VB.sKeep, "keep VMs by default"), saveSettings, VB.sMsg),
    VB.tabs, VB.panes.network, VB.panes.coding, VB.panes.bench);
  VB.newTask.dispatchEvent(new Event("change"));
  Object.keys(vbSig).forEach(k => delete vbSig[k]);
  vbShowTab(vbTab);
}

function vbShowTab(k) {
  if (!VB.panes[k]) k = "network";
  vbTab = k;
  try { localStorage.setItem("vmlabs-tab", k); } catch { /* per-browser convenience only */ }
  for (const [name, pane] of Object.entries(VB.panes)) pane.hidden = name !== k;
  for (const b of VB.tabs.children) { b.classList.toggle("on", b.dataset.tab === k); b.setAttribute("aria-selected", b.dataset.tab === k); }
}

/* Open a run from the Benchmarks tab in its own tab, selected. */
function vbOpenRun(r) {
  if (r.kind === "network") { vtSelRun = r.id; vtSig.runs = ""; vtSig.detail = ""; vbShowTab("network"); vtLoad(); }
  else { vbSelRun = r.id; vbSig.runs = ""; vbSig.detail = ""; vbShowTab("coding"); loadVB(); }
}

/* Benchmarks tab: every multi-agent run of both kinds, one table per benchmark. Redrawn when either list changes. */
function renderBench() {
  if (!VB || !VB.benchBox) return;
  const runs = [...vbData.runs.map(r => ({ ...r, kind: "coding" })),
                ...(typeof vtData !== "undefined" ? vtData.runs.map(r => ({ ...r, kind: "network" })) : [])].filter(r => r.benchmark_id);
  const sig = JSON.stringify(runs);
  if (sig === vbSig.bench) return;
  vbSig.bench = sig;
  if (!runs.length) { VB.benchBox.replaceChildren(h("p", { class: "hint" }, "No benchmarks yet.")); return; }
  const groups = {};
  for (const r of runs) (groups[r.benchmark_id] = groups[r.benchmark_id] || []).push(r);
  const ordered = Object.values(groups).sort((a, b) => Math.max(...b.map(r => r.created)) - Math.max(...a.map(r => r.created)));
  VB.benchBox.replaceChildren(...ordered.map(grp => {
    const g = grp[0];
    const what = (g.kind === "network" ? `${g.topology_title} · ` : "Coding VM · ") + (g.task_title || (g.custom_prompt ? "your prompt" : "no task"));
    const rows = [...grp].sort((a, b) => (b.score ? +b.score.passed : -1) - (a.score ? +a.score.passed : -1) || a.agent.localeCompare(b.agent));
    return h("div", { class: "col bench" },
      h("div", { class: "row" }, h("b", {}, what), h("span", { class: "status" }, `${grp.length} agents · started ${vbFmtTime(Math.min(...grp.map(r => r.created)))}`)),
      h("div", { class: "bench-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ...["Agent", "Model", "State", "Score", "Commands run", "Time"].map(t => h("th", {}, t)))),
        h("tbody", {}, ...rows.map(r => h("tr", { tabindex: 0, onclick: () => vbOpenRun(r), onkeydown: e => { if (e.key === "Enter") vbOpenRun(r); } },
          h("td", {}, r.agent), h("td", {}, r.agent_model || "?"),
          h("td", {}, h("span", { class: "chip " + r.state }, VB_STATE_LABEL[r.state] || r.state)),
          h("td", { class: r.score ? (r.score.passed ? "pass" : "fail") : "" }, r.score ? (r.score.passed ? "PASS" : "FAIL") : r.task_id ? "—" : "not scored"),
          h("td", { class: "num" }, r.agent_turns ? `${r.agent_commands ?? "?"} in ${r.agent_turns} turn${r.agent_turns === 1 ? "" : "s"}` : "—"),
          h("td", { class: "num" }, vbElapsed(r))))))));
  }));
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
  renderBench();
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
        ` ${r.agent ? r.agent + (r.agent_model ? ` (${r.agent_model})` : "") : "(no agent)"}${r.task_title ? " — " + r.task_title : r.custom_prompt ? " — your prompt" : " — scratch VM"}${r.interactive ? " (session)" : ""} `,
        h("span", { class: "status" }, `${vbElapsed(r)}${r.score ? " · " + (r.score.passed ? "PASS" : "FAIL") : ""}`))));
  }));
}

async function loadVBDetail() {
  let r; try { r = await api("/api/vmbench/runs/" + vbSelRun); } catch { vbSelRun = null; return; }
  const key = [r.id, r.state, r.transcript.length, r.terminal_active, r.score && r.score.passed, (r.conversation || []).length].join("|");
  if (key === vbSig.detail) return;
  vbSig.detail = key;
  const live = ["queued", "provisioning", "working", "attached", "scoring"].includes(r.state);
  const canTerminal = ["ready", "working", "attached", "scoring", "done"].includes(r.state);
  const canScore = r.task_id && ["ready", "working", "attached", "done"].includes(r.state);
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
  parts.push(h("div", { class: "hint" }, `${r.task_title ? "Task: " + r.task_title : r.custom_prompt ? "Your prompt" : "No task (scratch VM)"}${r.agent ? ` · agent: ${r.agent} (${r.agent_model || "?"})` : " · no agent attached"}${r.agent_turns ? ` · ran ${r.agent_commands ?? "?"} commands in ${r.agent_turns} turn${r.agent_turns === 1 ? "" : "s"}` : ""}${r.interactive ? " · interactive session" : ""} · started ${vbFmtTime(r.started)} · elapsed ${vbElapsed(r)}`));
  if (r.custom_prompt) parts.push(h("pre", { style: "max-height:12vh" }, r.custom_prompt));
  if (r.conversation && r.conversation.length) parts.push(sessionConversation(r, "VM"));
  if (r.interactive && ["attached", "working"].includes(r.state))
    parts.push(sessionBox(r, "/api/vmbench/runs", "VM", () => { vbSig.detail = ""; loadVB(); }));
  if (r.score) {
    parts.push(h("div", { class: r.score.passed ? "pass" : "fail" }, r.score.passed ? "PASS" : "FAIL", ` (${r.score.duration_s}s)`));
    parts.push(h("pre", {}, r.score.output));
  }
  parts.push(h("div", { class: "hint" }, "Live transcript:"));
  parts.push(h("pre", { style: "max-height:30vh" }, r.transcript || "(nothing yet)"));
  replaceKeepingFocus(VB.detail, parts);
}

/* Redraw a run's detail view without losing your place in the follow-up box. Shared with vmtopo.js. */
function replaceKeepingFocus(container, parts) {
  const ta = container.querySelector("textarea.followup");
  const caret = ta && document.activeElement === ta ? [ta.selectionStart, ta.selectionEnd] : null;
  container.replaceChildren(...parts);
  const ta2 = container.querySelector("textarea.followup");
  if (ta2 && caret) { ta2.focus(); ta2.setSelectionRange(...caret); }
}

/* Conversation and follow-up box for an interactive session; shared by single VMs and labs (vmtopo.js).
   `what` names the machine(s) in the copy ("VM" or "lab"); `base` is the run type's API path. */
function sessionConversation(r, what) {
  const box = h("div", { class: "msgs", style: "max-height:40vh;border:1px solid var(--line);border-radius:8px;padding:6px" },
    ...r.conversation.map(m => h("div", { class: "msg " + (m.role === "user" ? "user" : m.role === "error" ? "error" : "agent") },
      h("div", { class: "prov" }, m.role === "user" ? "you" : m.role === "error" ? "error" : r.agent), h("div", { class: "txt" }, m.text))));
  if (r.state === "working") box.append(h("div", { class: "msg pending" }, `${r.agent} is working in the ${what}...`));
  setTimeout(() => { box.scrollTop = box.scrollHeight; });
  return h("div", { class: "col" }, h("div", { class: "hint" }, "Conversation:"), box);
}

function sessionBox(r, base, what, reload) {
  const draftKey = `${base}/${r.id}`;
  const ta = h("textarea", { class: "followup", placeholder: r.state === "working"
    ? `More guidance for ${r.agent} (sent once it finishes its current reply)` : `More guidance for ${r.agent} (Ctrl+Enter to send)` });
  ta.value = vbDrafts[draftKey] || "";
  ta.addEventListener("input", () => { vbDrafts[draftKey] = ta.value; });
  const msg = h("div", { class: "fail" });
  const send = h("button", { class: "primary", onclick: async () => {
    const text = ta.value; if (!text.trim()) return;
    msg.textContent = ""; send.disabled = true;
    try { await api(`${base}/${r.id}/message`, { text }); delete vbDrafts[draftKey]; ta.value = ""; reload(); }
    catch (e) { msg.textContent = e.message; }
    finally { send.disabled = false; }
  } }, "Send");
  ta.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send.click(); } });
  const end = h("button", { onclick: async () => {
    const machines = what === "lab" ? "its VMs are" : "the VM is";
    if (!confirm(`End the session? ${r.agent} is detached from the ${what}${r.task_id ? ", the task is scored" : ""}, and ${machines} ${r.keep ? "kept" : "destroyed"}.`)) return;
    try { await api(`${base}/${r.id}/end`, {}); delete vbDrafts[draftKey]; reload(); } catch (e) { msg.textContent = e.message; }
  } }, "End session");
  const idle = r.idle_deadline ? ` Ends by itself at ${vbFmtTime(r.idle_deadline)} if you send nothing more.` : "";
  return h("div", { class: "col" },
    h("div", { class: "compose" }, ta, h("div", { class: "col" }, send, end)), msg,
    h("div", { class: "hint" }, `${r.agent} stays attached to this ${what} until you end the session.${idle}`));
}

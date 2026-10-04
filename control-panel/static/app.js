"use strict";
const $ = (s, el = document) => el.querySelector(s);
function h(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null) e.append(k.nodeType ? k : document.createTextNode(k));
  return e;
}
async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "X-Control-Panel": "1", "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

let state = { instances: [] };
let open = new Set(JSON.parse(localStorage.getItem("openAgents") || "[]"));
const panes = {};               // name -> pane object
const saveOpen = () => localStorage.setItem("openAgents", JSON.stringify([...open]));

/* ---------- jobs tray ---------- */
function trackJob(id, title, onDone) {
  const box = h("div", { class: "job" }, h("div", { class: "t" }, title), h("div", { class: "l" }, "working..."));
  $("#jobs").append(box);
  const tick = async () => {
    try {
      const j = await api("/api/jobs/" + id);
      $(".l", box).textContent = j.lines.slice(-3).join("\n") || "working...";
      if (!j.done) return setTimeout(tick, 1000);
      if (!j.ok) box.classList.add("err");
      onDone && onDone(j);
      refresh();
      setTimeout(() => box.remove(), j.ok ? 4000 : 20000);
      box.addEventListener("click", () => box.remove());
    } catch (e) { $(".l", box).textContent = e.message; setTimeout(() => box.remove(), 8000); }
  };
  tick();
}
async function action(path, title, body = {}, onDone) {
  try { const r = await api(path, body); trackJob(r.job, title, onDone); }
  catch (e) { alert(e.message); }
}

/* ---------- shared model server bar ---------- */
let sigShared = "", sigSide = "";
function renderShared() {
  const s = state.shared; if (!s) return;
  const sig = JSON.stringify(s); if (sig === sigShared) return; sigShared = sig;
  const el = $("#shared"); const keep = $("input", el) ? $("input", el).value : "";
  el.replaceChildren();
  const pull = h("input", { placeholder: "model e.g. qwen3:14b", size: 18, value: keep });
  el.append(
    h("span", {}, "Model server: ", h("b", {}, s.running ? "running" : "stopped"),
      s.running ? ` · ${s.loaded ? "loaded " + s.loaded : "idle (loads on first prompt)"}` : ""),
    h("span", { class: "status", title: s.note || "" }, `${s.modeLabel || ""}${s.note ? " ⚠ " + s.note : ""}${s.models.length ? " · " : ""}` + s.models.map(m => `${m.name} (${m.size})`).join(", ")),
    s.running ? h("button", { onclick: () => action("/api/shared/stop", "Stop model server") }, "Stop")
              : h("button", { class: "primary", onclick: () => action("/api/shared/start", "Start model server") }, "Start"),
    pull, h("button", { onclick: () => pull.value && action("/api/shared/pull", "Download " + pull.value, { model: pull.value }) }, "Download model"));
}

/* ---------- cloud backend fields: shared by the create dialog and the Model tab ---------- */
function cloudFields(cur = {}) {
  const claude = (state.cloudModels || {}).anthropic || [];
  const provider = cur.provider || "anthropic";
  const prov = h("select", {}, ...[["anthropic", "Anthropic (Claude)"], ["openai", "OpenAI"], ["openai-compatible", "OpenAI-compatible (custom URL)"]]
    .map(([v, t]) => h("option", { value: v, selected: v === provider }, t)));
  const modelSel = h("select", {}, ...claude.map(m => h("option", { value: m.id }, m.label)));
  if (provider === "anthropic" && cur.cloudModel) {
    if (!claude.some(m => m.id === cur.cloudModel)) modelSel.append(h("option", { value: cur.cloudModel }, `${cur.cloudModel} (current; not in the list)`));
    modelSel.value = cur.cloudModel;
  }
  const modelTxt = h("input", { placeholder: "model name, exactly as your provider spells it", value: provider !== "anthropic" ? cur.cloudModel || "" : "", autocomplete: "off", spellcheck: "false" });
  const up = h("input", { placeholder: "https://openrouter.ai/api", value: cur.upstream || "", autocomplete: "off" });
  const rate = h("input", { type: "number", min: 1, max: 600, value: cur.rate || 30, style: "max-width:90px;flex:none" });
  const upRow = h("div", { class: "row" }, "Endpoint", up);
  const sync = () => {
    const claudeOn = prov.value === "anthropic";
    modelSel.style.display = claudeOn ? "" : "none"; modelTxt.style.display = claudeOn ? "none" : "";
    upRow.style.display = prov.value === "openai-compatible" ? "" : "none";
  };
  prov.addEventListener("change", sync); sync();
  return {
    rows: [h("div", { class: "row" }, "Provider", prov), h("div", { class: "row" }, "Model", modelSel, modelTxt), upRow,
           h("div", { class: "row" }, "Max requests/min", rate, h("span", { class: "hint" }, "cost guard: the relay throttles the agent"))],
    read: () => ({ provider: prov.value, model: prov.value === "anthropic" ? modelSel.value : modelTxt.value.trim(), upstream: up.value.trim(), rate: +rate.value }),
  };
}

/* ---------- create agent: pick local or cloud before anything is built ---------- */
const newDlg = h("dialog", { class: "dlg" });
document.body.append(newDlg);
function openCreateDialog(name0 = "") {
  let mode = "local";
  const nameIn = h("input", { value: name0, maxlength: 20, placeholder: "new-agent-name", autocomplete: "off", spellcheck: "false" });
  const cf = cloudFields();
  const tok = h("input", { type: "password", placeholder: "paste API key", autocomplete: "new-password", spellcheck: "false" });
  const localPick = h("div", { class: "col" });
  let picker = null;                                                    // set once the model list has loaded
  const pickerReady = buildLocalPicker(localPick, {}, nameIn.value, null, { choose: true }).then(p => picker = p);
  const localBox = h("div", { class: "col" }, h("p", { class: "hint" },
    "Runs on this computer's Ollama. Choose the model it uses (a model that isn't downloaded yet is downloaded when the agent is created). Needs a fairly powerful computer: on slower machines replies can time out. For VM Labs, a cloud model is recommended: local models often describe commands instead of running them."), localPick);
  const cloudBox = h("div", { class: "col" }, ...cf.rows, h("div", { class: "row" }, "API key", tok), h("p", { class: "hint" },
    `The key is ${(state.platform || {}).secretStore || "stored securely"} and held by a separate relay container; the agent only ever gets a dummy key. ` +
    "Cloud models cost money per use: use a spend-capped key. In cloud mode, prompts leave this computer. No local model is downloaded."));
  const sync = () => { localBox.style.display = mode === "local" ? "" : "none"; cloudBox.style.display = mode === "cloud" ? "" : "none"; };
  const modeRow = h("div", { class: "row" }, ...[["local", "Local model (Ollama)"], ["cloud", "Cloud model (API key)"]].map(([v, t]) =>
    h("label", { class: "row" }, h("input", { type: "radio", name: "new-agent-mode", value: v, checked: v === mode, onchange: () => { mode = v; sync(); } }), t)));
  const msg = h("div", { class: "fail" });
  const create = h("button", { class: "primary", onclick: async () => {
    msg.textContent = "";
    const name = nameIn.value.trim().toLowerCase();
    if (!name) { msg.textContent = "Give the agent a name."; return; }
    let local = {};
    if (mode === "local") {
      await pickerReady;
      const e = picker && picker.entry();
      if (!e) { msg.textContent = picker ? "Choose a model for this agent." : "Could not load the model list (see above)."; if (picker) picker.focus(); return; }
      if (e.fit === "toobig" && !confirm(`${e.name} is unlikely to run on this computer. Create the agent with it anyway?`)) return;
      if (!e.installed && !confirm(`Download ${e.name}?\n\nSize: ${GB(e.size_gb)} GB (you have ${GB(picker.freeDisk)} GB free).\nIt is downloaded from ollama.com while the agent is created.`)) return;
      local = { model: e.name };
    }
    const body = Object.assign({ name, backend: mode }, mode === "cloud" ? Object.assign(cf.read(), { token: tok.value.trim() }) : local);
    create.disabled = true;
    try {
      const r = await api("/api/agents", body);
      newDlg.close();
      open.add(name); saveOpen();
      trackJob(r.job, "Create agent " + name, () => refresh());
    } catch (e) { msg.textContent = e.message; }
    finally { create.disabled = false; }
  } }, "Create agent");
  newDlg.onclose = () => { tok.value = ""; };                     // don't leave a pasted key sitting in the DOM
  newDlg.replaceChildren(h("h3", {}, "New agent"), h("div", { class: "row" }, "Name", nameIn),
    h("h4", {}, "Model"), modeRow, localBox, cloudBox, msg,
    h("div", { class: "row" }, create, h("button", { onclick: () => newDlg.close() }, "Cancel")));
  sync();
  newDlg.showModal();
  nameIn.focus();
}

/* ---------- sidebar ---------- */
function renderSidebar() {
  const sig = JSON.stringify(state.instances.map(i => [i.name, i.status])) + [...open]; if (sig === sigSide) return; sigSide = sig;
  const ul = $("#agents"); ul.replaceChildren();
  if (!state.instances.length) ul.append(h("li", { class: "hint" }, "No agents yet."));
  for (const i of state.instances) {
    ul.append(h("li", {},
      h("label", {}, h("input", { type: "checkbox", checked: open.has(i.name), onchange: ev => { ev.target.checked ? open.add(i.name) : open.delete(i.name); saveOpen(); sigSide = ""; syncPanes(); } }),
        h("span", { class: "dot " + i.status }), i.name),
      h("span", { class: "status" }, i.status)));
  }
}

/* ---------- panes ---------- */
function syncPanes() {
  const main = $("#panes");
  for (const n of Object.keys(panes)) if (!open.has(n) || !state.instances.find(i => i.name === n)) { panes[n].destroy(); panes[n].el.remove(); delete panes[n]; }
  for (const n of open) {
    const inst = state.instances.find(i => i.name === n);
    if (inst && !panes[n]) { panes[n] = makePane(n); main.append(panes[n].el); }
    if (inst && panes[n]) panes[n].update(inst);
  }
  const empty = $(".empty", main);
  if (!Object.keys(panes).length) { if (!empty) main.append(h("p", { class: "empty" }, "Tick an agent in the sidebar to open its window.")); }
  else if (empty) empty.remove();
}

// The VM and network labs this agent is working in right now, shown under the agent's header. A click opens it in VM Labs.
function attachmentChips(list) {
  if (!list.length) return [h("div", { class: "hint" }, "Not working in any VM or network lab.")];
  return [h("span", { class: "hint" }, "Working in:"), ...list.map(a => h("button", {
    class: "chip " + a.state, title: "Open in VM Labs",
    onclick: () => { openVB(); vbOpenRun(a); } },
    `${a.kind === "network" ? "Network lab" : "VM"} ${a.id} · ${a.title} · ${a.state}`))];
}

function makePane(name) {
  let inst = state.instances.find(i => i.name === name);
  let tab = "chat", chatId = localStorage.getItem("chat-" + name) || "main", chats = {}, pending = false;
  let redrawMsgs = null, composeTa = null, chatTimer = null;
  const body = h("div", { class: "body" });
  const statusEl = h("span", { class: "status" });
  const dot = h("span", { class: "dot" });
  const link = h("a", { target: "_blank", rel: "noopener" }, "dashboard");
  const btns = h("span", { class: "row" });
  const att = h("div", { class: "att", style: "display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:4px 0" });
  const tabs = h("div", { class: "tabs" });
  const el = h("section", { class: "pane" },
    h("div", { class: "phead" }, dot, h("b", {}, name), statusEl, h("span", { class: "sp" }), link, btns), att, tabs, body);

  const running = () => inst && (inst.status === "healthy" || inst.status === "starting");

  function update(i) {
    inst = i;
    dot.className = "dot " + i.status; statusEl.textContent = i.status + " \u00b7 " + (i.backend === "cloud" ? "cloud: " + i.provider : "local");
    link.href = `http://127.0.0.1:${i.port}/`;
    att.replaceChildren(...attachmentChips(i.attachments || []));
    btns.replaceChildren(
      running() ? h("button", { onclick: () => action(`/api/agents/${name}/stop`, "Stop " + name) }, "Stop")
                : h("button", { class: "primary", onclick: () => action(`/api/agents/${name}/start`, "Start " + name) }, "Start"),
      h("button", { disabled: !running(), onclick: () => action(`/api/agents/${name}/restart`, "Restart " + name) }, "Restart"));
    if (!tabs.children.length) drawTabs();
    const send = $("button.send", body); if (send) send.disabled = !running() || pending;
  }
  function drawTabs() {
    tabs.replaceChildren(...[["chat", "Chat"], ["model", "Model"], ["net", "Network"], ["logs", "Logs"], ["sec", "Security"], ["info", "Info"]].map(([k, t]) =>
      h("button", { class: tab === k ? "on" : "", onclick: () => { tab = k; drawTabs(); drawBody(); } }, t)));
  }
  async function loadChats() { try { chats = await api(`/api/agents/${name}/chats`); } catch { chats = {}; } }

  /* chat tab */
  function drawChat() {
    const sel = h("select", { onchange: () => { chatId = sel.value; localStorage.setItem("chat-" + name, chatId); drawMsgs(); } });
    const ids = Object.keys(chats); if (!ids.includes(chatId)) ids.unshift(chatId);
    ids.forEach(id => sel.append(h("option", { value: id, selected: id === chatId }, (chats[id] && chats[id].title) || "New conversation")));
    const msgs = h("div", { class: "msgs" });
    const ta = h("textarea", { placeholder: "Message this agent (Ctrl+Enter to send)", onkeydown: e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); } } });
    const sendBtn = h("button", { class: "primary send", onclick: send }, "Send");
    function drawMsgs() {
      msgs.replaceChildren();
      for (const m of ((chats[chatId] || {}).messages || [])) msgs.append(msgEl(m, name));
      if (pending) msgs.append(h("div", { class: "msg agent pending" }, "thinking..."));
      msgs.scrollTop = msgs.scrollHeight;
    }
    async function send() {
      const text = ta.value.trim(); if (!text || pending || !running()) return;
      pending = true; sendBtn.disabled = true; ta.value = "";
      (chats[chatId] = chats[chatId] || { title: text.slice(0, 40), messages: [] }).messages.push({ role: "user", text });
      drawMsgs();
      try {
        const r = await api(`/api/agents/${name}/chat`, { chat: chatId, message: text });
        const poll = async () => {
          const j = await api("/api/jobs/" + r.job);
          if (!j.done) return setTimeout(poll, 1000);
          pending = false; await loadChats(); if (tab === "chat") drawChat(); update(inst);
        };
        poll();
      } catch (e) { pending = false; alert(e.message); await loadChats(); drawChat(); }
    }
    body.replaceChildren(
      h("div", { class: "row" }, sel,
        h("button", { onclick: () => { chatId = "c" + Date.now().toString(36); localStorage.setItem("chat-" + name, chatId); drawChat(); } }, "New conversation"),
        h("button", { onclick: async () => { if (confirm("Delete this conversation's history?")) { await api(`/api/agents/${name}/clear-chat`, { chat: chatId }); await loadChats(); chatId = "main"; drawChat(); } } }, "Delete")),
      msgs, h("div", { class: "compose" }, ta, sendBtn));
    redrawMsgs = drawMsgs; composeTa = ta;
    drawMsgs(); sendBtn.disabled = !running() || pending;
    if (!running()) msgs.append(h("div", { class: "hint" }, "Agent is stopped. Press Start to chat."));
  }

  /* model tab: local Ollama <-> cloud API key (held by a relay, never by the agent) */
  function drawModel() {
    const cloud = inst.backend === "cloud";
    let mode = cloud ? "cloud" : "local";
    const modeSel = h("div", { class: "row" },
      ...[["local", "Local (Ollama)"], ["cloud", "Cloud (API key)"]].map(([v, t]) =>
        h("label", { class: "row" }, h("input", { type: "radio", name: "mode-" + name, value: v, checked: v === mode, onchange: () => { mode = v; drawForm(); } }), t)));
    const form = h("div", { class: "col" });
    const msg = h("div", { class: "hint" });
    let apply;                                                  // assigned below; drawForm toggles its visibility
    function drawForm() {
      form.replaceChildren();
      apply.style.display = mode === "cloud" ? "" : "none";     // local mode has its own "Use this model" button
      if (mode === "local") {
        buildLocalPicker(form, inst, name, msg);
      } else {
        const cf = cloudFields(inst);
        const tok = h("input", { type: "password", placeholder: inst.tokenSet ? "key stored - paste a new one to replace" : "paste API key", autocomplete: "new-password", spellcheck: "false" });
        form.append(
          ...cf.rows,
          h("div", { class: "row" }, "API key", tok,
            h("button", { onclick: async () => {
              const v = tok.value.trim(); if (!v) return;
              tok.value = "";
              try { const r = await api(`/api/agents/${name}/token`, { token: v }); trackJob(r.job, "Store key for " + name, () => refresh()); }
              catch (e) { msg.className = "fail"; msg.textContent = e.message; }
            } }, "Save key"),
            inst.tokenSet ? h("button", { class: "danger", onclick: async () => { if (!confirm("Delete the stored key for this agent?")) return; try { await api(`/api/agents/${name}/token-delete`, {}); refresh(); } catch (e) { msg.className = "fail"; msg.textContent = e.message; } } }, "Remove") : null),
          h("div", { class: "status" }, inst.tokenSet ? `A key is stored (${(state.platform || {}).secretStore || "protected"}). It is never shown again.` : "No key stored yet."),
          h("p", { class: "hint" }, "The key is held by a separate relay container. The agent only gets a dummy key, so it cannot read or leak the real one. Cloud models cost money per use: set a spend limit with your provider. In cloud mode, prompts leave this computer."));
        form._read = cf.read;
      }
    }
    apply = h("button", { class: "primary", onclick: () => {
      msg.className = "hint"; msg.textContent = "";
      const body = Object.assign({ backend: mode }, mode === "cloud" ? form._read() : {});
      api(`/api/agents/${name}/model`, body).then(r => trackJob(r.job, `Switch ${name} to ${mode}`, () => refresh())).catch(e => { msg.className = "fail"; msg.textContent = e.message; });
    } }, "Apply");
    body.replaceChildren(modeSel, form, h("div", { class: "row" }, apply), msg);
    drawForm();
  }
  /* network tab */
  async function drawNet() {
    const r = await api(`/api/agents/${name}/allowlist`);
    const ta = h("textarea", { class: "al", spellcheck: "false" }); ta.value = r.text;
    const msg = h("div", { class: "hint" });
    body.replaceChildren(
      h("p", { class: "hint" }, "What this agent may reach on the internet, one entry per line. Everything else is blocked. ",
        "Accepted: domains (example.com, or .example.com to include subdomains), IPv4 addresses (203.0.113.7), ranges (203.0.113.10-203.0.113.20) and CIDR blocks (203.0.113.0/24). Lines starting with # are comments. Local model access is separate and always on."),
      ta, msg,
      h("div", { class: "row" }, h("button", { class: "primary", onclick: async () => {
        msg.className = "hint"; msg.textContent = "saving...";
        try {
          const s = await api(`/api/agents/${name}/allowlist`, { text: ta.value });
          msg.className = "pass";
          msg.textContent = `Saved and applied: ${s.domains.length} domain(s), ${s.ips.length} IP entr${s.ips.length === 1 ? "y" : "ies"}.` + (s.warnings.length ? "\nWarning: " + s.warnings.join("\nWarning: ") : "");
          msg.style.whiteSpace = "pre-wrap";
        } catch (e) { msg.className = "fail"; msg.textContent = e.message; }
      } }, "Save allowlist")));
  }
  /* logs tab */
  async function drawLogs() {
    const pre = h("pre", {}, "loading...");
    const sel = h("select", { onchange: load }, h("option", { value: "proxy" }, "Network requests (proxy)"), h("option", { value: "gateway" }, "Agent gateway"), h("option", { value: "ollama-relay" }, "Model relay"));
    async function load() { try { pre.textContent = (await api(`/api/agents/${name}/logs?which=${sel.value}&tail=300`)).text || "(empty)"; pre.scrollTop = pre.scrollHeight; } catch (e) { pre.textContent = e.message; } }
    body.replaceChildren(h("div", { class: "row" }, sel, h("button", { onclick: load }, "Refresh"), h("span", { class: "hint" }, "TCP_DENIED = blocked request")), pre);
    load();
  }

  /* security tab */
  function drawSec() {
    const out = h("div", {});
    body.replaceChildren(h("p", { class: "hint" }, "Runs live checks that this agent's boundaries hold."),
      h("div", { class: "row" }, h("button", { class: "primary", disabled: !running(), onclick: () => action(`/api/agents/${name}/verify`, "Verify " + name, {}, j => {
        out.replaceChildren(...(j.result || []).map(r => h("div", { class: r.ok ? "pass" : "fail" }, (r.ok ? "PASS  " : "FAIL  ") + r.check)));
      }) }, "Verify isolation")), out);
  }

  /* info tab */
  function drawInfo() {
    const copy = (t) => navigator.clipboard && navigator.clipboard.writeText(t);
    body.replaceChildren(
      h("div", { class: "kv" },
        "Dashboard", h("code", {}, `http://127.0.0.1:${inst.port}/`), h("button", { onclick: () => window.open(`http://127.0.0.1:${inst.port}/`, "_blank") }, "Open"),
        "Login token", h("code", {}, inst.token), h("button", { onclick: () => copy(inst.token) }, "Copy"),
        "Model", h("code", {}, inst.model || "?"), "",
        "Created", h("code", {}, inst.created || "?"), "",
        "Containers", h("code", {}, Object.entries(inst.services).map(([k, v]) => `${k}:${v}`).join("  ")), ""),
      h("hr"),
      h("div", { class: "row" }, h("button", { class: "danger", onclick: () => {
        const c = prompt(`This permanently deletes agent '${name}', its memory, workspace and chats.\nType its name to confirm:`);
        if (c === name) { open.delete(name); saveOpen(); action(`/api/agents/${name}/delete`, "Delete " + name, { confirm: c }); }
      } }, "Delete agent")));
  }

  async function drawBody() {
    if (tab === "chat") { await loadChats(); drawChat(); }
    else if (tab === "model") drawModel();
    else if (tab === "net") drawNet(); else if (tab === "logs") drawLogs();
    else if (tab === "sec") drawSec(); else drawInfo();
  }
  update(inst); drawBody();
  // Forwarded and peer messages are written by the server, so poll for them while the chat tab is showing.
  chatTimer = setInterval(async () => {
    if (tab !== "chat" || pending || document.hidden) return;
    let c; try { c = await api(`/api/agents/${name}/chats`); } catch { return; }
    if (JSON.stringify(c) === JSON.stringify(chats)) return;
    const idsChanged = Object.keys(c).join() !== Object.keys(chats).join();
    chats = c;
    if (idsChanged && !(composeTa && composeTa.value)) drawChat(); else if (redrawMsgs) redrawMsgs();
  }, 3000);
  function goChat(id) { chatId = id; localStorage.setItem("chat-" + name, id); if (tab === "chat") drawBody(); }
  return { el, update, goChat, destroy: () => clearInterval(chatTimer) };
}

/* ---------- boot ---------- */
async function refresh() {
  try {
    state = await api("/api/state");
    const b = $("#banner");
    if (!state.docker) { b.hidden = false; b.textContent = state.error; return; }
    b.hidden = !state.legacy;
    if (state.legacy) b.textContent = "The original single sandbox (platforms/windows/legacy-docker-sandbox) is still running and holds the GPU/port 18789. Stop it with: cd platforms/windows/legacy-docker-sandbox; docker compose down";
    renderShared(); renderSidebar(); syncPanes();
    if (typeof refreshPeerBadge === "function") refreshPeerBadge();
    if (typeof refreshVBBadge === "function") refreshVBBadge();
  } catch (e) { const b = $("#banner"); b.hidden = false; b.textContent = "Cannot reach the control panel server: " + e.message; }
}
$("#newForm").addEventListener("submit", async ev => {
  ev.preventDefault();
  const name = $("#newName").value.trim().toLowerCase();
  $("#newName").value = "";
  openCreateDialog(name);
});
refresh(); setInterval(refresh, 5000);

"use strict";
/* Local model selector for the Model tab: lists models downloaded on the shared Ollama server and a curated set you can
   download, each with its disk size, the memory it needs and whether it fits THIS computer. Cloud models are not listed. */

const GB = n => n == null ? "?" : (n >= 10 ? String(Math.round(n)) : n.toFixed(1));
const FIT_ICON = { gpu: "✓", partial: "∼", cpu: "∼", tight: "∼", toobig: "✗", unknown: "?" };

function modelOptionLabel(e, current, recommended) {
  const need = e.need_gb == null ? "memory ?" : `needs ${e.exact ? "" : "≈"}${GB(e.need_gb)} GB ${e.memory_kind}`;
  const tail = !e.disk_ok ? "not enough disk" : e.embedding_only ? "embedding model (not for chat)" : `${FIT_ICON[e.fit]} ${e.label}`;
  const warn = e.openclaw_tool_calling === "verified_broken" ? "  ⚠ tool calling tested broken"
             : e.openclaw_tool_calling === "verified_working" ? "  ✓ tool calling verified" : "";
  return `${e.name}${e.name === current ? " (in use)" : ""}${e.name === recommended ? " ★ recommended" : ""}  ·  ${GB(e.size_gb)} GB disk  ·  ${need}  ·  ${tail}${warn}`;
}

/* `opts.choose`: picker for the new-agent dialog - starts on "Choose a model...", has no action button of its own, and
   resolves to {value(), entry()} so the dialog can require a choice. Otherwise it's the Model tab's picker for agent `name`. */
async function buildLocalPicker(container, inst, name, msg, opts = {}) {
  container.replaceChildren(h("p", { class: "hint" }, "Looking at this computer and the model server..."));
  let d;
  try { d = await api("/api/models"); }
  catch (e) { container.replaceChildren(h("p", { class: "fail" }, e.message)); return null; }
  const m = d.machine;
  const mem = m.unified_memory ? `${GB(m.ram_gb)} GB unified memory`
    : `${m.vram_gb ? `${m.gpu_name}, ${GB(m.vram_gb)} GB VRAM, ` : ""}${GB(m.ram_gb)} GB RAM`;
  const summary = h("p", { class: "hint" },
    `This computer: ${mem}; ${GB(m.free_disk_gb)} GB free disk. Model server: ${d.modeLabel}. Memory figures assume a ${d.context_tokens / 1024}K-token context. `,
    "≈ means estimated (exact once downloaded). ✓ fits, ∼ runs but slower, ✗ too big.");
  if (!d.server_running) summary.append(" The model server is stopped: start it (top bar) to see downloaded models.");

  const current = inst.model || "";
  const all = [...d.installed, ...d.catalog];
  const sel = h("select", {});
  const group = (label, list) => list.length ? h("optgroup", { label },
    ...list.map(e => h("option", { value: e.name, disabled: e.embedding_only || (!e.installed && !e.disk_ok) }, modelOptionLabel(e, current, d.recommended)))) : null;
  if (opts.choose) sel.append(h("option", { value: "", disabled: true }, "Choose a model..."));
  sel.append(group("Downloaded on this computer", d.installed), group("Available to download", d.catalog));
  const pick = opts.choose ? "" : all.find(e => e.name === current) ? current : (all.find(e => !e.embedding_only) || {}).name;
  if (pick != null) sel.value = pick;

  const detail = h("div", { class: "mdetail" });
  const act = h("button", { class: "primary" }, "Use this model");
  const others = () => state.instances.filter(i => i.name !== name && i.backend === "local" && i.model && i.model !== sel.value);

  function showDetail() {
    const e = all.find(x => x.name === sel.value);
    if (!e) { detail.replaceChildren(d.recommended ? h("div", { class: "hint" }, `★ Recommended for this computer: ${d.recommended}`) : ""); return; }
    const lines = [];
    lines.push(h("div", {}, h("b", {}, e.name), e.params ? ` · ${e.params}` : "", e.quant ? ` · ${e.quant}` : "", e.family ? ` · ${e.family}` : ""));
    lines.push(h("div", {}, `Disk: ${GB(e.size_gb)} GB (${e.installed ? "already downloaded" : "to download"}) · Memory: ${e.need_gb == null ? "unknown" : (e.exact ? "" : "≈") + GB(e.need_gb) + " GB " + e.memory_kind} · `,
      h("span", { class: e.fit === "toobig" ? "fail" : e.fit === "gpu" ? "pass" : "" }, `${FIT_ICON[e.fit]} ${e.label}`)));
    lines.push(h("div", { class: "hint" }, e.tools === true ? "Function calling: this model is built/templated for it (Ollama's listing)."
      : e.tools === false ? "Function calling: NO, this model has no tool-use template at all." : "Function calling: unknown."));
    const tc = e.openclaw_tool_calling;
    lines.push(h("div", { class: tc === "verified_broken" ? "fail" : tc === "verified_working" ? "pass" : "hint" },
      tc === "verified_broken" ? "⚠ Tested through OpenClaw and found BROKEN: it does not reliably invoke tools here (see the note below). Fine for plain chat; not yet suitable for the VM-bench \"agent does the task\" feature."
      : tc === "verified_working" ? "✓ Tested through OpenClaw: it reliably invokes tools here."
      : "Not tested through OpenClaw specifically: \"function calling\" above is the model's own general capability, not a guarantee it works with this harness. Untested models may still work; they just haven't been checked here yet."));
    if (e.note) lines.push(h("div", { class: "hint" }, e.note));
    if (!e.disk_ok) lines.push(h("div", { class: "fail" }, `Not enough free disk (${GB(m.free_disk_gb)} GB free; needs about ${GB(e.size_gb + 2)} GB including headroom).`));
    if (e.fit === "toobig") lines.push(h("div", { class: "fail" }, "This is unlikely to run on this computer."));
    if (e.fit === "partial") lines.push(h("div", { class: "hint" }, "Part of the model will sit in system RAM, so replies will be noticeably slower."));
    const o = others();
    if (o.length) lines.push(h("div", { class: "hint" }, `Other agents use a different local model (${o.map(i => `${i.name}: ${i.model}`).join(", ")}). The shared server holds one model at a time, so it will reload models when agents take turns, which adds a pause to each switch.`));
    detail.replaceChildren(...lines);
    if (opts.choose) return;
    const inUse = e.name === current && inst.backend === "local";
    act.disabled = inUse || e.embedding_only || (!e.installed && !e.disk_ok) || !d.server_running && !e.installed;
    act.textContent = inUse ? "In use" : e.installed ? "Use this model" : `Download (${GB(e.size_gb)} GB) and use`;
  }
  sel.addEventListener("change", showDetail);

  const apply = model => api(`/api/agents/${name}/model`, { backend: "local", model })
    .then(r => trackJob(r.job, `${name} → ${model}`, () => refresh()))
    .catch(e => { msg.className = "fail"; msg.textContent = e.message; });
  act.addEventListener("click", async () => {
    const e = all.find(x => x.name === sel.value); if (!e) return;
    msg.className = "hint"; msg.textContent = "";
    if (e.installed) return apply(e.name);
    if (!confirm(`Download ${e.name}?\n\nSize: ${GB(e.size_gb)} GB (you have ${GB(m.free_disk_gb)} GB free).\nIt is downloaded from ollama.com, then ${name} switches to it.`)) return;
    try {
      const r = await api("/api/shared/pull", { model: e.name });
      trackJob(r.job, `Download ${e.name}`, j => { if (j.ok) apply(e.name); });
    } catch (err) { msg.className = "fail"; msg.textContent = err.message; }
  });

  container.replaceChildren(summary, h("div", { class: "row" }, "Model", sel), detail, opts.choose ? "" : h("div", { class: "row" }, act));
  showDetail();
  return { value: () => sel.value, entry: () => all.find(x => x.name === sel.value), focus: () => sel.focus(), freeDisk: m.free_disk_gb };
}

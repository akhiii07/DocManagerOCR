"use strict";

// Plain JS on purpose: no build step, no npm, no CDN. A no-egress deployment cannot pull
// a framework at runtime, and this page is small enough not to need one.

const MARK = { ok: "✓", attention: "!", blocked: "✕", skipped: "·" };
let polling = null;

// ---------------------------------------------------------------- upload

function wireBoxes() {
  document.querySelectorAll(".box").forEach((box) => {
    const dz = box.querySelector(".dropzone");
    const input = box.querySelector("input[type=file]");
    if (!dz) return;

    dz.addEventListener("click", () => input.click());
    input.addEventListener("change", () => {
      if (input.files.length) upload(box.dataset.box, input.files[0]);
      input.value = "";
    });
    ["dragenter", "dragover"].forEach((e) =>
      dz.addEventListener(e, (ev) => { ev.preventDefault(); dz.classList.add("over"); }));
    ["dragleave", "drop"].forEach((e) =>
      dz.addEventListener(e, () => dz.classList.remove("over")));
    dz.addEventListener("drop", (ev) => {
      ev.preventDefault();
      if (ev.dataTransfer.files.length) upload(box.dataset.box, ev.dataTransfer.files[0]);
    });
  });
}

async function upload(boxKey, file) {
  const box = document.querySelector(`.box[data-box="${boxKey}"]`);
  setBoxProcessing(box, file.name);

  const body = new FormData();
  body.append("box", boxKey);
  body.append("file", file);
  try {
    const res = await fetch("/api/upload", { method: "POST", body });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showBoxError(box, err.detail || `Upload failed (${res.status}).`);
      return;
    }
    startPolling();
  } catch (e) {
    showBoxError(box, "Upload failed: " + e.message);
  }
}

function setBoxProcessing(box, filename) {
  box.dataset.status = "processing";
  box.dataset.hasDoc = "1";
  box.querySelector(".badge").className = "badge processing";
  box.querySelector(".badge").textContent = "reading…";
  box.querySelector(".box-body").innerHTML =
    `<div class="filename"><span>${esc(filename)}</span></div>
     <ul class="stages"><li class="skipped"><span class="mark">·</span>
     <span><span class="stage-label">Working…</span>
     <span class="detail"> reading the document</span></span></li></ul>`;
}

function showBoxError(box, message) {
  box.dataset.status = "blocked";
  box.querySelector(".badge").className = "badge blocked";
  box.querySelector(".badge").textContent = "problem";
  box.querySelector(".box-body").innerHTML =
    `<div class="issues blockedbox">${esc(message)}</div>`;
}

// ---------------------------------------------------------------- polling

function startPolling() {
  refresh();
  if (polling) return;
  polling = setInterval(refresh, 1200);
}

async function refresh() {
  let state;
  try {
    state = await (await fetch("/api/state")).json();
  } catch { return; }

  state.boxes.forEach(renderBox);
  renderFindings(state);

  if (!state.processing && polling) { clearInterval(polling); polling = null; }
}

// ---------------------------------------------------------------- boxes

function renderBox(b) {
  const box = document.querySelector(`.box[data-box="${b.key}"]`);
  if (!box) return;
  box.dataset.status = b.status;
  box.dataset.hasDoc = b.document_id ? "1" : "0";

  const badge = box.querySelector(".badge");
  badge.className = "badge " + b.status;
  badge.textContent = {
    empty: "", processing: "reading…", ok: "✓ checked",
    attention: "needs attention", needs_confirmation: "confirm type",
    blocked: "cannot use",
  }[b.status] || "";

  if (!b.document_id) { box.querySelector(".box-body").innerHTML = ""; return; }

  let html = `<div class="filename"><span>${esc(b.filename || "")}</span>
      <button class="ghost" data-remove="${b.document_id}">remove</button></div>`;

  if (b.stages.length) {
    html += `<ul class="stages">` + b.stages.map((s) =>
      `<li class="${s.status}"><span class="mark">${MARK[s.status] || ""}</span>
       <span><span class="stage-label">${esc(s.label)}</span>
       <span class="detail"> ${esc(s.detail)}</span></span></li>`).join("") + `</ul>`;
  }

  // The one gate: a confident type mismatch holds the document rather than extracting it
  // with the wrong schema.
  if (b.status === "needs_confirmation") {
    html += `<div class="issues"><strong>Is this the right document?</strong>
      <div>${esc(b.issues[0] || "")}</div>
      <div class="actions">
        <button data-confirm="${b.document_id}">Yes, it is a ${esc(b.label)}</button>
        ${b.suggested_type ? `<button class="ghost" data-move="${b.document_id}"
           data-target="${b.suggested_type}">Move to ${esc(b.suggested_label)}</button>` : ""}
      </div></div>`;
  } else if (b.issues.length) {
    html += `<div class="issues${b.status === "blocked" ? " blockedbox" : ""}">
      <ul>${b.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>`;
  }

  if (b.fields.length) {
    html += `<div class="fields">` + b.fields.map((f) =>
      renderField(b, f)).join("") + `</div>`;
  }

  box.querySelector(".box-body").innerHTML = html;
}

// The confirmation step: accept the value, or replace it. A correction becomes a claim
// asserted by the reviewer and re-runs the case checks.
function renderField(b, f) {
  const id = b.document_id;
  const ev = f.evidence
    ? `<a class="ev" data-evidence="${esc(f.evidence)}"
          data-label="${esc(b.label)} — ${esc(f.label)} (page ${f.page})">view on page</a>`
    : "";
  const notes = f.notes.map((n) => `<div class="fnote">${esc(n)}</div>`).join("");

  let mark = "";
  if (f.feedback === "accepted") mark = `<span class="fb ok">✓ accepted</span>`;
  if (f.feedback === "corrected") mark = `<span class="fb edited">✎ corrected</span>`;

  const actions = f.feedback
    ? `<button class="ghost tiny" data-edit="${id}|${esc(f.name)}">change</button>`
    : `<button class="ghost tiny" data-accept="${id}|${esc(f.name)}">accept</button>
       <button class="ghost tiny" data-edit="${id}|${esc(f.name)}">correct</button>`;

  const was = f.original_value
    ? `<div class="fnote was">system read: ${esc(f.original_value)}</div>` : "";

  return `<div class="field" data-field="${esc(f.name)}">
      <span class="fname">${esc(f.label)}</span>
      <span class="fval">${esc(f.value)}</span>
      <span class="conf ${f.confidence}">${f.confidence}</span>${mark}${ev}
      <span class="facts">${actions}</span>
    </div>${was}${notes}
    <div class="editrow" data-editrow="${esc(f.name)}" hidden>
      <input type="text" value="${esc(f.value)}" data-input="${id}|${esc(f.name)}">
      <button data-save="${id}|${esc(f.name)}">Save</button>
      <button class="ghost" data-canceledit="${esc(f.name)}">Cancel</button>
      <div class="editerr"></div>
    </div>`;
}

// ---------------------------------------------------------------- findings

function renderFindings(state) {
  const s = state.summary || {};
  document.getElementById("summary").innerHTML =
    state.documents_present === 0 ? "" :
    `<div class="stat blocker"><b>${s.blockers ?? 0}</b>blockers</div>
     <div class="stat review"><b>${s.review_required ?? 0}</b>to review</div>
     <div class="stat cleared"><b>${s.cleared ?? 0}</b>cleared</div>
     <div class="stat"><b>${s.not_determinable ?? 0}</b>not determinable</div>`;

  const host = document.getElementById("findings");
  const shown = (state.findings || []).filter(
    (f) => f.disposition !== "CLEARED" && f.disposition !== "NOT_APPLICABLE");

  if (!state.documents_present) {
    host.innerHTML = `<p class="empty">Attach a document to begin.</p>`;
  } else if (!shown.length) {
    host.innerHTML = `<p class="empty">Nothing needs attention yet.</p>`;
  } else {
    shown.sort((a, b) => a.order - b.order);
    host.innerHTML = shown.map((f) => `
      <div class="finding ${f.disposition}">
        <div class="f-head">
          <span class="f-disp">${f.disposition.replace("_", " ")}</span>
          <span class="f-title">${esc(f.title)}</span>
          <span class="tag">${f.severity}</span>
          <span class="tag ${f.regulatory ? "reg" : ""}">${
            f.regulatory ? "regulatory" : "business rule"}</span>
          ${f.advisory ? `<span class="tag">advisory</span>` : ""}
        </div>
        <p class="f-msg">${esc(f.message)}</p>
        ${f.recommended_action
          ? `<p class="f-act">${esc(f.recommended_action)}</p>` : ""}
      </div>`).join("");
  }

  const notes = document.getElementById("notes");
  notes.innerHTML = (state.notes || []).map((n) => `<li>${esc(n)}</li>`).join("");
  document.getElementById("notes-wrap").hidden = !(state.notes || []).length;
}

// ---------------------------------------------------------------- actions

document.addEventListener("click", async (ev) => {
  const t = ev.target;

  if (t.dataset.evidence) {
    document.getElementById("evidence-img").src = t.dataset.evidence;
    document.getElementById("evidence-title").textContent = t.dataset.label || "Evidence";
    document.getElementById("evidence").hidden = false;
    return;
  }
  if (t.id === "evidence-close" || t.id === "evidence") {
    document.getElementById("evidence").hidden = true;
    return;
  }
  if (t.dataset.accept) {
    const [id, field] = t.dataset.accept.split("|");
    return post("/api/accept-field", { document_id: id, field });
  }
  if (t.dataset.edit) {
    const field = t.dataset.edit.split("|")[1];
    const row = document.querySelector(`[data-editrow="${CSS.escape(field)}"]`);
    if (row) { row.hidden = false; row.querySelector("input").focus(); }
    return;
  }
  if (t.dataset.canceledit) {
    const row = document.querySelector(`[data-editrow="${CSS.escape(t.dataset.canceledit)}"]`);
    if (row) { row.hidden = true; row.querySelector(".editerr").textContent = ""; }
    return;
  }
  if (t.dataset.save) return saveCorrection(t.dataset.save);

  if (t.dataset.confirm) return post("/api/confirm", { document_id: t.dataset.confirm });
  if (t.dataset.move)
    return post("/api/move", { document_id: t.dataset.move, target: t.dataset.target });
  if (t.dataset.remove) return post("/api/remove", { document_id: t.dataset.remove });
  if (t.id === "reset") {
    await post("/api/reset", {});
    document.querySelectorAll(".box").forEach((b) => {
      b.dataset.status = "empty"; b.dataset.hasDoc = "0";
      b.querySelector(".box-body").innerHTML = "";
      b.querySelector(".badge").textContent = "";
    });
  }
});

async function saveCorrection(key) {
  const [id, field] = key.split("|");
  const row = document.querySelector(`[data-editrow="${CSS.escape(field)}"]`);
  const input = row.querySelector("input");
  const err = row.querySelector(".editerr");
  err.textContent = "";

  const body = new FormData();
  body.append("document_id", id);
  body.append("field", field);
  body.append("value", input.value);

  const res = await fetch("/api/correct-field", { method: "POST", body });
  if (!res.ok) {
    // A correction that cannot be read is refused with the reason, not stored as a guess.
    const data = await res.json().catch(() => ({}));
    err.textContent = data.error || "Could not save that value.";
    return;
  }
  startPolling();
}

async function post(url, fields) {
  const body = new FormData();
  Object.entries(fields).forEach(([k, v]) => body.append(k, v));
  await fetch(url, { method: "POST", body });
  startPolling();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

wireBoxes();
refresh();

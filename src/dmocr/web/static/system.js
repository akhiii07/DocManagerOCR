"use strict";

// System view. Read-only: nothing here changes a case, except the explicit
// "run evaluation" button.

const PANELS = {
  rules: ["/api/system/rules", renderRules],
  regulatory: ["/api/system/regulatory", renderRegulatory],
  trace: ["/api/system/trace", renderTrace],
  verification: ["/api/system/verification", renderVerification],
  evaluation: ["/api/system/evaluation", renderEvaluation],
  "open-items": ["/api/system/open-items", renderOpenItems],
};

let current = "rules";
let evalPoll = null;

async function load(name) {
  const [url, render] = PANELS[name];
  const host = document.getElementById("panel-" + name);
  if (!host.innerHTML) host.innerHTML = `<p class="empty">Loading…</p>`;
  try {
    host.innerHTML = render(await (await fetch(url)).json());
  } catch (e) {
    host.innerHTML = `<p class="empty">Could not load: ${esc(e.message)}</p>`;
  }
}

function show(name) {
  current = name;
  Object.keys(PANELS).forEach((k) => {
    document.getElementById("panel-" + k).hidden = k !== name;
  });
  document.querySelectorAll(".panelnav .tab").forEach((b) =>
    b.classList.toggle("on", b.dataset.panel === name));
  load(name);
}

// ---------------------------------------------------------------- rules

function renderRules(d) {
  if (!d.rules.length) return `<p class="empty">${esc(d.note || "No rules.")}</p>`;

  let html = `<div class="summary">
    <div class="stat"><b>${d.total}</b>rules</div>
    <div class="stat ${d.approved ? "cleared" : "review"}"><b>${d.approved}</b>approved</div>
    <div class="stat"><b>${d.regulatory}</b>regulatory</div>
    <div class="stat"><b>${d.business}</b>business</div>
  </div>`;
  if (d.note) html += `<p class="callout">${esc(d.note)}</p>`;

  html += d.rules.map((r) => {
    const o = r.outcome;
    const disp = o ? o.disposition : "—";
    return `<details class="row">
      <summary>
        <span class="mono">${esc(r.rule_id)}</span>
        <span class="rtitle">${esc(r.title)}</span>
        <span class="tag">${r.severity}</span>
        <span class="tag ${r.regulatory ? "reg" : ""}">${
          r.regulatory ? "regulatory" : "business rule"}</span>
        <span class="tag ${r.status === "APPROVED" ? "" : "warn"}">${r.status}</span>
        ${o ? `<span class="tag ${o.disposition}">${esc(disp)}</span>` : ""}
      </summary>
      <div class="rowbody">
        <div class="kv"><span>Checks</span><code>${esc(r.check)}</code></div>
        ${Object.keys(r.params || {}).length
          ? `<div class="kv"><span>Parameters</span><code>${esc(JSON.stringify(r.params))}</code></div>` : ""}
        ${Object.keys(r.applicability || {}).length
          ? `<div class="kv"><span>Applies when</span><code>${esc(JSON.stringify(r.applicability))}</code></div>` : ""}
        <div class="kv"><span>Determinacy</span><span>${esc(r.determinacy)}</span></div>
        ${o ? `<div class="kv"><span>On this case</span>
            <span><strong>${esc(o.determination)}</strong> &rarr; ${esc(o.disposition)}
            ${o.advisory ? `<span class="tag">advisory</span>` : ""}<br>
            <span class="muted">${esc(o.message)}</span></span></div>`
           : `<div class="kv"><span>On this case</span><span class="muted">not evaluated — no documents yet</span></div>`}
        ${r.citations.length
          ? `<div class="kv"><span>Cites</span><span>${r.citations.map((c) =>
              `<div class="cite ${c.resolves ? "" : "bad"}">
                 <code>${esc(c.id)}</code> ${c.location ? esc(c.location) : ""}
                 ${c.quote ? `<blockquote>${esc(c.quote)}…</blockquote>` : ""}
               </div>`).join("")}</span></div>`
          : `<div class="kv"><span>Cites</span><span class="muted">nothing — this is a
              business rule, not a regulatory checkpoint</span></div>`}
        ${r.recommended_action
          ? `<div class="kv"><span>Action</span><span>${esc(r.recommended_action)}</span></div>` : ""}
      </div></details>`;
  }).join("");
  return html;
}

// ---------------------------------------------------------------- regulatory

function renderRegulatory(d) {
  let html = `<div class="summary">
    <div class="stat"><b>${d.total}</b>requirements</div>
    <div class="stat cleared"><b>${d.rule_ready}</b>rule-ready</div>
    <div class="stat review"><b>${d.blocked}</b>blocked</div>
    <div class="stat"><b>${d.instruments.length}</b>instruments</div>
  </div>
  <p class="callout">A requirement may only become a rule when its source has been read
  from an authoritative copy and it is not flagged for legal review.</p>`;

  html += d.requirements.map((r) => `
    <details class="row ${r.rule_ready ? "" : "blocked"}">
      <summary>
        <span class="mono">${esc(r.id)}</span>
        <span class="tag ${r.rule_ready ? "" : "warn"}">${
          r.rule_ready ? "rule-ready" : "blocked"}</span>
        <span class="tag">${esc(r.feasibility || "")}</span>
        <span class="muted">${esc(r.location || "")}</span>
      </summary>
      <div class="rowbody">
        ${r.blocked_because ? `<p class="callout warn">${esc(r.blocked_because)}</p>` : ""}
        <div class="kv"><span>Instrument</span><span>${esc(r.instrument || r.source)}</span></div>
        <blockquote>${esc(r.quote || "")}</blockquote>
        ${r.notes ? `<div class="kv"><span>Notes</span><span class="muted">${esc(r.notes)}</span></div>` : ""}
      </div></details>`).join("");

  if (d.negative_findings.length) {
    html += `<h3>Negative findings</h3>
      <p class="muted small">What an instrument does NOT say is a finding too — and the
      kind that gets silently re-derived, or invented, if it is not written down.</p>`;
    html += d.negative_findings.map((n) => `
      <details class="row"><summary><span class="mono">${esc(n.id)}</span>
        <span class="muted">${esc((n.conclusion || "").slice(0, 90))}…</span></summary>
        <div class="rowbody">
          <div class="kv"><span>Searched for</span><code>${esc(n.searched_for || "")}</code></div>
          <div class="kv"><span>Conclusion</span><span>${esc(n.conclusion || "")}</span></div>
          <div class="kv"><span>Consequence</span><span>${esc(n.consequence || "")}</span></div>
        </div></details>`).join("");
  }
  return html;
}

// ---------------------------------------------------------------- trace

function renderTrace(d) {
  if (!d.documents.length)
    return `<p class="empty">No documents yet. Upload one on the Review tab.</p>`;

  let html = "";
  if (d.context) {
    html += `<p class="callout">Pinned for reproducibility —
      pipeline ${esc(d.context.pipeline_version)}, rules ${esc(d.context.rule_set_version)},
      OCR ${esc(d.context.models.ocr || "n/a")}, regulation as at
      ${esc(d.context.regulatory_as_of)}.</p>`;
  }

  html += d.documents.map((doc) => {
    const q = doc.quality || {}, r = doc.reading || {}, c = doc.classification || {},
          x = doc.extraction;
    return `<details class="row" open>
      <summary><span class="rtitle">${esc(doc.box)}</span>
        <span class="muted">${esc(doc.filename)}</span>
        <span class="tag ${doc.status}">${esc(doc.status)}</span></summary>
      <div class="rowbody">
        <div class="kv"><span>File</span><span>${(doc.size_bytes/1024).toFixed(0)} KB ·
          sha ${esc(q.sha256 || "")} · safety ${esc(q.safety || "")}</span></div>
        <div class="kv"><span>Quality</span><span>${esc(q.verdict || "")} ·
          ${q.page_count || "?"} page(s) · text layer: ${q.has_text_layer ? "yes" : "no"}
          ${(q.notes || []).length ? `<br><span class="muted">${esc((q.notes||[]).join("; "))}</span>` : ""}</span></div>
        <div class="kv"><span>Reading</span><span>${r.text_layer_pages ?? 0} from text layer,
          ${r.ocr_pages ?? 0} by OCR${r.mean_confidence != null
            ? ` · mean OCR confidence ${(r.mean_confidence*100).toFixed(1)}%` : ""}
          ${(r.failures||[]).length ? `<br><span class="warn">${esc(r.failures.join("; "))}</span>` : ""}</span></div>
        <div class="kv"><span>Classification</span><span>${esc(c.predicted || "—")}
          (${esc(c.confidence || "")}, score ${c.score ?? "—"})
          ${c.unknown_reason ? ` · reason ${esc(c.unknown_reason)}` : ""}
          ${c.scores ? `<br><span class="muted">all scores: ${esc(JSON.stringify(c.scores))}</span>` : ""}</span></div>
        ${(c.signals || []).length ? `<div class="kv"><span>Signals matched</span>
          <span class="muted small">${c.signals.map((s) =>
            `${esc(s.name)} p${s.page} “${esc(s.text)}” +${s.weight}`).join("<br>")}</span></div>` : ""}
        ${x ? `<div class="kv"><span>Extraction</span><span>${x.fields} field(s)
            ${x.missing_required.length
              ? `<br><span class="warn">missing required: ${esc(x.missing_required.join(", "))}</span>` : ""}
            ${x.discarded_ungrounded.length
              ? `<br><span class="warn">discarded (not locatable on the page):
                 ${esc(x.discarded_ungrounded.join("; "))}</span>` : ""}</span></div>
          <div class="kv"><span>Fields</span><span class="muted small">${x.by_field.map((f) =>
            `${esc(f.name)} → ${esc(f.attribute)} · p${f.page} · ${esc(f.confidence)} · via ${esc(f.source)}`
          ).join("<br>")}</span></div>` : ""}
        ${doc.corrections.length ? `<div class="kv"><span>Corrected</span>
          <span>${esc(doc.corrections.join(", "))}</span></div>` : ""}
      </div></details>`;
  }).join("");

  if (d.claims.length) {
    html += `<h3>Canonical claims</h3>
      <p class="muted small">Several sources may assert the same attribute. Disagreement is
      preserved, not resolved — that disagreement is the finding.</p>
      <table class="grid"><thead><tr><th>Attribute</th><th>Claims</th>
        <th>Resolves to</th><th>Why</th></tr></thead><tbody>` +
      d.claims.map((c) => `<tr><td><code>${esc(c.attribute)}</code></td>
        <td>${c.claims}</td>
        <td><span class="tag ${c.determination}">${esc(c.determination)}</span></td>
        <td class="muted">${esc(c.rationale)}</td></tr>`).join("") +
      `</tbody></table>`;
  }
  if (d.parties.length) {
    html += `<h3>Resolved parties</h3>` + d.parties.map((p) =>
      `<div class="kv"><span>${esc(p.roles.join(", "))}</span>
       <span>${esc(p.variants.join("  ·  "))}</span></div>`).join("");
  }
  return html;
}

// ---------------------------------------------------------------- verification

function renderVerification(d) {
  if (!d.planned) return `<p class="empty">${esc(d.note)}</p>`;
  const s = d.summary;

  let html = `<div class="summary">
    <div class="stat"><b>${s.results}</b>results</div>
    <div class="stat ${s.checks_performed ? "cleared" : "review"}"><b>${s.checks_performed}</b>answered</div>
    <div class="stat review"><b>${s.pending_manual}</b>pending</div>
    <div class="stat"><b>${s.source_unavailable}</b>unavailable</div>
  </div>
  <p class="callout">An unavailable source is <strong>never</strong> a compliance failure.
  Unanswered checks count toward case completeness, never toward pass or fail.</p>
  <table class="grid"><thead><tr><th>Source</th><th>Tier</th><th>How</th>
    <th>Would send</th><th>Why</th></tr></thead><tbody>` +
    d.plan.map((p) => `<tr>
      <td><code>${esc(p.source_id)}</code><br><span class="muted small">${esc(p.authority)}</span></td>
      <td>${esc(p.tier)}<br><span class="muted small">${esc(p.tier_confidence)}</span></td>
      <td><span class="tag ${p.execution === "AUTOMATED" ? "" : "warn"}">${esc(p.execution)}</span></td>
      <td><code>${esc(JSON.stringify(p.lookup_keys))}</code>
        ${p.ambiguous_keys.length ? `<br><span class="warn small">disputed, not used:
          ${esc(p.ambiguous_keys.join(", "))}</span>` : ""}</td>
      <td class="muted small">${esc(p.reason)}</td></tr>`).join("") +
    `</tbody></table>`;

  if (d.tasks.length) {
    html += `<h3>Operator tasks</h3>
      <p class="muted small">Only one registered source can be automated. These are the
      rest — the operator supplies access, the system supplies the comparison and audit.</p>`;
    html += d.tasks.map((t) => `<pre class="task">${esc(t.instruction)}</pre>`).join("");
  }
  if (d.notes.length)
    html += `<h3>Notes</h3><ul class="muted small">` +
      d.notes.map((n) => `<li>${esc(n)}</li>`).join("") + `</ul>`;
  return html;
}

// ---------------------------------------------------------------- evaluation

function renderEvaluation(d) {
  let html = `<div class="actions">
    <button id="run-eval" ${d.running ? "disabled" : ""}>
      ${d.running ? "Running…" : "Run evaluation"}</button>
    ${d.running ? `<span class="muted">This takes about a minute.</span>` : ""}
  </div>`;
  if (d.last_error) html += `<p class="callout warn">${esc(d.last_error)}</p>`;
  if (!d.available) return html + `<p class="empty">${esc(d.note)}</p>`;

  const x = d.extraction || {}, c = d.classification || {}, o = d.ocr || {};
  const pct = (v) => v == null ? "n/a" : (v * 100).toFixed(1) + "%";

  html += `<p class="callout warn">${esc(d.caveat)}</p>
    <div class="summary">
      <div class="stat"><b>${d.coverage.documents ?? 0}</b>documents</div>
      <div class="stat"><b>${d.coverage.labelled_fields ?? 0}</b>labelled fields</div>
      <div class="stat cleared"><b>${pct(x.precision)}</b>precision</div>
      <div class="stat cleared"><b>${pct(x.recall)}</b>recall</div>
      <div class="stat ${x.dangerous_error_rate ? "blocker" : "cleared"}">
        <b>${pct(x.dangerous_error_rate)}</b>dangerous errors</div>
    </div>
    <p class="muted small">“Dangerous” means a value that was produced and was wrong —
    the kind that reaches a reviewer as an answer rather than as a gap. A missing value is
    a safe failure and is counted separately.</p>
    <h3>Classification</h3>
    <div class="kv"><span>Accuracy on decided</span><span>${pct(c.accuracy_on_decided)}</span></div>
    <div class="kv"><span>Deferral rate</span><span>${pct(c.deferral_rate)}
      <span class="muted">— routed to a human, not counted as error</span></span></div>
    <h3>OCR</h3>
    <div class="kv"><span>CER / WER</span><span>${pct(o.cer_mean)} / ${pct(o.wer_mean)}
      <span class="muted">— a large gap means characters read but word boundaries lost</span></span></div>`;

  const fields = Object.entries(d.by_field || {});
  if (fields.length) {
    html += `<h3>By field</h3><table class="grid"><thead><tr><th>Field</th><th>Correct</th>
      <th>Near</th><th>Wrong</th><th>Missing</th><th>Spurious</th></tr></thead><tbody>` +
      fields.map(([n, v]) => `<tr><td><code>${esc(n)}</code></td><td>${v.correct}</td>
        <td>${v.near}</td><td class="${v.wrong ? "warn" : ""}">${v.wrong}</td>
        <td>${v.missing}</td><td class="${v.spurious ? "warn" : ""}">${v.spurious}</td></tr>`).join("") +
      `</tbody></table>`;
  }
  if ((d.notes || []).length)
    html += `<h3>Notes</h3><ul class="muted small">` +
      d.notes.map((n) => `<li>${esc(n)}</li>`).join("") + `</ul>`;
  return html;
}

// ---------------------------------------------------------------- open items

function renderOpenItems(d) {
  if (!d.items.length) return `<p class="empty">${esc(d.note || "None.")}</p>`;
  let html = `<div class="summary">
    <div class="stat review"><b>${d.open}</b>open</div>
    <div class="stat cleared"><b>${d.closed}</b>closed</div>
  </div>`;
  const sections = [...new Set(d.items.map((i) => i.section))];
  for (const s of sections) {
    html += `<h3>${esc(s)}</h3><table class="grid"><tbody>` +
      d.items.filter((i) => i.section === s).map((i) => `
        <tr class="${i.closed ? "done" : ""}">
          <td class="num">${esc(i.number)}</td>
          <td>${i.closed ? "<s>" : ""}${esc(i.item)}${i.closed ? "</s>" : ""}
            <div class="muted small">${esc(i.detail)}</div></td>
        </tr>`).join("") + `</tbody></table>`;
  }
  return html;
}

// ---------------------------------------------------------------- wiring

document.addEventListener("click", async (ev) => {
  const t = ev.target;
  if (t.dataset.panel) return show(t.dataset.panel);
  if (t.id === "refresh") return load(current);
  if (t.id === "run-eval") {
    t.disabled = true; t.textContent = "Running…";
    await fetch("/api/system/run-evaluation", { method: "POST" });
    if (!evalPoll) {
      evalPoll = setInterval(async () => {
        const d = await (await fetch("/api/system/evaluation")).json();
        if (!d.running) { clearInterval(evalPoll); evalPoll = null; load("evaluation"); }
      }, 2000);
    }
  }
});

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

show("rules");

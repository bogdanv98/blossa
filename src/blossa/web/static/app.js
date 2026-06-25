// Copyright (c) 2026 Bogdan Voinea · SPDX-License-Identifier: AGPL-3.0-only
"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else n.setAttribute(k, v);
  }
  for (const kid of kids) n.append(kid);
  return n;
};

let MAP = null;

// --- tabs -------------------------------------------------------------------
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("#tab-" + btn.dataset.tab).classList.add("active");
  });
});

// --- load the map -----------------------------------------------------------
async function loadMap() {
  const res = await fetch("/api/map");
  MAP = await res.json();
  const label = MAP.multi_schema
    ? `${MAP.schema_name} · ${MAP.table_count} tables`
    : `${MAP.schema_name} · ${MAP.table_count} tables · ${MAP.provider}`;
  $("#schema-name").textContent = label;
  renderTableList(MAP.tables);
  renderPrograms(MAP.programs || []);
}

// --- ask --------------------------------------------------------------------
$("#ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = $("#question").value.trim();
  if (!question) return;
  setStatus("#ask-status", "Translating your question to SQL…");
  $("#answer").classList.add("hidden");
  $("#ask-btn").disabled = true;
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Ask failed.");
    if (!data.sql || !data.sql.trim()) {
      // No SQL: a plain-language answer (e.g. "what does this procedure do") or a genuine
      // "can't answer". The model's explanation is the response — show it, not as an error.
      setStatus("#ask-status", data.explanation || "I couldn't turn that into a query.");
      return;
    }
    showAnswer(data);
    setStatus("#ask-status", "");
    runSql(); // auto-run; the SQL stays visible and editable for re-running
  } catch (err) {
    setStatus("#ask-status", err.message, true);
  } finally {
    $("#ask-btn").disabled = false;
  }
});

function showAnswer(data) {
  $("#answer").classList.remove("hidden");
  $("#sql").value = data.sql;
  $("#explanation").textContent = data.explanation || "";
  const badge = $("#confidence");
  badge.textContent = data.confidence || "";
  badge.className = "badge " + (data.confidence || "");
  const ul = $("#assumptions");
  ul.replaceChildren();
  (data.assumptions || []).forEach((a) => ul.append(el("li", { text: a })));
  $("#results").replaceChildren();
  setStatus("#run-status", "");
}

$("#run-btn").addEventListener("click", runSql);

async function runSql() {
  const sql = $("#sql").value.trim();
  if (!sql) return;
  setStatus("#run-status", "Running…");
  $("#run-btn").disabled = true;
  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql, max_rows: 100 }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Query failed.");
    renderResults(data);
    setStatus("#run-status", "");
  } catch (err) {
    setStatus("#run-status", err.message, true);
    $("#results").replaceChildren();
  } finally {
    $("#run-btn").disabled = false;
  }
}

function renderResults(data) {
  const box = $("#results");
  box.replaceChildren();
  if (!data.rows.length) {
    box.append(el("p", { class: "muted", text: "No rows returned." }));
    return;
  }
  const isNum = data.rows[0].map((v) => typeof v === "number");
  const thead = el("tr");
  data.columns.forEach((c, i) =>
    thead.append(el("th", { class: isNum[i] ? "num" : "", text: c }))
  );
  const tbody = el("tbody");
  data.rows.forEach((row) => {
    const tr = el("tr");
    row.forEach((v, i) =>
      tr.append(el("td", { class: isNum[i] ? "num" : "", text: v === null ? "" : String(v) }))
    );
    tbody.append(tr);
  });
  const table = el("table", {}, el("thead", {}, thead), tbody);
  box.append(table);
  const note = data.capped
    ? `${data.row_count} rows (capped at 100). Shown to you only — not sent to the model.`
    : `${data.row_count} row(s). Shown to you only — not sent to the model.`;
  box.append(el("p", { class: "muted result-note small", text: note }));
}

// --- schema browser ---------------------------------------------------------
function renderTableList(tables) {
  const ul = $("#tables");
  ul.replaceChildren();
  tables.forEach((t) => {
    const li = el("li", { text: t.name });
    li.addEventListener("click", () => {
      document.querySelectorAll("#tables li").forEach((x) => x.classList.remove("active"));
      li.classList.add("active");
      renderTableDetail(t);
    });
    ul.append(li);
  });
}

$("#table-search").addEventListener("input", (e) => {
  const q = e.target.value.toLowerCase();
  const filtered = MAP.tables.filter((t) => t.name.toLowerCase().includes(q));
  renderTableList(filtered);
});

function renderTableDetail(t) {
  const box = $("#table-detail");
  box.replaceChildren();
  box.append(el("h2", { text: t.name }));
  if (t.purpose)
    box.append(el("p", { class: "purpose", text: `${t.purpose} (${t.purpose_confidence})` }));
  if (t.comment) box.append(el("p", { class: "muted", text: `Documented: ${t.comment}` }));
  const meta = [];
  if (t.num_rows !== null && t.num_rows !== undefined) meta.push(`${t.num_rows} rows (approx.)`);
  if (meta.length) box.append(el("p", { class: "muted small", text: meta.join(" · ") }));

  const head = el(
    "tr", {},
    el("th", { text: "Column" }), el("th", { text: "Type" }),
    el("th", { text: "Key" }), el("th", { text: "Null" }),
    el("th", { text: "Inferred meaning" }), el("th", { text: "Conf." })
  );
  const body = el("tbody");
  t.columns.forEach((c) => {
    body.append(
      el("tr", {},
        el("td", {}, el("code", { text: c.name })),
        el("td", { text: c.type }),
        el("td", {}, c.key ? el("span", { class: "pill", text: c.key }) : el("span", { text: "" })),
        el("td", { text: c.nullable ? "yes" : "no" }),
        el("td", { text: c.comment || c.meaning || "—" }),
        el("td", {}, c.confidence ? el("span", { class: "badge " + c.confidence, text: c.confidence }) : el("span", { text: "" }))
      )
    );
  });
  box.append(el("table", {}, el("thead", {}, head), body));

  appendRels(box, "References out", t.references_out);
  appendRels(box, "Referenced by", t.references_in);
  if (t.findings.length) {
    box.append(el("h3", { text: "Findings" }));
    const ul = el("ul", { class: "rels" });
    t.findings.forEach((f) => ul.append(el("li", { text: f })));
    box.append(ul);
  }
}

function appendRels(box, title, items) {
  if (!items.length) return;
  box.append(el("h3", { text: title }));
  const ul = el("ul", { class: "rels" });
  items.forEach((r) => ul.append(el("li", { text: r })));
  box.append(ul);
}

// --- program logic ----------------------------------------------------------
function renderPrograms(programs) {
  const box = $("#programs");
  box.replaceChildren();
  if (!programs.length) {
    box.append(el("p", { class: "muted", text: "No stored program units were found (or the scan ran without a model to read their logic)." }));
    return;
  }
  programs.forEach((p) => {
    const card = el("div", { class: "program-card" });
    const head = el("div", { class: "program-head" },
      el("code", { text: p.name }),
      el("span", { class: "pill", text: p.kind }),
      p.confidence ? el("span", { class: "badge " + p.confidence, text: p.confidence }) : el("span", {})
    );
    card.append(head);
    card.append(el("p", { text: p.summary || "—" }));
    if (p.tables_used && p.tables_used.length)
      card.append(el("p", { class: "muted small", text: "Tables: " + p.tables_used.join(", ") }));
    box.append(card);
  });
}

$("#logic-search").addEventListener("input", (e) => {
  const q = e.target.value.toLowerCase();
  const all = MAP.programs || [];
  renderPrograms(all.filter((p) =>
    p.name.toLowerCase().includes(q) ||
    (p.summary || "").toLowerCase().includes(q) ||
    (p.tables_used || []).join(" ").toLowerCase().includes(q)
  ));
});

// --- helpers ----------------------------------------------------------------
function setStatus(sel, msg, isError = false) {
  const n = $(sel);
  n.textContent = msg;
  n.classList.toggle("error", isError);
}

loadMap().catch((e) => setStatus("#ask-status", "Could not load the map: " + e.message, true));

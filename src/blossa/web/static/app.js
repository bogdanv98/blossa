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
  renderTree();
  renderTableList(MAP.tables);
  renderPrograms(MAP.programs || []);
  renderProcesses(MAP.scheduler_jobs || [], MAP.scheduler_chains || []);
  renderLogs(MAP.log_tables || []);
}

// --- ask --------------------------------------------------------------------
// Multi-turn refine: prior turns (question + the SQL the model produced) are sent back so a
// follow-up like "now break it down by year" can build on the last query. Only questions and SQL
// travel back to the model — never query results — so the no-raw-rows boundary holds across turns.
let CONVERSATION = []; // confirmed earlier turns: [{question, sql}]
let pendingTurn = null; // the latest answered turn, not yet folded into CONVERSATION

function foldPending() {
  if (!pendingTurn) return;
  // Respect manual SQL edits: if the answer panel is showing this turn's query, record what's in
  // the box now (the user may have tweaked it before refining).
  const shown = !$("#answer").classList.contains("hidden");
  const sql = shown ? $("#sql").value.trim() : pendingTurn.sql;
  CONVERSATION.push({ question: pendingTurn.question, sql });
  pendingTurn = null;
}

$("#ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = $("#question").value.trim();
  if (!question) return;
  foldPending(); // the previous turn becomes history before we ask the next one
  setStatus("#ask-status", "Translating your question to SQL…");
  $("#answer").classList.add("hidden");
  $("#ask-btn").disabled = true;
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, history: CONVERSATION }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Ask failed.");
    if (data.kind === "code") {
      // A request to BUILD something. The answer is code for a human to review and run
      // elsewhere — Blossa never executes it, so there is no Run button here.
      showGeneratedCode(question, data);
      pendingTurn = { question, sql: "" };
      renderThread();
      $("#question").value = "";
      setStatus("#ask-status", "");
      return;
    }
    if (!data.sql || !data.sql.trim()) {
      // No SQL: a plain-language answer (e.g. "what does this procedure do") or a genuine
      // "can't answer". The model's explanation is the response — show it, not as an error.
      setStatus("#ask-status", data.explanation || "I couldn't turn that into a query.");
      pendingTurn = { question, sql: "" }; // still part of the thread for the next follow-up
      renderThread();
      return;
    }
    pendingTurn = { question, sql: data.sql };
    showAnswer(data);
    renderThread();
    $("#question").value = "";
    setStatus("#ask-status", "");
    runSql(); // auto-run; the SQL stays visible and editable for re-running
  } catch (err) {
    setStatus("#ask-status", err.message, true);
  } finally {
    $("#ask-btn").disabled = false;
  }
});

// "New thread" forgets the conversation so the next question starts fresh.
$("#new-thread-btn").addEventListener("click", () => {
  CONVERSATION = [];
  pendingTurn = null;
  $("#answer").classList.add("hidden");
  $("#question").value = "";
  setStatus("#ask-status", "");
  renderThread();
  $("#question").focus();
});

function renderThread() {
  const box = $("#thread");
  box.replaceChildren();
  const hasConvo = CONVERSATION.length > 0 || pendingTurn !== null;
  $("#new-thread-btn").classList.toggle("hidden", !hasConvo);
  $("#question").placeholder = hasConvo
    ? "Refine it — e.g. now break it down by year, or only the top 5…"
    : "Ask in plain language, e.g. how many employees per department?";
  CONVERSATION.forEach((t) => {
    const turn = el("div", { class: "turn" }, el("p", { class: "turn-q", text: t.question }));
    if (t.sql) turn.append(el("pre", { class: "turn-sql", text: t.sql }));
    box.append(turn);
  });
}

// Generated code lives in its own pane: copyable, never runnable from here. The Run button and
// the editable SQL box stay hidden, because this is not a query — it is a change for a human to
// review and apply with their own tool.
function showGeneratedCode(question, data) {
  const box = $("#generated");
  box.replaceChildren();
  box.classList.remove("hidden");
  $("#answer").classList.add("hidden");

  const label = [data.object_type, data.object_name].filter(Boolean).join(" ") || "Generated code";
  const head = el("div", { class: "sql-head" }, el("h3", { text: label }));
  if (data.confidence)
    head.append(el("span", { class: "badge " + data.confidence, text: data.confidence }));
  box.append(el("p", { class: "current-question", text: question }), head);

  if (!data.code || !data.code.trim()) {
    box.append(el("p", { class: "muted", text: data.explanation || "No code was produced." }));
    return;
  }
  box.append(sourcePane(data.code));
  box.append(el("p", { class: "notice", text:
    "Not executed. Blossa only writes this — review it and run it with your own tool." }));
  if (data.explanation) box.append(el("p", { class: "muted", text: data.explanation }));
  (data.warnings || []).forEach((w) =>
    box.append(el("p", { class: "notice warn", text: w })));
  if (data.assumptions && data.assumptions.length) {
    const ul = el("ul", { class: "assumptions" });
    data.assumptions.forEach((a) => ul.append(el("li", { text: a })));
    box.append(ul);
  }
}

function showAnswer(data) {
  $("#generated").classList.add("hidden");
  $("#answer").classList.remove("hidden");
  $("#current-question").textContent = pendingTurn ? pendingTurn.question : "";
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
    renderGrid($("#results"), data, { name: "ask" });
    setStatus("#run-status", "");
  } catch (err) {
    setStatus("#run-status", err.message, true);
    $("#results").replaceChildren();
  } finally {
    $("#run-btn").disabled = false;
  }
}

// --- shared result grid: sortable columns + CSV export ----------------------
// Used by both the Ask tab and the SQL workspace. Rows are shown to the user only — never sent
// back to the model — so sorting/exporting all happen client-side on data already on this page.
function renderGrid(box, data, opts = {}) {
  box.replaceChildren();
  if (!data.rows || !data.rows.length) {
    box.append(el("p", { class: "muted", text: "No rows returned." }));
    return;
  }
  const cols = data.columns;
  const rows = data.rows.slice(); // a copy we can reorder without touching the response
  const isNum = data.rows[0].map((v) => typeof v === "number");
  let sortIdx = -1;
  let sortDir = 1;

  const toolbar = el("div", { class: "grid-toolbar" });
  const note = data.capped
    ? `${data.row_count} rows (capped at 100) · shown to you only, never sent to the model`
    : `${data.row_count} row(s) · shown to you only, never sent to the model`;
  toolbar.append(el("span", { class: "muted small", text: note }));
  const exp = el("button", { class: "ghost small", type: "button", text: "Export CSV" });
  exp.addEventListener("click", () => exportCsv(cols, rows, opts.name || "results"));
  toolbar.append(exp);
  box.append(toolbar);

  const thead = el("thead");
  const tbody = el("tbody");
  const table = el("table", { class: "grid" }, thead, tbody);

  function draw() {
    const tr = el("tr");
    cols.forEach((c, i) => {
      const caret = i === sortIdx ? (sortDir > 0 ? " ▲" : " ▼") : "";
      const th = el("th", { class: (isNum[i] ? "num " : "") + "sortable" }, el("span", { text: c + caret }));
      th.addEventListener("click", () => {
        if (sortIdx === i) sortDir = -sortDir;
        else { sortIdx = i; sortDir = 1; }
        rows.sort((a, b) => cmpCells(a[i], b[i]) * sortDir);
        draw();
      });
      tr.append(th);
    });
    thead.replaceChildren(tr);
    tbody.replaceChildren();
    rows.forEach((row) => {
      const rtr = el("tr");
      row.forEach((v, i) =>
        rtr.append(el("td", { class: isNum[i] ? "num" : "", text: v === null ? "" : String(v) }))
      );
      tbody.append(rtr);
    });
  }
  draw();
  box.append(table);
}

function cmpCells(a, b) {
  if (a === null || a === undefined) return 1; // nulls sort last
  if (b === null || b === undefined) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true });
}

function exportCsv(cols, rows, name) {
  const esc = (v) => {
    if (v === null || v === undefined) return "";
    const s = String(v);
    return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const lines = [cols.map(esc).join(",")];
  rows.forEach((r) => lines.push(r.map(esc).join(",")));
  const blob = new Blob([lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: name + ".csv" });
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// --- SQL workspace ----------------------------------------------------------
// A small IDE surface: an object tree on the left, a SQL editor + result grid on the right, and an
// "Ask AI" bar that drops generated SQL straight into the editor. Everything reuses the existing
// read-only /api/run and /api/ask endpoints — same trust boundary as the rest of the app.
// The object browser lists every DB object, foldered by type: tables, views, the stored program
// units (packages/procedures/functions/triggers) and the rest of the catalog (sequences, synonyms,
// materialized views, types, indexes). Tables & views are queryable (preview); every object can
// show its DDL. `filter` narrows every folder by name.
function renderTree(filter = "") {
  const box = $("#ws-tree");
  box.replaceChildren();
  const q = filter.trim().toLowerCase();
  const match = (n) => n.toLowerCase().includes(q);
  const tables = MAP.tables.filter((t) => match(t.name));
  // A package matches on its routines too — searching "get_balance" should find CORE_BANKING,
  // since that name exists nowhere else in the catalog.
  const progs = (MAP.programs || []).filter(
    (p) => match(p.name) || (p.subprograms || []).some((s) => match(s.name))
  );
  const others = (MAP.other_objects || []).filter((o) => match(o.name));
  const ofKind = (k) => progs.filter((p) => (p.kind || "").toUpperCase() === k);
  const ofType = (t) => others.filter((o) => (o.type || "").toUpperCase() === t);

  const cats = [
    { label: "Tables", items: tables, render: treeTable, open: true },
    { label: "Views", items: ofKind("VIEW"), render: (p) => treeObject(p, "view"), open: true },
    { label: "Packages", items: ofKind("PACKAGE"), render: (p) => treeObject(p, "program") },
    { label: "Procedures", items: ofKind("PROCEDURE"), render: (p) => treeObject(p, "program") },
    { label: "Functions", items: ofKind("FUNCTION"), render: (p) => treeObject(p, "program") },
    { label: "Triggers", items: ofKind("TRIGGER"), render: (p) => treeObject(p, "program") },
    { label: "Materialized views", items: ofType("MATERIALIZED VIEW"), render: treeCatalogObject },
    { label: "Sequences", items: ofType("SEQUENCE"), render: treeCatalogObject },
    { label: "Synonyms", items: ofType("SYNONYM"), render: treeCatalogObject },
    { label: "Types", items: ofType("TYPE"), render: treeCatalogObject },
    { label: "Indexes", items: ofType("INDEX"), render: treeCatalogObject },
    // Scheduler objects. The Processes tab explains what they do; here they are just part of the
    // inventory, so a browsing user can see the schema schedules something at all.
    { label: "Jobs", items: ofType("JOB"), render: treeCatalogObject },
    { label: "Chains", items: ofType("CHAIN"), render: treeCatalogObject },
    { label: "Scheduler programs", items: ofType("PROGRAM"), render: treeCatalogObject },
    { label: "Schedules", items: ofType("SCHEDULE"), render: treeCatalogObject },
  ];
  // Browsing shows EVERY category, empty ones included: "Views 0" tells you this schema has none,
  // where a missing folder only makes you wonder whether the tool can show them at all. While
  // searching, empty folders are noise — the point is then to see what matched.
  let any = false;
  cats.forEach((c) => {
    if (q !== "" && !c.items.length) return;
    if (c.items.length) any = true;
    box.append(treeCategory(c.label, c.items, c.render, c.items.length > 0 && (c.open || q !== "")));
  });
  if (!any) box.append(el("p", { class: "muted small", text: "No objects match." }));
}

function treeCategory(label, items, render, open) {
  const empty = items.length === 0;
  const body = el("div", { class: "ws-cat-body" + (open ? "" : " hidden") });
  items.forEach((it) => body.append(render(it)));
  const tri = el("span", { class: "ws-tri", text: empty ? "" : open ? "▾" : "▸" });
  const head = el("div", { class: "ws-cat" + (empty ? " empty" : "") }, tri,
    el("span", { class: "ws-cat-label", text: label }),
    el("span", { class: "ws-cat-count", text: String(items.length) }));
  if (empty) head.title = `No ${label.toLowerCase()} in the scanned schema(s).`;
  else
    head.addEventListener("click", () => {
      tri.textContent = body.classList.toggle("hidden") ? "▸" : "▾";
    });
  return el("div", { class: "ws-cat-wrap" }, head, body);
}

function treeTable(t) {
  const cols = el("div", { class: "ws-cols hidden" });
  const tri = el("span", { class: "ws-tri", text: "▸" });
  const head = el("div", { class: "ws-thead" }, tri, el("span", { class: "ws-tname", text: t.name }));
  head.addEventListener("click", () => {
    if (!cols.dataset.built) { buildTreeCols(cols, t); cols.dataset.built = "1"; }
    tri.textContent = cols.classList.toggle("hidden") ? "▸" : "▾";
  });
  head.append(iconButton("⤓", "Preview 100 rows", () => previewName(t.name)));
  head.append(iconButton("DDL", "Show CREATE statement", () => showObjectDetail(
    { name: t.name, kind: "TABLE", summary: t.purpose, confidence: t.purpose_confidence }, "table")));
  return el("div", { class: "ws-table" }, head, cols);
}

// A view or a stored program: clicking shows its source/DDL + AI summary; views can also be
// previewed. A package expands to the routines it declares — they are not catalog objects, so
// this tree is the only place they can be browsed.
function treeObject(p, kind) {
  const routines = p.subprograms || [];
  const body = el("div", { class: "ws-cols hidden" });
  const tri = el("span", { class: routines.length ? "ws-tri" : "ws-tri-gap",
    text: routines.length ? "▸" : "" });
  const head = el("div", { class: "ws-thead" }, tri, el("span", { class: "ws-tname", text: p.name }));
  if (p.status && p.status.toUpperCase() === "INVALID")
    head.append(el("span", { class: "pill invalid", title: "The object is INVALID", text: "!" }));
  head.addEventListener("click", () => {
    showObjectDetail(p, kind);
    if (!routines.length) return;
    if (!body.dataset.built) {
      routines.forEach((r) => body.append(treeSubprogram(p, r)));
      body.dataset.built = "1";
    }
    tri.textContent = body.classList.toggle("hidden") ? "▸" : "▾";
  });
  if (kind === "view") head.append(iconButton("⤓", "Preview 100 rows", () => previewName(p.name)));
  return el("div", { class: "ws-table" }, head, body);
}

// A routine inside a package. Clicking inserts the qualified call into the editor, the way
// clicking a column does — there is no DDL of its own to show, only the package's.
function treeSubprogram(pkg, routine) {
  const row = el("div", { class: "ws-col" },
    el("span", { class: "ws-cname", text: routine.name }),
    el("span", { class: "ws-ctype muted", text: (routine.kind || "").toLowerCase() }));
  row.title = routine.does
    ? `${routine.does} (click to insert the call)`
    : `${routine.kind} declared in ${pkg.name} — click to insert the call`;
  row.addEventListener("click", (e) => {
    e.stopPropagation();
    insertAtCursor($("#ws-sql"), `${pkg.name}.${routine.name}`);
  });
  return row;
}

// Sequences, synonyms, materialized views, types, indexes: name + status in the tree, DDL on click.
function treeCatalogObject(o) {
  const head = el("div", { class: "ws-thead" },
    el("span", { class: "ws-tri-gap" }),
    el("span", { class: "ws-tname", text: o.name }));
  if (o.status && o.status.toUpperCase() === "INVALID")
    head.append(el("span", { class: "pill invalid", title: "The object is INVALID", text: "!" }));
  head.addEventListener("click", () =>
    showObjectDetail({ name: o.name, kind: o.type, status: o.status }, "object"));
  if ((o.type || "").toUpperCase() === "MATERIALIZED VIEW")
    head.append(iconButton("⤓", "Preview 100 rows", () => previewName(o.name)));
  return el("div", { class: "ws-table" }, head);
}

function iconButton(text, title, onClick) {
  const b = el("button", { class: "ws-prev", type: "button", title, text });
  b.addEventListener("click", (e) => { e.stopPropagation(); onClick(); });
  return b;
}

// One detail pane for every object kind: what the AI understood (when it looked at it), the
// captured source, and — on demand — the real CREATE statement from the database.
function showObjectDetail(p, kind) {
  const box = $("#ws-detail");
  box.replaceChildren();
  const type = (p.kind || kind).toUpperCase();
  const head = el("div", { class: "ws-detail-head" },
    el("code", { text: p.name }),
    el("span", { class: "pill", text: type.toLowerCase() }));
  if (p.confidence) head.append(el("span", { class: "badge " + p.confidence, text: p.confidence }));
  if (p.status && p.status.toUpperCase() === "INVALID")
    head.append(el("span", { class: "pill invalid", text: "INVALID" }));
  if (kind === "view" || kind === "table" || type === "MATERIALIZED VIEW") {
    const prev = el("button", { class: "ghost small", type: "button", text: "Preview data ▸" });
    prev.addEventListener("click", () => previewName(p.name));
    head.append(prev);
  }
  const ddlBtn = el("button", { class: "ghost small", type: "button", text: "DDL ▸" });
  head.append(ddlBtn);
  box.append(head);

  if (p.summary || kind === "view" || kind === "program") {
    box.append(el("p", { class: p.summary ? "ws-detail-sum" : "muted small",
      text: p.summary || "No AI summary (the scan ran without a model, or produced none)." }));
  }
  if (p.tables_used && p.tables_used.length)
    box.append(el("p", { class: "muted small", text: "Tables used: " + p.tables_used.join(", ") }));
  // A package's routines are not catalog objects, so this list is the only place they show up.
  if (p.subprograms && p.subprograms.length)
    box.append(el("p", { class: "muted small",
      text: "Contains: " + p.subprograms.map((s) => s.name).join(", ") }));
  if (p.source && p.source.trim()) {
    box.append(el("p", { class: "muted small", text: kind === "view" ? "Defining query:" : "Source:" }));
    box.append(sourcePane(p.source));
  }
  const ddlBox = el("div", { class: "ws-ddl" });
  box.append(ddlBox);
  ddlBtn.addEventListener("click", () => loadDdl(p.name, type, ddlBox));
  box.classList.remove("hidden");
  if (!p.source || !p.source.trim()) loadDdl(p.name, type, ddlBox); // nothing else to show
}

// The object's CREATE statement: Oracle's own text via DBMS_METADATA when the account may read it,
// otherwise rebuilt from the scanned map. DDL is structure, never rows.
async function loadDdl(name, type, box) {
  box.replaceChildren();
  const status = el("p", { class: "muted small", text: "Fetching DDL…" });
  box.append(status);
  try {
    const res = await fetch("/api/ddl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, type }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "No DDL available.");
    status.textContent = data.source === "database"
      ? "DDL as Oracle reports it (DBMS_METADATA)."
      : "Rebuilt from the scanned map — the database would not hand over its own DDL.";
    box.append(sourcePane(data.ddl));
  } catch (err) {
    status.textContent = err.message;
    status.className = "status error";
  }
}

// A code block with a copy button — DDL and source are meant to be pasted somewhere.
function sourcePane(text) {
  const copy = el("button", { class: "ghost small", type: "button", text: "Copy" });
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      copy.textContent = "Copied";
    } catch {
      copy.textContent = "Copy failed";
    }
    setTimeout(() => { copy.textContent = "Copy"; }, 1500);
  });
  return el("div", { class: "ws-source-wrap" },
    el("div", { class: "grid-toolbar" }, copy),
    el("pre", { class: "ws-source", text }));
}

function buildTreeCols(box, t) {
  t.columns.forEach((c) => {
    const row = el("div", { class: "ws-col" },
      el("span", { class: "ws-cname", text: c.name }),
      el("span", { class: "ws-ctype muted", text: c.type }));
    if (c.key) row.append(el("span", { class: "pill", text: c.key }));
    row.title = c.meaning || c.comment || "";
    row.addEventListener("click", () => insertAtCursor($("#ws-sql"), c.name));
    box.append(row);
  });
}

function previewName(name) {
  $("#ws-sql").value = `SELECT *\nFROM ${name}\nFETCH FIRST 100 ROWS ONLY`;
  $("#ws-ask-note").textContent = "";
  runWorkspaceSql();
}

function insertAtCursor(ta, text) {
  const s = ta.selectionStart ?? ta.value.length;
  const e = ta.selectionEnd ?? ta.value.length;
  ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
  ta.selectionStart = ta.selectionEnd = s + text.length;
  ta.focus();
}

async function runWorkspaceSql() {
  const sql = $("#ws-sql").value.trim();
  if (!sql) return;
  $("#ws-detail").classList.add("hidden"); // results replace any object-source view
  setStatus("#ws-status", "Running…");
  $("#ws-run-btn").disabled = true;
  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql, max_rows: 100 }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Query failed.");
    renderGrid($("#ws-results"), data, { name: "query" });
    setStatus("#ws-status", "");
  } catch (err) {
    setStatus("#ws-status", err.message, true);
    $("#ws-results").replaceChildren();
  } finally {
    $("#ws-run-btn").disabled = false;
  }
}

$("#ws-run-btn").addEventListener("click", runWorkspaceSql);
$("#ws-sql").addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); runWorkspaceSql(); }
});
$("#ws-search").addEventListener("input", (e) => renderTree(e.target.value));

// Ask AI inside the workspace: NL → SQL dropped into the editor, then auto-run. Single-shot (no
// multi-turn thread here — that lives in the Ask tab); only the question + schema reach the model.
$("#ws-ask-bar").addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = $("#ws-ask").value.trim();
  if (!question) return;
  setStatus("#ws-status", "Asking the model for SQL…");
  $("#ws-ask-btn").disabled = true;
  $("#ws-ask-note").textContent = "";
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Ask failed.");
    if (!data.sql || !data.sql.trim()) {
      $("#ws-ask-note").textContent = data.explanation || "The model didn't produce SQL for that.";
      setStatus("#ws-status", "");
      return;
    }
    $("#ws-sql").value = data.sql;
    const bits = [];
    if (data.explanation) bits.push(data.explanation);
    if (data.assumptions && data.assumptions.length)
      bits.push("Assumptions: " + data.assumptions.join("; "));
    $("#ws-ask-note").textContent = bits.join("  ·  ");
    setStatus("#ws-status", "");
    runWorkspaceSql();
  } catch (err) {
    setStatus("#ws-status", err.message, true);
  } finally {
    $("#ws-ask-btn").disabled = false;
  }
});

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

// --- scheduled processes ----------------------------------------------------
// A chain's order lives in its rules, not its steps, so the rules are rendered as the readable
// part: "when this holds, start that". Steps are shown with the procedure they actually call, so
// a reader can carry the name straight over to the Logic tab.

// "START "A","B"" -> ["A", "B"]; "END 0" -> []. Used only to label a rule as a fan-out.
function ruleTargets(action) {
  const m = /^\s*START\s+(.*)$/i.exec(action || "");
  if (!m) return [];
  return m[1].split(",").map((s) => s.trim().replace(/^"|"$/g, "")).filter(Boolean);
}

function ruleShape(rule) {
  const targets = ruleTargets(rule.action);
  if (/^\s*END\b/i.test(rule.action || "")) return "end";
  if (targets.length > 1) return "fan-out";
  if (/\bAND\b/i.test(rule.condition || "")) return "join";
  if (/\bFAILED\b/i.test(rule.condition || "")) return "on failure";
  return "";
}

function renderProcesses(jobs, chains) {
  const box = $("#processes");
  box.replaceChildren();
  if (!jobs.length && !chains.length) {
    box.append(el("p", { class: "muted", text:
      "This schema schedules nothing — no DBMS_SCHEDULER jobs or chains were found." }));
    return;
  }

  jobs.forEach((j) => {
    const card = el("div", { class: "program-card" });
    card.append(el("div", { class: "program-head" },
      el("code", { text: j.name }),
      el("span", { class: "pill", text: j.job_type || "JOB" }),
      el("span", { class: "badge " + (j.enabled ? "high" : "low"),
                   text: j.state || (j.enabled ? "enabled" : "disabled") })
    ));
    if (j.comment) card.append(el("p", { text: j.comment }));
    const facts = [];
    if (j.job_action) facts.push("Runs: " + j.job_action);
    if (j.repeat_interval) facts.push("Cadence: " + j.repeat_interval);
    if (j.next_run) facts.push("Next: " + j.next_run);
    if (j.last_start) facts.push("Last: " + j.last_start);
    facts.push("Restartable: " + (j.restartable ? "yes" : "no"));
    card.append(el("p", { class: "muted small", text: facts.join(" · ") }));
    box.append(card);
  });

  chains.forEach((c) => {
    const card = el("div", { class: "program-card" });
    card.append(el("div", { class: "program-head" },
      el("code", { text: c.name }),
      el("span", { class: "pill", text: "CHAIN" }),
      el("span", { class: "badge " + (c.enabled ? "high" : "low"),
                   text: c.enabled ? "enabled" : "disabled" })
    ));
    if (c.comment) card.append(el("p", { text: c.comment }));
    card.append(el("p", { class: "muted small",
      text: `${c.steps.length} step(s) · ${c.rules.length} rule(s)` }));

    const steps = el("table", { class: "proc-table" });
    steps.append(el("thead", {}, el("tr", {},
      el("th", { text: "Step" }), el("th", { text: "Calls" }), el("th", { text: "What it does" }))));
    const sbody = el("tbody", {});
    c.steps.forEach((s) => sbody.append(el("tr", {},
      el("td", {}, el("code", { text: s.name })),
      el("td", {}, el("code", { text: s.action || s.program || "—" })),
      el("td", { class: "muted small", text: s.does || "" })
    )));
    steps.append(sbody);
    card.append(steps);

    const rules = el("table", { class: "proc-table" });
    rules.append(el("thead", {}, el("tr", {},
      el("th", { text: "Rule" }), el("th", { text: "When" }), el("th", { text: "Then" }))));
    const rbody = el("tbody", {});
    c.rules.forEach((r) => {
      const shape = ruleShape(r);
      rbody.append(el("tr", {},
        el("td", {}, el("code", { text: r.name })),
        el("td", { class: "small", text: r.condition }),
        el("td", { class: "small" },
          el("code", { text: r.action }),
          shape ? el("span", { class: "pill", text: shape }) : el("span", {}))
      ));
    });
    rules.append(rbody);
    card.append(rules);
    box.append(card);
  });
}

$("#proc-search").addEventListener("input", (e) => {
  const q = e.target.value.toLowerCase();
  const hit = (s) => (s || "").toLowerCase().includes(q);
  renderProcesses(
    (MAP.scheduler_jobs || []).filter((j) => hit(j.name) || hit(j.job_action) || hit(j.comment)),
    (MAP.scheduler_chains || []).filter((c) =>
      hit(c.name) || hit(c.comment) ||
      c.steps.some((s) => hit(s.name) || hit(s.action) || hit(s.does)) ||
      c.rules.some((r) => hit(r.name) || hit(r.condition) || hit(r.action)))
  );
});

// --- application logs -------------------------------------------------------
function renderLogs(logs) {
  const box = $("#logs");
  box.replaceChildren();
  if (!logs.length) {
    box.append(el("p", { class: "muted", text: "No application log/error/audit tables were recognised in this schema." }));
    return;
  }
  logs.forEach((lt) => {
    const card = el("div", { class: "program-card" });
    card.append(el("div", { class: "program-head" },
      el("code", { text: lt.name }),
      el("span", { class: "pill", text: lt.kind }),
      lt.confidence ? el("span", { class: "badge " + lt.confidence, text: lt.confidence }) : el("span", {})
    ));
    const roles = el("div", { class: "log-roles" });
    (lt.columns || []).forEach((c) =>
      roles.append(el("span", { class: "log-role" },
        el("span", { class: "log-role-name", text: c.role.replace(/_/g, " ") }),
        el("code", { text: c.column })
      ))
    );
    card.append(roles);
    if (lt.evidence && lt.evidence.length)
      card.append(el("p", { class: "muted small", text: "Why: " + lt.evidence.join("; ") }));

    const actions = el("div", { class: "log-actions" });

    // Spike trend is deterministic (only counts leave the DB) → offered whenever there's a timestamp.
    const hasTime = (lt.columns || []).some((c) => c.role === "event_time");
    if (hasTime) {
      const spikeOut = el("div", { class: "log-spikes" });
      const sbtn = el("button", { class: "ghost", type: "button", text: "Show spikes" });
      sbtn.addEventListener("click", () => showSpikes(lt.name, sbtn, spikeOut));
      actions.append(sbtn);
      card.append(actions);
      card.append(spikeOut);
    }

    // Root-cause explanation reads real error text → only offered for logs that have a message,
    // and the server still refuses unless the model is local. Results render inline.
    const hasMessage = (lt.columns || []).some((c) => c.role === "message");
    if (hasMessage) {
      const out = el("div", { class: "log-causes" });
      const btn = el("button", { class: "ghost", type: "button", text: "Explain recent errors" });
      btn.addEventListener("click", () => explainLog(lt.name, btn, out));
      if (!hasTime) card.append(actions);
      actions.append(btn);
      card.append(out);
    }
    box.append(card);
  });
}

async function showSpikes(name, btn, out) {
  btn.disabled = true;
  out.replaceChildren(el("p", { class: "muted small", text: "Charting error volume over time…" }));
  try {
    const res = await fetch("/api/logs/spikes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ table: name, grain: "hour" }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Spike analysis failed.");
    out.replaceChildren();
    renderSpikes(out, data);
  } catch (err) {
    out.replaceChildren(el("p", { class: "status error small", text: err.message }));
  } finally {
    btn.disabled = false;
  }
}

function renderSpikes(out, data) {
  const buckets = data.buckets || [];
  if (!buckets.length) {
    out.append(el("p", { class: "muted small", text: data.note || "No entries in this window." }));
    return;
  }
  const spikeSet = new Set((data.spikes || []).map((s) => s.bucket));
  const headText = (data.spikes && data.spikes.length)
    ? `⚠ ${data.spikes.length} spike(s) — baseline ${data.baseline}/${data.grain}, flagged at ≥${data.factor}× and ≥${data.min_count}`
    : `No spikes — baseline ${data.baseline}/${data.grain} across ${data.bucket_count} buckets`;
  out.append(el("p", { class: (data.spikes && data.spikes.length) ? "spike-head small" : "muted small", text: headText }));

  const peak = buckets.reduce((m, b) => Math.max(m, b.count), 1) || 1;
  const chart = el("div", { class: "spike-chart" });
  buckets.forEach((b) => {
    const isSpike = spikeSet.has(b.bucket);
    const row = el("div", { class: "spike-row" + (isSpike ? " is-spike" : "") });
    row.append(el("span", { class: "spike-label", text: b.bucket }));
    const barWrap = el("span", { class: "spike-bar-wrap" });
    barWrap.append(el("span", { class: "spike-bar", style: `width:${Math.max(2, Math.round(100 * b.count / peak))}%` }));
    row.append(barWrap);
    row.append(el("span", { class: "spike-count", text: String(b.count) + (isSpike ? "  ⚠" : "") }));
    chart.append(row);
  });
  out.append(chart);

  if (data.onsets && data.onsets.length) {
    out.append(el("p", { class: "muted small", text: "Sources that started spiking:" }));
    const ul = el("ul", { class: "spike-onsets" });
    data.onsets.forEach((o) =>
      ul.append(el("li", { class: "small" },
        el("code", { text: o.source || "(unattributed)" }),
        el("span", { class: "muted", text: ` at ${o.bucket} — ${o.count} (${o.ratio}× baseline)` })
      ))
    );
    out.append(ul);
  }
}

async function explainLog(name, btn, out) {
  btn.disabled = true;
  out.replaceChildren(el("p", { class: "muted small", text: "Clustering recent errors with the local model…" }));
  try {
    const res = await fetch("/api/logs/explain", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ table: name }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Explain failed.");
    out.replaceChildren();
    const note = `From ${data.sample_size} redacted entries — the error text stayed on this machine.`;
    out.append(el("p", { class: "muted small", text: note }));
    if (!data.clusters || !data.clusters.length) {
      out.append(el("p", { class: "muted small", text: data.note || "No clusters found." }));
      return;
    }
    data.clusters.forEach((c) => {
      const item = el("div", { class: "cause" });
      const head = el("p", { class: "cause-head" }, el("strong", { text: c.cause }));
      if (c.count) head.append(el("span", { class: "muted small", text: `  ×${c.count}` }));
      if (c.severity) head.append(el("span", { class: "badge", text: c.severity }));
      item.append(head);
      if (c.suggested_action) item.append(el("p", { class: "small", text: "→ " + c.suggested_action }));
      if (c.example) item.append(el("p", { class: "muted small", text: "e.g. " + c.example }));
      out.append(item);
    });
  } catch (err) {
    out.replaceChildren(el("p", { class: "status error small", text: err.message }));
  } finally {
    btn.disabled = false;
  }
}

$("#logs-search").addEventListener("input", (e) => {
  const q = e.target.value.toLowerCase();
  const all = MAP.log_tables || [];
  renderLogs(all.filter((lt) =>
    lt.name.toLowerCase().includes(q) ||
    lt.kind.toLowerCase().includes(q) ||
    (lt.columns || []).map((c) => c.column + " " + c.role).join(" ").toLowerCase().includes(q)
  ));
});

// --- helpers ----------------------------------------------------------------
function setStatus(sel, msg, isError = false) {
  const n = $(sel);
  n.textContent = msg;
  n.classList.toggle("error", isError);
}

loadMap().catch((e) => setStatus("#ask-status", "Could not load the map: " + e.message, true));

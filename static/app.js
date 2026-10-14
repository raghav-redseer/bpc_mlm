// L'Oreal BPC Tagging Engine - frontend. Plain JS, no build step, no framework.

// ---------------------------------------------------------------- Footer: "is my tab stale?"
// The single biggest source of confusing-looking bugs so far: a tab left open across a code
// change, silently still running the old JS. Checks the server's reported last-changed time once
// on load (to display it) and again every 60s - if it ever differs from what this tab loaded
// with, something changed server-side since, so the warning tells you to hard-refresh instead of
// leaving you to wonder whether you're looking at a real bug or a stale tab.
let footerLoadedVersion = null;
async function checkVersion(isPoll) {
  try {
    const { server_code_updated_at } = await fetch("/version").then((r) => r.json());
    if (footerLoadedVersion === null) {
      footerLoadedVersion = server_code_updated_at;
      document.getElementById("footer-version").textContent = `Server code last changed: ${server_code_updated_at}`;
      return;
    }
    if (isPoll && server_code_updated_at !== footerLoadedVersion) {
      document.getElementById("footer-stale-warning").classList.remove("hidden");
    }
  } catch (err) {
    // Server unreachable mid-poll - not worth surfacing, next poll will recover.
  }
}
checkVersion(false);
setInterval(() => checkVersion(true), 60000);

// ---------------------------------------------------------------- Password visibility toggle
// Every password field (Monthly Setup lock/unlock, Expert Data Tagging finalize/reopen,
// Category Topline SL/Manager approvals) gets a small eye button to reveal what was typed -
// several of these fields are rendered fresh via innerHTML on every board refresh, so rather
// than re-wiring this at every render call site, a MutationObserver just wraps any password
// input as soon as it appears anywhere in the page, including the static ones already in the
// page at load time.
function wrapPasswordInput(input) {
  if (input.dataset.pwWrapped) return;
  input.dataset.pwWrapped = "1";
  const wrap = document.createElement("span");
  wrap.className = "pw-wrap";
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "pw-toggle";
  btn.textContent = "\u{1F441}️";
  btn.title = "Show password";
  btn.addEventListener("click", () => {
    const showing = input.type === "text";
    input.type = showing ? "password" : "text";
    btn.textContent = showing ? "\u{1F441}️" : "\u{1F648}";
    btn.title = showing ? "Show password" : "Hide password";
  });
  wrap.appendChild(btn);
}
function wrapPasswordInputsIn(root) {
  root.querySelectorAll('input[type="password"]').forEach(wrapPasswordInput);
}
wrapPasswordInputsIn(document);
new MutationObserver((mutations) => {
  for (const m of mutations) {
    m.addedNodes.forEach((node) => {
      if (node.nodeType !== 1) return;
      if (node.matches && node.matches('input[type="password"]')) wrapPasswordInput(node);
      if (node.querySelectorAll) wrapPasswordInputsIn(node);
    });
  }
}).observe(document.body, { childList: true, subtree: true });

// ---------------------------------------------------------------- Tabs
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "tagging") initExpertProgress();
    if (btn.dataset.tab === "glossary") loadGlossary();
    if (btn.dataset.tab === "topline") initTopline();
    if (btn.dataset.tab === "bsc") initBsc();
    if (btn.dataset.tab === "brandvalidation") initBrandValidation();
    if (btn.dataset.tab === "masterdata") initMasterData();
  });
});

// ---------------------------------------------------------------- Tab 0: Monthly Setup
// Per-month platform gating: pending_upload -> draft_ready -> final_uploaded -> finalized for
// every platform with expert data this month; everything else is marked no_expert_data and
// needs no further action here. Tagging itself reuses the exact same /tag-bulk job pipeline as
// the Bulk Tag tab below - this section only files a completed job's output under the
// platform+month's one canonical name (see attach-draft on the backend).
let monthlyPlatforms = [];
let monthlyCurrentMonth = null;
let monthlyState = null;          // {locked, platforms: {name: {...}}} for monthlyCurrentMonth
let monthlySelectedFiles = {};    // platform -> File (raw file about to be tagged)
let monthlySelectedFinalFiles = {}; // platform -> File (reviewed file about to be uploaded)

const MONTHLY_STAGE_LABEL = {
  no_expert_data: "No expert data",
  pending_upload: "Analyst: awaiting raw file upload",
  draft_ready: "Analyst: draft tagged - awaiting review",
  final_uploaded: "Analyst: final uploaded - ready to send to SL",
  pending_sl_review: "SL: pending review",
  finalized: "SL: finalized",
};
const MONTHLY_STAGE_BADGE = {
  no_expert_data: "monthly-badge-skip",
  pending_upload: "monthly-badge-pending",
  draft_ready: "monthly-badge-draft",
  final_uploaded: "monthly-badge-final",
  pending_sl_review: "monthly-badge-review",
  finalized: "monthly-badge-done",
};

// Category Topline's own analyst -> SL -> Manager review track - separate badge/stage per
// platform, shown alongside (not instead of) the expert-data tagging badge above.
const CT_STAGE_LABEL = {
  analyst: "CT: Analyst",
  pending_sl_review: "CT: Pending SL Review",
  pending_manager_review: "CT: Pending Manager Review",
  finalized: "CT: Finalized",
};
const CT_STAGE_BADGE = {
  analyst: "monthly-badge-pending",
  pending_sl_review: "monthly-badge-review",
  pending_manager_review: "monthly-badge-manager-review",
  finalized: "monthly-badge-done",
};

const MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function monthLabel(year, monthIdx) {
  return `${MONTH_ABBR[monthIdx]}'${String(year % 100).padStart(2, "0")}`;
}
function parseMonthLabel(label) {
  const m = /^([A-Za-z]{3})'(\d{2})$/.exec(label || "");
  if (!m) return null;
  const monthIdx = MONTH_ABBR.indexOf(m[1]);
  if (monthIdx < 0) return null;
  return { year: 2000 + parseInt(m[2], 10), monthIdx };
}
function sortMonths(months) {
  return [...months].sort((a, b) => {
    const pa = parseMonthLabel(a), pb = parseMonthLabel(b);
    if (!pa || !pb) return a.localeCompare(b);
    return (pa.year * 12 + pa.monthIdx) - (pb.year * 12 + pb.monthIdx);
  });
}
async function initMonthly() {
  monthlyPlatforms = (await fetch("/monthly-setup/platforms").then((r) => r.json())).platforms;
  await populateMonthlyCreateSelect();
  renderMonthlyPrefsList(); // matrix is always visible - disabled until a month is opened
}

// Status should be visible without needing to pick a month by hand every time this tab loads -
// there's only ever one month actually in play at a time now (sequential: next month can't be
// built before this one's done), so the page auto-opens the most recently created one on first
// load. NOT /monthly-setup/active, which is a separate, admin-gated "which month Expert Data
// Tagging defaults to" concept from a time when several months could be in flight at once.
// "Change month" deliberately does NOT call this - its whole point is a blank slate to pick from.
async function autoOpenCurrentMonth() {
  const { months: createdMonths } = await fetch("/monthly-setup/months").then((r) => r.json());
  const latest = createdMonths[createdMonths.length - 1];
  if (latest) await openMonthlyMonth(latest);
}

// Lists any month already created (so you can continue where you left off) plus - ONLY once
// every one of those is Complete - the single next sequential month (the backend computes which
// one, never a window of several future months to offer: you can't actually build Oct'26 before
// Sep'26 is done, since each month's no-expert-data forecast is derived from the prior actual
// month's own numbers).
async function populateMonthlyCreateSelect() {
  const { months: createdMonths, next_month: nextMonth } = await fetch("/monthly-setup/months").then((r) => r.json());
  const options = sortMonths(nextMonth ? [...new Set([...createdMonths, nextMonth])] : createdMonths);
  const sel = document.getElementById("monthly-month-new");
  sel.innerHTML = `<option value="">- select -</option>` +
    options.map((m2) => `<option value="${escapeHtml(m2)}">${escapeHtml(m2)}</option>`).join("");
}

document.getElementById("monthly-month-create").addEventListener("click", () => {
  const month = document.getElementById("monthly-month-new").value;
  if (!month) return;
  openMonthlyMonth(month);
});
document.getElementById("monthly-month-change-btn").addEventListener("click", async () => {
  monthlyCurrentMonth = null;
  monthlyState = null;
  document.getElementById("monthly-month-new").value = "";
  document.getElementById("monthly-month-new").disabled = false;
  document.getElementById("monthly-month-create").disabled = false;
  document.getElementById("monthly-month-active-row").classList.add("hidden");
  document.getElementById("monthly-status-board").innerHTML = "";
  await initMonthly(); // refresh the Create option list in case a month was just created
  renderMonthlyMonth();
});

// /board (not the plain /monthly-setup/{month} GET) - backfills category_topline in-memory for
// every platform so the status board below always has something to show, even for a platform
// that's never had a CT action run on it yet.
async function fetchMonthlyBoardState(month) {
  return fetch(`/monthly-setup/${encodeURIComponent(month)}/board`).then((r) => r.json());
}

async function openMonthlyMonth(month) {
  monthlyCurrentMonth = month;
  // The plain GET is what actually creates the month's state the first time it's opened (/board
  // 404s on a month that doesn't exist yet) - called here for that side effect, then /board for
  // the category_topline-backfilled version the status board renders from.
  await fetch(`/monthly-setup/${encodeURIComponent(month)}`);
  monthlyState = await fetchMonthlyBoardState(month);

  await populateMonthlyCreateSelect();
  document.getElementById("monthly-month-new").value = month;

  document.getElementById("monthly-month-new").disabled = true;
  document.getElementById("monthly-month-create").disabled = true;
  document.getElementById("monthly-month-active-row").classList.remove("hidden");
  document.getElementById("monthly-active-month-label").textContent = month;

  renderMonthlyMonth();
}

function renderMonthlyMonth() {
  renderMonthlyPrefsList();
  const pointer = document.getElementById("monthly-locked-pointer");
  pointer.classList.toggle("hidden", !(monthlyState && monthlyState.locked));
  const pointerText = document.getElementById("monthly-locked-pointer-text");
  if (monthlyState && monthlyState.month_complete) {
    pointerText.innerHTML = `<strong>&#9989; Complete and archived.</strong> Reopen it (admin) from the <strong>Expert Data Tagging</strong> tab if changes are needed.`;
  } else {
    pointerText.innerHTML = `In progress. Head to the <strong>Expert Data Tagging</strong> tab to upload/review/finalize each platform's file.`;
  }
  renderMonthlyStatusBoard();
}

// Per-platform status (both the expert-data tagging stage and the Category Topline review
// stage) lives here now, not in Expert Data Tagging - this is the tab you already come to for
// platform setup, so the at-a-glance "where does each platform stand" view belongs beside it
// rather than requiring a tab switch. Expert Data Tagging keeps the actual upload/review actions
// for expert-data platforms; Category Topline tab keeps its own review actions.
function renderMonthlyStatusBoard() {
  const container = document.getElementById("monthly-status-board");
  if (!monthlyState || !monthlyState.locked) { container.innerHTML = ""; return; }
  const entries = Object.entries(monthlyState.platforms || {});
  if (!entries.length) { container.innerHTML = ""; return; }

  // A table with one column per workflow step (not two badges run together in a row) - the
  // whole point is that it was previously impossible to tell which badge belonged to which
  // step at a glance. "No expert data" now shows its own real (grey) badge instead of a blank
  // cell, same "always show something, never a silent blank" standard used everywhere else.
  container.innerHTML = `
    <h3 style="margin:0 0 8px;">Status</h3>
    <table id="monthly-status-table" class="status-table">
      <thead><tr><th>Platform</th><th>Expert Data</th><th>Category Topline</th></tr></thead>
      <tbody>
        ${entries
          .map(([name, p]) => {
            const ct = p.category_topline || {};
            const tagBadge = `<span class="monthly-badge ${MONTHLY_STAGE_BADGE[p.status] || ""}">${escapeHtml(MONTHLY_STAGE_LABEL[p.status] || p.status)}</span>`;
            const ctBadge = `<span class="monthly-badge ${CT_STAGE_BADGE[ct.status] || ""}">${escapeHtml(CT_STAGE_LABEL[ct.status] || "CT: Analyst")}</span>`;
            return `<tr><td class="status-table-name">${escapeHtml(name)}</td><td>${tagBadge}</td><td>${ctBadge}</td></tr>`;
          })
          .join("")}
      </tbody>
    </table>
  `;
}

document.getElementById("monthly-goto-tagging-btn").addEventListener("click", () => {
  document.querySelector('.tab-btn[data-tab="tagging"]').click();
});

// The platform matrix is always rendered. It's disabled (no month open yet, or the current
// month's preferences are locked) or editable (a month is open and unlocked) - never hidden.
function renderMonthlyPrefsList() {
  const hasMonth = !!monthlyCurrentMonth;
  const complete = hasMonth && monthlyState && monthlyState.month_complete;
  const locked = hasMonth && monthlyState && monthlyState.locked;
  const editable = hasMonth && !locked;

  const list = document.getElementById("monthly-prefs-list");
  list.innerHTML = monthlyPlatforms
    .map((p) => {
      const existing = hasMonth && monthlyState.platforms[p];
      const checked = existing ? existing.has_expert_data : false;
      const classes = ["monthly-pref-tile"];
      if (checked) classes.push("selected");
      if (!editable) classes.push("disabled");
      return `<label class="${classes.join(" ")}">
        <input type="checkbox" class="monthly-pref-check" data-platform="${escapeHtml(p)}" ${checked ? "checked" : ""} ${editable ? "" : "disabled"}>
        <span class="check-icon">&#10003;</span><span>${escapeHtml(p)}</span>
      </label>`;
    })
    .join("");
  list.querySelectorAll(".monthly-pref-check").forEach((cb) => {
    cb.addEventListener("change", () => {
      cb.closest(".monthly-pref-tile").classList.toggle("selected", cb.checked);
    });
  });

  document.getElementById("monthly-lock-btn").classList.toggle("hidden", !!locked);
  document.getElementById("monthly-lock-btn").disabled = !editable;
  document.getElementById("monthly-lock-password").disabled = !editable;
  document.getElementById("monthly-lock-password").classList.toggle("hidden", !!locked);
  document.getElementById("monthly-edit-prefs-row").classList.toggle("hidden", !locked || complete);

  const hint = document.getElementById("monthly-prefs-hint");
  if (!hasMonth) {
    hint.textContent = "Open or create a month above to enable this.";
  } else if (complete) {
    hint.textContent = `${monthlyCurrentMonth} is Complete and archived - an admin must reopen it (Expert Data Tagging tab) before anything here can change.`;
  } else if (locked) {
    hint.textContent = `In progress for ${monthlyCurrentMonth} - click "Edit platform preferences" to change.`;
  } else {
    hint.textContent = `Toggle on every platform you'll be receiving/tagging an expert file for ${monthlyCurrentMonth}. Everything left off moves straight past this phase.`;
  }
}

document.getElementById("monthly-lock-btn").addEventListener("click", async () => {
  if (!monthlyCurrentMonth) return;
  const password = document.getElementById("monthly-lock-password").value;
  const platforms = {};
  document.querySelectorAll(".monthly-pref-check").forEach((cb) => { platforms[cb.dataset.platform] = cb.checked; });
  const statusEl = document.getElementById("monthly-prefs-status");
  statusEl.textContent = "Locking...";
  try {
    const res = await fetch(`/monthly-setup/${encodeURIComponent(monthlyCurrentMonth)}/lock`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platforms, password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      statusEl.textContent = `Error: ${err.detail}`;
      return;
    }
    monthlyState = await fetchMonthlyBoardState(monthlyCurrentMonth);
    document.getElementById("monthly-lock-password").value = "";
    statusEl.textContent = "";
    renderMonthlyMonth();
  } catch (err) {
    statusEl.textContent = `Error: ${String(err)}`;
  }
});

document.getElementById("monthly-edit-prefs-btn").addEventListener("click", async () => {
  const password = document.getElementById("monthly-edit-prefs-password").value;
  const statusEl = document.getElementById("monthly-prefs-status");
  try {
    const res = await fetch(`/monthly-setup/${encodeURIComponent(monthlyCurrentMonth)}/unlock`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      statusEl.textContent = `Error: ${err.detail}`;
      return;
    }
    monthlyState.locked = false;
    document.getElementById("monthly-edit-prefs-password").value = "";
    renderMonthlyMonth();
    // A successful unlock used to clear this to "" - with no error and no visible confirmation,
    // it looked exactly like clicking the button had done nothing.
    statusEl.textContent = "Unlocked - platforms are editable again below.";
  } catch (err) {
    statusEl.textContent = `Error: ${String(err)}`;
  }
});

// ---------------------------------------------------------------- Expert Data Tagging: per-
// platform tagging progress board (top of the tab). Separate "current month" context from
// Monthly Setup - this one is about VIEWING/working a month's tagging pipeline, not editing
// which platforms have expert data. Defaults to whichever month was most recently locked, but
// any locked month can be revisited (nothing here is ever lost between sessions - every change
// is written to monthly_platform_state.json immediately, including a full history per platform).
let expertCurrentMonth = null;
let expertState = null;
let expertActiveMonth = null; // the admin-set default month (null until an admin explicitly sets one)

async function initExpertProgress() {
  const card = document.getElementById("expert-progress-card");
  expertActiveMonth = (await fetch("/monthly-setup/active").then((r) => r.json())).month;
  document.getElementById("expert-active-month-label").textContent = expertActiveMonth || "(none set)";
  const lockedMonths = sortMonths((await fetch("/monthly-setup/locked-months").then((r) => r.json())).months);
  const hint = document.getElementById("expert-progress-hint");
  const sel = document.getElementById("expert-month-select");

  if (!lockedMonths.length) {
    card.classList.remove("hidden");
    hint.classList.remove("hidden");
    sel.innerHTML = "";
    document.getElementById("expert-platform-board").innerHTML = "";
    return;
  }
  card.classList.remove("hidden");
  hint.classList.add("hidden");
  sel.innerHTML = lockedMonths.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("");
  const target = lockedMonths.includes(expertActiveMonth) ? expertActiveMonth : lockedMonths[lockedMonths.length - 1];
  sel.value = target;
  await loadExpertMonth(target);
}

document.getElementById("expert-set-active-btn").addEventListener("click", async () => {
  const password = document.getElementById("expert-set-active-password").value;
  const statusEl = document.getElementById("expert-set-active-status");
  statusEl.textContent = "Checking...";
  try {
    const res = await fetch(`/monthly-setup/${encodeURIComponent(expertCurrentMonth)}/set-active`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      statusEl.textContent = `Error: ${err.detail}`;
      return;
    }
    const result = await res.json();
    expertActiveMonth = result.active_month;
    document.getElementById("expert-active-month-label").textContent = expertActiveMonth;
    document.getElementById("expert-set-active-password").value = "";
    statusEl.textContent = "Done.";
  } catch (err) {
    statusEl.textContent = `Error: ${String(err)}`;
  }
});

document.getElementById("expert-month-select").addEventListener("change", (e) => loadExpertMonth(e.target.value));

async function loadExpertMonth(month) {
  expertCurrentMonth = month;
  expertState = await fetch(`/monthly-setup/${encodeURIComponent(month)}/board`).then((r) => r.json());
  renderExpertStatusBoard();
}

function renderExpertStatusBoard() {
  const board = document.getElementById("expert-platform-board");
  const completeRow = document.getElementById("expert-complete-row");
  const completeBanner = document.getElementById("expert-complete-banner");
  // Only expert-data platforms get a card here - a no-expert-data platform has nothing left to
  // do in this tab (its hard input, review, and status all live in the Category Topline tab and
  // Monthly Setup's status board now).
  const allEntries = Object.entries(expertState.platforms);
  const entries = allEntries.filter(([, p]) => p.has_expert_data);

  if (expertState.month_complete) {
    completeBanner.classList.remove("hidden");
    completeRow.classList.add("hidden");
    document.getElementById("expert-complete-month-label").textContent = expertCurrentMonth;
    board.innerHTML = entries.map(([name, p]) => renderMonthlyPlatformCard(name, p, true)).join("");
    return;
  }
  completeBanner.classList.add("hidden");

  if (!entries.length) {
    board.innerHTML = allEntries.length
      ? `<p class="hint">No platforms with expert data for ${escapeHtml(expertCurrentMonth)} - platforms without expert data are set up entirely from the Category Topline tab (see Monthly Setup for their status).</p>`
      : `<p class="hint">No platforms set up for ${escapeHtml(expertCurrentMonth)} yet.</p>`;
    completeRow.classList.add("hidden");
    return;
  }
  board.innerHTML = entries.map(([name, p]) => renderMonthlyPlatformCard(name, p, false)).join("");
  bindMonthlyPlatformCardEvents();

  const allFinalized = entries.every(([, p]) => p.status === "finalized");
  completeRow.classList.toggle("hidden", !allFinalized);
}

document.getElementById("expert-complete-btn").addEventListener("click", async () => {
  const password = document.getElementById("expert-complete-password").value;
  const statusEl = document.getElementById("expert-complete-status");
  statusEl.textContent = "Checking...";
  try {
    const res = await fetch(`/monthly-setup/${encodeURIComponent(expertCurrentMonth)}/complete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      statusEl.textContent = `Error: ${err.detail}`;
      return;
    }
    expertState = await res.json();
    document.getElementById("expert-complete-password").value = "";
    statusEl.textContent = "";
    renderExpertStatusBoard();
  } catch (err) {
    statusEl.textContent = `Error: ${String(err)}`;
  }
});

document.getElementById("expert-reopen-btn").addEventListener("click", async () => {
  const password = document.getElementById("expert-reopen-password").value;
  if (!confirm(`Reopen ${expertCurrentMonth}? This undoes Complete and allows edits again.`)) return;
  const statusEl = document.getElementById("expert-reopen-status");
  statusEl.textContent = "Checking...";
  try {
    const res = await fetch(`/monthly-setup/${encodeURIComponent(expertCurrentMonth)}/reopen-month`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      statusEl.textContent = `Error: ${err.detail}`;
      return;
    }
    expertState = await res.json();
    document.getElementById("expert-reopen-password").value = "";
    statusEl.textContent = "";
    renderExpertStatusBoard();
  } catch (err) {
    statusEl.textContent = `Error: ${String(err)}`;
  }
});

function renderMonthlyPlatformCard(name, p, readOnly) {
  const label = MONTHLY_STAGE_LABEL[p.status] || p.status;
  const badgeClass = MONTHLY_STAGE_BADGE[p.status] || "";
  const draftLink = `/monthly-setup/${encodeURIComponent(expertCurrentMonth)}/${encodeURIComponent(name)}/draft`;
  const finalLink = `/monthly-setup/${encodeURIComponent(expertCurrentMonth)}/${encodeURIComponent(name)}/final`;
  const history = p.history || [];
  const historyHtml = history.length
    ? `<details class="monthly-history">
        <summary>History (${history.length})</summary>
        <ul>${history.map((h) => `<li><span class="monthly-history-time">${escapeHtml(h.at)}</span> - ${escapeHtml(h.event)}</li>`).join("")}</ul>
      </details>`
    : "";

  let body = "";
  if (readOnly) {
    body = `
      <div class="row">
        <a class="button-link" href="${draftLink}">Download Draft</a>
        <a class="button-link" href="${finalLink}">Download Final</a>
      </div>
      <p class="hint">Finalized${p.finalized_at ? " on " + escapeHtml(p.finalized_at) : ""} - archived with the month.</p>`;
  } else if (p.status === "pending_upload") {
    const categoryLevel = categoryLevelPlatforms.includes(name.toLowerCase());
    const columnFields = categoryLevel
      ? `<input type="text" class="monthly-sheet-input" data-platform="${escapeHtml(name)}" placeholder="Data sheet (blank = first)" style="max-width:170px;" title="Exact sheet name holding the Category/Sub-category rows">
        <input type="text" class="monthly-category-col-input" data-platform="${escapeHtml(name)}" value="Category" style="max-width:110px;" title="Category column header">
        <input type="text" class="monthly-subcat1-col-input" data-platform="${escapeHtml(name)}" placeholder="Sub cat 1 (blank = same as Category)" style="max-width:150px;" title="Sub cat 1 column header - leave blank for a platform with only one native category field">
        <input type="text" class="monthly-subcat2-col-input" data-platform="${escapeHtml(name)}" placeholder="Sub cat 2 (blank = same as Category)" style="max-width:150px;" title="Sub cat 2 column header - leave blank for a platform with only one native category field">
        <input type="text" class="monthly-brand-col-input" data-platform="${escapeHtml(name)}" value="brand" style="max-width:110px;" title="Brand column header">`
      : `<input type="text" class="monthly-col-input" data-platform="${escapeHtml(name)}" value="product_name" style="max-width:160px;" title="Exact product-name column header in the raw file">`;
    body = `
      <div class="row">
        <input type="file" class="monthly-file-input" data-platform="${escapeHtml(name)}" accept=".xlsx,.csv">
        ${columnFields}
        <button class="primary monthly-tag-btn" data-platform="${escapeHtml(name)}">Tag &amp; Create Draft</button>
      </div>
      <div class="progress-bar hidden monthly-progress"><div class="progress-fill"></div></div>
      <p class="hint monthly-progress-label"></p>`;
  } else if (p.status === "draft_ready") {
    body = `
      <div class="row">
        <a class="button-link" href="${draftLink}">Download Draft</a>
      </div>
      <p class="hint">Review the draft, make any corrections outside the tool, then upload the reviewed file as the Final.</p>
      <div class="row">
        <input type="file" class="monthly-final-input" data-platform="${escapeHtml(name)}" accept=".xlsx,.csv">
        <button class="primary monthly-final-btn" data-platform="${escapeHtml(name)}">Upload reviewed Final</button>
      </div>
      <span class="edit-status monthly-final-status"></span>`;
  } else if (p.status === "final_uploaded") {
    body = `
      <div class="row">
        <a class="button-link" href="${draftLink}">Download Draft</a>
        <a class="button-link" href="${finalLink}">Download Final</a>
      </div>
      <div class="row">
        <input type="file" class="monthly-final-input" data-platform="${escapeHtml(name)}" accept=".xlsx,.csv">
        <button class="monthly-final-btn" data-platform="${escapeHtml(name)}">Re-upload Final</button>
        <button class="primary monthly-sendreview-btn" data-platform="${escapeHtml(name)}">Send to SL Review</button>
      </div>
      <span class="edit-status monthly-final-status"></span>`;
  } else if (p.status === "pending_sl_review") {
    body = `
      <div class="row">
        <a class="button-link" href="${draftLink}">Download Draft</a>
        <a class="button-link" href="${finalLink}">Download Final</a>
      </div>
      <p class="hint">Awaiting SL review - locked from further analyst uploads until SL finalizes or sends it back.</p>
      <div class="row">
        <button class="monthly-sendback-btn" data-platform="${escapeHtml(name)}">Send back to analyst</button>
        <input type="password" class="monthly-finalize-password" data-platform="${escapeHtml(name)}" placeholder="SL password" style="max-width:140px;">
        <button class="primary monthly-finalize-btn" data-platform="${escapeHtml(name)}">Finalize (SL)</button>
      </div>
      <span class="edit-status monthly-final-status"></span>`;
  } else if (p.status === "finalized") {
    body = `
      <div class="row">
        <a class="button-link" href="${draftLink}">Download Draft</a>
        <a class="button-link" href="${finalLink}">Download Final</a>
      </div>
      <p class="hint">Finalized${p.finalized_at ? " on " + escapeHtml(p.finalized_at) : ""} - ready for the next phase.</p>
      <div class="row">
        <input type="password" class="monthly-reopen-password" data-platform="${escapeHtml(name)}" placeholder="SL password" style="max-width:140px;">
        <button class="monthly-reopen-btn" data-platform="${escapeHtml(name)}">Reopen (SL)</button>
      </div>
      <span class="edit-status monthly-final-status"></span>`;
  }

  return `<div class="card monthly-platform-card" data-platform="${escapeHtml(name)}">
    <div class="row space-between">
      <h3 style="margin:0;">${escapeHtml(name)}</h3>
      <span class="monthly-badge ${badgeClass}">${escapeHtml(label)}</span>
    </div>
    ${body}
    ${historyHtml}
  </div>`;
}

function monthlyCard(platform) {
  return document.querySelector(`.monthly-platform-card[data-platform="${CSS.escape(platform)}"]`);
}

function bindMonthlyPlatformCardEvents() {
  document.querySelectorAll(".monthly-file-input").forEach((el) => {
    el.addEventListener("change", () => { monthlySelectedFiles[el.dataset.platform] = el.files[0] || null; });
  });
  document.querySelectorAll(".monthly-final-input").forEach((el) => {
    el.addEventListener("change", () => { monthlySelectedFinalFiles[el.dataset.platform] = el.files[0] || null; });
  });
  document.querySelectorAll(".monthly-tag-btn").forEach((btn) => {
    btn.addEventListener("click", () => startMonthlyTagging(btn.dataset.platform));
  });
  document.querySelectorAll(".monthly-final-btn").forEach((btn) => {
    btn.addEventListener("click", () => uploadMonthlyFinal(btn.dataset.platform));
  });
  document.querySelectorAll(".monthly-sendreview-btn").forEach((btn) => {
    btn.addEventListener("click", () => sendToReviewPlatform(btn.dataset.platform));
  });
  document.querySelectorAll(".monthly-sendback-btn").forEach((btn) => {
    btn.addEventListener("click", () => sendBackPlatform(btn.dataset.platform));
  });
  document.querySelectorAll(".monthly-finalize-btn").forEach((btn) => {
    btn.addEventListener("click", () => finalizeMonthlyPlatform(btn.dataset.platform));
  });
  document.querySelectorAll(".monthly-reopen-btn").forEach((btn) => {
    btn.addEventListener("click", () => reopenMonthlyPlatform(btn.dataset.platform));
  });
}

async function startMonthlyTagging(platform) {
  const file = monthlySelectedFiles[platform];
  if (!file) { alert("Choose a file first."); return; }
  const card = monthlyCard(platform);
  const progressWrap = card.querySelector(".monthly-progress");
  const progressFill = progressWrap.querySelector(".progress-fill");
  const progressLabel = card.querySelector(".monthly-progress-label");
  const btn = card.querySelector(".monthly-tag-btn");
  btn.disabled = true;
  progressWrap.classList.remove("hidden");
  progressLabel.textContent = "Uploading...";

  const formData = new FormData();
  formData.append("file", file);
  formData.append("platform", platform);
  formData.append("save_to_disk", "false");
  formData.append("save_folder", "");
  formData.append("overwrite_existing", "false");

  let endpoint = "/tag-bulk";
  if (categoryLevelPlatforms.includes(platform.toLowerCase())) {
    endpoint = "/tag-bulk-category";
    // None of these are .trim()'d - a real workbook's sheet name or column header can (and for
    // Flipkart's "Aug Raw Data " / Amazon's "AZ Expert Data ", does) legitimately carry a
    // trailing space, so trimming would silently break the lookup against the user's actual
    // file (the same lesson already applied to sheet_name's backend handling).
    const categoryColValue = card.querySelector(".monthly-category-col-input").value || "Category";
    formData.append("sheet_name", card.querySelector(".monthly-sheet-input").value);
    formData.append("category_col", categoryColValue);
    // A single-tag-column platform like Amazon leaves Sub cat 1/Sub cat 2 blank in the UI - default
    // to the SAME value as Category here (client-side, not left for the backend to infer) so a
    // real, matching column name is always what gets sent, not an empty or placeholder one.
    formData.append("subcat1_col", card.querySelector(".monthly-subcat1-col-input").value || categoryColValue);
    formData.append("subcat2_col", card.querySelector(".monthly-subcat2-col-input").value || categoryColValue);
    formData.append("brand_col", card.querySelector(".monthly-brand-col-input").value || "brand");
  } else {
    formData.append("column_name", card.querySelector(".monthly-col-input").value.trim() || "product_name");
  }

  try {
    const res = await fetch(endpoint, { method: "POST", body: formData });
    const { job_id } = await res.json();
    pollMonthlyJob(platform, job_id, progressFill, progressLabel, btn);
  } catch (err) {
    progressLabel.textContent = `Error: ${String(err)}`;
    btn.disabled = false;
  }
}

function pollMonthlyJob(platform, jobId, progressFill, progressLabel, btn) {
  const interval = setInterval(async () => {
    const res = await fetch(`/tag-bulk/${jobId}/status`);
    const job = await res.json();
    if (job.status === "error") {
      clearInterval(interval);
      progressLabel.textContent = `Error: ${job.error}`;
      btn.disabled = false;
      return;
    }
    if (job.status === "processing" && job.total) {
      const pct = Math.round((job.done / job.total) * 100);
      progressFill.style.width = `${pct}%`;
      progressLabel.textContent = `Tagging ${job.done.toLocaleString()} / ${job.total.toLocaleString()} unique SKUs (${pct}%)`;
    } else if (job.status === "reading") {
      progressLabel.textContent = "Reading file...";
    }
    if (job.status === "done") {
      clearInterval(interval);
      progressFill.style.width = "100%";
      progressLabel.textContent = "Saving draft...";
      try {
        const attachRes = await fetch(
          `/monthly-setup/${encodeURIComponent(expertCurrentMonth)}/${encodeURIComponent(platform)}/attach-draft`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ job_id: jobId }),
          }
        );
        const updated = await attachRes.json();
        expertState.platforms[platform] = updated;
        renderExpertStatusBoard();
      } catch (err) {
        progressLabel.textContent = `Error saving draft: ${String(err)}`;
        btn.disabled = false;
      }
    }
  }, 500);
}

async function uploadMonthlyFinal(platform) {
  const file = monthlySelectedFinalFiles[platform];
  if (!file) { alert("Choose the reviewed file first."); return; }
  const card = monthlyCard(platform);
  const statusEl = card.querySelector(".monthly-final-status");
  statusEl.textContent = "Uploading...";
  const formData = new FormData();
  formData.append("file", file);
  try {
    const res = await fetch(
      `/monthly-setup/${encodeURIComponent(expertCurrentMonth)}/${encodeURIComponent(platform)}/upload-final`,
      { method: "POST", body: formData }
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      statusEl.textContent = `Error: ${err.detail}`;
      return;
    }
    const updated = await res.json();
    expertState.platforms[platform] = updated;
    renderExpertStatusBoard();
  } catch (err) {
    statusEl.textContent = `Error: ${String(err)}`;
  }
}

async function sendToReviewPlatform(platform) {
  try {
    const res = await fetch(
      `/monthly-setup/${encodeURIComponent(expertCurrentMonth)}/${encodeURIComponent(platform)}/send-to-review`,
      { method: "POST" }
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      alert(`Error: ${err.detail}`);
      return;
    }
    expertState.platforms[platform] = await res.json();
    renderExpertStatusBoard();
  } catch (err) {
    alert(`Error: ${String(err)}`);
  }
}

async function sendBackPlatform(platform) {
  if (!confirm(`Send ${platform} back to the analyst for ${expertCurrentMonth}?`)) return;
  try {
    const res = await fetch(
      `/monthly-setup/${encodeURIComponent(expertCurrentMonth)}/${encodeURIComponent(platform)}/send-back`,
      { method: "POST" }
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      alert(`Error: ${err.detail}`);
      return;
    }
    expertState.platforms[platform] = await res.json();
    renderExpertStatusBoard();
  } catch (err) {
    alert(`Error: ${String(err)}`);
  }
}

async function finalizeMonthlyPlatform(platform) {
  const card = monthlyCard(platform);
  const password = card.querySelector(".monthly-finalize-password").value;
  if (!confirm(`Finalize ${platform} for ${expertCurrentMonth}? Make sure you've reviewed the final file first.`)) return;
  try {
    const res = await fetch(
      `/monthly-setup/${encodeURIComponent(expertCurrentMonth)}/${encodeURIComponent(platform)}/finalize`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      }
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      alert(`Error: ${err.detail}`);
      return;
    }
    expertState.platforms[platform] = await res.json();
    renderExpertStatusBoard();
  } catch (err) {
    alert(`Error: ${String(err)}`);
  }
}

async function reopenMonthlyPlatform(platform) {
  const card = monthlyCard(platform);
  const password = card.querySelector(".monthly-reopen-password").value;
  if (!confirm(`Reopen ${platform} for ${expertCurrentMonth}? It goes back to "final uploaded" for another SL review - the final file itself is untouched.`)) return;
  try {
    const res = await fetch(
      `/monthly-setup/${encodeURIComponent(expertCurrentMonth)}/${encodeURIComponent(platform)}/reopen`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      }
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      alert(`Error: ${err.detail}`);
      return;
    }
    expertState.platforms[platform] = await res.json();
    renderExpertStatusBoard();
  } catch (err) {
    alert(`Error: ${String(err)}`);
  }
}

initMonthly().then(autoOpenCurrentMonth);

// ---------------------------------------------------------------- Tab 1: Test a Product
const productInput = document.getElementById("product-input");
const tagBtn = document.getElementById("tag-btn");
const testResults = document.getElementById("test-results");

async function tagProduct() {
  const name = productInput.value.trim();
  if (!name) return;
  tagBtn.disabled = true;
  try {
    const res = await fetch("/tag", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ product_name: name }),
    });
    const data = await res.json();
    renderTestResult(name, data);
    productInput.value = "";
    productInput.focus();
  } catch (err) {
    renderTestResult(name, { method: "Error", L1: null, L2: null, L3: null, error: String(err) });
  } finally {
    tagBtn.disabled = false;
  }
}

function renderTestResult(name, data) {
  const div = document.createElement("div");
  const isUnmapped = !data.L1;
  div.className = "result-item" + (isUnmapped ? " unmapped" : "");
  const path = [data.L1, data.L2, data.L3].filter(Boolean).join(" > ") || "Not mapped";
  const conf = data.confidence ? ` &middot; confidence: ${data.confidence}` : "";
  div.innerHTML = `
    <div class="name">${escapeHtml(name)}</div>
    <div class="path">${escapeHtml(path)}</div>
    <div class="meta">method: ${escapeHtml(data.method || "-")}${conf}</div>
  `;
  testResults.prepend(div);
}

tagBtn.addEventListener("click", tagProduct);
productInput.addEventListener("keydown", (e) => { if (e.key === "Enter") tagProduct(); });

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------------------------------------------------------------- Tab 2: Bulk Tag a File
const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");
const bulkBtn = document.getElementById("bulk-btn");
const bulkFileLabel = document.getElementById("bulk-file-label");
const columnNameInput = document.getElementById("column-name");
const platformNameInput = document.getElementById("platform-name");
const bulkProgress = document.getElementById("bulk-progress");
const bulkProgressLabel = document.getElementById("bulk-progress-label");
const bulkProgressFill = document.getElementById("bulk-progress-fill");
const bulkSummary = document.getElementById("bulk-summary");
const bulkSummaryBody = document.getElementById("bulk-summary-body");
const bulkDownloadLink = document.getElementById("bulk-download-link");
const bulkError = document.getElementById("bulk-error");
const saveToDiskCheckbox = document.getElementById("save-to-disk");
const saveFolderInput = document.getElementById("save-folder");
const saveFolderRow = document.getElementById("save-folder-row");
const overwriteCheckbox = document.getElementById("overwrite-existing");
const overwriteRow = document.getElementById("overwrite-row");
const categoryLevelFields = document.getElementById("category-level-fields");
const sheetNameInput = document.getElementById("sheet-name");
const categoryColInput = document.getElementById("category-col");
const subcat1ColInput = document.getElementById("subcat1-col");
const subcat2ColInput = document.getElementById("subcat2-col");
const brandColInput = document.getElementById("brand-col");

let selectedFile = null;

// Platforms whose expert data arrives pre-aggregated by category (e.g. Flipkart) rather than
// per-product - these get a different upload form (category/sub-category columns, no product
// name column) and go through /tag-bulk-category instead of /tag-bulk.
let categoryLevelPlatforms = [];
fetch("/category-level-platforms")
  .then((r) => r.json())
  .then((data) => { categoryLevelPlatforms = (data.platforms || []).map((p) => p.toLowerCase()); syncCategoryLevelFields(); })
  .catch(() => {});

function isCategoryLevelPlatform() {
  return categoryLevelPlatforms.includes(platformNameInput.value.trim().toLowerCase());
}

function syncCategoryLevelFields() {
  const on = isCategoryLevelPlatform();
  categoryLevelFields.classList.toggle("hidden", !on);
  columnNameInput.closest(".row").querySelectorAll("label, input#column-name")
    .forEach((el) => { if (el.htmlFor === "column-name" || el.id === "column-name") el.classList.toggle("hidden", on); });
}
platformNameInput.addEventListener("input", syncCategoryLevelFields);

// Pre-fill the save-folder field with the app's current default so the user sees exactly
// where a copy would land, and can freely type a different path over it.
fetch("/tag-bulk/default-folder")
  .then((r) => r.json())
  .then((data) => { saveFolderInput.value = data.path; saveFolderInput.placeholder = data.path; })
  .catch(() => {});

function syncSaveRows() {
  const on = saveToDiskCheckbox.checked;
  saveFolderRow.classList.toggle("hidden", !on);
  overwriteRow.classList.toggle("hidden", !on);
}
saveToDiskCheckbox.addEventListener("change", syncSaveRows);
syncSaveRows();

["dragenter", "dragover"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); })
);
["dragleave", "drop"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); })
);
dropzone.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files[0];
  if (file) setSelectedFile(file);
});
fileInput.addEventListener("change", () => {
  if (fileInput.files[0]) setSelectedFile(fileInput.files[0]);
});

function setSelectedFile(file) {
  selectedFile = file;
  bulkFileLabel.textContent = `Selected: ${file.name} (${(file.size / 1024).toFixed(0)} KB)`;
  bulkBtn.disabled = false;
  bulkSummary.classList.add("hidden");
  bulkError.classList.add("hidden");
}

bulkBtn.addEventListener("click", async () => {
  if (!selectedFile) return;
  bulkBtn.disabled = true;
  bulkSummary.classList.add("hidden");
  bulkError.classList.add("hidden");
  bulkProgress.classList.remove("hidden");
  bulkProgressLabel.textContent = "Uploading...";
  bulkProgressFill.style.width = "0%";

  const formData = new FormData();
  formData.append("file", selectedFile);
  formData.append("platform", platformNameInput.value.trim());
  formData.append("save_to_disk", saveToDiskCheckbox.checked);
  formData.append("save_folder", saveFolderInput.value.trim());
  formData.append("overwrite_existing", overwriteCheckbox.checked);

  const categoryLevel = isCategoryLevelPlatform();
  let endpoint = "/tag-bulk";
  if (categoryLevel) {
    endpoint = "/tag-bulk-category";
    // None of these are .trim()'d - a real workbook's sheet name or column header can (and for
    // Flipkart's "Aug Raw Data " / Amazon's "AZ Expert Data ", does) legitimately carry a
    // trailing space, so trimming would silently break the lookup against the user's file.
    const categoryColValue = categoryColInput.value || "Category";
    formData.append("sheet_name", sheetNameInput.value);
    formData.append("category_col", categoryColValue);
    // A single-tag-column platform like Amazon leaves Sub cat 1/Sub cat 2 blank in the UI -
    // default to the SAME value as Category (client-side) so a real, matching column name is
    // always what gets sent.
    formData.append("subcat1_col", subcat1ColInput.value || categoryColValue);
    formData.append("subcat2_col", subcat2ColInput.value || categoryColValue);
    formData.append("brand_col", brandColInput.value || "brand");
  } else {
    formData.append("column_name", columnNameInput.value.trim() || "product_name");
  }

  try {
    const res = await fetch(endpoint, { method: "POST", body: formData });
    const { job_id } = await res.json();
    pollBulkJob(job_id);
  } catch (err) {
    showBulkError(String(err));
  }
});

function pollBulkJob(jobId) {
  const interval = setInterval(async () => {
    const res = await fetch(`/tag-bulk/${jobId}/status`);
    const job = await res.json();

    if (job.status === "error") {
      clearInterval(interval);
      showBulkError(job.error);
      return;
    }

    if (job.status === "processing" && job.total) {
      const pct = Math.round((job.done / job.total) * 100);
      bulkProgressLabel.textContent = `Tagging ${job.done.toLocaleString()} / ${job.total.toLocaleString()} unique SKUs (${pct}%) — ${(job.total_rows || 0).toLocaleString()} rows total`;
      bulkProgressFill.style.width = `${pct}%`;
    } else if (job.status === "reading") {
      bulkProgressLabel.textContent = "Reading file...";
    }

    if (job.status === "done") {
      clearInterval(interval);
      bulkProgressFill.style.width = "100%";
      showBulkSummary(job.summary, jobId, job.saved_path, job.save_error, job.preview);
      bulkBtn.disabled = false;
    }
  }, 500);
}

function showBulkSummary(summary, jobId, savedPath, saveError, preview) {
  bulkProgress.classList.add("hidden");
  bulkSummary.classList.remove("hidden");
  const stats = [
    { n: summary.total_rows, l: "Total rows", tone: "neutral" },
    { n: summary.unique_skus, l: "Unique SKUs", tone: "neutral" },
    { n: summary.new_skus, l: "New SKUs (never tagged before)", tone: "info" },
    { n: summary.recurring_skus, l: "Recurring SKUs (tagged before)", tone: "neutral" },
    { n: summary.clean, l: "Tagged cleanly", tone: "good" },
    { n: summary.fallback_to_existing, l: "Filled from prior tag", tone: "info" },
    { n: summary.manual_review, l: "Manual review required", tone: "warn" },
    { n: summary.flagged, l: "Flagged for review", tone: "warn" },
  ];
  if (summary.has_conflict_check) stats.push({ n: summary.conflicts, l: "L2 conflicts vs existing", tone: "neutral" });
  if (summary.verification_added) {
    stats.push({ n: summary.verification_added, l: "New rule candidates found (see Glossary tab)", tone: "info" });
  }
  bulkSummaryBody.innerHTML = stats
    .map((s) => `<div class="stat stat-${s.tone}"><div class="n">${s.n.toLocaleString()}</div><div class="l">${s.l}</div></div>`)
    .join("");
  bulkDownloadLink.href = `/tag-bulk/${jobId}/download`;

  const savedNote = document.getElementById("bulk-saved-path");
  if (savedNote) {
    if (savedPath) {
      savedNote.textContent = `Also saved on this computer at: ${savedPath}`;
      savedNote.classList.remove("warn-text");
    } else if (saveError) {
      savedNote.textContent = `Could not auto-save to disk (the file may be open elsewhere, e.g. in Excel) - use the download button above instead. Details: ${saveError}`;
      savedNote.classList.add("warn-text");
    } else {
      savedNote.textContent = "Not saved to disk (only the download above has a copy).";
      savedNote.classList.remove("warn-text");
    }
  }

  loadBulkPreview(preview || []);
}

let bulkPreviewRows = [];
let bulkPreviewNameCol = null;

const bulkPreviewSearch = document.getElementById("bulk-preview-search");
const bulkPreviewSearchClear = document.getElementById("bulk-preview-search-clear");
const bulkPreviewMethodFilter = document.getElementById("bulk-preview-method-filter");
const bulkPreviewReviewFilter = document.getElementById("bulk-preview-review-filter");

function loadBulkPreview(rows) {
  bulkPreviewRows = rows;
  bulkPreviewNameCol = rows.length ? Object.keys(rows[0])[0] : null;
  const controls = document.getElementById("bulk-preview-controls");
  controls.classList.toggle("hidden", !rows.length);
  if (rows.length) {
    const methods = [...new Set(rows.map((r) => r.Tag_Method).filter(Boolean))].sort();
    bulkPreviewMethodFilter.innerHTML = `<option value="">All tag methods</option>` +
      methods.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("");
  }
  bulkPreviewSearch.value = "";
  bulkPreviewSearchClear.hidden = true;
  bulkPreviewMethodFilter.value = "";
  bulkPreviewReviewFilter.value = "";
  renderBulkPreview();
}

function renderBulkPreview() {
  const table = document.getElementById("bulk-preview-table");
  const wrap = document.getElementById("bulk-preview-wrap");
  const empty = document.getElementById("bulk-preview-empty");
  const countLabel = document.getElementById("bulk-preview-count");
  if (!bulkPreviewRows.length) {
    table.innerHTML = "";
    wrap.classList.add("hidden");
    empty.classList.remove("hidden");
    countLabel.textContent = "";
    return;
  }
  const search = bulkPreviewSearch.value.toLowerCase();
  const methodFilter = bulkPreviewMethodFilter.value;
  const reviewFilter = bulkPreviewReviewFilter.value;
  let rows = bulkPreviewRows;
  if (methodFilter) rows = rows.filter((r) => r.Tag_Method === methodFilter);
  if (reviewFilter) rows = rows.filter((r) => r.Manual_Review_Required === reviewFilter);
  if (search) {
    rows = rows.filter((r) => Object.values(r).some((v) => String(v ?? "").toLowerCase().includes(search)));
  }
  countLabel.textContent = `${rows.length.toLocaleString()} of ${bulkPreviewRows.length.toLocaleString()} unique SKUs`;

  const cols = Object.keys(bulkPreviewRows[0]);
  const thead = `<thead><tr>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>`;
  const tbody = `<tbody>${rows
    .slice(0, 2000)
    .map((r) => `<tr>${cols.map((c) => `<td>${escapeHtml(r[c] ?? "")}</td>`).join("")}</tr>`)
    .join("")}</tbody>`;
  table.innerHTML = thead + tbody;
  empty.classList.add("hidden");
  wrap.classList.remove("hidden");
}

bulkPreviewSearch.addEventListener("input", () => {
  bulkPreviewSearchClear.hidden = !bulkPreviewSearch.value;
  renderBulkPreview();
});
bulkPreviewSearchClear.addEventListener("click", () => {
  bulkPreviewSearch.value = "";
  bulkPreviewSearchClear.hidden = true;
  bulkPreviewSearch.focus();
  renderBulkPreview();
});
bulkPreviewMethodFilter.addEventListener("change", renderBulkPreview);
bulkPreviewReviewFilter.addEventListener("change", renderBulkPreview);

function showBulkError(message) {
  bulkProgress.classList.add("hidden");
  bulkError.classList.remove("hidden");
  bulkError.textContent = `Error: ${message}`;
  bulkBtn.disabled = false;
}

// ---------------------------------------------------------------- Tab 3: Rule Glossary
let glossaryRows = [];
let pendingRaw = [];              // raw /pending-rules objects - id, keyword, modifier, l1/l2/l3, examples, reason
let pendingCategoryRaw = [];      // raw /pending-category-rules objects - id, platform, category/subcat1/subcat2,
                                   // row_count - unmapped Category x Sub-cat1 x Sub-cat2 combos from a category-
                                   // level platform (e.g. Flipkart), reviewed in the same queue as SKU rules below.
let glossarySort = { key: null, dir: 1 };
let glossaryMode = "all";         // "all" | "pending"
let glossarySelected = new Set(); // pending rule ids checked in "pending" mode (SKU and category ids share one set)
let glossaryEdits = {};           // pending rule id -> {l0?,l1,l2,l3?} - corrected category, if edited,
                                   // surviving re-renders (search/filter typing) until approved

function pendingDisplayRows() {
  const skuRows = pendingRaw.map((p) => ({
    rule_type: "PENDING REVIEW", keyword: p.keyword, modifier: p.modifier || "",
    l1: p.l1, l2: p.l2, l3: p.l3, confidence: "", notes: p.reason,
  }));
  const categoryRows = pendingCategoryRaw.map((p) => ({
    rule_type: "PENDING REVIEW (Category)",
    keyword: `${p.category} / ${p.subcat1} / ${p.subcat2}`, modifier: p.platform,
    l1: "", l2: "", l3: "", confidence: "", notes: `${p.row_count.toLocaleString()} row(s)`,
  }));
  return [...skuRows, ...categoryRows];
}

async function loadGlossary() {
  const [glossaryRes, pending, pendingCategory] = await Promise.all([
    fetch("/glossary").then((r) => r.json()),
    fetch("/pending-rules").then((r) => r.json()),
    fetch("/pending-category-rules").then((r) => r.json()),
  ]);
  glossaryRows = glossaryRes.rules;
  pendingRaw = pending;
  pendingCategoryRaw = pendingCategory;
  glossarySelected = new Set();
  glossaryEdits = {};
  document.getElementById("glossary-pending-count").textContent = pendingRaw.length + pendingCategoryRaw.length;

  const typeFilter = document.getElementById("glossary-type-filter");
  const types = [...new Set([...glossaryRows, ...pendingDisplayRows()].map((r) => r.rule_type))];
  typeFilter.innerHTML = `<option value="">All rule types</option>` +
    types.map((t) => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join("");

  renderGlossaryHead();
  renderGlossaryTable();
}

function setGlossaryMode(mode) {
  glossaryMode = mode;
  glossarySelected = new Set();
  document.getElementById("glossary-bulk-actions").classList.toggle("hidden", mode !== "pending");
  document.getElementById("glossary-hint").classList.toggle("hidden", mode === "pending");
  document.getElementById("glossary-pending-toggle").classList.toggle("primary", mode === "pending");
  document.getElementById("glossary-select-all").checked = false;
  renderGlossaryHead();
  renderGlossaryTable();
}

function renderGlossaryHead() {
  const head = document.getElementById("glossary-head");
  if (glossaryMode === "pending") {
    head.innerHTML = `<th style="width:28px"></th><th>Type</th><th>Keyword / Category combo</th>
      <th>Modifier / Platform</th><th>L0</th><th>L1</th><th>L2</th><th>L3</th>
      <th>Example(s) / Rows</th><th>Reason</th>`;
    return;
  }
  head.innerHTML = `<th data-key="rule_type">Rule Type</th><th data-key="keyword">Keyword</th>
    <th data-key="modifier">Modifier</th><th data-key="l1">L1</th><th data-key="l2">L2</th>
    <th data-key="l3">L3</th><th data-key="confidence">Confidence</th><th data-key="notes">Notes</th>`;
  head.querySelectorAll("th[data-key]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      glossarySort.dir = glossarySort.key === key ? -glossarySort.dir : 1;
      glossarySort.key = key;
      renderGlossaryTable();
    });
  });
}

function allPendingCombined() {
  const sku = pendingRaw.map((r) => ({ ...r, kind: "sku" }));
  const category = pendingCategoryRaw.map((r) => ({ ...r, kind: "category" }));
  return [...sku, ...category];
}

function currentPendingFiltered() {
  const search = document.getElementById("glossary-search").value.toLowerCase();
  const typeFilter = document.getElementById("glossary-type-filter").value;
  let rows = allPendingCombined();
  if (typeFilter === "PENDING REVIEW") rows = rows.filter((r) => r.kind === "sku");
  else if (typeFilter === "PENDING REVIEW (Category)") rows = rows.filter((r) => r.kind === "category");
  else if (typeFilter) rows = [];
  if (search) {
    rows = rows.filter((r) =>
      (r.kind === "sku"
        ? [r.keyword, r.modifier, r.l1, r.l2, r.l3, r.reason, ...(r.examples || [])]
        : [r.category, r.subcat1, r.subcat2, r.platform])
        .some((v) => String(v || "").toLowerCase().includes(search))
    );
  }
  return rows;
}

function renderGlossaryTable() {
  const tbody = document.getElementById("glossary-body");

  if (glossaryMode === "pending") {
    const rows = currentPendingFiltered();
    tbody.innerHTML = rows
      .map((r) => {
        const checked = glossarySelected.has(r.id) ? "checked" : "";
        const edit = glossaryEdits[r.id] || {};
        if (r.kind === "category") {
          const l0 = edit.l0 !== undefined ? edit.l0 : "";
          const l1 = edit.l1 !== undefined ? edit.l1 : "";
          const l2 = edit.l2 !== undefined ? edit.l2 : "";
          return `<tr data-rule-type="PENDING REVIEW (Category)">
            <td><input type="checkbox" class="glossary-row-check" data-id="${escapeHtml(r.id)}" ${checked}></td>
            <td>Category</td>
            <td>${escapeHtml(`${r.category} / ${r.subcat1} / ${r.subcat2}`)}</td>
            <td>${escapeHtml(r.platform)}</td>
            <td><input type="text" class="l123-input" data-id="${escapeHtml(r.id)}" data-field="l0" value="${escapeHtml(l0)}"></td>
            <td><input type="text" class="l123-input" data-id="${escapeHtml(r.id)}" data-field="l1" value="${escapeHtml(l1)}"></td>
            <td><input type="text" class="l123-input" data-id="${escapeHtml(r.id)}" data-field="l2" value="${escapeHtml(l2)}"></td>
            <td></td>
            <td class="wrap">${r.row_count.toLocaleString()} row(s)</td>
            <td class="wrap">Unmapped category combo - not in the mapping table</td>
          </tr>`;
        }
        const examples = (r.examples || []).join("; ");
        const l1 = edit.l1 !== undefined ? edit.l1 : (r.l1 || "");
        const l2 = edit.l2 !== undefined ? edit.l2 : (r.l2 || "");
        const l3 = edit.l3 !== undefined ? edit.l3 : (r.l3 || "");
        return `<tr data-rule-type="PENDING REVIEW">
          <td><input type="checkbox" class="glossary-row-check" data-id="${escapeHtml(r.id)}" ${checked}></td>
          <td>SKU</td>
          <td>${escapeHtml(r.keyword)}</td>
          <td>${escapeHtml(r.modifier || "")}</td>
          <td></td>
          <td><input type="text" class="l123-input" data-id="${escapeHtml(r.id)}" data-field="l1" value="${escapeHtml(l1)}"></td>
          <td><input type="text" class="l123-input" data-id="${escapeHtml(r.id)}" data-field="l2" value="${escapeHtml(l2)}"></td>
          <td><input type="text" class="l123-input" data-id="${escapeHtml(r.id)}" data-field="l3" value="${escapeHtml(l3)}"></td>
          <td class="wrap">${escapeHtml(examples)}</td>
          <td class="wrap">${escapeHtml(r.reason || "")}</td>
        </tr>`;
      })
      .join("");
    tbody.querySelectorAll(".glossary-row-check").forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) glossarySelected.add(cb.dataset.id);
        else glossarySelected.delete(cb.dataset.id);
        updateGlossaryBulkButtons();
      });
    });
    tbody.querySelectorAll(".l123-input").forEach((el) => {
      el.addEventListener("input", () => {
        const id = el.dataset.id;
        if (!glossaryEdits[id]) glossaryEdits[id] = {};
        glossaryEdits[id][el.dataset.field] = el.value;
      });
    });
    updateGlossaryBulkButtons();
    return;
  }

  const search = document.getElementById("glossary-search").value.toLowerCase();
  const typeFilter = document.getElementById("glossary-type-filter").value;
  let rows = [...glossaryRows, ...pendingDisplayRows()];
  if (typeFilter) rows = rows.filter((r) => r.rule_type === typeFilter);
  if (search) {
    rows = rows.filter((r) => Object.values(r).some((v) => String(v || "").toLowerCase().includes(search)));
  }
  if (glossarySort.key) {
    rows.sort((a, b) => {
      const av = String(a[glossarySort.key] || "");
      const bv = String(b[glossarySort.key] || "");
      return av.localeCompare(bv) * glossarySort.dir;
    });
  }
  tbody.innerHTML = rows
    .map(
      (r) => `<tr data-rule-type="${escapeHtml(r.rule_type)}">
        <td>${escapeHtml(r.rule_type)}</td>
        <td class="wrap">${escapeHtml(r.keyword)}</td>
        <td>${escapeHtml(r.modifier)}</td>
        <td>${escapeHtml(r.l1)}</td>
        <td>${escapeHtml(r.l2)}</td>
        <td>${escapeHtml(r.l3)}</td>
        <td>${escapeHtml(r.confidence)}</td>
        <td class="wrap">${escapeHtml(r.notes)}</td>
      </tr>`
    )
    .join("");
}

function updateGlossaryBulkButtons() {
  const n = glossarySelected.size;
  document.getElementById("glossary-approve-btn").disabled = n === 0;
  document.getElementById("glossary-reject-btn").disabled = n === 0;
}

document.getElementById("glossary-search").addEventListener("input", renderGlossaryTable);
document.getElementById("glossary-type-filter").addEventListener("change", renderGlossaryTable);
document.getElementById("glossary-refresh").addEventListener("click", loadGlossary);
document.getElementById("glossary-pending-toggle").addEventListener("click", () => {
  setGlossaryMode(glossaryMode === "pending" ? "all" : "pending");
});
document.getElementById("glossary-select-all").addEventListener("change", (e) => {
  const rows = currentPendingFiltered();
  if (e.target.checked) rows.forEach((r) => glossarySelected.add(r.id));
  else rows.forEach((r) => glossarySelected.delete(r.id));
  renderGlossaryTable();
});

const glossaryApproveBtn = document.getElementById("glossary-approve-btn");
const glossaryRejectBtn = document.getElementById("glossary-reject-btn");
const glossaryBulkStatus = document.getElementById("glossary-bulk-status");

glossaryApproveBtn.addEventListener("click", async () => {
  const ids = [...glossarySelected];
  if (!ids.length) return;
  // Send whatever's currently in the L0/L1/L2/L3 inputs for each selected row - the auto-suggested
  // category on an Auto-Verification candidate, edited or not, not necessarily the original.
  const skuIds = ids.filter((id) => pendingRaw.some((r) => r.id === id));
  const categoryIds = ids.filter((id) => pendingCategoryRaw.some((r) => r.id === id));
  const skuApprovals = skuIds.map((id) => {
    const original = pendingRaw.find((r) => r.id === id) || {};
    const edit = glossaryEdits[id] || {};
    return {
      id,
      l1: edit.l1 !== undefined ? edit.l1 : (original.l1 || ""),
      l2: edit.l2 !== undefined ? edit.l2 : (original.l2 || ""),
      l3: edit.l3 !== undefined ? edit.l3 : (original.l3 || ""),
    };
  });
  const categoryApprovals = categoryIds.map((id) => {
    const edit = glossaryEdits[id] || {};
    return { id, l0: edit.l0 || "", l1: edit.l1 || "", l2: edit.l2 || "" };
  });
  const missingCategory = categoryApprovals.filter((a) => !a.l0 || !a.l1 || !a.l2);
  if (missingCategory.length) {
    glossaryBulkStatus.textContent = `Fill in L0, L1 and L2 for every selected category row first (${missingCategory.length} incomplete).`;
    return;
  }

  glossaryBulkStatus.textContent = "Approving...";
  try {
    let approvedCount = 0;
    const errors = [];
    if (skuApprovals.length) {
      const res = await fetch("/pending-rules/bulk-approve", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approvals: skuApprovals }),
      });
      if (res.ok) approvedCount += (await res.json()).count;
      else errors.push((await res.json().catch(() => ({ detail: res.statusText }))).detail);
    }
    if (categoryApprovals.length) {
      const res = await fetch("/pending-category-rules/bulk-approve", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approvals: categoryApprovals }),
      });
      if (res.ok) approvedCount += (await res.json()).count;
      else errors.push((await res.json().catch(() => ({ detail: res.statusText }))).detail);
    }
    glossaryBulkStatus.textContent = errors.length
      ? `Approved ${approvedCount} rule(s), error(s): ${errors.join("; ")}`
      : `Approved ${approvedCount} rule(s) - live immediately.`;
    await loadGlossary();
  } catch (err) {
    glossaryBulkStatus.textContent = `Error: ${String(err)}`;
  }
});

glossaryRejectBtn.addEventListener("click", async () => {
  const ids = [...glossarySelected];
  if (!ids.length) return;
  if (!confirm(`Reject ${ids.length} pending rule(s)? This removes them from the queue permanently.`)) return;
  const skuIds = ids.filter((id) => pendingRaw.some((r) => r.id === id));
  const categoryIds = ids.filter((id) => pendingCategoryRaw.some((r) => r.id === id));

  glossaryBulkStatus.textContent = "Rejecting...";
  try {
    let rejectedCount = 0;
    const errors = [];
    if (skuIds.length) {
      const res = await fetch("/pending-rules/bulk-reject", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: skuIds }),
      });
      if (res.ok) rejectedCount += (await res.json()).rejected_count;
      else errors.push((await res.json().catch(() => ({ detail: res.statusText }))).detail);
    }
    if (categoryIds.length) {
      const res = await fetch("/pending-category-rules/bulk-reject", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: categoryIds }),
      });
      if (res.ok) rejectedCount += (await res.json()).rejected_count;
      else errors.push((await res.json().catch(() => ({ detail: res.statusText }))).detail);
    }
    glossaryBulkStatus.textContent = errors.length
      ? `Rejected ${rejectedCount} rule(s), error(s): ${errors.join("; ")}`
      : `Rejected ${rejectedCount} rule(s).`;
    await loadGlossary();
  } catch (err) {
    glossaryBulkStatus.textContent = `Error: ${String(err)}`;
  }
});

// ------------------------------------------------- Suggest a rule (Glossary tab)
const srFields = ["sr-rule-type", "sr-keyword", "sr-modifier", "sr-l1", "sr-l2", "sr-l3"];
const srCheckBtn = document.getElementById("sr-check-btn");
const srSubmitBtn = document.getElementById("sr-submit-btn");
const srResult = document.getElementById("sr-result");
let srCheckedSignature = null;

function srCurrentValues() {
  return {
    ruleType: document.getElementById("sr-rule-type").value,
    keyword: document.getElementById("sr-keyword").value.trim(),
    modifier: document.getElementById("sr-modifier").value.trim(),
    l1: document.getElementById("sr-l1").value.trim(),
    l2: document.getElementById("sr-l2").value.trim(),
    l3: document.getElementById("sr-l3").value.trim(),
  };
}
function srSignature(v) {
  return JSON.stringify(v);
}
// Changing any field after a Check invalidates it - Submit must always follow a fresh Check
// against exactly what's about to be sent, never a check the user has since edited past.
srFields.forEach((id) =>
  document.getElementById(id).addEventListener("input", () => { srSubmitBtn.disabled = true; })
);

srCheckBtn.addEventListener("click", async () => {
  const v = srCurrentValues();
  if (!v.keyword) {
    srResult.classList.remove("hidden");
    srResult.innerHTML = `<p class="warn-text">Keyword is required.</p>`;
    return;
  }
  srCheckBtn.disabled = true;
  try {
    const res = await fetch("/pending-rules/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keyword: v.keyword, modifier: v.modifier, l1: v.l1, l2: v.l2, l3: v.l3 }),
    });
    const data = await res.json();
    srResult.classList.remove("hidden");
    const cur = data.current_tag || {};
    const curPath = [cur.L1, cur.L2, cur.L3].filter(Boolean).join(" > ") || "Not mapped";
    let html = `<p><strong>Test text:</strong> "${escapeHtml(data.test_text)}"</p>
      <p><strong>The engine currently tags this as:</strong> ${escapeHtml(curPath)} (method: ${escapeHtml(cur.method || "-")})</p>`;
    if (data.duplicate) {
      html += `<p class="warn-text"><strong>Already covered:</strong> found in ${escapeHtml(data.duplicate.source)}
        &rarr; ${escapeHtml((data.duplicate.l1_l2_l3 || []).filter(Boolean).join(" > "))}.
        Submitting this would duplicate it.</p>`;
    } else {
      html += `<p>No existing rule covers this exact keyword/modifier - safe to submit if the L1/L2/L3 above is correct.</p>`;
    }
    srResult.innerHTML = html;
    srCheckedSignature = srSignature(v);
    srSubmitBtn.disabled = !!data.duplicate || !v.l1 || !v.l2;
  } catch (err) {
    srResult.classList.remove("hidden");
    srResult.innerHTML = `<p class="warn-text">Check failed: ${escapeHtml(String(err))}</p>`;
  } finally {
    srCheckBtn.disabled = false;
  }
});

srSubmitBtn.addEventListener("click", async () => {
  const v = srCurrentValues();
  if (srSignature(v) !== srCheckedSignature) {
    srResult.classList.remove("hidden");
    srResult.innerHTML = `<p class="warn-text">Run Check again - these fields changed since the last check.</p>`;
    return;
  }
  srSubmitBtn.disabled = true;
  try {
    await fetch("/pending-rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        keyword: v.keyword, modifier: v.modifier, l1: v.l1, l2: v.l2, l3: v.l3,
        reason: document.getElementById("sr-reason").value.trim() || `Manual (${v.ruleType})`,
      }),
    });
    srResult.classList.remove("hidden");
    srResult.innerHTML = `<p>Submitted - see it under "PENDING REVIEW" in the table above.</p>`;
    srFields.forEach((id) => { document.getElementById(id).value = document.getElementById(id).tagName === "SELECT" ? document.getElementById(id).value : ""; });
    document.getElementById("sr-reason").value = "";
    srCheckedSignature = null;
    loadGlossary();
  } catch (err) {
    srResult.innerHTML = `<p class="warn-text">Submit failed: ${escapeHtml(String(err))}</p>`;
    srSubmitBtn.disabled = false;
  }
});

// ---------------------------------------------------------------- Tab 3: Category Topline
let toplineMeta = null;
let toplineLoaded = false;
let lastToplineRows = [];

// Standard practice across the whole portal: missing/not-applicable data reads as "NA", never a
// silent blank cell - confirmed explicitly, applies everywhere these shared formatters are used
// (Category Topline, Brand Share Cons, Master Data, the new Expert MoM/YoY preview columns, ...).
function fmtPct(v) {
  return v === null || v === undefined ? "NA" : `${(v * 100).toFixed(1)}%`;
}
// Default is 1 decimal for the normal (read-only) report tables - callers that need finer
// precision (Manual Change's edit/preview views, Master Data) pass digits=2 explicitly.
function fmtNum(v, digits = 1) {
  return v === null || v === undefined ? "NA" : v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

async function initTopline() {
  if (!toplineLoaded) {
    toplineMeta = await fetch("/category-topline/meta").then((r) => r.json());
    populatePlatformOptions(document.getElementById("topline-platform"), toplineMeta.variants.all);
    toplineLoaded = true;
  }
  await refreshToplineMonthPanelForPlatform();
  renderTopline();
  renderToplineGmvInput();
}

// Re-fetched per platform (not just once at tab-init) - a platform whose expert data just got
// finalized can offer a month no OTHER platform has yet (see /category-topline/meta's platform
// param), so the month list itself is platform-specific, not shared globally like it used to be.
let toplineMonthPanelToken = 0;
async function refreshToplineMonthPanelForPlatform() {
  const platform = document.getElementById("topline-platform").value;
  if (!platform) return;
  const myToken = ++toplineMonthPanelToken;
  const meta = await fetch(`/category-topline/meta?platform=${encodeURIComponent(platform)}`).then((r) => r.json());
  if (myToken !== toplineMonthPanelToken) return;  // a newer platform switch has since started
  initToplineMonthPanel(meta.months);
  populateOverrideMonthSelect(meta.months);
}

// ------------------------------------------- Category Topline: manual YoY/MoM targets (no expert
// data) - lets an analyst enter a target growth rate for a category Expert data doesn't cover at
// all, so set-total-gmv's forecast can lean toward it with the exact same guardrail/trust level
// real Expert data gets (see _manual_yoy_override_lookup on the backend).
const overrideMonthSel = document.getElementById("topline-override-month");
const overrideCategorySel = document.getElementById("topline-override-category");
const overrideTypeSel = document.getElementById("topline-override-type");
const overrideValueInput = document.getElementById("topline-override-value");
const overrideStatus = document.getElementById("topline-override-status");

function populateOverrideMonthSelect(months) {
  const prev = overrideMonthSel.value;
  overrideMonthSel.innerHTML = months.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("");
  if (months.includes(prev)) overrideMonthSel.value = prev;
  else if (months.length) overrideMonthSel.value = months[months.length - 1];  // default to the latest (usually the in-progress) month
  loadToplineOverridesTable();
}

function populateOverrideCategorySelect() {
  const prev = overrideCategorySel.value;
  const rows = lastToplineRows.filter((r) => ["l0", "l0_flat", "l1", "l2"].includes(r.kind));
  overrideCategorySel.innerHTML = rows
    .map((r) => `<option value="${escapeHtml(r.row_key)}">${"&nbsp;".repeat(r.level * 2)}${escapeHtml(r.label)}</option>`)
    .join("");
  if (rows.some((r) => r.row_key === prev)) overrideCategorySel.value = prev;
}

async function loadToplineOverridesTable() {
  const platform = document.getElementById("topline-platform").value;
  const month = overrideMonthSel.value;
  const body = document.getElementById("topline-override-body");
  if (!platform || !month) {
    body.innerHTML = "";
    return;
  }
  const overrides = await fetch(
    `/category-topline/yoy-overrides?platform=${encodeURIComponent(platform)}&month=${encodeURIComponent(month)}`
  ).then((r) => r.json());
  const labelByKey = new Map(lastToplineRows.map((r) => [r.row_key, r.label]));
  const rowKeys = Object.keys(overrides);
  if (!rowKeys.length) {
    body.innerHTML = `<tr><td colspan="4" class="hint">No manual targets set for this month.</td></tr>`;
    return;
  }
  body.innerHTML = rowKeys
    .map((rowKey) => {
      const o = overrides[rowKey];
      const label = labelByKey.get(rowKey) || rowKey;
      return `<tr><td>${escapeHtml(label)}</td><td>${o.type === "yoy" ? "YoY" : "MoM"}</td>
        <td>${o.value}%</td>
        <td><button type="button" class="override-remove" data-row-key="${escapeHtml(rowKey)}">Remove</button></td></tr>`;
    })
    .join("");
  body.querySelectorAll(".override-remove").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await fetch("/category-topline/yoy-overrides", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ platform, month, row_key: btn.dataset.rowKey }),
      });
      loadToplineOverridesTable();
    });
  });
}

overrideMonthSel.addEventListener("change", loadToplineOverridesTable);

document.getElementById("topline-override-save").addEventListener("click", async () => {
  const platform = document.getElementById("topline-platform").value;
  const month = overrideMonthSel.value;
  const rowKey = overrideCategorySel.value;
  const type = overrideTypeSel.value;
  const value = parseFloat(overrideValueInput.value);
  if (!platform || !month || !rowKey || isNaN(value)) {
    overrideStatus.textContent = "Pick a category and enter a value first.";
    return;
  }
  overrideStatus.textContent = "Saving...";
  const res = await fetch("/category-topline/yoy-overrides", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ platform, month, row_key: rowKey, type, value }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    overrideStatus.textContent = `Error: ${err.detail}`;
    return;
  }
  overrideValueInput.value = "";
  overrideStatus.textContent = "Saved - will apply next time Save & Compute runs.";
  loadToplineOverridesTable();
});

// ------------------------------------------- Category Topline: hard-input (Total GMV/MoM%/YoY%)
// Lives here now, not in Expert Data Tagging - as soon as Monthly Setup locks a platform as
// "no expert data" for whichever month is currently active, its hard input becomes available
// right here, below the platform/month filters. Saving immediately (re)computes the forecast -
// same set-total-gmv endpoint as before, just a different place to call it from - and refreshes
// the month list/table so the newly-written month shows up without a manual Refresh click.
//
// The review-progression controls (Send to SL Review -> SL Approve -> Manager Finalize) are a
// SEPARATE row, up in the top filter bar beside Manual Change - same four backend stages
// (analyst -> pending_sl_review -> pending_manager_review -> finalized), same "once SL has sent
// it on, only a Manager password can move it again" rule the backend already enforces
// (editing/re-entering Total GMV is gated to the analyst stage server-side; the hard-input row
// just mirrors that by only showing the input fields while status is "analyst").
let toplineCtContext = { activeMonth: null, platform: null };

function toplineCtHint() {
  return document.getElementById("topline-gmv-hint") || document.getElementById("topline-ct-hint");
}

async function ctTabAction(action) {
  const { activeMonth, platform } = toplineCtContext;
  const hint = toplineCtHint();
  try {
    const res = await fetch(
      `/monthly-setup/${encodeURIComponent(activeMonth)}/${encodeURIComponent(platform)}/category-topline/${action}`,
      { method: "POST" }
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      if (hint) hint.textContent = `Error: ${err.detail}`;
      return;
    }
    await renderToplineGmvInput();
  } catch (err) {
    if (hint) hint.textContent = `Error: ${String(err)}`;
  }
}

async function ctTabPasswordAction(action, passwordInputId) {
  const { activeMonth, platform } = toplineCtContext;
  const hint = toplineCtHint();
  const password = document.getElementById(passwordInputId).value;
  try {
    const res = await fetch(
      `/monthly-setup/${encodeURIComponent(activeMonth)}/${encodeURIComponent(platform)}/category-topline/${action}`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }) }
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      if (hint) hint.textContent = `Error: ${err.detail}`;
      return;
    }
    await renderToplineGmvInput();
  } catch (err) {
    if (hint) hint.textContent = `Error: ${String(err)}`;
  }
}

async function renderToplineGmvInput() {
  const inputContainer = document.getElementById("topline-gmv-input-row");
  const reviewContainer = document.getElementById("topline-ct-review");
  const platform = document.getElementById("topline-platform").value;
  if (!platform) { inputContainer.innerHTML = ""; reviewContainer.innerHTML = ""; return; }

  const { month: activeMonth } = await fetch("/monthly-setup/active").then((r) => r.json());
  if (!activeMonth) { inputContainer.innerHTML = ""; reviewContainer.innerHTML = ""; return; }

  const board = await fetch(`/monthly-setup/${encodeURIComponent(activeMonth)}/board`).then((r) => r.json());
  const p = board.platforms[platform];
  // Every locked platform gets the Total GMV hard-input, expert-data or not - it was wrongly
  // hidden for expert-data platforms entirely (p.has_expert_data used to gate this off), even
  // though Category Topline's forecast engine explicitly supports (and needs) a manual Total GMV
  // for EVERY platform, expert-data ones included - the expert-alignment guardrails only kick in
  // once this number exists, same as a no-expert-data platform's plain forecast.
  if (!p) { inputContainer.innerHTML = ""; reviewContainer.innerHTML = ""; return; }

  toplineCtContext = { activeMonth, platform };
  const ct = p.category_topline || { status: "analyst", total_gmv_input: null };
  const resolvedText = ct.total_gmv_input != null
    ? `Resolved Total BPC GMV: ${Number(ct.total_gmv_input).toLocaleString(undefined, { maximumFractionDigits: 2 })}`
    : "Not yet entered";

  // ---- Row 1 (top filter bar, beside Manual Change): review-progression controls only.
  const stageNote = CT_STAGE_LABEL[ct.status] || ct.status;
  if (ct.status === "analyst") {
    reviewContainer.innerHTML = `
      <button id="topline-gmv-sendreview" ${ct.total_gmv_input == null ? "disabled" : ""}>Send to SL Review</button>
      <span class="hint" id="topline-ct-hint"></span>
    `;
    document.getElementById("topline-gmv-sendreview").addEventListener("click", () => ctTabAction("send-to-review"));
  } else if (ct.status === "pending_sl_review") {
    reviewContainer.innerHTML = `
      <span class="hint">${escapeHtml(stageNote)}</span>
      <button id="topline-ct-sendback-analyst">Send back to analyst</button>
      <input type="password" id="topline-ct-sl-password" placeholder="SL password" style="max-width:140px;">
      <button id="topline-ct-sl-approve" class="primary">SL Approve</button>
      <span class="hint" id="topline-ct-hint"></span>
    `;
    document.getElementById("topline-ct-sendback-analyst").addEventListener("click", () => ctTabAction("send-back-to-analyst"));
    document.getElementById("topline-ct-sl-approve").addEventListener("click", () => ctTabPasswordAction("sl-approve", "topline-ct-sl-password"));
  } else if (ct.status === "pending_manager_review") {
    reviewContainer.innerHTML = `
      <span class="hint">${escapeHtml(stageNote)}</span>
      <button id="topline-ct-sendback-sl">Send back to SL</button>
      <input type="password" id="topline-ct-mgr-password" placeholder="Manager password" style="max-width:150px;">
      <button id="topline-ct-mgr-finalize" class="primary">Manager Finalize</button>
      <span class="hint" id="topline-ct-hint"></span>
    `;
    document.getElementById("topline-ct-sendback-sl").addEventListener("click", () => ctTabAction("send-back-to-sl"));
    document.getElementById("topline-ct-mgr-finalize").addEventListener("click", () => ctTabPasswordAction("manager-finalize", "topline-ct-mgr-password"));
  } else if (ct.status === "finalized") {
    reviewContainer.innerHTML = `
      <span class="hint">Finalized${ct.finalized_at ? " on " + escapeHtml(ct.finalized_at) : ""}.</span>
      <input type="password" id="topline-ct-mgr-password" placeholder="Manager password" style="max-width:150px;">
      <button id="topline-ct-reopen">Reopen (Manager)</button>
      <span class="hint" id="topline-ct-hint"></span>
    `;
    document.getElementById("topline-ct-reopen").addEventListener("click", () => ctTabPasswordAction("reopen", "topline-ct-mgr-password"));
  }

  // ---- Row 2 (below the filters): the hard input itself, editable only at the analyst stage.
  if (ct.status === "analyst") {
    const mode = ct.gmv_input_mode || "gmv";
    const prefillValue = ct.gmv_input_value ?? ct.total_gmv_input ?? "";
    inputContainer.innerHTML = `
      <label style="margin:0;"><strong>${escapeHtml(activeMonth)} hard input (${escapeHtml(platform)}):</strong></label>
      <select id="topline-gmv-mode">
        <option value="gmv" ${mode === "gmv" ? "selected" : ""}>Total GMV</option>
        <option value="mom" ${mode === "mom" ? "selected" : ""}>MoM %</option>
        <option value="yoy" ${mode === "yoy" ? "selected" : ""}>YoY %</option>
      </select>
      <input type="number" step="0.01" id="topline-gmv-value" placeholder="${mode === "gmv" ? "Total BPC GMV" : "% change"}" value="${prefillValue}" style="max-width:120px;">
      <button id="topline-gmv-save" class="primary">Save &amp; Compute</button>
      <span class="hint" id="topline-gmv-hint">${escapeHtml(resolvedText)}</span>
    `;
    document.getElementById("topline-gmv-save").addEventListener("click", async () => {
      const hint = document.getElementById("topline-gmv-hint");
      const value = parseFloat(document.getElementById("topline-gmv-value").value);
      if (isNaN(value)) { hint.textContent = "Enter a number first."; return; }
      const selMode = document.getElementById("topline-gmv-mode").value;
      const body = selMode === "gmv" ? { total_gmv: value } : selMode === "mom" ? { mom_pct: value } : { yoy_pct: value };
      hint.textContent = "Computing...";
      try {
        const res = await fetch(
          `/monthly-setup/${encodeURIComponent(activeMonth)}/${encodeURIComponent(platform)}/category-topline/set-total-gmv`,
          { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
        );
        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: res.statusText }));
          hint.textContent = `Error: ${err.detail}`;
          return;
        }
        toplineLoaded = false; // the new month column means the meta/month list must be re-fetched
        await initTopline();
      } catch (err) {
        hint.textContent = `Error: ${String(err)}`;
      }
    });
  } else {
    inputContainer.innerHTML = `<span class="hint"><strong>${escapeHtml(activeMonth)} (${escapeHtml(platform)}):</strong> ${escapeHtml(resolvedText)} - ${escapeHtml(stageNote)}.</span>`;
  }
}

// ------------------------------------------- Category Topline: month multi-select picker
// Replaces a From/To contiguous range with an explicit pick of whichever specific months the
// user wants on screen (plus All/None) - MoM/YoY growth still come through correctly for a
// shown month even if its comparator month isn't itself selected, since the backend computes
// both over the full month range before filtering down to the pick.
function initToplineMonthPanel(months) {
  const list = document.getElementById("topline-month-list");
  list.innerHTML = months
    .map((m) => `<label class="month-multiselect-item"><input type="checkbox" class="topline-month-cb" value="${escapeHtml(m)}" checked> ${escapeHtml(m)}</label>`)
    .join("");
  list.querySelectorAll(".topline-month-cb").forEach((cb) =>
    cb.addEventListener("change", () => {
      updateToplineMonthBtnLabel();
      renderTopline();
    })
  );
  updateToplineMonthBtnLabel();
}

function getSelectedToplineMonths() {
  return Array.from(document.querySelectorAll(".topline-month-cb:checked")).map((cb) => cb.value);
}

function updateToplineMonthBtnLabel() {
  const all = document.querySelectorAll(".topline-month-cb").length;
  const sel = getSelectedToplineMonths().length;
  const btn = document.getElementById("topline-month-btn");
  const label = sel === all ? "All months" : sel === 0 ? "No months" : `${sel} month${sel > 1 ? "s" : ""}`;
  btn.textContent = `${label} ▾`;
}

document.getElementById("topline-month-btn").addEventListener("click", () => {
  document.getElementById("topline-month-panel").classList.toggle("hidden");
});
document.getElementById("topline-month-all").addEventListener("click", () => {
  document.querySelectorAll(".topline-month-cb").forEach((cb) => (cb.checked = true));
  updateToplineMonthBtnLabel();
  renderTopline();
});
document.getElementById("topline-month-none").addEventListener("click", () => {
  document.querySelectorAll(".topline-month-cb").forEach((cb) => (cb.checked = false));
  updateToplineMonthBtnLabel();
  renderTopline();
});
document.addEventListener("click", (e) => {
  const wrap = document.getElementById("topline-month-select");
  if (wrap && !wrap.contains(e.target)) document.getElementById("topline-month-panel").classList.add("hidden");
});

function populatePlatformOptions(select, platforms) {
  const current = select.value;
  select.innerHTML = platforms.map((p) => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join("");
  if (platforms.includes(current)) select.value = current;
}

// Shared by Category Topline and Brand Share Cons - fills both ends of a date-range picker
// with every month the workbook actually has, defaulting to the full range (From=earliest,
// To=latest) so a first-time visit shows everything rather than an empty/partial view.
function populateMonthRange(fromSel, toSel, months) {
  const keepFrom = months.includes(fromSel.value) ? fromSel.value : months[0];
  const keepTo = months.includes(toSel.value) ? toSel.value : months[months.length - 1];
  const opts = months.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("");
  fromSel.innerHTML = opts;
  toSel.innerHTML = opts;
  fromSel.value = keepFrom;
  toSel.value = keepTo;
}

document.getElementById("topline-platform").addEventListener("change", async () => {
  await refreshToplineMonthPanelForPlatform();
  renderTopline();
  renderToplineGmvInput();
});
document.getElementById("topline-refresh").addEventListener("click", async () => {
  await fetch("/category-topline/reload", { method: "POST" });
  toplineLoaded = false;
  initTopline();
});

// View 1/View 2 toggle - shared by BOTH the normal read-only table and Manual Change, since the
// selector itself lives in the one filter bar above both (confirmed: same control, not a
// per-tab setting). View 1 shows only Total/L0/L1 (the "how is the platform/top categories
// moving" overview); View 2 is the full L2 breakdown, same as always.
function toplineViewKeepsRow(kind) {
  const view = document.getElementById("topline-view-select").value;
  if (view === "2") return true;
  return kind === "total" || kind === "l0" || kind === "l0_flat" || kind === "l1";
}
document.getElementById("topline-view-select").addEventListener("change", () => {
  renderTopline();
  if (topManualToggle.checked) reapplyTopEditView();
});

// Switching View 1/2 while in Manual Change must NOT discard in-progress edits - it only changes
// which rows are visible, never re-fetches from the server (that's what loadTopEditView does, and
// it would wipe out anything typed so far). renderTopEditTable reads edited values straight out of
// the persistent topEditOverrides map (survives even for a row the new filter hides), so this only
// needs to re-render against the new row filter, then re-run the preview so cascaded L2 siblings/
// GMV/roll-ups reflect the edit even in a view that doesn't show the edited row itself (e.g. an L1
// edit made in View 1, inspected at L2 in View 2).
function reapplyTopEditView() {
  if (!topEditData) return;
  renderTopEditTable(topEditData.rows.filter((r) => toplineViewKeepsRow(r.kind)), topEditData.group_checks);
  if (collectTopEdits().length) {
    runTopEditPreview({ silent: true });
  } else {
    resetLiveTable();
  }
}

// Guards against an out-of-order response overwriting a newer one - e.g. switching platforms
// quickly fires a fetch for the new platform while the OLD platform's (possibly slower) request
// is still in flight; without this, whichever response happens to arrive LAST wins regardless of
// which was actually requested last, silently rendering stale/wrong data.
let toplineRenderToken = 0;

async function renderTopline() {
  const variant = "all";
  const platform = document.getElementById("topline-platform").value;
  if (!platform) return;
  const head = document.getElementById("topline-head");
  const subHead = document.getElementById("topline-subhead");
  const body = document.getElementById("topline-body");

  const selected = getSelectedToplineMonths();
  if (!selected.length) {
    head.innerHTML = `<th>Category</th>`;
    subHead.innerHTML = "";
    body.innerHTML = `<tr><td class="hint">Select at least one month above.</td></tr>`;
    return;
  }

  const myToken = ++toplineRenderToken;
  const params = new URLSearchParams({ variant, platform, months: selected.join(",") });
  const data = await fetch(`/category-topline?${params.toString()}`).then((r) => r.json());
  if (myToken !== toplineRenderToken) return;  // a newer render has since started - discard this one

  lastToplineRows = data.rows;
  populateOverrideCategorySelect();

  // Grouped by metric first (all GMV columns, then all MS, then all MoM, then all YoY), each
  // metric block covering every selected month - matches the layout of the original manual
  // sample sheet, rather than grouping by month with the 4 metrics interleaved underneath it.
  // Forecast/Trend are MS-only diagnostics from the no-expert-data forecasting engine, for ONE
  // specific month - the one currently being forecasted - never one blank column per selected
  // month like the first four blocks. Found by scanning for whichever displayed month actually
  // has a non-null forecast/trend value anywhere; the whole block is omitted if none of the
  // currently selected months is a forecasted one.
  const allIdx = data.months.map((_, i) => i);
  const fcIdx = data.months.findIndex((_, i) => data.rows.some((r) => r.forecast[i] != null || r.trend[i] != null));

  const METRICS = [
    { key: "gmv", label: "GMV", fmt: fmtNum, idxs: allIdx },
    { key: "share", label: "MS", fmt: fmtPct, idxs: allIdx },
    // MoM over 100% either way is almost always either a data artifact (a near-zero prior month
    // exploding the ratio) or worth a second look either way - flagged rather than hidden.
    { key: "growth", label: "MoM", fmt: fmtPct, idxs: allIdx, flag: (v) => v !== null && v !== undefined && Math.abs(v) > 1 },
    { key: "yoy", label: "YoY", fmt: fmtPct, idxs: allIdx },
  ];
  if (fcIdx !== -1) {
    METRICS.push({ key: "forecast", label: "Forecast (MS)", fmt: fmtPct, idxs: [fcIdx] });
    METRICS.push({ key: "trend", label: "Trend (MS)", fmt: fmtPct, idxs: [fcIdx] });
    // Only non-null for an expert-data platform whose Total GMV has been (re)computed since its
    // expert data was consolidated into Master Data - what the final MS was aligned toward, shown
    // so the analyst/SL can judge the alignment and manually override (Manual Change) if it looks off.
    if (data.rows.some((r) => r.expert_mom[fcIdx] != null || r.expert_yoy[fcIdx] != null)) {
      // expert_flag[i] is true when this row is worth a manual look - either the Market Share
      // guardrail flagged/capped the move (3pp+ from the plain baseline, hard-capped past 4pp),
      // or the non-driving signal (MoM when YoY drove the adjustment, or vice versa) disagrees
      // with the final result by more than its own tolerance band - flagged on both Expert
      // columns so it's visible wherever the analyst is looking.
      const expertFlagged = (v, r, i) => r.expert_flag && r.expert_flag[i];
      METRICS.push({ key: "expert_mom", label: "Expert (MoM)", fmt: fmtPct, idxs: [fcIdx], flag: expertFlagged });
      METRICS.push({ key: "expert_yoy", label: "Expert (YoY)", fmt: fmtPct, idxs: [fcIdx], flag: expertFlagged });
    }
  }

  // Expert ABS/MoM/YoY PREVIEW - confirmed non-negotiable: must appear the moment a platform's
  // expert data is finalized for a month, regardless of whether Total GMV has been entered/
  // forecast computed at all (the whole point is to inform that decision, not follow it).
  // Computed directly from the finalized tagged file, not the forecast-alignment engine -
  // independent of fcIdx, which is why this is its own block rather than folded into the one
  // above. Short labels (Exp ABS/MoM/YoY) by request - not "Expert ... (Preview)" in full.
  const previewIdx = data.months.findIndex((_, i) =>
    data.rows.some((r) => r.expert_preview_abs[i] != null || r.expert_preview_mom[i] != null || r.expert_preview_yoy[i] != null));
  if (previewIdx !== -1) {
    METRICS.push({ key: "expert_preview_abs", label: "Exp ABS", fmt: fmtNum, idxs: [previewIdx] });
    METRICS.push({ key: "expert_preview_mom", label: "Exp MoM", fmt: fmtPct, idxs: [previewIdx] });
    METRICS.push({ key: "expert_preview_yoy", label: "Exp YoY", fmt: fmtPct, idxs: [previewIdx] });
  }

  head.innerHTML = `<th>Category</th>` + METRICS.map((m) => `<th colspan="${m.idxs.length}" class="metric-group-start">${m.label}</th>`).join("");
  subHead.innerHTML = `<th></th>` + METRICS.map((m) => m.idxs.map((i, j) => `<th${j === 0 ? ' class="metric-group-start"' : ""}>${escapeHtml(data.months[i])}</th>`).join("")).join("");
  // Freeze both header rows while scrolling down through what can be dozens of category rows -
  // the general `th { position: sticky; top: 0 }` rule alone makes BOTH rows stick to the exact
  // same top:0, overlapping each other instead of stacking. The subhead row's own "top" needs to
  // be set to the head row's actual rendered height - measured here (not hardcoded) so it stays
  // correct regardless of font-size/padding changes.
  const headHeight = head.getBoundingClientRect().height;
  Array.from(subHead.children).forEach((th) => { th.style.top = `${headHeight}px`; });

  body.innerHTML = data.rows
    .filter((r) => toplineViewKeepsRow(r.kind))
    .map((r) => {
      const cells = METRICS
        .map((m) => m.idxs
          .map((i, j) => {
            const v = r[m.key][i];
            const classes = [j === 0 ? "metric-group-start" : "", m.flag && m.flag(v, r, i) ? "mom-flag" : ""].filter(Boolean).join(" ");
            const title = m.flag && m.flag(v, r, i)
              ? ' title="Worth a manual look - either the Market Share move was 3+ percentage points from the plain Forecast+Trend baseline (hard-capped past 4pp), or MoM and YoY disagree by more than their own tolerance band. Review and adjust manually if needed."'
              : "";
            return `<td${classes ? ` class="${classes}"` : ""}${title}>${m.fmt(v)}</td>`;
          })
          .join(""))
        .join("");
      const indent = 14 * r.level;
      return `<tr class="${rowKindClass(r.kind)}"><td style="padding-left:${indent}px">${escapeHtml(r.label)}</td>${cells}</tr>`;
    })
    .join("");
}

// Total BPC / L0 / L1 / L2 get a hierarchy color so the eye can tell rollup level apart from
// brand-level detail at a glance, without needing to read the indentation. Brand/Others rows
// intentionally get no class - they stay the default row background.
function rowKindClass(kind) {
  if (kind === "total") return "row-total";
  if (kind === "l0" || kind === "l0_flat") return "row-l0";
  if (kind === "l1") return "row-l1";
  if (kind === "l2") return "row-l2";
  return "";
}

// ------------------------------------------------- Category Topline: Manual Change mode
// Market Share is the input here (not GMV) - staged locally as the user edits, sent to
// /edit-preview to see the cascading effect (including the auto-adjusting locked sibling) before
// anything is saved, and only written to disk by /edit-commit once the user explicitly confirms.
let topEditData = null;   // last /edit-view (or /edit-preview-refreshed) response
let topEditDirty = false; // true once a preview has been run and not yet confirmed/invalidated
let topLiveDebounce = null; // debounce timer: re-checks the cascade ~350ms after the user stops typing
let topPreviewSeq = 0;    // bumped on every /edit-preview call so a slow, out-of-order response
                           // (e.g. from a keystroke two edits ago) can detect it's stale and no-op
                           // instead of clobbering the table with an older cascade.
let topEditInitialized = false; // true once a view has loaded at least once - guards the very
                                 // first toggle-on from asking to "discard" edits that don't exist yet
let topEditLastPlatform = null; // last platform/month a view successfully loaded for - restored
let topEditLastMonth = null;    // on the dropdown if the user cancels an unsaved-edit warning
const SHARE_EDIT_EPS = 1e-8;    // float-noise guard once both sides are rounded to the same precision

// Typed-but-not-yet-saved Market Share edits, row_key -> the percentage string currently in that
// row's input. Kept OUTSIDE the DOM (unlike before) because the View 1/View 2 toggle can hide the
// very row that was edited (e.g. an L2 edit made in View 2, then switched to View 1) - the edit
// must still count and still get included when computing/saving, even while its row isn't
// rendered at all. Cleared only on an actual reload (fresh platform/month, or after a save) -
// never by a view-filter change, which is purely cosmetic.
let topEditOverrides = new Map();

const topManualToggle = document.getElementById("topline-manual-toggle");
const topNormalView = document.getElementById("topline-normal-view");
const topEditView = document.getElementById("topline-edit-view");
const topEditMonthSel = document.getElementById("topline-edit-month");
const topEditPreviewBtn = document.getElementById("topline-edit-preview-btn");
const topEditConfirmBtn = document.getElementById("topline-edit-confirm-btn");
const topEditStatus = document.getElementById("topline-edit-status");
const topPlatformSel = document.getElementById("topline-platform");

function pctStr(share) {
  return share === null || share === undefined ? "" : (share * 100).toFixed(4);
}

// Market Share edits are easy to lose by accident (a stray click on Platform/Month, or toggling
// Manual Change off and back on) since every one of those reloads fresh data from disk. Warn
// before that happens rather than silently discarding whatever hasn't been saved yet.
function hasUnsavedTopEdits() {
  return topManualToggle.checked && !!topEditData && collectTopEdits().length > 0;
}
function confirmDiscardTopEdits(message) {
  if (!hasUnsavedTopEdits()) return true;
  return confirm(message || "You have unsaved Market Share edits that haven't been saved yet. Continuing will discard them. Continue?");
}

topManualToggle.addEventListener("change", () => {
  const on = topManualToggle.checked;
  if (on && topEditInitialized && !confirmDiscardTopEdits()) {
    topManualToggle.checked = false;
    return;
  }
  topNormalView.classList.toggle("hidden", on);
  topEditView.classList.toggle("hidden", !on);
  if (on) loadTopEditView();
});
topPlatformSel.addEventListener("change", () => {
  if (!topManualToggle.checked) return;
  if (!confirmDiscardTopEdits("You have unsaved Market Share edits for the current platform. Switching platforms will discard them. Continue?")) {
    topPlatformSel.value = topEditLastPlatform;
    return;
  }
  loadTopEditView();
});
topEditMonthSel.addEventListener("change", () => {
  if (!confirmDiscardTopEdits("You have unsaved Market Share edits for the current month. Switching months will discard them. Continue?")) {
    topEditMonthSel.value = topEditLastMonth;
    return;
  }
  loadTopEditView();
});
topEditPreviewBtn.addEventListener("click", () => runTopEditPreview());
topEditConfirmBtn.addEventListener("click", runTopEditCommit);
window.addEventListener("beforeunload", (e) => {
  if (hasUnsavedTopEdits()) {
    e.preventDefault();
    e.returnValue = "";
  }
});

async function loadTopEditView() {
  const platform = topPlatformSel.value;
  if (!platform) return;
  clearTimeout(topLiveDebounce);
  topPreviewSeq++; // invalidate any in-flight preview from the view we're navigating away from
  const month = topEditMonthSel.value;
  const params = new URLSearchParams({ platform });
  if (month) params.set("month", month);
  topEditStatus.textContent = "Loading...";
  let data;
  try {
    const res = await fetch(`/category-topline/edit-view?${params.toString()}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      topEditStatus.textContent = `Error: ${err.detail}`;
      return;
    }
    data = await res.json();
  } catch (err) {
    topEditStatus.textContent = `Error: ${String(err)}`;
    return;
  }
  topEditData = data;
  topEditOverrides = new Map(); // fresh platform/month from disk - any prior edits no longer apply
  topEditDirty = false;
  topEditInitialized = true;
  topEditLastPlatform = platform;
  topEditLastMonth = data.month;
  topEditConfirmBtn.disabled = true;
  document.getElementById("topline-edit-preview-card").classList.add("hidden");
  if (!topEditMonthSel.options.length || topEditMonthSel.value !== data.month) {
    topEditMonthSel.innerHTML = data.months.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("");
    topEditMonthSel.value = data.month;
  }
  topEditStatus.textContent = "";
  renderTopEditTable(data.rows.filter((r) => toplineViewKeepsRow(r.kind)), data.group_checks);
}

function topGroupKeyFor(r) {
  if (r.kind === "l0" || r.kind === "l0_flat") return "TOTAL";
  if (r.kind === "l1") return `L0:${r.l0}`;
  if (r.kind === "l2") return `L1:${r.l0}:${r.l1}`;
  return null;
}

function renderTopEditTable(rows, groupChecks) {
  const body = document.getElementById("topline-edit-body");
  body.innerHTML = rows
    .map((r) => {
      const indent = 14 * r.level;
      const overrideVal = topEditOverrides.has(r.row_key) ? topEditOverrides.get(r.row_key) : null;
      const sharePct = overrideVal !== null ? overrideVal : pctStr(r.share);
      const revertHidden = overrideVal !== null && isShareEdited(parseFloat(overrideVal), r.share) ? "" : "hidden";
      const shareCell = r.editable
        ? `<span class="share-cell-wrap">
             <input type="number" step="0.0001" min="0" class="share-input" data-row-key="${escapeHtml(r.row_key)}" value="${sharePct}">
             <button type="button" class="revert-icon" data-row-key="${escapeHtml(r.row_key)}" title="Revert to original value" ${revertHidden}>&#8635;</button>
           </span>`
        : `<span class="locked-share" title="Auto-adjusts to keep the parent total unchanged">&#128274; ${sharePct}%</span>`;
      const gk = topGroupKeyFor(r);
      const check = gk ? groupChecks[gk] : null;
      const rollup = r.kind === "total" ? "" : (check
        ? (check.ok ? `<span class="rollup-ok">&#10003; OK</span>` : `<span class="rollup-bad">&#10007; Check</span>`)
        : "");
      return `<tr class="${rowKindClass(r.kind)}" data-row-key="${escapeHtml(r.row_key)}" data-group-key="${escapeHtml(gk || "")}">
        <td style="padding-left:${indent}px">${escapeHtml(r.label)}</td>
        <td class="cell-gmv">${fmtNum(r.gmv, 2)}</td>
        <td class="cell-share">${shareCell}</td>
        <td>${fmtPct(r.mom_growth_prev)}</td>
        <td class="cell-mom-curr">${fmtPct(r.mom_growth_curr)}</td>
        <td class="cell-yoy">${r.yoy_growth === null ? "&mdash;" : fmtPct(r.yoy_growth)}</td>
        <td class="cell-rollup">${rollup}</td>
        <td>${fmtNum(r.expert_abs, 2)}</td>
        <td>${fmtPct(r.expert_mom)}</td>
        <td>${fmtPct(r.expert_yoy)}</td>
      </tr>`;
    })
    .join("");

  const originalByKey = new Map(rows.map((r) => [r.row_key, r]));
  const inputsByKey = new Map();
  body.querySelectorAll(".share-input").forEach((el) => inputsByKey.set(el.dataset.rowKey, el));

  body.querySelectorAll(".share-input").forEach((el) => {
    const original = originalByKey.get(el.dataset.rowKey);
    const revertBtn = el.parentElement.querySelector(".revert-icon");
    el.addEventListener("input", () => {
      const newPct = parseFloat(el.value);
      const edited = !isNaN(newPct) && isShareEdited(newPct, original.share);
      if (revertBtn) revertBtn.hidden = !edited;
      if (edited) topEditOverrides.set(el.dataset.rowKey, el.value);
      else topEditOverrides.delete(el.dataset.rowKey);
      topEditDirty = false;
      topEditConfirmBtn.disabled = true;
      document.getElementById("topline-edit-preview-card").classList.add("hidden");
      clearTimeout(topLiveDebounce);
      topLiveDebounce = setTimeout(() => runTopEditPreview({ silent: true }), 350);
    });
  });
  body.querySelectorAll(".revert-icon").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = inputsByKey.get(btn.dataset.rowKey);
      const original = originalByKey.get(btn.dataset.rowKey);
      if (!input || !original) return;
      input.value = pctStr(original.share);
      btn.hidden = true;
      topEditOverrides.delete(btn.dataset.rowKey);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  });
}

// Pushes a /edit-preview response's cascade straight into the visible rows - so the locked
// sibling's share/GMV and both rows' MoM(latest)/YoY move in front of the user's eyes as they
// type, without waiting for them to click "Preview changes". Never touches an editable row's own
// share <input> (that's the value they're actively typing), only its GMV/growth read-out cells.
function applyLiveChangesToTable(changes, groupChecks) {
  const rowsByKey = new Map();
  document.querySelectorAll("#topline-edit-body tr").forEach((tr) => rowsByKey.set(tr.dataset.rowKey, tr));
  changes.forEach((c) => {
    const tr = rowsByKey.get(c.row_key);
    if (!tr) return;
    tr.querySelector(".cell-gmv").textContent = fmtNum(c.new_gmv, 2);
    const lockedSpan = tr.querySelector(".cell-share .locked-share");
    if (lockedSpan) {
      lockedSpan.innerHTML = `&#128274; ${pctStr(c.new_share)}%`;
    }
    tr.querySelector(".cell-mom-curr").textContent = fmtPct(c.mom_growth_curr);
    // textContent (not innerHTML) here, so this needs the literal character, not the &mdash; entity.
    tr.querySelector(".cell-yoy").textContent = c.yoy_growth === null || c.yoy_growth === undefined ? "—" : fmtPct(c.yoy_growth);
  });
  applyRollupBadges(groupChecks);
}

function applyRollupBadges(groupChecks) {
  document.querySelectorAll("#topline-edit-body tr").forEach((tr) => {
    const gk = tr.dataset.groupKey;
    if (!gk) return;
    const check = groupChecks[gk];
    const cell = tr.querySelector(".cell-rollup");
    if (!cell) return;
    cell.innerHTML = check
      ? (check.ok ? `<span class="rollup-ok">&#10003; OK</span>` : `<span class="rollup-bad">&#10007; Check</span>`)
      : "";
  });
}

// Restores every row's read-only cells back to the pristine /edit-view values - used when the
// user types back to (or otherwise ends up at) their original share, so the GMV/MoM/YoY read-outs
// don't stay stuck showing a stale in-between value from a nudge that's since been undone.
function resetLiveTable() {
  if (!topEditData) return;
  const rowsByKey = new Map();
  document.querySelectorAll("#topline-edit-body tr").forEach((tr) => rowsByKey.set(tr.dataset.rowKey, tr));
  topEditData.rows.forEach((r) => {
    const tr = rowsByKey.get(r.row_key);
    if (!tr) return;
    tr.querySelector(".cell-gmv").textContent = fmtNum(r.gmv, 2);
    const lockedSpan = tr.querySelector(".cell-share .locked-share");
    if (lockedSpan) {
      lockedSpan.innerHTML = `&#128274; ${pctStr(r.share)}%`;
    }
    tr.querySelector(".cell-mom-curr").textContent = fmtPct(r.mom_growth_curr);
    tr.querySelector(".cell-yoy").textContent = r.yoy_growth === null || r.yoy_growth === undefined ? "—" : fmtPct(r.yoy_growth);
  });
  applyRollupBadges(topEditData.group_checks);
}

// Compare against the ORIGINAL share rounded to the same 4-decimal percentage precision the
// input itself displays - not the full-precision original. Diffing against full precision meant
// round-trip display rounding alone (up to 0.00005pp at 4 decimals) looked like an edit; rounding
// both sides identically removes that noise at its source instead of widening past it with a
// tolerance that would then also swallow genuinely small (but real) edits.
function isShareEdited(newPct, originalShare) {
  if (isNaN(newPct)) return false;
  if (originalShare === null || originalShare === undefined) return true;
  const originalPct = Number((originalShare * 100).toFixed(4));
  return Math.abs(newPct - originalPct) > SHARE_EDIT_EPS;
}

function collectTopEdits() {
  if (!topEditData) return [];
  // Reads from the persistent topEditOverrides map, not the currently-rendered DOM - an edited
  // row may be hidden by the active View 1/View 2 filter right now, but it was still edited and
  // must still be included (see topEditOverrides' comment).
  const edits = [];
  topEditOverrides.forEach((value, rowKey) => {
    const original = topEditData.rows.find((r) => r.row_key === rowKey);
    const newPct = parseFloat(value);
    if (!original || isNaN(newPct)) return;
    if (isShareEdited(newPct, original.share)) {
      edits.push({ row_key: rowKey, new_share: newPct / 100 });
    }
  });
  return edits;
}

async function runTopEditPreview(opts) {
  const silent = opts && opts.silent;
  const edits = collectTopEdits();
  const mySeq = ++topPreviewSeq; // any earlier in-flight request is now stale and must not apply its result
  if (!edits.length) {
    resetLiveTable();
    document.getElementById("topline-edit-preview-card").classList.add("hidden");
    if (!silent) topEditStatus.textContent = "No changes to preview - edit a Market Share value first.";
    return;
  }
  const platform = topPlatformSel.value;
  const month = topEditMonthSel.value;
  if (!silent) topEditStatus.textContent = "Checking...";
  try {
    const res = await fetch("/category-topline/edit-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform, month, edits }),
    });
    if (mySeq !== topPreviewSeq) return; // a newer keystroke/preview has since superseded this one
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      topEditStatus.textContent = `Error: ${err.detail}`;
      return;
    }
    const preview = await res.json();
    if (mySeq !== topPreviewSeq) return; // re-check post-await: response could have arrived out of order
    applyLiveChangesToTable(preview.changes, preview.group_checks);
    const card = document.getElementById("topline-edit-preview-card");
    card.classList.remove("hidden");
    // Scoped to only the groups THIS edit touched (server-computed) - not every group in the whole
    // sheet. A pre-existing, edit-independent data issue elsewhere (confirmed: Flipkart's own Hair
    // Care/Body Skin L2 breakdowns already don't reconcile with their own L1 totals, regardless of
    // any edit) must never block saving an otherwise-clean edit to an unrelated category.
    const allOk = preview.edit_ok;
    document.getElementById("topline-edit-preview-body").innerHTML =
      `<table class="preview-table"><thead><tr><th>Category</th><th>Old GMV</th><th>New GMV</th><th>New Share</th></tr></thead><tbody>` +
      preview.changes.map((c) => `<tr><td>${escapeHtml(c.label)}</td><td>${fmtNum(c.old_gmv, 2)}</td><td>${fmtNum(c.new_gmv, 2)}</td><td>${pctStr(c.new_share)}%</td></tr>`).join("") +
      `</tbody></table>` +
      `<p class="${allOk ? "rollup-ok" : "rollup-bad"}">${allOk ? "✓ All roll-ups check out - safe to save." : "✗ Roll-up check failed - review before saving."}</p>`;
    topEditDirty = allOk;
    topEditConfirmBtn.disabled = !allOk;
    topEditStatus.textContent = "";
  } catch (err) {
    if (mySeq !== topPreviewSeq) return;
    topEditStatus.textContent = `Error: ${String(err)}`;
  }
}

async function runTopEditCommit() {
  if (!topEditDirty) return;
  const edits = collectTopEdits();
  if (!edits.length) return; // defensive: nothing to save (e.g. reverted back to original since Confirm enabled)
  topPreviewSeq++; // invalidate any preview still in flight so it can't re-enable Confirm after this commit starts
  const platform = topPlatformSel.value;
  const month = topEditMonthSel.value;
  topEditConfirmBtn.disabled = true;
  topEditStatus.textContent = "Saving...";
  try {
    const res = await fetch("/category-topline/edit-commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform, month, edits }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      topEditStatus.textContent = `Error: ${err.detail}`;
      topEditConfirmBtn.disabled = false;
      return;
    }
    topEditStatus.textContent = "Saved to Category Topline and Brand Share Cons.";
    document.getElementById("topline-edit-preview-card").classList.add("hidden");
    await fetch("/category-topline/reload", { method: "POST" });
    await fetch("/brand-share-cons/reload", { method: "POST" });
    toplineLoaded = false;
    bscLoaded = false;
    loadTopEditView();
  } catch (err) {
    topEditStatus.textContent = `Error: ${String(err)}`;
    topEditConfirmBtn.disabled = false;
  }
}

// ---------------------------------------------------------------- Tab 4: Brand Share Cons
let bscMeta = null;
let bscLoaded = false;
let bscRowsCache = [];

async function initBsc() {
  if (!bscLoaded) {
    bscMeta = await fetch("/brand-share-cons/meta").then((r) => r.json());
    populatePlatformOptions(document.getElementById("bsc-platform"), bscMeta.variants.all);
    bscLoaded = true;
  }
  renderBsc();
}

const bscSearchInput = document.getElementById("bsc-search");
const bscSearchClearBtn = document.getElementById("bsc-search-clear");
document.getElementById("bsc-platform").addEventListener("change", renderBsc);
document.getElementById("bsc-metric").addEventListener("change", renderBscTable);
bscSearchInput.addEventListener("input", () => {
  bscSearchClearBtn.hidden = !bscSearchInput.value;
  renderBscTable();
});
bscSearchClearBtn.addEventListener("click", () => {
  bscSearchInput.value = "";
  bscSearchClearBtn.hidden = true;
  bscSearchInput.focus();
  renderBscTable();
});
document.getElementById("bsc-from").addEventListener("change", renderBsc);
document.getElementById("bsc-to").addEventListener("change", renderBsc);
document.getElementById("bsc-refresh").addEventListener("click", async () => {
  await fetch("/brand-share-cons/reload", { method: "POST" });
  bscLoaded = false;
  initBsc();
});

async function renderBsc() {
  const variant = "all";
  const platform = document.getElementById("bsc-platform").value;
  if (!platform) return;
  const fromSel = document.getElementById("bsc-from");
  const toSel = document.getElementById("bsc-to");
  if (!fromSel.options.length) populateMonthRange(fromSel, toSel, bscMeta.months);
  const params = new URLSearchParams({ variant, platform, from_month: fromSel.value, to_month: toSel.value });
  const data = await fetch(`/brand-share-cons?${params.toString()}`).then((r) => r.json());
  bscRowsCache = data.rows;
  bscMonths = data.months;
  renderBscTable();
}

let bscMonths = [];

function renderBscTable() {
  const metric = document.getElementById("bsc-metric").value;
  const search = document.getElementById("bsc-search").value.toLowerCase();

  const head = document.getElementById("bsc-head");
  head.innerHTML = `<th>Category / Brand</th>` + bscMonths.map((m) => `<th>${m}</th>`).join("");

  let rows = bscRowsCache;
  if (search) {
    rows = rows.filter((r) => r.label.toLowerCase().includes(search));
  }

  const body = document.getElementById("bsc-body");
  body.innerHTML = rows
    .map((r) => {
      const vals = r[metric];
      const cells = vals
        .map((v) => `<td>${metric === "gmv" || metric === "units" || metric === "asp" ? fmtNum(v) : fmtPct(v)}</td>`)
        .join("");
      const indent = 14 * r.level;
      return `<tr class="${rowKindClass(r.kind)}"><td style="padding-left:${indent}px">${escapeHtml(r.label)}</td>${cells}</tr>`;
    })
    .join("");
}

// ------------------------------------------------- Brand Share Cons: Manual Change mode
// Same pattern as Category Topline's Manual Change (share-driven edit, live cascade, revert
// icons, preview/confirm gate, unsaved-edit guards, race-safe debounce) - scoped one level down:
// only BRAND rows are ever editable here, within whichever L2 (or flat L0) they belong to, and
// only when that group has an "Others" brand to absorb the change. Total/L0/L1/L2 rows are shown
// as read-only context (they belong to Category Topline and never change from this tab).
let bscEditData = null;
let bscEditDirty = false;
let bscLiveDebounce = null;
let bscPreviewSeq = 0;
let bscEditInitialized = false;
let bscEditLastPlatform = null;
let bscEditLastMonth = null;

const bscManualToggle = document.getElementById("bsc-manual-toggle");
const bscNormalView = document.getElementById("bsc-normal-view");
const bscEditView = document.getElementById("bsc-edit-view");
const bscEditMonthSel = document.getElementById("bsc-edit-month");
const bscEditPreviewBtn = document.getElementById("bsc-edit-preview-btn");
const bscEditConfirmBtn = document.getElementById("bsc-edit-confirm-btn");
const bscEditStatus = document.getElementById("bsc-edit-status");
const bscPlatformSel = document.getElementById("bsc-platform");

function hasUnsavedBscEdits() {
  return bscManualToggle.checked && !!bscEditData && collectBscEdits().length > 0;
}
function confirmDiscardBscEdits(message) {
  if (!hasUnsavedBscEdits()) return true;
  return confirm(message || "You have unsaved brand Market Share edits that haven't been saved yet. Continuing will discard them. Continue?");
}

bscManualToggle.addEventListener("change", () => {
  const on = bscManualToggle.checked;
  if (on && bscEditInitialized && !confirmDiscardBscEdits()) {
    bscManualToggle.checked = false;
    return;
  }
  bscNormalView.classList.toggle("hidden", on);
  bscEditView.classList.toggle("hidden", !on);
  if (on) loadBscEditView();
});
bscPlatformSel.addEventListener("change", () => {
  if (!bscManualToggle.checked) return;
  if (!confirmDiscardBscEdits("You have unsaved brand Market Share edits for the current platform. Switching platforms will discard them. Continue?")) {
    bscPlatformSel.value = bscEditLastPlatform;
    return;
  }
  loadBscEditView();
});
bscEditMonthSel.addEventListener("change", () => {
  if (!confirmDiscardBscEdits("You have unsaved brand Market Share edits for the current month. Switching months will discard them. Continue?")) {
    bscEditMonthSel.value = bscEditLastMonth;
    return;
  }
  loadBscEditView();
});
bscEditPreviewBtn.addEventListener("click", () => runBscEditPreview());
bscEditConfirmBtn.addEventListener("click", runBscEditCommit);
window.addEventListener("beforeunload", (e) => {
  if (hasUnsavedBscEdits()) {
    e.preventDefault();
    e.returnValue = "";
  }
});

async function loadBscEditView() {
  const platform = bscPlatformSel.value;
  if (!platform) return;
  clearTimeout(bscLiveDebounce);
  bscPreviewSeq++;
  const month = bscEditMonthSel.value;
  const params = new URLSearchParams({ platform });
  if (month) params.set("month", month);
  bscEditStatus.textContent = "Loading...";
  let data;
  try {
    const res = await fetch(`/brand-share-cons/edit-view?${params.toString()}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      bscEditStatus.textContent = `Error: ${err.detail}`;
      return;
    }
    data = await res.json();
  } catch (err) {
    bscEditStatus.textContent = `Error: ${String(err)}`;
    return;
  }
  bscEditData = data;
  bscEditDirty = false;
  bscEditInitialized = true;
  bscEditLastPlatform = platform;
  bscEditLastMonth = data.month;
  bscEditConfirmBtn.disabled = true;
  document.getElementById("bsc-edit-preview-card").classList.add("hidden");
  if (!bscEditMonthSel.options.length || bscEditMonthSel.value !== data.month) {
    bscEditMonthSel.innerHTML = data.months.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("");
    bscEditMonthSel.value = data.month;
  }
  bscEditStatus.textContent = "";
  renderBscEditTable(data.rows, data.group_checks);
}

function renderBscEditTable(rows, groupChecks) {
  const body = document.getElementById("bsc-edit-body");
  body.innerHTML = rows
    .map((r) => {
      const indent = 14 * r.level;
      const sharePct = pctStr(r.share);
      let shareCell;
      if (r.editable) {
        shareCell = `<span class="share-cell-wrap">
             <input type="number" step="0.0001" min="0" class="share-input" data-row-key="${escapeHtml(r.row_key)}" value="${sharePct}">
             <button type="button" class="revert-icon" data-row-key="${escapeHtml(r.row_key)}" title="Revert to original value" hidden>&#8635;</button>
           </span>`;
      } else if (r.locked) {
        shareCell = `<span class="locked-share" title="Auto-adjusts to keep its category's total unchanged">&#128274; ${sharePct}%</span>`;
      } else if (r.kind === "brand") {
        shareCell = `<span class="context-share" title="No 'Others' brand in this category to absorb changes, so brands here aren't editable">${sharePct}%</span>`;
      } else if (r.kind === "total") {
        shareCell = "";
      } else {
        shareCell = `<span class="context-share" title="Owned by Category Topline - edit it there instead">${sharePct}%</span>`;
      }
      const check = r.group_key ? groupChecks[r.group_key] : null;
      const rollup = check
        ? (check.ok ? `<span class="rollup-ok">&#10003; OK</span>` : `<span class="rollup-bad">&#10007; Check</span>`)
        : "";
      return `<tr class="${rowKindClass(r.kind)}" data-row-key="${escapeHtml(r.row_key)}" data-group-key="${escapeHtml(r.group_key || "")}">
        <td style="padding-left:${indent}px">${escapeHtml(r.label)}</td>
        <td class="cell-gmv">${fmtNum(r.gmv, 2)}</td>
        <td class="cell-share">${shareCell}</td>
        <td>${fmtPct(r.mom_growth_prev)}</td>
        <td class="cell-mom-curr">${fmtPct(r.mom_growth_curr)}</td>
        <td class="cell-yoy">${r.yoy_growth === null ? "&mdash;" : fmtPct(r.yoy_growth)}</td>
        <td class="cell-rollup">${rollup}</td>
      </tr>`;
    })
    .join("");

  const originalByKey = new Map(rows.map((r) => [r.row_key, r]));
  const inputsByKey = new Map();
  body.querySelectorAll(".share-input").forEach((el) => inputsByKey.set(el.dataset.rowKey, el));

  body.querySelectorAll(".share-input").forEach((el) => {
    const original = originalByKey.get(el.dataset.rowKey);
    const revertBtn = el.parentElement.querySelector(".revert-icon");
    el.addEventListener("input", () => {
      const newPct = parseFloat(el.value);
      if (revertBtn) revertBtn.hidden = isNaN(newPct) || !isShareEdited(newPct, original.share);
      bscEditDirty = false;
      bscEditConfirmBtn.disabled = true;
      document.getElementById("bsc-edit-preview-card").classList.add("hidden");
      clearTimeout(bscLiveDebounce);
      bscLiveDebounce = setTimeout(() => runBscEditPreview({ silent: true }), 350);
    });
  });
  body.querySelectorAll(".revert-icon").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = inputsByKey.get(btn.dataset.rowKey);
      const original = originalByKey.get(btn.dataset.rowKey);
      if (!input || !original) return;
      input.value = pctStr(original.share);
      btn.hidden = true;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  });
}

function applyBscLiveChangesToTable(changes, groupChecks) {
  const rowsByKey = new Map();
  document.querySelectorAll("#bsc-edit-body tr").forEach((tr) => rowsByKey.set(tr.dataset.rowKey, tr));
  changes.forEach((c) => {
    const tr = rowsByKey.get(c.row_key);
    if (!tr) return;
    tr.querySelector(".cell-gmv").textContent = fmtNum(c.new_gmv, 2);
    const lockedSpan = tr.querySelector(".cell-share .locked-share");
    if (lockedSpan) lockedSpan.innerHTML = `&#128274; ${pctStr(c.new_share)}%`;
    tr.querySelector(".cell-mom-curr").textContent = fmtPct(c.mom_growth_curr);
    tr.querySelector(".cell-yoy").textContent = c.yoy_growth === null || c.yoy_growth === undefined ? "—" : fmtPct(c.yoy_growth);
  });
  applyBscRollupBadges(groupChecks);
}

function applyBscRollupBadges(groupChecks) {
  document.querySelectorAll("#bsc-edit-body tr").forEach((tr) => {
    const gk = tr.dataset.groupKey;
    if (!gk) return;
    const check = groupChecks[gk];
    const cell = tr.querySelector(".cell-rollup");
    if (!cell) return;
    cell.innerHTML = check
      ? (check.ok ? `<span class="rollup-ok">&#10003; OK</span>` : `<span class="rollup-bad">&#10007; Check</span>`)
      : "";
  });
}

function resetBscLiveTable() {
  if (!bscEditData) return;
  const rowsByKey = new Map();
  document.querySelectorAll("#bsc-edit-body tr").forEach((tr) => rowsByKey.set(tr.dataset.rowKey, tr));
  bscEditData.rows.forEach((r) => {
    const tr = rowsByKey.get(r.row_key);
    if (!tr) return;
    tr.querySelector(".cell-gmv").textContent = fmtNum(r.gmv, 2);
    const lockedSpan = tr.querySelector(".cell-share .locked-share");
    if (lockedSpan) lockedSpan.innerHTML = `&#128274; ${pctStr(r.share)}%`;
    tr.querySelector(".cell-mom-curr").textContent = fmtPct(r.mom_growth_curr);
    tr.querySelector(".cell-yoy").textContent = r.yoy_growth === null || r.yoy_growth === undefined ? "—" : fmtPct(r.yoy_growth);
  });
  applyBscRollupBadges(bscEditData.group_checks);
}

function collectBscEdits() {
  if (!bscEditData) return [];
  const edits = [];
  document.querySelectorAll("#bsc-edit-body .share-input").forEach((el) => {
    const rowKey = el.dataset.rowKey;
    const original = bscEditData.rows.find((r) => r.row_key === rowKey);
    const newPct = parseFloat(el.value);
    if (!original || isNaN(newPct)) return;
    if (isShareEdited(newPct, original.share)) {
      edits.push({ row_key: rowKey, new_share: newPct / 100 });
    }
  });
  return edits;
}

async function runBscEditPreview(opts) {
  const silent = opts && opts.silent;
  const edits = collectBscEdits();
  const mySeq = ++bscPreviewSeq;
  if (!edits.length) {
    resetBscLiveTable();
    document.getElementById("bsc-edit-preview-card").classList.add("hidden");
    if (!silent) bscEditStatus.textContent = "No changes to preview - edit a brand's Market Share value first.";
    return;
  }
  const platform = bscPlatformSel.value;
  const month = bscEditMonthSel.value;
  if (!silent) bscEditStatus.textContent = "Checking...";
  try {
    const res = await fetch("/brand-share-cons/edit-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform, month, edits }),
    });
    if (mySeq !== bscPreviewSeq) return;
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      bscEditStatus.textContent = `Error: ${err.detail}`;
      return;
    }
    const preview = await res.json();
    if (mySeq !== bscPreviewSeq) return;
    applyBscLiveChangesToTable(preview.changes, preview.group_checks);
    const card = document.getElementById("bsc-edit-preview-card");
    card.classList.remove("hidden");
    // Scoped server-side to only the groups THIS edit touched - see the matching comment on
    // Category Topline's edit-preview handler.
    const allOk = preview.edit_ok;
    document.getElementById("bsc-edit-preview-body").innerHTML =
      `<table class="preview-table"><thead><tr><th>Brand</th><th>Old GMV</th><th>New GMV</th><th>New Share</th></tr></thead><tbody>` +
      preview.changes.map((c) => `<tr><td>${escapeHtml(c.label)}</td><td>${fmtNum(c.old_gmv, 2)}</td><td>${fmtNum(c.new_gmv, 2)}</td><td>${pctStr(c.new_share)}%</td></tr>`).join("") +
      `</tbody></table>` +
      `<p class="${allOk ? "rollup-ok" : "rollup-bad"}">${allOk ? "✓ All roll-ups check out - safe to save." : "✗ Roll-up check failed - review before saving."}</p>`;
    bscEditDirty = allOk;
    bscEditConfirmBtn.disabled = !allOk;
    bscEditStatus.textContent = "";
  } catch (err) {
    if (mySeq !== bscPreviewSeq) return;
    bscEditStatus.textContent = `Error: ${String(err)}`;
  }
}

async function runBscEditCommit() {
  if (!bscEditDirty) return;
  const edits = collectBscEdits();
  if (!edits.length) return;
  bscPreviewSeq++;
  const platform = bscPlatformSel.value;
  const month = bscEditMonthSel.value;
  bscEditConfirmBtn.disabled = true;
  bscEditStatus.textContent = "Saving...";
  try {
    const res = await fetch("/brand-share-cons/edit-commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform, month, edits }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      bscEditStatus.textContent = `Error: ${err.detail}`;
      bscEditConfirmBtn.disabled = false;
      return;
    }
    bscEditStatus.textContent = "Saved to Brand Share Cons.";
    document.getElementById("bsc-edit-preview-card").classList.add("hidden");
    await fetch("/brand-share-cons/reload", { method: "POST" });
    bscLoaded = false;
    loadBscEditView();
  } catch (err) {
    bscEditStatus.textContent = `Error: ${String(err)}`;
    bscEditConfirmBtn.disabled = false;
  }
}

// ---------------------------------------------------------------- Tab: Brand Validation
// Read-only, whole-brand rollup (Brand Share Cons step 3) - fetched fresh on every render, same
// as Category Topline/BSC's own read views, so a Manual Change made elsewhere shows up here
// without any special-casing.
let bvLoaded = false;
let bvData = null;

async function initBrandValidation() {
  if (!bvLoaded) {
    const bscMetaLocal = await fetch("/brand-share-cons/meta").then((r) => r.json());
    populatePlatformOptions(document.getElementById("bv-platform"), bscMetaLocal.variants.all);
    bvLoaded = true;
  }
  await renderBrandValidation();
}

async function renderBrandValidation() {
  const platform = document.getElementById("bv-platform").value;
  if (!platform) return;
  const from = document.getElementById("bv-from").value;
  const to = document.getElementById("bv-to").value;
  const params = new URLSearchParams({ platform });
  if (from) params.set("from_month", from);
  if (to) params.set("to_month", to);
  bvData = await fetch(`/brand-validation?${params.toString()}`).then((r) => r.json());
  populateMonthRange(document.getElementById("bv-from"), document.getElementById("bv-to"), bvData.months);

  const note = document.getElementById("bv-no-expert-note");
  if (!bvData.has_expert_data) {
    note.textContent = "No Raw expert data file covers this platform/month range - every Expert column below reads NA.";
    note.classList.remove("hidden");
  } else {
    note.classList.add("hidden");
  }
  renderBrandValidationTable();
}


// Deterministic (same name always -> same color, no maintained brand->color map) pick of one of
// the 6 .tm-badge-N classes defined in style.css, purely a visual aid for scanning the Tier
// column quickly - the color itself carries no meaning beyond "same color = same parent company".
function tmBrandBadgeClass(name) {
  let sum = 0;
  for (let i = 0; i < name.length; i++) sum += name.charCodeAt(i);
  return `tm-badge-${sum % 6}`;
}

function renderBrandValidationTable() {
  if (!bvData) return;
  const body = document.getElementById("bv-body");
  const months = bvData.months;

  // table-layout: fixed only reads column widths off a <colgroup> (or the first row, divided
  // awkwardly across colspans) - an explicit <col> per column is the only way to pin the Brand/
  // Tier columns to an exact width regardless of header colspans, which is what the sticky
  // second column's "left" offset in style.css depends on lining up with exactly.
  const BRAND_COL_W = 190, TIER_COL_W = 120, METRIC_COL_W = 64;
  document.getElementById("bv-colgroup").innerHTML =
    `<col style="width:${BRAND_COL_W}px"><col style="width:${TIER_COL_W}px">` +
    months.map(() => `<col style="width:${METRIC_COL_W}px">`.repeat(3)).join("");
  // table-layout: fixed still computes the table's own "auto" width from cell content (not the
  // colgroup sum) before distributing it across columns - so an unconstrained-width table ends
  // up far wider than the colgroup intends, and the columns get redistributed to fill that extra
  // space instead of honoring their declared pixel widths (confirmed by measuring rendered column
  // widths vs. the colgroup - they didn't match, and not by any uniform scale factor). Giving the
  // table an explicit pixel width equal to the colgroup's own sum removes that ambiguity - there's
  // no longer any "auto" computation for the browser to second-guess the colgroup with.
  const totalWidth = BRAND_COL_W + TIER_COL_W + months.length * 3 * METRIC_COL_W;
  document.getElementById("bv-table").style.width = `${totalWidth}px`;

  // Two-row header: top row spans each month's 3 sub-metric columns, second row labels them.
  document.getElementById("bv-head").innerHTML = `<th>Brand</th><th>Tier</th>` + months.map((m) => `
    <th colspan="3" class="metric-group-start">${escapeHtml(m)}</th>
  `).join("");
  document.getElementById("bv-subhead").innerHTML = `<th></th><th></th>` + months.map(() => `
    <th class="metric-group-start">GMV (BSC / Expert)</th><th>MoM (BSC / Expert)</th><th>YoY (BSC / Expert)</th>
  `).join("");

  const search = document.getElementById("bv-search").value.toLowerCase();
  const rows = search ? bvData.rows.filter((r) => r.brand.toLowerCase().includes(search)) : bvData.rows;

  const fmtGmv = (v) => (v == null ? "NA" : v.toFixed(2));
  // MoM/YoY only - growth sign is the whole point of these two columns, so each value gets its
  // own green/red regardless of the other side's sign (BSC and Expert can disagree in direction).
  const fmtPctColored = (v) => {
    if (v == null) return "NA";
    const text = `${(v * 100).toFixed(1)}%`;
    const cls = v > 0 ? "pct-pos" : v < 0 ? "pct-neg" : "";
    return cls ? `<span class="${cls}">${text}</span>` : text;
  };
  const cell = (bscVal, expertVal, isPct, groupStart) => {
    const hasExpert = expertVal != null;
    const mismatch = hasExpert && isPct && Math.abs((bscVal ?? 0) - expertVal) > 0.03;
    const classes = [groupStart ? "metric-group-start" : "", mismatch ? "mom-flag" : ""].filter(Boolean).join(" ");
    const titleAttr = mismatch ? ' title="BSC and Expert differ by more than 3 percentage points"' : "";
    const fmt = isPct ? fmtPctColored : fmtGmv;
    return `<td${classes ? ` class="${classes}"` : ""}${titleAttr}>${fmt(bscVal)} / ${fmt(expertVal)}</td>`;
  };

  body.innerHTML = rows.map((r) => {
    const tierClass = r.tier === "lo" ? "bv-row-lo" : "";
    const tierLabel = r.tm_brand
      ? `<span class="tm-badge ${tmBrandBadgeClass(r.tm_brand)}">${escapeHtml(r.tm_brand)}</span>`
      : "";
    const cells = months.map((_, i) =>
      cell(r.bsc_absolute[i], r.expert_absolute[i], false, true) +
      cell(r.bsc_mom[i], r.expert_mom[i], true, false) +
      cell(r.bsc_yoy[i], r.expert_yoy[i], true, false)
    ).join("");
    return `<tr class="${tierClass}"><td>${escapeHtml(r.brand)}</td><td>${tierLabel}</td>${cells}</tr>`;
  }).join("");
}

document.getElementById("bv-platform").addEventListener("change", renderBrandValidation);
document.getElementById("bv-from").addEventListener("change", renderBrandValidation);
document.getElementById("bv-to").addEventListener("change", renderBrandValidation);
document.getElementById("bv-refresh").addEventListener("click", renderBrandValidation);
const bvSearchInput = document.getElementById("bv-search");
const bvSearchClearBtn = document.getElementById("bv-search-clear");
bvSearchInput.addEventListener("input", () => {
  bvSearchClearBtn.hidden = !bvSearchInput.value;
  renderBrandValidationTable();
});
bvSearchClearBtn.addEventListener("click", () => {
  bvSearchInput.value = "";
  bvSearchClearBtn.hidden = true;
  bvSearchInput.focus();
  renderBrandValidationTable();
});

// ---------------------------------------------------------------- Tab 5: Master Data
let mdMeta = null;
let mdOffset = 0;
const mdLimit = 100;

async function initMasterData() {
  if (!mdMeta) {
    mdMeta = await fetch("/master-data/meta").then((r) => r.json());
    fillSelect("md-platform", mdMeta.platforms, "All platforms");
    fillSelect("md-l0", mdMeta.l0, "All L0");
    fillSelect("md-l1", mdMeta.l1, "All L1");
    fillSelect("md-l2", mdMeta.l2, "All L2");
    fillSelect("md-month", mdMeta.months, "All months");
  }
  mdOffset = 0;
  loadMasterData();
}

function fillSelect(id, values, allLabel) {
  const el = document.getElementById(id);
  el.innerHTML = `<option value="">${allLabel}</option>` + values.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
}

async function loadMasterData() {
  const params = new URLSearchParams({
    platform: document.getElementById("md-platform").value,
    l0: document.getElementById("md-l0").value,
    l1: document.getElementById("md-l1").value,
    l2: document.getElementById("md-l2").value,
    month: document.getElementById("md-month").value,
    brand: document.getElementById("md-brand").value.trim(),
    limit: mdLimit,
    offset: mdOffset,
  });
  const data = await fetch(`/master-data?${params.toString()}`).then((r) => r.json());

  document.getElementById("md-count").textContent =
    `${data.total.toLocaleString()} matching rows (of ${mdMeta.total_rows.toLocaleString()} total)`;
  document.getElementById("md-page-label").textContent =
    `Rows ${data.total === 0 ? 0 : mdOffset + 1}-${Math.min(mdOffset + mdLimit, data.total)} of ${data.total.toLocaleString()}`;
  document.getElementById("md-prev").disabled = mdOffset === 0;
  document.getElementById("md-next").disabled = mdOffset + mdLimit >= data.total;

  document.getElementById("md-body").innerHTML = data.rows
    .map(
      (r) => `<tr>
        <td>${escapeHtml(r.Month)}</td><td>${escapeHtml(r.Quarter)}</td><td>${r.Year}</td>
        <td>${escapeHtml(r.Archetypes)}</td><td>${escapeHtml(r.Platform)}</td>
        <td>${escapeHtml(r.L0_Cat)}</td><td>${escapeHtml(r.L1_Cat)}</td><td>${escapeHtml(r.L2_Cat)}</td>
        <td>${escapeHtml(r["Sell Out perimeter"] || "")}</td><td>${escapeHtml(r["BMS perimeter"] || "")}</td>
        <td>${escapeHtml(r["LO Brand"] || "")}</td><td>${escapeHtml(r["TM Brand"] || "")}</td>
        <td>${escapeHtml(r.Brand)}</td>
        <td>${fmtNum(r["GMV (INR Cr.)"], 2)}</td><td>${fmtNum(r["Units (#)"], 0)}</td><td>${fmtNum(r["ASP (INR)"], 2)}</td>
        <td>${escapeHtml(r.Legacy || "")}</td>
      </tr>`
    )
    .join("");
}

["md-platform", "md-l0", "md-l1", "md-l2", "md-month"].forEach((id) =>
  document.getElementById(id).addEventListener("change", () => { mdOffset = 0; loadMasterData(); })
);
document.getElementById("md-refresh").addEventListener("click", () => { mdOffset = 0; loadMasterData(); });
document.getElementById("md-brand").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { mdOffset = 0; loadMasterData(); }
});
document.getElementById("md-prev").addEventListener("click", () => {
  mdOffset = Math.max(0, mdOffset - mdLimit);
  loadMasterData();
});
document.getElementById("md-next").addEventListener("click", () => {
  mdOffset += mdLimit;
  loadMasterData();
});

// Initial load
loadGlossary();

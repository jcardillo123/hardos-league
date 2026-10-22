/* Draft board client. No build step, no framework - it has to boot on draft
   night with nothing but a browser. */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const PROJECTION_COUNT = 3;

const state = {
  data: null,        // last /api/state payload
  columns: [],       // position-column payload
  flat: [],          // big-board payload
  projection: null,  // last /api/projection payload
  suggestion: null,  // last /api/suggest payload
  history: null,     // owners available for the manager dropdowns
  query: "",
  sort: "ecr",
  mode: "positions", // positions | big
  view: "board",
  coverage: {},      // which metrics actually have data in the pool
  // Which ranking drives the projection's board-availability term. Remembered
  // across reloads so a comparison survives a refresh.
  signal: localStorage.getItem("draft.signal") || "ecr",
  // Which projection source scores the suggestions: rankings | vegas | blend.
  valueSource: localStorage.getItem("draft.valueSource") || "blend",
};

const SORT_FIELD = {
  ecr: "ecr",
  adp: "adp",
  espn_adp: "espn_adp",
  vor: "vor",
  proj: "proj_points",
  last: "pts_last",
  vegas_vor: "vegas_vor",
  vegas_proj: "vegas_proj",
  // The edge is vegas_vor - vor, so it exists exactly where vegas_vor does.
  edge: "vegas_vor",
};

const SORT_LABEL = {
  ecr: "ECR", adp: "Consensus ADP", espn_adp: "ESPN ADP",
  vor: "VOR", proj: "Projected", last: "Last season",
  vegas_vor: "Vegas VOR", vegas_proj: "Vegas projected",
  edge: "Vegas vs projection",
};

const VALUE_LABEL = {
  rankings: "the rankings projections",
  vegas: "Vegas-implied projections",
  blend: "an even blend of both projections",
};

// Short forms for the projection panel's signal toggle and inline comparisons.
const SIGNAL_SHORT = { ecr: "ECR", adp: "ADP", espn_adp: "ESPN" };

/* ------------------------------- helpers -------------------------------- */

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
  return body;
}

let toastTimer;
function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("is-error", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 4200 : 2000);
}

const num = (v, digits = 0) =>
  v === null || v === undefined ? "–" : Number(v).toFixed(digits);

const pickLabel = (round, slot) => `${round}.${String(slot).padStart(2, "0")}`;

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

/* ------------------------------ top bar --------------------------------- */

function renderClock() {
  const s = state.data;
  const clock = $(".clock");
  const cur = s.current;

  if (!cur) {
    $("#clockPick").textContent = "Done";
    $("#clockWho").textContent = `${s.n_picks} picks recorded`;
    clock.classList.remove("is-me");
    $("#until").innerHTML = "Draft complete";
    return;
  }

  $("#clockPick").textContent =
    `Pick ${pickLabel(cur.round, cur.slot)}  ·  #${cur.overall}`;
  $("#clockWho").textContent =
    cur.is_me ? `${cur.manager_name} — you're up` : cur.manager_name;
  clock.classList.toggle("is-me", !!cur.is_me);

  const t = s.timing || {};
  if (cur.is_me) {
    $("#until").innerHTML = `<strong>On the clock</strong>`;
  } else if (t.until_next === null || t.until_next === undefined) {
    $("#until").innerHTML = `No picks remaining for you`;
  } else {
    const gap =
      t.my_following_overall && t.my_next_overall
        ? t.my_following_overall - t.my_next_overall - 1
        : null;
    // A gap of 0 means the snake turns on you: two picks in a row.
    const after =
      gap === null ? "" : gap === 0 ? " · back-to-back" : ` · then <strong>${gap}</strong> more`;
    $("#until").innerHTML = `<strong>${t.until_next}</strong> picks until you${after}`;
  }
}

/* Flags sort options the current pool cannot actually order by. Without this a
   user toggles ADP, sees nothing move, and reasonably concludes it is broken. */
function updateSortAvailability(players) {
  const coverage = {};
  Object.entries(SORT_FIELD).forEach(([key, field]) => {
    coverage[key] = players.reduce(
      (n, p) => n + (p[field] === null || p[field] === undefined ? 0 : 1), 0
    );
  });
  state.coverage = coverage;

  $$("#sort option").forEach((opt) => {
    const have = coverage[opt.value] ?? 0;
    opt.disabled = have === 0;
    opt.textContent = have === 0
      ? `${SORT_LABEL[opt.value]} — no data`
      : SORT_LABEL[opt.value];
  });

  const warn = $("#sortWarning");
  const empty = Object.entries(coverage)
    .filter(([, n]) => n === 0)
    .map(([k]) => SORT_LABEL[k]);
  if (!empty.length) {
    warn.hidden = true;
    return;
  }
  warn.hidden = false;
  warn.innerHTML =
    `<strong>${empty.join(" and ")}</strong> ${empty.length > 1 ? "are" : "is"} ` +
    `empty in the current pool, so sorting by ${empty.length > 1 ? "them" : "it"} ` +
    `does nothing. Drop a FantasyPros CSV in <code>data/rankings/</code> and ` +
    `rebuild on the Setup tab to populate ${empty.length > 1 ? "them" : "it"}.`;
}

/* --------------------------- position columns --------------------------- */

function card(player, prev) {
  const el = document.createElement("div");
  el.className = `card pos-${player.position}`;
  if (prev && prev.tier !== null && player.tier !== null && prev.tier !== player.tier) {
    el.classList.add("tier-break");
  }
  el.dataset.key = player.player_key;

  const rank = player.ecr !== null ? Math.round(player.ecr) : "–";
  const bye = player.bye ? `bye ${player.bye}` : "";
  const meta = [player.position + (player.team ? ` · ${player.team}` : ""), bye]
    .filter(Boolean).join("  ·  ");

  // Always show the metric currently being sorted on, so the toggle is visibly
  // doing something even when it does not reorder anything.
  const field = SORT_FIELD[state.sort];
  const value = player[field];
  const digits = state.sort === "vor" || state.sort === "ecr" || state.sort === "adp" ? 0 : 0;

  el.innerHTML = `
    <div class="rank">${rank}</div>
    <div>
      <div class="name">${escapeHtml(player.name)}</div>
      <div class="meta">${meta}</div>
    </div>
    <div class="num"><b>${num(value, digits)}</b>${SORT_LABEL[state.sort]}</div>`;

  el.addEventListener("click", () => draftPlayer(player.player_key, player.name));
  return el;
}

function renderColumns() {
  const board = $("#board");
  board.innerHTML = "";
  board.style.gridTemplateColumns =
    `repeat(${state.columns.length}, minmax(0, 1fr))`;

  state.columns.forEach((col) => {
    const wrap = document.createElement("div");
    wrap.className = "column";
    wrap.innerHTML =
      `<div class="column-head"><span>${escapeHtml(col.label)}</span>
       <span class="count">${col.players.length}</span></div>`;
    const body = document.createElement("div");
    body.className = "column-body";
    col.players.forEach((p, i) => body.appendChild(card(p, col.players[i - 1])));
    if (!col.players.length) {
      body.innerHTML = `<div class="empty muted">nothing available</div>`;
    }
    wrap.appendChild(body);
    board.appendChild(wrap);
  });
}

/* ------------------------------ big board ------------------------------- */

const BIG_COLUMNS = [
  { key: "ecr", label: "ECR", sort: "ecr", digits: 0 },
  { key: "adp", label: "ADP", sort: "adp", digits: 1 },
  { key: "espn_adp", label: "ESPN", sort: "espn_adp", digits: 1 },
  { key: "vor", label: "VOR", sort: "vor", digits: 0 },
  { key: "proj_points", label: "Proj", sort: "proj", digits: 0 },
  { key: "pts_last", label: "Last", sort: "last", digits: 0 },
  { key: "vegas_vor", label: "Vegas", sort: "vegas_vor", digits: 0,
    title: "Value over replacement from Vegas-implied season totals" },
  // The disagreement between the two projection sources, in VOR points. Positive
  // means the betting market likes him more than the rankings projection does,
  // which is where the draft-day value tends to hide.
  { key: "vegas_edge", label: "Edge", sort: "edge", digits: 0, signed: true,
    title: "Vegas VOR minus projection VOR: positive = Vegas is higher on him",
    derive: (p) => (
      p.vegas_vor == null || p.vor == null ? null : p.vegas_vor - p.vor
    ) },
];

function renderBigBoard() {
  const wrap = $("#bigboard");
  const rows = state.flat.map((p, i) => {
    const cells = BIG_COLUMNS.map((c) => {
      const value = c.derive ? c.derive(p) : p[c.key];
      const classes = [];
      if (c.sort === state.sort) classes.push("is-sorted");
      if (c.signed && value != null && Math.abs(value) >= 10) {
        classes.push(value > 0 ? "edge-up" : "edge-down");
      }
      const attr = classes.length ? ` class="${classes.join(" ")}"` : "";
      const text = c.signed && value != null
        ? (value > 0 ? "+" : "") + num(value, c.digits)
        : num(value, c.digits);
      return `<td${attr}>${text}</td>`;
    }).join("");
    return `<tr data-key="${escapeHtml(p.player_key)}" class="pos-row-${p.position}">
      <td class="idx">${i + 1}</td>
      <td class="pname">${escapeHtml(p.name)}</td>
      <td><span class="badge badge-${p.position}">${p.position}</span></td>
      <td class="muted">${escapeHtml(p.team || "–")}</td>
      <td class="muted">${p.bye ?? "–"}</td>
      ${cells}
    </tr>`;
  }).join("");

  const heads = BIG_COLUMNS.map(
    (c) => `<th class="sortable${c.sort === state.sort ? " is-sorted" : ""}"
              data-sort="${c.sort}"${c.title ? ` title="${escapeHtml(c.title)}"` : ""}
              >${c.label}</th>`
  ).join("");

  wrap.innerHTML = `
    <table class="bigtable">
      <thead><tr>
        <th class="idx">#</th><th>Player</th><th>Pos</th><th>Tm</th><th>Bye</th>
        ${heads}
      </tr></thead>
      <tbody>${rows || `<tr><td colspan="${5 + BIG_COLUMNS.length}"
          class="muted">nothing available</td></tr>`}</tbody>
    </table>`;

  wrap.querySelectorAll("tbody tr[data-key]").forEach((tr) => {
    tr.addEventListener("click", () => {
      const player = state.flat.find((p) => p.player_key === tr.dataset.key);
      if (player) draftPlayer(player.player_key, player.name);
    });
  });

  // Clicking a metric header sorts by it - the usual spreadsheet affordance.
  wrap.querySelectorAll("th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      if ((state.coverage[th.dataset.sort] ?? 0) === 0) {
        toast(`${SORT_LABEL[th.dataset.sort]} has no data in this pool`, true);
        return;
      }
      state.sort = th.dataset.sort;
      $("#sort").value = state.sort;
      loadBoard();
    });
  });
}

/* ----------------------------- projection ------------------------------- */

function renderProjection() {
  const list = $("#proj");
  const proj = state.projection;

  if (!proj || !proj.picks || !proj.picks.length) {
    list.innerHTML = `<li class="muted">${
      proj && proj.note ? escapeHtml(proj.note) : "no upcoming picks"
    }</li>`;
    $("#projNote").textContent = "";
    $("#projHelp").textContent = "";
    return;
  }

  const anyHistory = proj.picks.some((p) => p.has_history);
  const other = proj.compare_signal;
  $("#projNote").textContent = `(next ${proj.picks.length})`;

  list.innerHTML = proj.picks.map((pick) => {
    const top = pick.positions.filter((d) => d.p >= 0.02).slice(0, 4);
    const bars = top.map((d) => {
      // The other signal's number rides alongside, so divergence is visible
      // without toggling back and forth. Only shown when it actually differs.
      // Positional reach: this manager takes THIS position N picks earlier or
      // later than the market implies. The number that says "he may jump the
      // board here", so it sits directly on the position's row.
      const posReach = ((pick.reach || {}).by_position || {})[d.position];
      const reachTag = posReach === undefined ? "" : `
        <span class="preach ${posReach > 0 ? "is-early" : "is-late"}"
              title="takes ${d.position} ${Math.abs(Math.round(posReach))} picks ${
                posReach > 0 ? "earlier" : "later"
              } than the market implies">
          ${posReach > 0 ? "▲" : "▼"}${Math.abs(Math.round(posReach))}</span>`;

      const otherLabel = SIGNAL_SHORT[other] || (other || "").toUpperCase();
      const shift = d.delta === undefined ? "" : (
        Math.abs(d.delta) >= 0.02
          ? `<span class="pbar-alt ${d.delta < 0 ? "is-up" : "is-down"}"
                   title="${otherLabel} says ${Math.round(d.p_other * 100)}%">
               ${otherLabel} ${Math.round(d.p_other * 100)}%</span>`
          : `<span class="pbar-alt is-same" title="both signals agree">=</span>`
      );
      return `
      <div class="pbar-row">
        <span class="pbar-pos badge badge-${d.position}">${d.position}</span>
        <span class="pbar"><i style="width:${Math.round(d.p * 100)}%"></i></span>
        <span class="pbar-val">${Math.round(d.p * 100)}%</span>
        ${reachTag}
        ${shift}
      </div>`;
    }).join("");

    const likely = pick.likely.slice(0, 2).map(
      (l) => `${escapeHtml(l.name)} <span class="muted">${l.position}</span>`
    ).join(" · ");

    const needs = pick.open_needs.length
      ? pick.open_needs.join(" ")
      : "starters full";

    const r = pick.reach;
    const reachBadge = !r ? "" : `
      <span class="reach-badge is-${r.label.replace(" ", "-")}"
            title="median ${r.median > 0 ? "+" : ""}${r.median} picks vs the market
                   (mean ${r.mean > 0 ? "+" : ""}${r.mean}), ${Math.round(r.reach_rate * 100)}% of
                   picks a round-plus early, ${Math.round(r.wait_rate * 100)}% a round-plus late,
                   n=${r.n}">
        ${r.label} ${r.median > 0 ? "+" : ""}${Math.round(r.median)}</span>`;

    return `<li class="${pick.is_me ? "is-me" : ""}">
      <div class="proj-head">
        <span class="proj-who">${escapeHtml(pick.manager_name)}</span>
        ${reachBadge}
        <span class="proj-pick">${pickLabel(pick.round, pick.slot)}</span>
      </div>
      <div class="proj-meta muted">
        needs ${escapeHtml(needs)}
        ${pick.has_history
          ? `· n=${pick.sample_size}`
          : `· <span class="warn-inline">no history</span>`}
      </div>
      ${bars}
      ${likely ? `<div class="proj-likely">likely: ${likely}</div>` : ""}
    </li>`;
  }).join("");

  const fellBack = new Set();
  proj.picks.forEach((p) =>
    ((p.availability || {}).fell_back_to_ecr || []).forEach((x) => fellBack.add(x))
  );

  let help = anyHistory
    ? `Blends each manager's phase tendencies, their open starter slots, and ` +
      `what is still on the board — ranked by ${SORT_LABEL[proj.signal] || proj.signal}. ` +
      `Updates after every pick.`
    : "No linked league history for these managers, so these are league-average " +
      "rates. Link owners on the Setup tab.";
  if (proj.signal === "adp" && fellBack.size) {
    help += ` ${[...fellBack].join("/")} fell back to ECR — no ADP left at ` +
            `${fellBack.size > 1 ? "those positions" : "that position"}.`;
  }
  $("#projHelp").textContent = help;
}

/* ------------------------------ mock mode ------------------------------- */

function renderMockControls() {
  const s = state.data;
  const on = !!(s.league || {}).mock_mode;
  $("#mockFlag").hidden = !on;
  $("#mockSim").hidden = !on;

  // Rebuild whenever the seats OR the names change, not just when the count does.
  // Comparing only `options.length` meant reordering the draft in the Managers box
  // left this dropdown showing the previous order - twelve managers before, twelve
  // after, so it never refreshed.
  const slot = $("#mockSlot");
  if (slot) {
    const signature = s.managers
      .map((m) => `${m.draft_slot}:${m.name}`)
      .join("|");
    if (slot.dataset.signature !== signature) {
      slot.innerHTML = s.managers
        .map((m) => `<option value="${m.draft_slot}">${m.draft_slot}. ${escapeHtml(m.name)}</option>`)
        .join("");
      slot.dataset.signature = signature;
      // Seats moved, so any prior choice is meaningless: fall back to my own seat.
      slot.value = String((s.league || {}).my_slot || 1);
    }
    // Otherwise leave the selection alone - the user may have picked a seat to
    // mock from and a background refresh should not snap it back.
  }
  const sig = $("#mockSignal");
  if (sig) sig.value = (s.league || {}).mock_signal || "ecr";
}

/* Narrates what the bots did, so a mock is inspectable rather than a board that
   silently changed. Shows the reach habit that shaped each pick. */
function renderMockLog(picks, note) {
  const log = $("#mockLog");
  if (!log) return;
  if (!picks || !picks.length) {
    log.innerHTML = `<div class="muted small">${
      note ? escapeHtml(note) : "no simulated picks"
    }</div>`;
    return;
  }
  log.innerHTML =
    `<div class="muted small">${picks.length} simulated pick(s)${
      note ? " — " + escapeHtml(note) : ""
    }</div>` +
    picks.map((p) => {
      const reach = p.reach_median === null || p.reach_median === undefined
        ? ""
        : `<span class="muted">reach ${p.reach_median > 0 ? "+" : ""}${p.reach_median}</span>`;
      return `<div class="mockrow">
        <span class="muted">${p.round}.${String(p.slot).padStart(2, "0")}</span>
        <span class="badge badge-${p.position}">${p.position}</span>
        <span class="mockname">${escapeHtml(p.name)}</span>
        <span class="muted">${escapeHtml(p.manager_name)}</span>
        ${reach}
      </div>`;
    }).join("");
}

async function mockStart() {
  const slot = Number($("#mockSlot").value);
  const label = $("#mockSlot").selectedOptions[0]?.textContent || `slot ${slot}`;
  if (!confirm(`Start a mock draft as ${label}? This clears the board.`)) return;
  const seedRaw = $("#mockSeed").value.trim();
  try {
    const res = await api("/api/mock/start", {
      method: "POST",
      body: JSON.stringify({
        my_slot: slot,
        signal: $("#mockSignal").value,
        seed: seedRaw === "" ? null : Number(seedRaw),
      }),
    });
    state.data = res.state;
    renderMockLog(res.summary.advanced, res.summary.note);
    toast(`Mock started — ${res.summary.advanced.length} picks before you`);
    await refresh();
    switchView("board");
  } catch (err) {
    toast(err.message, true);
  }
}

async function mockAdvance() {
  try {
    const res = await api("/api/mock/advance", {
      method: "POST",
      body: JSON.stringify({}),
    });
    state.data = res.state;
    renderMockLog(res.picks, res.note);
    toast(res.picks.length
      ? `${res.picks.length} opponent pick(s) simulated`
      : (res.note || "nothing to simulate"));
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
}

async function mockStop() {
  try {
    const res = await api("/api/mock/stop", { method: "POST" });
    state.data = res.state;
    toast("Mock mode off — picks left as they are");
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
}

/* ----------------------------- suggestion ------------------------------- */

function renderSuggestion() {
  const s = state.suggestion;
  const list = $("#suggest");
  const warn = $("#suggestWarn");

  if (!s || !s.suggestions || !s.suggestions.length) {
    list.innerHTML = `<li class="muted">${
      s && s.note ? escapeHtml(s.note) : "nothing to suggest"
    }</li>`;
    $("#dropoff").innerHTML = "";
    $("#suggestNote").textContent = "";
    $("#suggestHelp").textContent = "";
    warn.hidden = true;
    return;
  }

  const unit = s.value_basis === "vor" ? "pts" : "ranks";
  $("#suggestNote").textContent = s.on_the_clock
    ? "(you're on the clock)"
    : `(for pick #${s.my_next_overall}, ${s.intervening_picks} ahead of you)`;

  // Positional drop-off is the real decision content: it says which position
  // gets materially worse if you spend this pick elsewhere.
  $("#dropoff").innerHTML =
    `<span class="muted small">Cost of waiting a turn</span>` +
    s.positions
      .filter((p) => p.vona > 0)
      .map((p) => `<span class="drop" title="best available now ${p.expected_best_now}
             ${unit}, at your next pick ${p.expected_best_later} ${unit}">
             <span class="badge badge-${p.position}">${p.position}</span>
             −${Math.round(p.vona)}</span>`)
      .join("");

  list.innerHTML = s.suggestions.map((item) => `
    <li data-key="${escapeHtml(item.player_key)}"
        class="${s.on_the_clock ? "is-clickable" : ""}">
      <div class="sug-head">
        <span class="badge badge-${item.position}">${item.position}</span>
        <span class="sug-name">${escapeHtml(item.name)}</span>
        <span class="muted small">${escapeHtml(item.team || "")}${
          item.bye ? " · bye " + item.bye : ""
        }</span>
        <span class="sug-score">${Math.round(item.score)}</span>
      </div>
      <div class="sug-reason muted">${escapeHtml(item.reason)}</div>
    </li>`).join("");

  if (s.on_the_clock) {
    list.querySelectorAll("li[data-key]").forEach((li) => {
      li.addEventListener("click", () => {
        const item = s.suggestions.find((x) => x.player_key === li.dataset.key);
        if (item) draftPlayer(item.player_key, item.name);
      });
    });
  }

  warn.hidden = !(s.notes && s.notes.length);
  if (s.notes && s.notes.length) warn.textContent = s.notes.join(" ");

  const valueFrom = VALUE_LABEL[s.value_source] || s.value_source;
  $("#suggestHelp").textContent =
    `score = (value + cost of waiting) × roster fit, from ${s.trials} simulated ` +
    `drafts ranked by ${s.signal.toUpperCase()}. Value comes from ${valueFrom}` +
    (s.on_the_clock ? ". Click a row to draft." : ".");
}

/* ------------------------------ sidebar --------------------------------- */

function renderSidebar() {
  const s = state.data;
  const needs = s.my_needs || {};
  $("#needs").innerHTML = ["QB", "RB", "WR", "TE", "FLEX", "K", "DST", "BN"]
    .filter((k) => needs[k] !== undefined)
    .map((k) => {
      const open = needs[k] > 0;
      return `<span class="need ${open ? "is-open" : ""}">${k} ${open ? needs[k] : "✓"}</span>`;
    }).join("");

  $("#rosterCount").textContent = `(${(s.my_roster || []).length})`;
  $("#roster").innerHTML = (s.my_roster || []).map(
    (p) => `<li><span class="slot">R${p.round}</span>
      <span>${escapeHtml(p.name)}</span>
      <span class="muted">${p.position}${p.team ? " · " + p.team : ""}</span></li>`
  ).join("") || `<li class="muted">no picks yet</li>`;

  const recent = (s.picks || []).slice(-14).reverse();
  $("#recent").innerHTML = recent.map(
    (p) => `<li class="${p.is_me ? "is-me" : ""}">
      <span>${escapeHtml(p.name)}</span>
      <span class="who">${pickLabel(p.round, p.slot)} ${escapeHtml(p.manager_name)}</span></li>`
  ).join("") || `<li class="muted">no picks yet</li>`;
}

/* -------------------------------- grid ---------------------------------- */

async function renderGrid() {
  const { managers, grid } = await api("/api/grid");
  const head = managers.map(
    (m) => `<th class="${m.is_me ? "is-me" : ""}">${m.draft_slot}. ${escapeHtml(m.name)}</th>`
  ).join("");

  const body = grid.map((row, i) => {
    const cells = row.map((pick) => {
      if (!pick) return `<td></td>`;
      return `<td class="filled ${pick.is_me ? "is-me" : ""}">
        <span class="pname">${escapeHtml(pick.name)}</span>
        <span class="pmeta">${pick.position}${pick.team ? " · " + pick.team : ""}</span></td>`;
    }).join("");
    return `<tr><td class="rnd">${i + 1}</td>${cells}</tr>`;
  }).join("");

  $("#grid").innerHTML =
    `<thead><tr><th class="rnd"></th>${head}</tr></thead><tbody>${body}</tbody>`;
}

/* -------------------------------- setup --------------------------------- */

function renderSetup() {
  const s = state.data;
  const L = s.league;
  $("#cfgTeams").value = L.n_teams;
  $("#cfgRounds").value = L.rounds;
  $("#cfgScoring").value = L.scoring;
  $("#cfgSnake").value = L.snake_type;

  $("#cfgSlots").innerHTML = ["QB", "RB", "WR", "TE", "FLEX", "K", "DST", "BN"]
    .map((k) => `<label>${k}
      <input type="number" min="0" max="12" data-slot="${k}"
             value="${L.roster_slots[k] ?? 0}"></label>`).join("");

  const owners = (state.history && state.history.owners) || [];
  renderManagers(s.managers, owners);

  const me = (s.managers || []).find((m) => m.is_me);
  $("#managersHelp").textContent = owners.length
    ? "Pick each seat's owner from your league history. Selecting an owner also " +
      "links their past drafts, which is what powers the projections. " +
      (me
        ? `The "me" radio follows ${me.name}, not the seat — reorder the draft and `
          + "it moves with them."
        : 'Tick "me" to say which owner you are.')
    : "No league history imported, so type names by hand. Import history to get " +
      "dropdowns and pick projections.";

  $("#poolInfo").textContent =
    `${s.pool_size} players in the pool · ${s.n_picks} of ${s.total_picks} picks made`;
}

function renderManagers(managers, owners) {
  const list = $("#managers");
  list.innerHTML = managers.map((m) => {
    const control = owners.length
      ? `<select data-mid="${m.id}" class="ownerpick">
           <option value="">— unassigned —</option>
           ${owners.map((o) => `<option value="${escapeHtml(o.owner_guid)}"
             ${o.owner_guid === m.owner_guid ? "selected" : ""}>
             ${escapeHtml(o.owner_label)}</option>`).join("")}
         </select>`
      : `<input type="text" data-mid="${m.id}" value="${escapeHtml(m.name)}">`;
    return `<li>
      <span class="slotnum">${m.draft_slot}</span>
      ${control}
      <label title="This is my seat">
        <input type="radio" name="isme" data-me="${m.id}" ${m.is_me ? "checked" : ""}> me
      </label>
    </li>`;
  }).join("");

  list.querySelectorAll("select.ownerpick").forEach(
    (sel) => sel.addEventListener("change", flagDuplicateOwners)
  );
  flagDuplicateOwners();
}

/* One owner cannot hold two seats. The server enforces it, but silently - it
   would just move them - so warn before the user saves something surprising. */
function flagDuplicateOwners() {
  const picks = $$("select.ownerpick").map((s) => s.value).filter(Boolean);
  const seen = new Set();
  const dupes = new Set();
  picks.forEach((v) => (seen.has(v) ? dupes.add(v) : seen.add(v)));

  const box = $("#managerDupe");
  if (!dupes.size) {
    box.hidden = true;
    return;
  }
  const owners = (state.history && state.history.owners) || [];
  const names = [...dupes].map(
    (g) => (owners.find((o) => o.owner_guid === g) || {}).owner_label || g
  );
  box.hidden = false;
  box.textContent =
    `${names.join(", ")} assigned to more than one seat. Saving will keep only ` +
    `the last one.`;
}

/* ------------------------------- loading -------------------------------- */

async function loadBoard() {
  if (state.query) {
    const { players } = await api(
      `/api/players?q=${encodeURIComponent(state.query)}&sort=${state.sort}&limit=100`
    );
    state.flat = players;
    state.columns = [{ label: `Search: "${state.query}"`, players }];
  } else if (state.mode === "big") {
    const { players } = await api(`/api/players?sort=${state.sort}&limit=300`);
    state.flat = players;
  } else {
    const { columns } = await api(`/api/board?sort=${state.sort}&per_column=80`);
    state.columns = columns;
    state.flat = columns.flatMap((c) => c.players);
  }

  updateSortAvailability(state.flat);

  // Empty the inactive container rather than only hiding it. Leaving stale rows
  // in the DOM keeps their click handlers live and leaves them matchable by
  // querySelector - so the Enter-to-draft shortcut could target an invisible
  // row from the other layout.
  const showBig = state.mode === "big" && !state.query;
  $("#board").hidden = showBig;
  $("#bigboard").hidden = !showBig;
  if (showBig) {
    $("#board").innerHTML = "";
    renderBigBoard();
  } else {
    $("#bigboard").innerHTML = "";
    renderColumns();
  }
}

async function loadProjection() {
  try {
    state.projection = await api(
      `/api/projection?limit=${PROJECTION_COUNT}&signal=${state.signal}&compare=true`
    );
  } catch (err) {
    state.projection = { picks: [], note: err.message };
  }
  renderProjection();
  syncSignalControl();
}

async function loadSuggestion() {
  try {
    state.suggestion = await api(
      `/api/suggest?limit=6&signal=${state.signal}&value=${state.valueSource}`
    );
  } catch (err) {
    state.suggestion = { suggestions: [], note: err.message };
  }
  renderSuggestion();
  syncValueControl();
}

/* Both read the same live board, so they always refresh together. Run in
   parallel: the suggestion involves a few hundred simulated drafts. */
async function loadAdvice() {
  await Promise.all([loadProjection(), loadSuggestion()]);
}

/* Either ADP covers only the top of the board, so a toggle is disabled outright
   when its column is empty - otherwise it silently behaves exactly like ECR. */
const SIGNAL_HELP = {
  ecr: "Rank by expert consensus",
  adp: "Rank by consensus average draft position, from your rankings files",
  espn_adp: "Rank by ESPN's platform ADP — where players go in the league you draft in",
};
const SIGNAL_MISSING = {
  adp: "No consensus ADP in the pool — needs a rankings file with an ADP column",
  espn_adp: "No ESPN ADP in the pool — run tools/espn_adp.py, then rebuild",
};

function syncSignalControl() {
  $$("#projSignal .seg-btn").forEach((b) => {
    const key = b.dataset.signal;
    const have = key === "ecr" || (state.coverage[key] ?? 0) > 0;
    b.classList.toggle("is-active", key === state.signal);
    b.disabled = !have;
    b.title = have ? SIGNAL_HELP[key] : SIGNAL_MISSING[key];
  });
  // If the stored signal has no data (a rebuild dropped the column), fall back.
  if ((state.coverage[state.signal] ?? 1) === 0 && state.signal !== "ecr") {
    setSignal("ecr");
  }
}

function setSignal(signal) {
  if (signal === state.signal) return;
  state.signal = signal;
  localStorage.setItem("draft.signal", signal);
  loadAdvice();
}

/* The Vegas projections are an optional drop-in file, so both toggles that need
   them are disabled outright when data/vegas/ is empty - the same treatment the
   board signals get, and for the same reason: a control that silently does
   nothing reads as a bug. */
const VALUE_HELP = {
  rankings: "Score suggestions on the rankings projections (RotoBaller/FantasyPros)",
  vegas: "Score suggestions on season totals implied by the betting market",
  blend: "Score suggestions on an even average of both projection sources",
};
const VALUE_MISSING =
  "No Vegas projections in the pool — drop a winwithodds CSV in data/vegas/, then rebuild";

function syncValueControl() {
  // Coverage is computed from the loaded board, and the suggestion can arrive
  // first. Staying permissive until the board reports in avoids disabling the
  // toggle on a technicality of load order and then reverting a valid choice.
  const coverageKnown = Object.keys(state.coverage).length > 0;
  const haveVegas = !coverageKnown || (state.coverage.vegas_vor ?? 0) > 0;
  $$("#valueSource .seg-btn").forEach((b) => {
    const key = b.dataset.value;
    const have = key === "rankings" || haveVegas;
    b.classList.toggle("is-active", key === state.valueSource);
    b.disabled = !have;
    b.title = have ? VALUE_HELP[key] : VALUE_MISSING;
  });
  if (!haveVegas && state.valueSource !== "rankings") {
    setValueSource("rankings");
  }
}

function setValueSource(source) {
  if (source === state.valueSource) return;
  state.valueSource = source;
  localStorage.setItem("draft.valueSource", source);
  loadSuggestion();
}

async function refresh({ board = true } = {}) {
  state.data = await api("/api/state");
  renderClock();
  renderSidebar();
  renderMockControls();
  if (board) await loadBoard();
  await loadAdvice();
  if (state.view === "setup") renderSetup();
  if (state.view === "grid") await renderGrid();
}

async function loadHistory() {
  try {
    state.history = await api("/api/history");
  } catch {
    state.history = { owners: [] };
  }
}

/* ------------------------------- actions -------------------------------- */

async function draftPlayer(playerKey, name) {
  try {
    const res = await api("/api/pick", {
      method: "POST",
      body: JSON.stringify({ player_key: playerKey }),
    });
    state.data = res.state;
    const advanced = res.advanced || [];
    toast(
      `${name} → ${labelForPick(res.overall)}` +
      (advanced.length ? ` · ${advanced.length} opponent picks simulated` : "")
    );
    if (advanced.length) renderMockLog(advanced, "after your pick");
    if (state.query) {
      state.query = "";
      $("#search").value = "";
    }
    renderClock();
    renderSidebar();
    await loadBoard();
    await loadAdvice();
    if (state.view === "grid") await renderGrid();
  } catch (err) {
    toast(err.message, true);
  }
}

function labelForPick(overall) {
  const pick = (state.data.picks || []).find((p) => p.overall === overall);
  return pick ? `${pick.manager_name} (${pickLabel(pick.round, pick.slot)})` : `#${overall}`;
}

async function undo() {
  try {
    const res = await api("/api/undo", { method: "POST" });
    state.data = res.state;
    toast("Pick undone");
    renderClock();
    renderSidebar();
    await loadBoard();
    await loadAdvice();
    if (state.view === "grid") await renderGrid();
  } catch (err) {
    toast(err.message, true);
  }
}

/* ------------------------------- wiring --------------------------------- */

function switchView(view) {
  state.view = view;
  $$(".tab").forEach((t) => t.classList.toggle("is-active", t.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.id === `view-${view}`));
  if (view === "grid") renderGrid();
  if (view === "setup") renderSetup();
}

function setMode(mode) {
  state.mode = mode;
  $$("#boardMode .seg-btn").forEach(
    (b) => b.classList.toggle("is-active", b.dataset.mode === mode)
  );
  loadBoard();
}

$$("#boardMode .seg-btn").forEach(
  (b) => b.addEventListener("click", () => setMode(b.dataset.mode))
);

$$("#projSignal .seg-btn").forEach(
  (b) => b.addEventListener("click", () => setSignal(b.dataset.signal))
);

$$("#valueSource .seg-btn").forEach(
  (b) => b.addEventListener("click", () => setValueSource(b.dataset.value))
);

let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const value = e.target.value.trim();
  searchTimer = setTimeout(() => {
    state.query = value;
    loadBoard();
  }, 140);
});

$("#search").addEventListener("keydown", (e) => {
  // "Return" is what older WebKit reports for the same key.
  if (e.key === "Enter" || e.key === "Return") {
    const target = $(".card[data-key], .bigtable tbody tr[data-key]");
    if (target) target.click();
  } else if (e.key === "Escape") {
    e.target.value = "";
    state.query = "";
    loadBoard();
    e.target.blur();
  }
});

$("#sort").addEventListener("change", (e) => {
  state.sort = e.target.value;
  loadBoard();
});

$("#undo").addEventListener("click", undo);
$$(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));

document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName);
  if (e.key === "/" && !typing) {
    e.preventDefault();
    $("#search").focus();
  } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
    e.preventDefault();
    undo();
  } else if (e.key.toLowerCase() === "b" && !typing) {
    setMode(state.mode === "big" ? "positions" : "big");
  }
});

$("#saveLeague").addEventListener("click", async () => {
  const roster_slots = {};
  $$("#cfgSlots input[data-slot]").forEach((i) => {
    roster_slots[i.dataset.slot] = Number(i.value) || 0;
  });
  try {
    const res = await api("/api/league", {
      method: "POST",
      body: JSON.stringify({
        n_teams: Number($("#cfgTeams").value),
        rounds: Number($("#cfgRounds").value),
        scoring: $("#cfgScoring").value,
        snake_type: $("#cfgSnake").value,
        roster_slots,
      }),
    });
    state.data = res.state;
    toast("League saved. Rebuild the pool so VOR matches the new settings.");
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#saveManagers").addEventListener("click", async () => {
  const checked = $("input[name=isme]:checked");
  const meId = checked ? Number(checked.dataset.me) : 0;
  const usingOwners = !!$("select.ownerpick");

  const managers = usingOwners
    ? $$("select.ownerpick").map((sel) => ({
        id: Number(sel.dataset.mid),
        owner_guid: sel.value,
        is_me: Number(sel.dataset.mid) === meId,
      }))
    : $$("#managers input[type=text]").map((i) => ({
        id: Number(i.dataset.mid),
        name: i.value,
        is_me: Number(i.dataset.mid) === meId,
      }));

  try {
    const res = await api("/api/managers", {
      method: "POST",
      body: JSON.stringify({ managers }),
    });
    state.data = res.state;
    toast("Managers saved");
    await loadHistory();
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#seedManagers").addEventListener("click", async () => {
  if (!confirm("Rebuild the manager list from the newest season of league history?")) return;
  try {
    const res = await api("/api/history/seed-managers", { method: "POST" });
    toast(`Seeded ${res.summary.seeded} managers from ${res.summary.season}`);
    await loadHistory();
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
});

async function rebuild(refreshStats) {
  toast(refreshStats ? "Re-downloading stats… this takes a minute" : "Rebuilding pool…");
  try {
    const res = await api("/api/ingest", {
      method: "POST",
      body: JSON.stringify({ refresh: refreshStats }),
    });
    const s = res.summary;
    toast(
      `${s.players} players (${s.source}${s.rankings_file ? " / " + s.rankings_file : ""})` +
      ` · ${s.unmatched} unmatched ids`
    );
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
}

$("#mockStart").addEventListener("click", mockStart);
$("#mockStop").addEventListener("click", mockStop);
$("#mockSim").addEventListener("click", mockAdvance);

$("#rebuild").addEventListener("click", () => rebuild(false));
$("#rebuildRefresh").addEventListener("click", () => rebuild(true));

$("#reset").addEventListener("click", async () => {
  if (!confirm("Clear every pick in this draft?")) return;
  const res = await api("/api/reset", { method: "POST" });
  state.data = res.state;
  toast(`Cleared ${res.cleared} picks`);
  await refresh();
});

loadHistory()
  .then(refresh)
  .catch((err) => toast(err.message, true));

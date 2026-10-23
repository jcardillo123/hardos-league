/* League history dashboard.
 *
 * Vanilla, no build step, same as the draft tool. Every view is a pure render
 * from a fetched payload; nothing is computed in the browser that the API
 * could compute, so the numbers on screen and the numbers in `verify` can
 * never drift apart.
 */

const S = {
  seasons: [],
  current: null,
  season: null,
  view: "standings",
  week: null,
  h2h: null,
  owners: {},
  stamp: null,       // when a published build was exported; static mode only
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const n1 = (v) => (v === null || v === undefined) ? "&ndash;" : Number(v).toFixed(1);
const n2 = (v) => (v === null || v === undefined) ? "&ndash;" : Number(v).toFixed(2);
const signed = (v) => (v > 0 ? "+" : "") + Number(v).toFixed(1);
const cls = (v) => v > 0.05 ? "num-good" : v < -0.05 ? "num-bad" : "dim";

/* Two transports, one set of views.
 *
 * Served by `league.main` there is a FastAPI behind every number. Published to
 * GitHub Pages there is not - Pages serves files, so `league.export` writes
 * every answer the API would give into `docs/data/*.json` and the reader below
 * answers from those instead. The export marks its copy of the page with
 * `window.LEAGUE_STATIC`, which is the only thing that selects between them.
 *
 * Nothing above this line knows which mode it is in, and the JSON comes out of
 * the same `queries.py` the API calls, so the two cannot report different
 * numbers. */
const STATIC = !!window.LEAGUE_STATIC;

async function live(path) {
  const r = await fetch(path);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* non-JSON */ }
    throw new Error(msg);
  }
  return r.json();
}

/* One fetch per file per page load. The big ones (a season of rosters, every
 * lineup row) are only pulled when a view that needs them is opened. */
const FILES = {};
function file(name) {
  if (!FILES[name]) {
    FILES[name] = fetch(`data/${name}`).then((r) => {
      if (!r.ok) throw new Error(`${name} is missing from this build`);
      return r.json();
    }).catch((e) => { delete FILES[name]; throw e; });
  }
  return FILES[name];
}

/* Filtering the live API does in SQL. Done here rather than exported as one
 * file per filter combination: there are 84 of those, and any one going stale
 * is a silently wrong answer. */
function take(rows, q, pred, cmp) {
  const season = q.get("season");
  const pos = q.get("position");
  const out = rows.filter((r) =>
    (!season || r.season === +season) &&
    (!pos || r.position === pos) &&
    (!pred || pred(r)));
  if (cmp) out.sort(cmp);
  const limit = +(q.get("limit") || 0);
  return limit ? out.slice(0, limit) : out;
}

/* These two mirror the ORDER BY in `queries.top_performances` and
 * `queries.player_seasons`, tiebreaks included. Both ends are deterministic so
 * the published tables are in the same order as the live ones, which is what
 * makes a payload-level diff of the two builds meaningful. */
const byWeek = (a, b) =>
  (b.points ?? 0) - (a.points ?? 0) ||
  b.season - a.season || b.week - a.week ||
  a.player_name.localeCompare(b.player_name);

const bySeason = (a, b) =>
  b.total_points - a.total_points ||
  b.season - a.season ||
  a.player_name.localeCompare(b.player_name) ||
  a.owner_label.localeCompare(b.owner_label);

async function stat(path) {
  const [p, qs] = path.split("?");
  const q = new URLSearchParams(qs || "");
  const seg = p.replace(/^\/api\//, "").split("/");

  switch (seg[0]) {
    case "seasons": return file("seasons.json");
    case "owners": return file("owners.json");
    case "standings": return file(`standings-${seg[1]}.json`);
    case "results": return file(`results-${seg[1]}.json`);
    case "careers": return file("careers.json");
    case "records": return file("records.json");

    case "roster": {
      const d = await file(`rosters-${seg[1]}.json`);
      const players = d[`${seg[2]}-${seg[3]}`];
      if (!players) throw new Error("No roster stored for that team-week.");
      return { players };
    }
    case "luck":
      return { board: (await file("luck.json"))[q.get("season") || "all"] };
    case "lineup-misses":
      return { misses: (await file("misses.json"))[q.get("season") || "all"] };
    case "moves":
      return (await file("moves.json"))[q.get("season") || "all"];
    case "h2h":
      return (await file("h2h.json"))[
        q.get("regular_only") === "true" ? "regular" : "full"];
    case "rivalry": {
      // Owner guids are brace-wrapped, which makes a poor filename, so every
      // pair lives in one file keyed "a|b".
      const d = await file("rivalries.json");
      const r = d[`${seg[1]}|${seg[2]}`];
      if (!r) throw new Error("Those two have never played each other.");
      return r;
    }
    case "manager": {
      const d = (await file("managers.json"))[seg[1]];
      if (!d) throw new Error("No such manager.");
      return d;
    }
    case "players":
      return { seasons: take((await file("players.json")).seasons, q, null,
        bySeason) };
    case "performances":
      return { performances: take((await file("players.json")).weeks, q,
        (r) => r.started === 1 && r.points !== null, byWeek) };
    case "player-search": {
      const term = (q.get("name") || "").toLowerCase();
      const weeks = (await file("players.json")).weeks
        .filter((r) => r.player_name.toLowerCase().includes(term))
        .slice(0, 200);     // the export is ordered season desc, week desc
      return { name: term, weeks };
    }
  }
  throw new Error(`${path} is not available in the published build`);
}

const api = (path) => (STATIC ? stat(path) : live(path));

function showErr(e) {
  const box = $("err");
  box.textContent = e.message || String(e);
  box.hidden = false;
}
function clearErr() { $("err").hidden = true; }

/* ------------------------------------------------------------------ *
 *  sortable tables
 * ------------------------------------------------------------------ */

/* cols: [{key, label, left?, fmt?, cls?, sort?, pin?}]  `sort` overrides the
 * raw value used for ordering. Clicking a header re-sorts in place without
 * refetching.
 *
 * `pin` marks the column that stays put when the table is scrolled sideways
 * on a narrow screen - the one that says whose row you are looking at. It
 * defaults to the first column, which is wrong wherever the first column is
 * a rank or a date, so those tables name their own. */
function table(rows, cols, opts = {}) {
  const id = "t" + Math.random().toString(36).slice(2, 8);
  const pin = Math.max(0, cols.findIndex((c) => c.pin));
  const head = cols.map((c, i) =>
    `<th class="${c.left ? "left" : ""}${i === pin ? " pin" : ""}" ` +
    `data-i="${i}">${c.label}</th>`).join("");
  // The wrapper is what scrolls sideways on a narrow screen. Without it a
  // 12-column table drags the whole page with it, and the sticky tabs slide
  // off the left edge.
  const html =
    `<div class="tscroll"><table id="${id}" class="sortable">` +
    `<thead><tr>${head}</tr></thead><tbody></tbody></table></div>`;

  // Rendering is deferred to a function so re-sorting reuses it.
  const draw = (root) => {
    const el = root.querySelector("#" + id);
    const tb = el.querySelector("tbody");
    let dir = opts.desc === false ? 1 : -1;
    let key = opts.sortBy ?? null;

    const paint = () => {
      const sorted = [...rows];
      if (key !== null) {
        const c = cols[key];
        const get = (r) => c.sort ? c.sort(r) : r[c.key];
        sorted.sort((a, b) => {
          const x = get(a), y = get(b);
          if (x === y) return 0;
          if (x === null || x === undefined) return 1;
          if (y === null || y === undefined) return -1;
          return (typeof x === "string" ? x.localeCompare(y) : x - y) * dir;
        });
      }
      tb.innerHTML = sorted.map((r) => {
        const tds = cols.map((c, i) => {
          const raw = r[c.key];
          const body = c.fmt ? c.fmt(r) : (raw ?? "&ndash;");
          const k = c.cls ? c.cls(r) : "";
          return `<td class="${c.left ? "left " : ""}${k}` +
                 `${i === pin ? " pin" : ""}">${body}</td>`;
        }).join("");
        const click = opts.onRow ? ' class="clickable"' : "";
        return `<tr${click} data-k="${esc(opts.rowKey ? opts.rowKey(r) : "")}">${tds}</tr>`;
      }).join("");
      el.querySelectorAll("thead th").forEach((th, i) =>
        th.classList.toggle("sorted", i === key));
    };

    el.querySelectorAll("thead th").forEach((th) => {
      th.onclick = () => {
        const i = +th.dataset.i;
        if (i === key) dir = -dir; else { key = i; dir = -1; }
        paint();
      };
    });
    if (opts.onRow) {
      tb.onclick = (ev) => {
        const tr = ev.target.closest("tr");
        if (tr) opts.onRow(tr.dataset.k);
      };
    }
    paint();
  };
  return { html, draw };
}

function mount(hostId, t) {
  const host = $(hostId);
  host.innerHTML = t.html;
  t.draw(host);
}

function bar(pct, color) {
  const w = Math.max(0, Math.min(100, pct));
  return `<span class="bar-wrap">${n1(pct)}<span class="bar">` +
         `<i style="width:${w}%;background:${color}"></i></span></span>`;
}

/* Diverging bar for luck: centre is zero, scaled so `max` fills half. */
function dbar(v, max) {
  const half = 50;
  const w = Math.min(half, Math.abs(v) / max * half);
  const side = v >= 0
    ? `left:50%;width:${w}%;background:var(--good)`
    : `right:50%;width:${w}%;background:var(--bad)`;
  return `<span class="bar-wrap"><span class="${cls(v)}">${signed(v)}</span>` +
         `<span class="dbar"><i style="${side}"></i></span></span>`;
}

const posTag = (p) => p ? `<span class="pos ${esc(p)}">${esc(p)}</span>` : "";

/* ------------------------------------------------------------------ *
 *  boot
 * ------------------------------------------------------------------ */

/* Table headers stick below the top bar, and the top bar wraps to two rows on
 * a narrow window, so the offset has to be measured rather than assumed. */
function measureStick() {
  const h = document.querySelector(".stickyhead").getBoundingClientRect().height;
  document.documentElement.style.setProperty("--stick", Math.round(h) + "px");
}
// A ResizeObserver rather than a resize listener: the first measurement can
// land before fonts settle or while the tab is hidden, and only the observer
// corrects it when the real layout arrives.
new ResizeObserver(measureStick).observe(document.querySelector(".stickyhead"));

async function boot() {
  measureStick();
  if (STATIC) {
    // Nothing to refresh against: a published build is a snapshot, and the
    // button's whole job is writing to a database that is not there.
    $("refresh-btn").hidden = true;
    file("manifest.json").then((m) => { S.stamp = m.generated_at; })
      .catch(() => { /* a stamp is nice to have, not worth an error box */ })
      .finally(updateFreshness);
  }
  try {
    const d = await api("/api/seasons");
    S.seasons = d.seasons;
    S.current = d.current;
    S.season = d.seasons[0].season;

    const sel = $("season-select");
    sel.innerHTML = d.seasons.map((s) =>
      `<option value="${s.season}">${s.season}${s.complete ? "" : " (live)"}</option>`
    ).join("");
    sel.value = S.season;
    sel.onchange = () => { S.season = +sel.value; S.week = null; render(); };

    // Scope pickers that want an "all seasons" option.
    const allOpt = '<option value="">All seasons</option>';
    $("luck-scope").innerHTML = allOpt + d.seasons.map((s) =>
      `<option value="${s.season}">${s.season}</option>`).join("");
    $("player-season").innerHTML = allOpt + d.seasons.map((s) =>
      `<option value="${s.season}">${s.season}</option>`).join("");
    $("moves-scope").innerHTML = allOpt + d.seasons.map((s) =>
      `<option value="${s.season}">${s.season}</option>`).join("");

    const owners = await api("/api/owners");
    owners.owners.forEach((o) => { S.owners[o.owner_guid] = o.label; });

    updateFreshness();
    render();
  } catch (e) { showErr(e); }
}

function seasonMeta(y) { return S.seasons.find((s) => s.season === y) || {}; }

function updateFreshness() {
  const m = seasonMeta(S.season);
  $("live-pill").hidden = !!m.complete;
  const weeks = m.weeks_played ?? 0;
  const head = `${esc(m.league_name || "")} &middot; ${m.n_teams} teams`;
  if (STATIC) {
    $("freshness").innerHTML = `${head} &middot; ${weeks} weeks` +
      (S.stamp ? ` &middot; data as of ${esc(S.stamp)}` : "");
    return;
  }
  $("freshness").innerHTML = m.complete
    ? `${head} &middot; ${weeks} weeks`
    : `${head} &middot; <b>${weeks} weeks ingested</b> ` +
      `&middot; refresh to pull newer ones`;
  $("refresh-btn").textContent = `Refresh ${S.season} from ESPN`;
}

/* ------------------------------------------------------------------ *
 *  tabs
 * ------------------------------------------------------------------ */

$("tabs").onclick = (ev) => {
  const b = ev.target.closest(".tab");
  if (!b) return;
  document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  b.classList.add("active");
  $("view-" + b.dataset.view).classList.add("active");
  S.view = b.dataset.view;
  render();
};

function render() {
  clearErr();
  updateFreshness();
  const fn = {
    standings: renderStandings, weekly: renderWeekly, luck: renderLuck,
    h2h: renderH2H, managers: renderCareers, players: renderPlayers,
    moves: renderMoves, records: renderRecords,
  }[S.view];
  if (fn) fn().catch(showErr);
}

/* ------------------------------------------------------------------ *
 *  1. standings
 * ------------------------------------------------------------------ */

async function renderStandings() {
  const d = await api(`/api/standings/${S.season}`);
  const rows = d.standings;
  const maxLuck = Math.max(1, ...rows.map((r) => Math.abs(r.luck)));

  const t = table(rows, [
    { key: "final_rank", label: "#", left: true,
      fmt: (r) => r.final_rank
        ? `<span class="medal medal-${r.final_rank}">${r.final_rank}</span>`
        : '<span class="dim">&ndash;</span>' },
    { key: "owner_label", label: "Manager", left: true, pin: true,
      fmt: (r) => `<span class="strong">${esc(r.owner_label)}</span>` +
                  `<span class="mu-team">${esc(r.team_name)}</span>` },
    { key: "wins", label: "Record",
      fmt: (r) => `${r.wins}-${r.losses}${r.ties ? "-" + r.ties : ""}` },
    { key: "points_for", label: "PF", fmt: (r) => n1(r.points_for) },
    { key: "points_against", label: "PA", fmt: (r) => n1(r.points_against) },
    { key: "avg_actual", label: "Avg", fmt: (r) => n1(r.avg_actual) },
    { key: "expected_wins", label: "xW", fmt: (r) => n1(r.expected_wins) },
    { key: "luck", label: "Luck", fmt: (r) => dbar(r.luck, maxLuck) },
    { key: "all_play_pct", label: "All-play",
      fmt: (r) => bar(r.all_play_pct, "var(--accent)") },
    { key: "efficiency", label: "Eff",
      fmt: (r) => bar(r.efficiency, "var(--te)") },
    { key: "points_left", label: "Left", fmt: (r) => n1(r.points_left) },
    { key: "best_week", label: "Best wk", fmt: (r) => n1(r.best_week) },
    { key: "worst_week", label: "Worst wk", fmt: (r) => n1(r.worst_week) },
  ], {
    sortBy: 0, desc: false,
    rowKey: (r) => r.owner_guid,
    onRow: (guid) => guid && openManager(guid),
  });
  mount("standings-body", t);
}

/* ------------------------------------------------------------------ *
 *  2. weekly results
 * ------------------------------------------------------------------ */

async function renderWeekly() {
  const d = await api(`/api/results/${S.season}`);
  const weeks = d.weeks;
  if (!weeks.length) {
    $("week-chips").innerHTML = "";
    $("week-summary").innerHTML = "";
    $("matchups").innerHTML = '<p class="empty">No weeks played yet.</p>';
    return;
  }
  if (S.week === null) S.week = weeks[weeks.length - 1].week;

  $("week-chips").innerHTML = weeks.map((w) =>
    `<button class="chip${w.week === S.week ? " active" : ""}` +
    `${w.is_playoff ? " playoff" : ""}" data-w="${w.week}">` +
    `Wk ${w.week}${w.is_playoff ? " &middot; PO" : ""}</button>`).join("");
  $("week-chips").onclick = (ev) => {
    const c = ev.target.closest(".chip");
    if (c) { S.week = +c.dataset.w; renderWeekly().catch(showErr); }
  };

  const wk = weeks.find((w) => w.week === S.week) || {};
  $("week-summary").innerHTML =
    `<div class="card"><div class="kv">` +
    `<div><div class="k">League avg</div><div class="v">${n1(wk.avg_score)}</div></div>` +
    `<div><div class="k">High</div><div class="v num-good">${n1(wk.high)}</div></div>` +
    `<div><div class="k">Low</div><div class="v num-bad">${n1(wk.low)}</div></div>` +
    `<div><div class="k">Week winner</div><div class="v" style="font-size:15px">` +
    `${esc(wk.top_owner || "&ndash;")}</div></div>` +
    `</div></div>`;

  const games = d.results.filter((m) => m.week === S.week);
  $("matchups").innerHTML = games.map(renderMatchup).join("") ||
    '<p class="empty">Nothing scheduled.</p>';

  $("matchups").onclick = (ev) => {
    const side = ev.target.closest(".mu-side");
    if (!side || !side.dataset.team) return;
    toggleRoster(side);
  };
}

function renderMatchup(m) {
  const homeWin = m.played && m.home_actual > m.away_actual;
  const awayWin = m.played && m.away_actual > m.home_actual;
  const side = (who, win) => `
    <div class="mu-side${win ? " win" : ""}" data-team="${m[who + "_team"]}">
      <div class="mu-name">
        <span class="mu-owner">${esc(m[who + "_owner"] || "?")}</span>
        <span class="mu-team">${esc(m[who + "_name"] || "")}</span>
      </div>
      <div class="mu-opt">opt ${n1(m[who + "_optimal"])}</div>
      <div class="mu-score">${m.played ? n1(m[who + "_actual"]) : "&ndash;"}</div>
    </div>`;
  const tier = m.is_playoff
    ? (m.playoff_tier || "PLAYOFF").replace(/_/g, " ") : "";
  return `<div class="mu">
    <div class="mu-head">
      <span>Week ${m.week}${tier ? " &middot; " + esc(tier) : ""}</span>
      <span>${m.played ? "margin " + n1(m.margin) : "not played"}</span>
    </div>
    ${side("away", awayWin)}
    ${side("home", homeWin)}
  </div>`;
}

async function toggleRoster(sideEl) {
  const open = sideEl.nextElementSibling;
  if (open && open.classList.contains("roster-pop")) { open.remove(); return; }
  sideEl.parentElement.querySelectorAll(".roster-pop").forEach((e) => e.remove());
  const teamId = sideEl.dataset.team;
  try {
    const d = await api(`/api/roster/${S.season}/${S.week}/${teamId}`);
    const rows = d.players.map((p) => `
      <tr class="${p.started ? "" : "bench-row"}">
        <td class="left">${esc(p.slot_label)}</td>
        <td class="left">${posTag(p.position)}</td>
        <td class="left">${esc(p.player_name)}</td>
        <td class="dim">${esc(p.pro_team || "")}</td>
        <td>${n1(p.projected)}</td>
        <td class="${p.started ? "strong" : ""}">${n1(p.points)}</td>
      </tr>`).join("");
    const div = document.createElement("div");
    div.className = "roster-pop";
    div.innerHTML = `<div class="tscroll"><table><thead><tr>
      <th class="left">Slot</th><th class="left">Pos</th><th class="left">Player</th>
      <th>Team</th><th>Proj</th><th>Pts</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
    sideEl.after(div);
  } catch (e) { showErr(e); }
}

/* ------------------------------------------------------------------ *
 *  3. luck and lineup misses
 * ------------------------------------------------------------------ */

$("luck-scope").onchange = () => renderLuck().catch(showErr);

async function renderLuck() {
  const scope = $("luck-scope").value;
  const q = scope ? `?season=${scope}` : "";
  const [l, m] = await Promise.all([
    api("/api/luck" + q),
    api("/api/lineup-misses" + (scope ? `${q}&limit=25` : "?limit=25")),
  ]);

  const rows = l.board;
  const maxLuck = Math.max(1, ...rows.map((r) => Math.abs(r.luck)));
  mount("luck-body", table(rows, [
    { key: "label", label: "Manager", left: true,
      fmt: (r) => `<span class="strong">${esc(r.label)}</span>` },
    { key: "seasons", label: "Szns" },
    { key: "wins", label: "Record",
      fmt: (r) => `${r.wins}-${r.losses}${r.ties ? "-" + r.ties : ""}` },
    { key: "win_pct", label: "Win%", fmt: (r) => n1(r.win_pct) },
    { key: "expected_wins", label: "xW", fmt: (r) => n1(r.expected_wins) },
    { key: "luck", label: "Luck", fmt: (r) => dbar(r.luck, maxLuck) },
    { key: "luck_per_season", label: "Luck/szn",
      fmt: (r) => `<span class="${cls(r.luck_per_season)}">` +
                  `${signed(r.luck_per_season)}</span>` },
    { key: "all_play_pct", label: "All-play",
      fmt: (r) => bar(r.all_play_pct, "var(--accent)") },
    { key: "avg_actual", label: "Avg pts", fmt: (r) => n1(r.avg_actual) },
    { key: "efficiency", label: "Eff",
      fmt: (r) => bar(r.efficiency, "var(--te)") },
    { key: "avg_left", label: "Left/wk", fmt: (r) => n1(r.avg_left) },
  ], {
    sortBy: 5,
    rowKey: (r) => r.owner_guid,
    onRow: (guid) => guid && openManager(guid),
  }));

  mount("misses-body", table(m.misses, [
    { key: "season", label: "Season", left: true,
      fmt: (r) => `${r.season} wk${r.week}` },
    { key: "owner_label", label: "Manager", left: true, pin: true,
      fmt: (r) => esc(r.owner_label) },
    { key: "actual", label: "Started", fmt: (r) => n1(r.actual) },
    { key: "optimal", label: "Best available", fmt: (r) => n1(r.optimal) },
    { key: "left_behind", label: "Left",
      fmt: (r) => `<span class="strong num-bad">${n1(r.left_behind)}</span>` +
        (r.cost_a_win ? '<span class="flag">COST A WIN</span>' : "") },
    { key: "result", label: "Result",
      fmt: (r) => r.result
        ? `${r.result} ${n1(r.actual)}&ndash;${n1(r.opp_actual)}` : "&ndash;" },
    { key: "benched", label: "Should have started", left: true,
      sort: (r) => r.benched.length,
      fmt: (r) => r.benched.slice(0, 2).map((s) =>
        `<div class="swap"><b>${esc(s.in_name)}</b> ${n1(s.in_pts)} ` +
        `for <s>${esc(s.out_name)}</s> ${n1(s.out_pts)}</div>`).join("") },
  ], { sortBy: 4 }));
}

/* ------------------------------------------------------------------ *
 *  4. head to head
 * ------------------------------------------------------------------ */

$("h2h-regular").onchange = () => { S.h2h = null; renderH2H().catch(showErr); };

async function renderH2H() {
  const reg = $("h2h-regular").checked;
  const d = await api(`/api/h2h?regular_only=${reg}`);
  S.h2h = d;
  const os = d.owners;

  const head = `<tr><th class="rowhead"></th>` +
    os.map((o) => `<th>${esc(o.label.split(" ")[0])}</th>`).join("") +
    `<th>Total</th></tr>`;

  const body = os.map((a) => {
    const row = d.matrix[a.guid] || {};
    let tw = 0, tl = 0, tt = 0;
    const cells = os.map((b) => {
      if (a.guid === b.guid) return '<td class="self">&mdash;</td>';
      const c = row[b.guid];
      if (!c) return '<td class="none">&middot;</td>';
      tw += c.w; tl += c.l; tt += c.t;
      const k = c.w > c.l ? "num-good" : c.w < c.l ? "num-bad" : "dim";
      return `<td class="cell ${k}" data-a="${a.guid}" data-b="${b.guid}" ` +
             `title="${esc(a.label)} vs ${esc(b.label)}: avg margin ` +
             `${signed(c.avg_margin)}">${c.w}-${c.l}${c.t ? "-" + c.t : ""}</td>`;
    }).join("");
    const tk = tw > tl ? "num-good" : tw < tl ? "num-bad" : "dim";
    return `<tr><th class="rowhead">${esc(a.label)}</th>${cells}` +
           `<td class="${tk} strong">${tw}-${tl}${tt ? "-" + tt : ""}</td></tr>`;
  }).join("");

  const host = $("h2h-body");
  host.innerHTML = `<table class="matrix"><thead>${head}</thead><tbody>${body}</tbody></table>`;
  host.onclick = (ev) => {
    const c = ev.target.closest("td.cell");
    if (c) openRivalry(c.dataset.a, c.dataset.b);
  };
}

async function openRivalry(a, b) {
  try {
    const d = await api(`/api/rivalry/${a}/${b}`);
    const rows = d.games.map((g) => `
      <tr>
        <td class="left">${g.season} wk${g.week}${g.is_playoff ? " &middot; PO" : ""}</td>
        <td class="${g.result === "W" ? "num-good" : g.result === "L" ? "num-bad" : "dim"} strong">${g.result}</td>
        <td>${n1(g.actual)}</td>
        <td>${n1(g.opp_actual)}</td>
        <td class="${cls(g.margin)}">${signed(g.margin)}</td>
      </tr>`).join("");
    const r = d.record;
    openDialog(`${d.a.label} vs ${d.b.label} &mdash; ${r.w}-${r.l}${r.t ? "-" + r.t : ""}`,
      `<div class="tscroll"><table><thead><tr><th class="left">When</th>` +
      `<th>Result</th><th>${esc(d.a.label.split(" ")[0])}</th>` +
      `<th>${esc(d.b.label.split(" ")[0])}</th><th>Margin</th></tr></thead>` +
      `<tbody>${rows}</tbody></table></div>`);
  } catch (e) { showErr(e); }
}

/* ------------------------------------------------------------------ *
 *  5. managers
 * ------------------------------------------------------------------ */

async function renderCareers() {
  const d = await api("/api/careers");
  const maxLuck = Math.max(1, ...d.careers.map((r) => Math.abs(r.luck ?? 0)));
  mount("careers-body", table(d.careers, [
    { key: "label", label: "Manager", left: true,
      fmt: (r) => `<span class="strong">${esc(r.label)}</span>` +
        `<span class="mu-team">${r.first_seen}&ndash;${r.last_seen}</span>` },
    { key: "seasons", label: "Szns" },
    { key: "wins", label: "Record",
      fmt: (r) => `${r.wins}-${r.losses}${r.ties ? "-" + r.ties : ""}` },
    { key: "win_pct", label: "Win%",
      fmt: (r) => `<span class="strong">${n1(r.win_pct)}</span>` },
    { key: "titles", label: "Titles",
      fmt: (r) => r.titles ? `<span class="medal medal-1">${r.titles}</span>` : '<span class="dim">0</span>' },
    { key: "playoff_appearances", label: "Playoffs" },
    { key: "best_finish", label: "Best",
      fmt: (r) => r.best_finish ?? "&ndash;" },
    { key: "points_for", label: "PF", fmt: (r) => n1(r.points_for) },
    { key: "avg_actual", label: "Avg pts", fmt: (r) => n1(r.avg_actual) },
    { key: "luck", label: "Luck", fmt: (r) => dbar(r.luck ?? 0, maxLuck) },
    { key: "all_play_pct", label: "All-play",
      fmt: (r) => bar(r.all_play_pct ?? 0, "var(--accent)") },
    { key: "efficiency", label: "Eff",
      fmt: (r) => bar(r.efficiency ?? 0, "var(--te)") },
  ], {
    sortBy: 3,
    rowKey: (r) => r.owner_guid,
    onRow: (guid) => guid && openManager(guid),
  }));
}

async function openManager(guid) {
  try {
    const d = await api(`/api/manager/${guid}`);
    const seasons = d.seasons.map((s) => `
      <tr>
        <td class="left">${s.season}${s.complete ? "" : " <span class='dim'>(live)</span>"}</td>
        <td class="left dim">${esc(s.team_name || "")}</td>
        <td>${s.wins}-${s.losses}${s.ties ? "-" + s.ties : ""}</td>
        <td>${n1(s.points_for)}</td>
        <td>${n1(s.expected_wins)}</td>
        <td class="${cls(s.luck ?? 0)}">${signed(s.luck ?? 0)}</td>
        <td>${n1(s.efficiency)}</td>
        <td>${s.final_rank || "&ndash;"} <span class="dim">/ ${s.n_teams}</span></td>
      </tr>`).join("");
    const loyal = d.loyalty.slice(0, 15).map((p) => `
      <tr>
        <td class="left">${esc(p.player_name)}</td>
        <td class="left">${posTag(p.position)}</td>
        <td>${p.weeks}</td>
        <td>${p.seasons}</td>
        <td>${p.weeks_started}</td>
        <td>${n1(p.total_points)}</td>
      </tr>`).join("");
    openDialog(esc(S.owners[guid] || "Manager"),
      `<h2>Season by season</h2>
       <div class="tscroll"><table><thead><tr><th class="left">Season</th>
       <th class="left">Team</th>
       <th>Record</th><th>PF</th><th>xW</th><th>Luck</th><th>Eff%</th>
       <th>Finish</th></tr></thead><tbody>${seasons}</tbody></table></div>
       <h2>Most-rostered players</h2>
       <p class="note">The closest thing this data has to a personality test.</p>
       <div class="tscroll"><table><thead><tr><th class="left">Player</th>
       <th class="left">Pos</th>
       <th>Weeks</th><th>Szns</th><th>Started</th><th>Points</th></tr></thead>
       <tbody>${loyal}</tbody></table></div>`);
  } catch (e) { showErr(e); }
}

/* ------------------------------------------------------------------ *
 *  6. players
 * ------------------------------------------------------------------ */

$("player-season").onchange = () => renderPlayers().catch(showErr);
$("player-pos").onchange = () => renderPlayers().catch(showErr);

let searchTimer = null;
$("player-search").oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => renderPlayers().catch(showErr), 250);
};

async function renderPlayers() {
  const season = $("player-season").value;
  const pos = $("player-pos").value;
  const term = $("player-search").value.trim();

  if (term.length >= 2) {
    const d = await api(`/api/player-search?name=${encodeURIComponent(term)}`);
    if (!d.weeks.length) {
      $("players-body").innerHTML =
        `<p class="empty">Nobody matching &ldquo;${esc(term)}&rdquo; has ever been rostered here.</p>`;
      return;
    }
    mount("players-body", table(d.weeks, [
      { key: "season", label: "When", left: true,
        fmt: (r) => `${r.season} wk${r.week}`,
        sort: (r) => r.season * 100 + r.week },
      { key: "player_name", label: "Player", left: true, pin: true,
        fmt: (r) => esc(r.player_name) },
      { key: "position", label: "Pos", left: true, fmt: (r) => posTag(r.position) },
      { key: "owner_label", label: "Rostered by", left: true,
        fmt: (r) => esc(r.owner_label) },
      { key: "slot_label", label: "Slot", left: true,
        fmt: (r) => r.started
          ? `<span class="strong">${esc(r.slot_label)}</span>`
          : `<span class="dim">${esc(r.slot_label)}</span>` },
      { key: "projected", label: "Proj", fmt: (r) => n1(r.projected) },
      { key: "points", label: "Pts",
        fmt: (r) => `<span class="${r.started ? "strong" : "dim"}">${n1(r.points)}</span>` },
    ], { sortBy: 0 }));
    return;
  }

  const q = new URLSearchParams();
  if (season) q.set("season", season);
  if (pos) q.set("position", pos);
  q.set("limit", "80");
  const [ps, perf] = await Promise.all([
    api("/api/players?" + q),
    api("/api/performances?" + q),
  ]);

  const host = $("players-body");
  host.innerHTML = `<h2>Best player-seasons</h2>
    <p class="note">Grouped by player <em>and</em> roster, so a player who
    changed hands shows up once per manager &mdash; which is what makes
    &ldquo;who got the good half of his season&rdquo; answerable.</p>
    <div id="pseasons"></div>
    <h2>Best single weeks</h2>
    <p class="note">Started lineups only.</p>
    <div id="pweeks"></div>`;

  mount("pseasons", table(ps.seasons, [
    { key: "player_name", label: "Player", left: true,
      fmt: (r) => `<span class="strong">${esc(r.player_name)}</span>` },
    { key: "position", label: "Pos", left: true, fmt: (r) => posTag(r.position) },
    { key: "season", label: "Season", left: true },
    { key: "owner_label", label: "Manager", left: true,
      fmt: (r) => esc(r.owner_label) },
    { key: "weeks_rostered", label: "Weeks" },
    { key: "weeks_started", label: "Started" },
    { key: "total_points", label: "Total",
      fmt: (r) => `<span class="strong">${n1(r.total_points)}</span>` },
    { key: "started_points", label: "While starting",
      fmt: (r) => n1(r.started_points) },
    { key: "avg_points", label: "Avg", fmt: (r) => n1(r.avg_points) },
    { key: "best_week", label: "Best wk", fmt: (r) => n1(r.best_week) },
  ], { sortBy: 6 }));

  mount("pweeks", table(perf.performances, [
    { key: "season", label: "When", left: true,
      fmt: (r) => `${r.season} wk${r.week}`,
      sort: (r) => r.season * 100 + r.week },
    { key: "player_name", label: "Player", left: true, pin: true,
      fmt: (r) => `<span class="strong">${esc(r.player_name)}</span>` },
    { key: "position", label: "Pos", left: true, fmt: (r) => posTag(r.position) },
    { key: "owner_label", label: "Manager", left: true,
      fmt: (r) => esc(r.owner_label) },
    { key: "projected", label: "Proj", fmt: (r) => n1(r.projected) },
    { key: "points", label: "Pts",
      fmt: (r) => `<span class="strong">${n1(r.points)}</span>` },
    { key: "beat", label: "Over proj",
      sort: (r) => (r.points ?? 0) - (r.projected ?? 0),
      fmt: (r) => r.projected === null ? "&ndash;"
        : `<span class="${cls(r.points - r.projected)}">` +
          `${signed(r.points - r.projected)}</span>` },
  ], { sortBy: 5 }));
}

/* ------------------------------------------------------------------ *
 *  7. trades and waivers
 * ------------------------------------------------------------------ */

/* A+ and A share a colour, and so would A and B if the eye could not tell
 * them apart - the point of the pill is to be readable at a glance down a
 * column of 130 rows, not to encode six distinct values. */
const GRADE_CLS = { "A+": "g-a", A: "g-a", B: "g-b", C: "g-c", D: "g-d", F: "g-f" };
const gradeTag = (g) =>
  `<span class="grade ${GRADE_CLS[g] || "g-c"}">${esc(g)}</span>`;

const PICKUP_KIND = { WAIVER: "Waiver", FREEAGENT: "Free agent" };

/* The headline player of a side and a count of the rest.
 *
 * One line each way, not a full list: the query hands back every player in the
 * deal, but 130 rows of six-name cells is not a table anybody reads, and the
 * whole deal is one click away in the dialog. The rows are already two lines
 * tall for the team name, so two more costs nothing. */
function topName(players, got) {
  if (!players.length) return '<div class="swap dim">nothing recoverable</div>';
  const [p] = players;                     // the query sorts by pts_after
  const rest = players.length - 1;
  return `<div class="swap">${got ? "<b>" : "<s>"}${esc(p.player_name)}` +
         `${got ? "</b>" : "</s>"} ${n1(p.pts_after)}` +
         (rest ? ` <span class="dim">+${rest}</span>` : "") + "</div>";
}

$("moves-scope").onchange = () => renderMoves().catch(showErr);

async function renderMoves() {
  const scope = $("moves-scope").value;
  const d = await api("/api/moves" + (scope ? `?season=${scope}` : ""));

  // Keyed so a row click can find its own side again without a second
  // request: every player in the deal is already in the payload.
  const byKey = new Map();
  d.trades.forEach((t) => byKey.set(`${t.trade_id}|${t.team_id}`, t));

  if (!d.trades.length) {
    $("trades-body").innerHTML =
      '<p class="empty">No trades were made in this scope.</p>';
  } else {
    const maxNet = Math.max(1, ...d.trades.map((t) => Math.abs(t.net)));
    mount("trades-body", table(d.trades, [
      { key: "season", label: "When", left: true,
        fmt: (r) => `${r.season} wk${r.week}`,
        sort: (r) => r.season * 100 + r.week },
      // Both managers in the one pinned cell. A trade is a row about a pair,
      // and splitting the pair across two columns cost more width than the
      // table had: the opponent would scroll out of reach on a phone, which
      // is the one thing the pinned column exists to prevent.
      { key: "owner_label", label: "Manager", left: true, pin: true,
        fmt: (r) => `<span class="strong">${esc(r.owner_label)}</span>` +
          `<span class="mu-team">with ${esc(r.opp_owner_label || "?")}</span>` },
      { key: "grade", label: "Grade", sort: (r) => r.z ?? 0,
        fmt: (r) => gradeTag(r.grade) +
          (r.one_sided ? '<span class="flag">ONE SIDED</span>' : "") },
      { key: "net", label: "Net", fmt: (r) => dbar(r.net, maxNet) },
      { key: "got_after", label: "Got", fmt: (r) => n1(r.got_after) },
      { key: "gave_after", label: "Gave", fmt: (r) => n1(r.gave_after) },
      { key: "got_before", label: "Before",
        sort: (r) => r.got_before - r.gave_before,
        fmt: (r) => `<span class="dim">${n1(r.got_before)} for ` +
                    `${n1(r.gave_before)}</span>` },
      { key: "got", label: "In the deal", left: true,
        sort: (r) => r.got.length + r.gave.length,
        fmt: (r) => topName(r.got, true) + topName(r.gave, false) },
    ], {
      rowKey: (r) => `${r.trade_id}|${r.team_id}`,
      onRow: (k) => byKey.has(k) && openTrade(byKey.get(k)),
      sortBy: 3,
    }));
  }

  if (!d.pickups.length) {
    $("pickups-body").innerHTML =
      '<p class="empty">Nobody was picked up in this scope.</p>';
  } else {
    mount("pickups-body", table(d.pickups, [
      { key: "season", label: "When", left: true,
        fmt: (r) => `${r.season} wk${r.week}`,
        sort: (r) => r.season * 100 + r.week },
      { key: "player_name", label: "Player", left: true, pin: true,
        fmt: (r) => `<span class="strong">${esc(r.player_name)}</span>` },
      { key: "position", label: "Pos", left: true,
        fmt: (r) => posTag(r.position) },
      { key: "owner_label", label: "Picked up by", left: true,
        fmt: (r) => esc(r.owner_label) },
      { key: "kind", label: "How", left: true,
        fmt: (r) => `<span class="dim">${PICKUP_KIND[r.kind] || esc(r.kind)}</span>` },
      { key: "started_after", label: "Started pts",
        fmt: (r) => `<span class="strong num-good">${n1(r.started_after)}</span>` },
      { key: "pts_after", label: "Total pts", fmt: (r) => n1(r.pts_after) },
      { key: "weeks_after", label: "Weeks held" },
      { key: "bench", label: "In lineup",
        sort: (r) => r.pts_after ? r.started_after / r.pts_after : 0,
        fmt: (r) => bar(r.pts_after ? r.started_after / r.pts_after * 100 : 0,
                        "var(--accent)") },
    ], { sortBy: 5 }));
  }

  if (!d.managers.length) {
    $("movers-body").innerHTML =
      '<p class="empty">Nothing moved in this scope.</p>';
  } else {
    const maxNet = Math.max(1, ...d.managers.map((r) => Math.abs(r.trade_net)));
    mount("movers-body", table(d.managers, [
      { key: "owner_label", label: "Manager", left: true, pin: true,
        fmt: (r) => `<span class="strong">${esc(r.owner_label || "Unknown")}</span>` },
      { key: "trades", label: "Trades" },
      { key: "wins", label: "Won",
        sort: (r) => r.trades ? r.wins / r.trades : 0,
        fmt: (r) => r.trades
          ? `${r.wins}<span class="dim">/${r.trades}</span>` : "&ndash;" },
      { key: "trade_net", label: "Trade net",
        fmt: (r) => dbar(r.trade_net, maxNet) },
      { key: "got_after", label: "Got", fmt: (r) => n1(r.got_after) },
      { key: "gave_after", label: "Gave", fmt: (r) => n1(r.gave_after) },
      { key: "pickups", label: "Pickups" },
      { key: "pickup_started", label: "Pickup pts",
        fmt: (r) => n1(r.pickup_started) },
      { key: "best_pickup", label: "Best pickup", left: true,
        sort: (r) => r.best_pickup_pts ?? 0,
        fmt: (r) => r.best_pickup
          ? `<span class="strong">${esc(r.best_pickup)}</span>` +
            `<span class="mu-team">${r.best_pickup_season} &middot; ` +
            `${n1(r.best_pickup_pts)} started</span>`
          : "&ndash;" },
    ], {
      sortBy: 3,
      rowKey: (r) => r.owner_guid,
      onRow: (guid) => guid && openManager(guid),
    }));
  }
}

/* The whole deal, both sides, from the row that was clicked. `Before` and
 * `After` sit in adjacent columns on purpose: that pair is the only thing
 * that separates a manager who read the market from one who got lucky. */
function openTrade(t) {
  const side = (players, got) => players.length
    ? players.map((p) => `
      <tr>
        <td class="left">${esc(p.player_name)}</td>
        <td class="left">${posTag(p.position)}</td>
        <td class="dim">${n1(p.pts_before)}</td>
        <td class="strong ${got ? "num-good" : "num-bad"}">${n1(p.pts_after)}</td>
        <td>${n1(p.started_after)}</td>
        <td>${p.weeks_after}</td>
      </tr>`).join("")
    : `<tr><td colspan="6" class="left dim">Nothing recoverable on this ` +
      `side &mdash; see the note under the table.</td></tr>`;

  const head = `<thead><tr><th class="left">Player</th><th class="left">Pos</th>
    <th>Before</th><th>After</th><th>Started</th><th>Weeks</th></tr></thead>`;

  openDialog(
    `${esc(t.owner_label)} &middot; ${t.season} wk${t.week} ` +
    `&middot; ${gradeTag(t.grade)} <span class="${cls(t.net)}">` +
    `${signed(t.net)}</span>`,
    `<div class="kv">
       <div><div class="k">Team</div><div class="v">${esc(t.team_name || "—")}</div></div>
       <div><div class="k">With</div><div class="v">${esc(t.opp_owner_label || "—")}</div></div>
       <div><div class="k">Got</div><div class="v">${n1(t.got_after)}</div></div>
       <div><div class="k">Gave</div><div class="v">${n1(t.gave_after)}</div></div>
       <div><div class="k">Net</div><div class="v ${cls(t.net)}">${signed(t.net)}</div></div>
       <div><div class="k">Net started</div><div class="v ${cls(t.net_started)}">${signed(t.net_started)}</div></div>
       <div><div class="k">z</div><div class="v">${n2(t.z)}</div></div>
     </div>
     <h2>Received</h2>
     <div class="tscroll"><table>${head}<tbody>${side(t.got, true)}</tbody></table></div>
     <h2>Sent away</h2>
     <div class="tscroll"><table>${head}<tbody>${side(t.gave, false)}</tbody></table></div>
     <p class="note">Points are counted from week ${t.week} onwards and stop
     the week a player left the roster he moved to, so a rental earns credit
     only for the weeks he was actually owned.</p>`);
}

/* ------------------------------------------------------------------ *
 *  8. record book
 * ------------------------------------------------------------------ */

const RECORD_SPECS = [
  ["highest_week", "Highest weekly score", (r) =>
    `${esc(r.owner_label)} &middot; ${r.season} wk${r.week}`, (r) => n1(r.actual)],
  ["lowest_week", "Lowest weekly score", (r) =>
    `${esc(r.owner_label)} &middot; ${r.season} wk${r.week}`, (r) => n1(r.actual)],
  ["biggest_blowout", "Biggest blowout", (r) =>
    `${esc(r.owner_label)} over ${esc(r.opp_owner)} &middot; ${r.season} wk${r.week}`,
    (r) => `${n1(r.actual)}&ndash;${n1(r.opp_actual)}`],
  ["closest_game", "Closest game", (r) =>
    `${esc(r.owner_label)} over ${esc(r.opp_owner)} &middot; ${r.season} wk${r.week}`,
    (r) => `by ${n2(r.margin)}`],
  ["highest_losing_score", "Highest score in a loss", (r) =>
    `${esc(r.owner_label)} lost to ${esc(r.opp_owner)} &middot; ${r.season} wk${r.week}`,
    (r) => `${n1(r.actual)}&ndash;${n1(r.opp_actual)}`],
  ["lowest_winning_score", "Lowest score in a win", (r) =>
    `${esc(r.owner_label)} beat ${esc(r.opp_owner)} &middot; ${r.season} wk${r.week}`,
    (r) => `${n1(r.actual)}&ndash;${n1(r.opp_actual)}`],
  ["best_player_week", "Best player week", (r) =>
    `${esc(r.player_name)} ${posTag(r.position)} for ${esc(r.owner_label)} ` +
    `&middot; ${r.season} wk${r.week}`, (r) => n1(r.points)],
  ["most_left_behind", "Most points benched", (r) =>
    `${esc(r.owner_label)} &middot; ${r.season} wk${r.week}` +
    (r.cost_a_win ? '<span class="flag">COST A WIN</span>' : ""),
    (r) => n1(r.left_behind)],
];

async function renderRecords() {
  const d = await api("/api/records");
  $("records-body").innerHTML = RECORD_SPECS.map(([key, title, who, what]) => {
    const rows = (d[key] || []).map((r, i) => `
      <tr><td class="left dim">${i + 1}</td>
          <td class="left">${who(r)}</td>
          <td class="strong">${what(r)}</td></tr>`).join("");
    return `<div class="card"><h2>${title}</h2>
      <div class="tscroll"><table><tbody>${rows}</tbody></table></div></div>`;
  }).join("");
}

/* ------------------------------------------------------------------ *
 *  dialog + refresh
 * ------------------------------------------------------------------ */

function openDialog(title, body) {
  $("dlg-title").innerHTML = title;
  $("dlg-body").innerHTML = body;
  $("dlg").showModal();
}
$("dlg-close").onclick = () => $("dlg").close();
$("dlg").onclick = (ev) => { if (ev.target.id === "dlg") $("dlg").close(); };

$("refresh-btn").onclick = async () => {
  const btn = $("refresh-btn");
  btn.disabled = true;
  btn.textContent = "Refreshing…";
  clearErr();
  try {
    await fetch(`/api/refresh?season=${S.season}`, { method: "POST" })
      .then(async (r) => { if (!r.ok) throw new Error((await r.json()).detail); });
    // Poll the status endpoint only while a refresh is actually in flight.
    // This is the one place the page polls, and it stops the moment the job
    // reports done - there is no background refresh anywhere else.
    for (;;) {
      await new Promise((r) => setTimeout(r, 1200));
      const st = await api("/api/refresh/status");
      btn.textContent = st.message || "Refreshing…";
      if (!st.running) {
        if (st.error) throw new Error(st.error);
        break;
      }
    }
    const d = await api("/api/seasons");
    S.seasons = d.seasons;
    S.week = null;
    render();
  } catch (e) { showErr(e); }
  finally { btn.disabled = false; updateFreshness(); }
};

boot();

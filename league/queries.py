"""Read queries behind the API.

Separate from `stats.py`, which holds pure functions over rows. Everything here
takes a connection and returns plain dicts ready to serialise.

Two conventions run through the whole module:

  * Managers are joined on `owner_guid`, never on team or display name. Team
    names change every season and three of the thirteen managers have had more
    than one. The GUID is the only stable identity.
  * Anything that aggregates across seasons has to cope with the league growing
    from 10 teams to 12 in 2024. Win totals are comparable because the schedule
    length did not change, but all-play denominators are not: beating 9 of 9 and
    beating 11 of 11 are both a whole expected win, so all-play must be reduced
    to a per-week rate *before* it is summed. Pooling the raw counts and
    dividing once at the end silently weights the 12-team seasons more - a team
    that went 9-0 one week and 0-11 another earned 1.0 expected wins, but
    (9+0)/(9+11)*2 reports 0.90. Hence `WEEK_RATE` below, used everywhere
    instead of summed counts.
"""

# Expected wins contributed by one team-week: the share of the league it
# outscored, which is a whole win when it beat everybody. Guarded against a
# zero denominator so a week with no opponents recorded contributes nothing
# rather than raising.
WEEK_RATE = (
    "CASE WHEN (tw.all_play_w + tw.all_play_l + tw.all_play_t) > 0 "
    "THEN (tw.all_play_w + 0.5 * tw.all_play_t) * 1.0 "
    "     / (tw.all_play_w + tw.all_play_l + tw.all_play_t) "
    "ELSE NULL END"
)


# --------------------------------------------------------------------------- #
#  seasons and standings
# --------------------------------------------------------------------------- #

def seasons(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT s.season, s.league_name, s.n_teams, s.reg_weeks, s.total_weeks, "
        "       s.playoff_teams, s.roster_slots, s.complete, "
        "  (SELECT COUNT(DISTINCT week) FROM team_weeks w "
        "    WHERE w.season = s.season) AS weeks_played "
        "FROM seasons s ORDER BY s.season DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def standings(conn, season: int) -> list[dict]:
    """Final standings plus the luck-free measures beside them.

    `wins` is what happened. `expected_wins` is what the same scores would have
    earned against the whole league each week instead of against one scheduled
    opponent, so `luck` is the part of the record the schedule handed over.
    Playoff weeks are excluded: seeding already decides those matchups, so
    counting them would fold the reward for a good regular season back into the
    measure of it.
    """
    rows = conn.execute(
        "SELECT t.team_id, t.owner_guid, t.owner_label, t.team_name, t.abbrev, "
        "       t.wins, t.losses, t.ties, t.points_for, t.points_against, "
        "       t.final_rank, t.playoff_seed, "
        "   SUM(tw.actual)       AS actual_total, "
        "   SUM(tw.optimal)      AS optimal_total, "
        "   SUM(tw.bench_points) AS bench_total, "
        f"  SUM({WEEK_RATE})     AS exp_wins, "
        f"  AVG({WEEK_RATE})     AS ap_rate, "
        "   COUNT(tw.week)       AS games, "
        "   MAX(tw.actual)       AS best_week, "
        "   MIN(tw.actual)       AS worst_week "
        "FROM teams t "
        "LEFT JOIN team_weeks tw ON tw.season = t.season "
        "    AND tw.team_id = t.team_id AND tw.is_playoff = 0 "
        "WHERE t.season = ? GROUP BY t.team_id", (season,)
    ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        games = d.pop("games") or 0
        exp = d.pop("exp_wins") or 0.0
        d["games"] = games
        d["expected_wins"] = round(exp, 2)
        d["luck"] = round((d["wins"] or 0) - exp, 2)
        d["all_play_pct"] = round(100 * (d.pop("ap_rate") or 0.0), 1)
        d["points_left"] = round((d["optimal_total"] or 0)
                                 - (d["actual_total"] or 0), 1)
        d["efficiency"] = round(
            100 * (d["actual_total"] or 0) / d["optimal_total"], 1
        ) if d["optimal_total"] else 0.0
        d["avg_actual"] = round((d["actual_total"] or 0) / games, 1) if games else 0.0
        for k in ("actual_total", "optimal_total", "bench_total",
                  "points_for", "points_against", "best_week", "worst_week"):
            if d.get(k) is not None:
                d[k] = round(d[k], 1)
        out.append(d)

    # Order by the real outcome when there is one, by record while a season runs.
    if all(r["final_rank"] for r in out):
        out.sort(key=lambda r: r["final_rank"])
    else:
        out.sort(key=lambda r: (-(r["wins"] or 0), -(r["points_for"] or 0)))
    return out


def weekly_results(conn, season: int, week: int | None = None) -> list[dict]:
    """Matchups with each side's actual and optimal score."""
    sql = (
        "SELECT m.week, m.matchup_id, m.is_playoff, m.playoff_tier, m.winner, "
        "       m.home_team, m.away_team, m.home_points, m.away_points, "
        "  th.owner_label AS home_owner, ta.owner_label AS away_owner, "
        "  th.team_name   AS home_name,  ta.team_name   AS away_name, "
        "  wh.actual AS home_actual, wa.actual AS away_actual, "
        "  wh.optimal AS home_optimal, wa.optimal AS away_optimal, "
        "  wh.all_play_w AS home_ap_w, wa.all_play_w AS away_ap_w "
        "FROM matchups m "
        "LEFT JOIN teams th ON th.season=m.season AND th.team_id=m.home_team "
        "LEFT JOIN teams ta ON ta.season=m.season AND ta.team_id=m.away_team "
        "LEFT JOIN team_weeks wh ON wh.season=m.season AND wh.week=m.week "
        "                       AND wh.team_id=m.home_team "
        "LEFT JOIN team_weeks wa ON wa.season=m.season AND wa.week=m.week "
        "                       AND wa.team_id=m.away_team "
        "WHERE m.season = ? AND m.is_bye = 0 "
    )
    params = [season]
    if week is not None:
        sql += "AND m.week = ? "
        params.append(week)
    sql += "ORDER BY m.week, m.matchup_id"

    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        h, a = d.get("home_actual"), d.get("away_actual")
        d["played"] = bool(h is not None and a is not None and (h or a))
        d["margin"] = round(abs(h - a), 2) if d["played"] else None
        d["total"] = round(h + a, 2) if d["played"] else None
        out.append(d)
    return out


def season_weeks(conn, season: int) -> list[dict]:
    """Per-week league summary: high, low, average, who won it."""
    rows = conn.execute(
        "SELECT tw.week, tw.is_playoff, COUNT(*) n, "
        "       ROUND(AVG(tw.actual),1) avg_score, "
        "       ROUND(MAX(tw.actual),1) high, ROUND(MIN(tw.actual),1) low "
        "FROM team_weeks tw WHERE tw.season = ? AND tw.actual > 0 "
        "GROUP BY tw.week ORDER BY tw.week", (season,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        top = conn.execute(
            "SELECT t.owner_label, tw.actual FROM team_weeks tw "
            "JOIN teams t ON t.season=tw.season AND t.team_id=tw.team_id "
            "WHERE tw.season=? AND tw.week=? ORDER BY tw.actual DESC LIMIT 1",
            (season, r["week"])
        ).fetchone()
        d["top_owner"] = top["owner_label"] if top else None
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
#  luck and lineup management
# --------------------------------------------------------------------------- #

def luck_board(conn, season: int | None = None) -> list[dict]:
    """Who the schedule treated well, and who managed their lineup well.

    Two different things, deliberately side by side:

      luck       actual wins minus all-play expected wins. Positive means the
                 schedule handed over wins the scores did not earn.
      efficiency actual points as a share of the best lineup available from the
                 same roster. This one is the manager's own doing.

    A team can be high in both, which is the fantasy football equivalent of
    being born on third base and also hitting well.
    """
    where = "WHERE tw.is_playoff = 0 AND tw.actual > 0"
    params: list = []
    if season is not None:
        where += " AND tw.season = ?"
        params.append(season)

    rows = conn.execute(
        f"SELECT t.owner_guid, "
        f"   (SELECT label FROM owners o WHERE o.owner_guid=t.owner_guid) label, "
        f"   COUNT(*) games, COUNT(DISTINCT tw.season) seasons, "
        f"   SUM(CASE WHEN tw.result='W' THEN 1 ELSE 0 END) wins, "
        f"   SUM(CASE WHEN tw.result='L' THEN 1 ELSE 0 END) losses, "
        f"   SUM(CASE WHEN tw.result='T' THEN 1 ELSE 0 END) ties, "
        f"   SUM({WEEK_RATE}) exp_wins, AVG({WEEK_RATE}) ap_rate, "
        f"   SUM(tw.actual) actual_total, SUM(tw.optimal) optimal_total, "
        f"   AVG(tw.actual) avg_actual, "
        f"   AVG(tw.optimal - tw.actual) avg_left "
        f"FROM team_weeks tw "
        f"JOIN teams t ON t.season=tw.season AND t.team_id=tw.team_id "
        f"{where} AND t.owner_guid IS NOT NULL "
        f"GROUP BY t.owner_guid", params
    ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        exp = d.pop("exp_wins") or 0.0
        d["expected_wins"] = round(exp, 2)
        d["luck"] = round(d["wins"] - exp, 2)
        d["luck_per_season"] = round((d["wins"] - exp) / d["seasons"], 2) \
            if d["seasons"] else 0.0
        d["all_play_pct"] = round(100 * (d.pop("ap_rate") or 0.0), 1)
        d["win_pct"] = round(100 * (d["wins"] + 0.5 * d["ties"])
                             / d["games"], 1) if d["games"] else 0.0
        d["efficiency"] = round(100 * d["actual_total"]
                                / d["optimal_total"], 1) \
            if d["optimal_total"] else 0.0
        d["points_left"] = round((d["optimal_total"] or 0)
                                 - (d["actual_total"] or 0), 1)
        d["avg_actual"] = round(d["avg_actual"] or 0, 1)
        d["avg_left"] = round(d["avg_left"] or 0, 2)
        d["actual_total"] = round(d["actual_total"] or 0, 1)
        d["optimal_total"] = round(d["optimal_total"] or 0, 1)
        out.append(d)
    out.sort(key=lambda r: -r["luck"])
    return out


def worst_lineup_calls(conn, season: int | None = None,
                       limit: int = 25) -> list[dict]:
    """The weeks where the most points sat on the bench, with the names.

    Reported only when the gap plausibly mattered - a 40-point miss in a
    30-point win is trivia, the same miss in a 5-point loss is the season.
    `cost_a_win` marks the ones where the optimal lineup would have flipped the
    result.
    """
    where = "WHERE tw.actual > 0"
    params: list = []
    if season is not None:
        where += " AND tw.season = ?"
        params.append(season)
    params.append(limit)

    rows = conn.execute(
        f"SELECT tw.season, tw.week, tw.team_id, tw.actual, tw.optimal, "
        f"       tw.opp_actual, tw.result, tw.is_playoff, "
        f"       t.owner_label, t.team_name, "
        f"       ROUND(tw.optimal - tw.actual, 2) AS left_behind "
        f"FROM team_weeks tw "
        f"JOIN teams t ON t.season=tw.season AND t.team_id=tw.team_id "
        f"{where} ORDER BY left_behind DESC LIMIT ?", params
    ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["cost_a_win"] = bool(
            d["result"] == "L" and d["opp_actual"] is not None
            and d["optimal"] > d["opp_actual"]
        )
        d["benched"] = _benched_over_starter(conn, d["season"], d["week"],
                                            d["team_id"])
        out.append(d)
    return out


def _benched_over_starter(conn, season: int, week: int,
                          team_id: int) -> list[dict]:
    """The specific swaps that would have improved the lineup.

    Pairs the best benched player at a position against the worst starter the
    optimal solve dropped, so the row reads the way the manager would have
    thought about it: "you started X over Y".
    """
    from .stats import optimal_lineup
    rows = conn.execute(
        "SELECT player_name, position, points, started, lineup_slot "
        "FROM lineups WHERE season=? AND week=? AND team_id=?",
        (season, week, team_id)
    ).fetchall()
    players = [dict(r) for r in rows]
    _, chosen = optimal_lineup(players)
    chosen_ids = {id(p) for p in chosen}

    # Benched players the optimal lineup would have used, and the starters it
    # would have dropped to make room.
    gained = sorted(
        (p for p in players if id(p) in chosen_ids and not p["started"]),
        key=lambda p: -(p["points"] or 0),
    )
    dropped = sorted(
        (p for p in players if id(p) not in chosen_ids and p["started"]),
        key=lambda p: (p["points"] or 0),
    )
    return [
        {"in_name": g["player_name"], "in_pos": g["position"],
         "in_pts": g["points"], "out_name": d["player_name"],
         "out_pos": d["position"], "out_pts": d["points"],
         "swing": round((g["points"] or 0) - (d["points"] or 0), 2)}
        for g, d in zip(gained, dropped)
    ]


# --------------------------------------------------------------------------- #
#  head to head
# --------------------------------------------------------------------------- #

def head_to_head(conn, regular_only: bool = False) -> dict:
    """All-time record of every manager against every other.

    Built from `team_weeks` rather than `matchups` so it uses the same scores
    everything else does. Each game appears once per side, which is what makes
    the matrix symmetric.
    """
    where = "WHERE tw.result IS NOT NULL"
    if regular_only:
        where += " AND tw.is_playoff = 0"

    rows = conn.execute(
        f"SELECT ta.owner_guid AS a, tb.owner_guid AS b, tw.result, "
        f"       tw.actual, tw.opp_actual, tw.season, tw.week, tw.is_playoff "
        f"FROM team_weeks tw "
        f"JOIN teams ta ON ta.season=tw.season AND ta.team_id=tw.team_id "
        f"JOIN teams tb ON tb.season=tw.season AND tb.team_id=tw.opp_team "
        f"{where} AND ta.owner_guid IS NOT NULL AND tb.owner_guid IS NOT NULL"
    ).fetchall()

    cells: dict = {}
    for r in rows:
        c = cells.setdefault((r["a"], r["b"]), {
            "w": 0, "l": 0, "t": 0, "pf": 0.0, "pa": 0.0, "games": 0,
            "best": None, "worst": None,
        })
        c["games"] += 1
        c["w" if r["result"] == "W" else "l" if r["result"] == "L" else "t"] += 1
        c["pf"] += r["actual"] or 0
        c["pa"] += r["opp_actual"] or 0
        margin = (r["actual"] or 0) - (r["opp_actual"] or 0)
        if c["best"] is None or margin > c["best"]:
            c["best"] = margin
        if c["worst"] is None or margin < c["worst"]:
            c["worst"] = margin

    labels = {r["owner_guid"]: r["label"] for r in conn.execute(
        "SELECT owner_guid, label FROM owners")}
    matrix = {}
    for (a, b), c in cells.items():
        matrix.setdefault(a, {})[b] = {
            "w": c["w"], "l": c["l"], "t": c["t"], "games": c["games"],
            "pf": round(c["pf"], 1), "pa": round(c["pa"], 1),
            "avg_margin": round((c["pf"] - c["pa"]) / c["games"], 1),
            "best_margin": round(c["best"], 1),
            "worst_margin": round(c["worst"], 1),
        }

    order = [r["owner_guid"] for r in conn.execute(
        "SELECT owner_guid FROM owners ORDER BY seasons DESC, label")]
    return {"owners": [{"guid": g, "label": labels.get(g, g)} for g in order],
            "matrix": matrix}


def rivalry(conn, guid_a: str, guid_b: str) -> dict:
    """Every game two managers have played, most recent first."""
    rows = conn.execute(
        "SELECT tw.season, tw.week, tw.actual, tw.opp_actual, tw.result, "
        "       tw.is_playoff, tw.optimal, "
        "       ta.team_name AS a_team, tb.team_name AS b_team "
        "FROM team_weeks tw "
        "JOIN teams ta ON ta.season=tw.season AND ta.team_id=tw.team_id "
        "JOIN teams tb ON tb.season=tw.season AND tb.team_id=tw.opp_team "
        "WHERE ta.owner_guid=? AND tb.owner_guid=? AND tw.result IS NOT NULL "
        "ORDER BY tw.season DESC, tw.week DESC", (guid_a, guid_b)
    ).fetchall()
    games = []
    for r in rows:
        d = dict(r)
        d["margin"] = round((d["actual"] or 0) - (d["opp_actual"] or 0), 2)
        games.append(d)
    labels = {r["owner_guid"]: r["label"] for r in conn.execute(
        "SELECT owner_guid, label FROM owners")}
    w = sum(1 for g in games if g["result"] == "W")
    l = sum(1 for g in games if g["result"] == "L")
    t = len(games) - w - l
    return {
        "a": {"guid": guid_a, "label": labels.get(guid_a)},
        "b": {"guid": guid_b, "label": labels.get(guid_b)},
        "record": {"w": w, "l": l, "t": t},
        "games": games,
    }


def manager_careers(conn) -> list[dict]:
    """One row per manager, all seasons folded together."""
    rows = conn.execute(
        "SELECT t.owner_guid, o.label, o.first_seen, o.last_seen, o.seasons, "
        "   SUM(t.wins) wins, SUM(t.losses) losses, SUM(t.ties) ties, "
        "   ROUND(SUM(t.points_for),1) points_for, "
        "   ROUND(SUM(t.points_against),1) points_against, "
        # NULLIF guards the in-progress season, which carries final_rank 0 and
        # would otherwise win every "best finish" comparison outright.
        "   MIN(NULLIF(t.final_rank, 0)) best_finish, "
        "   MAX(NULLIF(t.final_rank, 0)) worst_finish, "
        "   SUM(CASE WHEN t.final_rank = 1 THEN 1 ELSE 0 END) titles, "
        "   SUM(CASE WHEN t.playoff_seed > 0 AND t.playoff_seed <= "
        "       (SELECT playoff_teams FROM seasons s WHERE s.season=t.season) "
        "       THEN 1 ELSE 0 END) playoff_appearances "
        "FROM teams t JOIN owners o ON o.owner_guid = t.owner_guid "
        "WHERE t.owner_guid IS NOT NULL GROUP BY t.owner_guid"
    ).fetchall()
    luck = {r["owner_guid"]: r for r in luck_board(conn)}
    out = []
    for r in rows:
        d = dict(r)
        g = (d["wins"] or 0) + (d["losses"] or 0) + (d["ties"] or 0)
        d["games"] = g
        d["win_pct"] = round(100 * ((d["wins"] or 0) + 0.5 * (d["ties"] or 0))
                             / g, 1) if g else 0.0
        extra = luck.get(d["owner_guid"])
        if extra:
            for k in ("luck", "expected_wins", "all_play_pct", "efficiency",
                      "avg_actual", "points_left"):
                d[k] = extra[k]
        out.append(d)
    out.sort(key=lambda r: -r["win_pct"])
    return out


def season_history(conn, owner_guid: str) -> list[dict]:
    """A manager's season-by-season line."""
    rows = conn.execute(
        "SELECT t.season, t.team_name, t.wins, t.losses, t.ties, "
        "       ROUND(t.points_for,1) points_for, "
        "       ROUND(t.points_against,1) points_against, "
        "       t.final_rank, t.playoff_seed, s.n_teams, s.complete "
        "FROM teams t JOIN seasons s ON s.season = t.season "
        "WHERE t.owner_guid = ? ORDER BY t.season DESC", (owner_guid,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        season_luck = luck_board(conn, d["season"])
        mine = next((x for x in season_luck if x["owner_guid"] == owner_guid),
                    None)
        if mine:
            for k in ("luck", "expected_wins", "all_play_pct", "efficiency",
                      "avg_actual", "points_left"):
                d[k] = mine[k]
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
#  players
# --------------------------------------------------------------------------- #

def top_performances(conn, season: int | None = None, position: str | None = None,
                     started_only: bool = True, limit: int = 50) -> list[dict]:
    """Single best weeks any player has had for anybody in this league."""
    where = ["l.points IS NOT NULL"]
    params: list = []
    if season is not None:
        where.append("l.season = ?")
        params.append(season)
    if position:
        where.append("l.position = ?")
        params.append(position)
    if started_only:
        where.append("l.started = 1")
    params.append(limit)

    return [dict(r) for r in conn.execute(
        "SELECT l.season, l.week, l.player_name, l.position, l.pro_team, "
        "       l.points, l.projected, l.started, l.slot_label, "
        "       t.owner_label, t.team_name "
        "FROM lineups l "
        "JOIN teams t ON t.season=l.season AND t.team_id=l.team_id "
        f"WHERE {' AND '.join(where)} "
        # The tiebreak is not cosmetic: two 55.4-point weeks sort arbitrarily
        # without it, so the same request can come back in a different order -
        # and the static export then disagrees with the API for no real reason.
        "ORDER BY l.points DESC, l.season DESC, l.week DESC, l.player_name "
        "LIMIT ?", params
    )]


def player_seasons(conn, season: int | None = None, position: str | None = None,
                   limit: int = 60) -> list[dict]:
    """Season totals per player per roster.

    Grouped by (player, season, team) rather than by player alone: a player who
    was dropped and picked up produces one row per manager who rostered him,
    which is what makes "who got the good half of his season" answerable.
    """
    where = ["l.points IS NOT NULL"]
    params: list = []
    if season is not None:
        where.append("l.season = ?")
        params.append(season)
    if position:
        where.append("l.position = ?")
        params.append(position)
    params.append(limit)

    return [dict(r) for r in conn.execute(
        "SELECT l.season, l.player_id, l.player_name, l.position, "
        "       t.owner_label, t.team_name, "
        "       COUNT(*) weeks_rostered, "
        "       SUM(l.started) weeks_started, "
        "       ROUND(SUM(l.points),1) total_points, "
        "       ROUND(SUM(CASE WHEN l.started=1 THEN l.points ELSE 0 END),1) "
        "           started_points, "
        "       ROUND(AVG(l.points),1) avg_points, "
        "       ROUND(MAX(l.points),1) best_week "
        "FROM lineups l "
        "JOIN teams t ON t.season=l.season AND t.team_id=l.team_id "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY l.season, l.player_id, l.team_id "
        # Deterministic for the same reason as `top_performances` above.
        "ORDER BY total_points DESC, l.season DESC, l.player_name, "
        "         t.owner_label LIMIT ?", params
    )]


def player_search(conn, name: str, limit: int = 200) -> list[dict]:
    """Every week a player appeared on any roster here."""
    return [dict(r) for r in conn.execute(
        "SELECT l.season, l.week, l.player_name, l.position, l.pro_team, "
        "       l.points, l.projected, l.started, l.slot_label, "
        "       t.owner_label, t.team_name "
        "FROM lineups l "
        "JOIN teams t ON t.season=l.season AND t.team_id=l.team_id "
        "WHERE l.player_name LIKE ? "
        "ORDER BY l.season DESC, l.week DESC, l.player_name LIMIT ?",
        (f"%{name}%", limit)
    )]


def roster_week(conn, season: int, week: int, team_id: int) -> list[dict]:
    """One team's full roster for one week, starters first."""
    return [dict(r) for r in conn.execute(
        "SELECT player_name, position, pro_team, slot_label, lineup_slot, "
        "       started, points, projected "
        "FROM lineups WHERE season=? AND week=? AND team_id=? "
        "ORDER BY started DESC, lineup_slot, points DESC",
        (season, week, team_id)
    )]


def owner_player_loyalty(conn, owner_guid: str, limit: int = 40) -> list[dict]:
    """Players a manager has rostered most, across seasons.

    The closest thing the data has to a personality test.
    """
    return [dict(r) for r in conn.execute(
        "SELECT l.player_name, l.position, "
        "       COUNT(*) weeks, COUNT(DISTINCT l.season) seasons, "
        "       SUM(l.started) weeks_started, "
        "       ROUND(SUM(l.points),1) total_points, "
        "       ROUND(AVG(l.points),1) avg_points "
        "FROM lineups l "
        "JOIN teams t ON t.season=l.season AND t.team_id=l.team_id "
        "WHERE t.owner_guid = ? "
        "GROUP BY l.player_id ORDER BY weeks DESC, total_points DESC LIMIT ?",
        (owner_guid, limit)
    )]


# --------------------------------------------------------------------------- #
#  record book
# --------------------------------------------------------------------------- #

def record_book(conn) -> dict:
    """League extremes. Mostly for arguments."""
    def q(sql, params=()):
        return [dict(r) for r in conn.execute(sql, params)]

    tw_join = ("FROM team_weeks tw JOIN teams t "
               "ON t.season=tw.season AND t.team_id=tw.team_id ")
    sel = ("SELECT tw.season, tw.week, t.owner_label, t.team_name, "
           "tw.actual, tw.optimal, tw.opp_actual, tw.result ")

    return {
        "highest_week": q(sel + tw_join
                          + "WHERE tw.actual > 0 ORDER BY tw.actual DESC LIMIT 10"),
        "lowest_week": q(sel + tw_join
                         + "WHERE tw.actual > 0 ORDER BY tw.actual ASC LIMIT 10"),
        "biggest_blowout": q(
            "SELECT tw.season, tw.week, t.owner_label, t.team_name, tw.actual, "
            "       tw.opp_actual, o.owner_label AS opp_owner, "
            "       ROUND(tw.actual - tw.opp_actual, 2) margin "
            "FROM team_weeks tw "
            "JOIN teams t ON t.season=tw.season AND t.team_id=tw.team_id "
            "JOIN teams o ON o.season=tw.season AND o.team_id=tw.opp_team "
            "WHERE tw.result='W' ORDER BY margin DESC LIMIT 10"),
        "closest_game": q(
            "SELECT tw.season, tw.week, t.owner_label, t.team_name, tw.actual, "
            "       tw.opp_actual, o.owner_label AS opp_owner, "
            "       ROUND(tw.actual - tw.opp_actual, 2) margin "
            "FROM team_weeks tw "
            "JOIN teams t ON t.season=tw.season AND t.team_id=tw.team_id "
            "JOIN teams o ON o.season=tw.season AND o.team_id=tw.opp_team "
            "WHERE tw.result='W' AND tw.actual > 0 "
            "ORDER BY margin ASC LIMIT 10"),
        "best_player_week": top_performances(conn, started_only=False, limit=10),
        "most_left_behind": worst_lineup_calls(conn, limit=10),
        "highest_losing_score": q(
            "SELECT tw.season, tw.week, t.owner_label, t.team_name, tw.actual, "
            "       tw.opp_actual, o.owner_label AS opp_owner "
            "FROM team_weeks tw "
            "JOIN teams t ON t.season=tw.season AND t.team_id=tw.team_id "
            "JOIN teams o ON o.season=tw.season AND o.team_id=tw.opp_team "
            "WHERE tw.result='L' ORDER BY tw.actual DESC LIMIT 10"),
        "lowest_winning_score": q(
            "SELECT tw.season, tw.week, t.owner_label, t.team_name, tw.actual, "
            "       tw.opp_actual, o.owner_label AS opp_owner "
            "FROM team_weeks tw "
            "JOIN teams t ON t.season=tw.season AND t.team_id=tw.team_id "
            "JOIN teams o ON o.season=tw.season AND o.team_id=tw.opp_team "
            "WHERE tw.result='W' AND tw.actual > 0 "
            "ORDER BY tw.actual ASC LIMIT 10"),
    }

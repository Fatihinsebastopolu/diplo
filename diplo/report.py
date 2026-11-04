"""Readable views of game logs: a per-game transcript and a multi-game report."""

from __future__ import annotations

import json
from pathlib import Path

from .engine import POWERS


from .viewer import activity, analyse_promises, load_events, promise_totals, promises_markdown, rebuild


def _views(events):
    try:
        return {v.phase: v for v in rebuild(events, render=False)[0]}
    except Exception as e:  # an old or partial log should still produce a transcript
        print(f"[diplo] note: could not rebuild game for promise analysis ({e})")
        return {}


def transcript(events_path: Path) -> str:
    """Markdown transcript of one game: press, orders, rejections, center counts."""
    ev = load_events(events_path)
    views = _views(ev)
    out = ["# Game transcript", ""]
    current = None
    for e in ev:
        k = e["event"]
        if k == "game_start":
            out += [f"Agents: {e['agents']}", f"Config: {e.get('config')}", ""]
        phase = e.get("phase")
        if phase and phase != current and k in ("press", "orders", "agent_error"):
            current = phase
            out += ["", f"## {phase}", ""]
        if k == "press":
            prop = f"  \n  *proposed:* `{'; '.join(e['proposed_orders'])}`" if e["proposed_orders"] else ""
            bad = f"  \n  **illegal proposals:** `{'; '.join(e['illegal_proposals'])}`" if e["illegal_proposals"] else ""
            out.append(f"- **{e['sender']} -> {e['recipient']}** (round {e['round'] + 1}): {e['text']}{prop}{bad}")
        elif k == "orders":
            line = f"- **{e['power']}** orders: `{'; '.join(e['accepted']) or '(none / all hold)'}`"
            if e["first_pass_errors"]:
                errs = "; ".join(f"{o} ({why})" for o, why in e["first_pass_errors"])
                line += f"  \n  first attempt rejected: {errs}"
                line += "  \n  repaired: yes" if e["repaired"] else "  \n  repaired: no (illegal orders dropped)"
            if e.get("note"):
                line += f"  \n  note: {e['note']}"
            out.append(line)
        elif k == "agent_error":
            out.append(f"- **{e['power']}** agent error during {e['step']}: {e['error']}")
        elif k == "phase_result":
            counts = ", ".join(f"{p[:3]} {n}" for p, n in e["centers"].items())
            if e["phase"] in views:
                pm = promises_markdown(views[e["phase"]])
                if pm:
                    out.append(pm)
            out += ["", f"*After {e['phase']}: centers {counts}*"]
        elif k == "game_end":
            out += ["", "## Final", "", "```json", json.dumps({kk: e[kk] for kk in ("centers", "solo_winner", "leaders", "usage")}, indent=2), "```"]
    return "\n".join(out) + "\n"


def timing_cost_replies(events: list[dict]) -> dict:
    """Per-game time and cost by phase and year, and how often messages got a reply.

    A message counts as answered if the recipient wrote back to the sender later in
    the same phase or in the first negotiation round of the next movement phase.
    """
    phase_cost, prev = {}, 0.0
    for e in events:
        if e["event"] == "game_start":
            prev = 0.0  # a resumed game's log starts a new usage count after the copied prefix
        if e["event"] == "usage":
            phase_cost[e["phase"]] = e["cost_usd"] - prev
            prev = e["cost_usd"]
    year_cost = {}
    for ph, c in phase_cost.items():
        year_cost[ph[1:5]] = year_cost.get(ph[1:5], 0.0) + c
    rounds = [e["seconds"] for e in events if e["event"] == "timing" and e["step"] == "negotiation"]
    orders = [e["seconds"] for e in events if e["event"] == "timing" and e["step"] == "orders"]
    # wall time per phase from event timestamps (works for logs made before timing events existed)
    phase_secs, last_t = {}, next((e["t"] for e in events if e["event"] == "game_start"), None)
    for e in events:
        if e["event"] == "game_start":
            last_t = e["t"]
        if e["event"] == "phase_result" and last_t is not None:
            phase_secs[e["phase"]] = e["t"] - last_t
            last_t = e["t"]
    year_secs = {}
    for ph, sec in phase_secs.items():
        year_secs[ph[1:5]] = year_secs.get(ph[1:5], 0.0) + sec
    humans = {p for e in events if e["event"] == "game_start" for p, k in e["agents"].items() if k == "human"}
    press = [e for e in events if e["event"] == "press"]
    mphases = list(dict.fromkeys(e["phase"] for e in press))
    def answered(m):
        nxt = mphases[mphases.index(m["phase"]) + 1] if mphases.index(m["phase"]) + 1 < len(mphases) else None
        return any(r["sender"] == m["recipient"] and r["recipient"] == m["sender"] and
                   ((r["phase"] == m["phase"] and r["round"] > m["round"]) or (r["phase"] == nxt and r["round"] == 0))
                   for r in press)
    # the last round of the final phase has no chance of a reply; leave it out
    last = (mphases[-1], max((m["round"] for m in press if m["phase"] == mphases[-1]), default=0)) if press else None
    eligible = [m for m in press if (m["phase"], m["round"]) != last]
    to_humans = [m for m in eligible if m["sender"] in humans]
    return {
        "cost_by_year": {y: round(c, 4) for y, c in year_cost.items()},
        "seconds_by_year": {y: round(s, 1) for y, s in year_secs.items()},
        "negotiation_round_seconds": rounds,
        "order_step_seconds": orders,
        "replied": sum(answered(m) for m in eligible), "messages": len(eligible),
        "human_messages_replied": sum(answered(m) for m in to_humans), "human_messages": len(to_humans),
    }


def aggregate(run_dir: Path) -> str:
    """Markdown table over every summary.json under run_dir."""
    summaries = [json.loads(p.read_text()) for p in sorted(Path(run_dir).rglob("summary.json"))]
    if not summaries:
        return f"No summary.json files found under {run_dir}\n"
    n = len(summaries)
    POWERS = list(dict.fromkeys(p for s in summaries for p in s["centers"]))  # this run's map
    rows = []
    for p in POWERS:
        cs = [s["centers"].get(p, 0) for s in summaries]
        rows.append(
            (
                p,
                sum(cs) / n,
                sum(1 for s in summaries if s["solo_winner"] == p),
                sum(1 for s in summaries if p in s["leaders"]),
                sum(1 for s in summaries if p not in s["eliminated"]),
            )
        )
    keys = {k for s in summaries for st in s["stats"].values() for k in st}
    tot = {k: sum(s["stats"][p].get(k, 0) for s in summaries for p in s["stats"]) for k in keys}
    costs = [s["usage"]["cost_usd"] for s in summaries if s.get("usage")]
    calls = [s["usage"]["calls"] for s in summaries if s.get("usage")]
    refusals = sum(s["usage"]["refusals"] for s in summaries if s.get("usage"))
    secs = [s["wall_seconds"] for s in summaries]

    def pct(a, b):
        return f"{100 * a / b:.1f}%" if b else "n/a"

    md = [f"# Run report: {run_dir}", "", f"Games: {n}; final phases: {sorted({s['final_phase'] for s in summaries})}", ""]
    md += ["## Outcomes by power", "", "| Power | Mean centers | Solo wins | Led / tied lead | Survived |", "|---|---|---|---|---|"]
    md += [f"| {p} | {m:.2f} | {w} | {l} | {sv}/{n} |" for p, m, w, l, sv in rows]
    md += ["", "## Legality", ""]
    md += [
        f"- Orders submitted: {tot['orders_submitted']}; illegal on first attempt: {tot['illegal_first_pass']} ({pct(tot['illegal_first_pass'], tot['orders_submitted'])})",
        f"- Order sets repaired after feedback: {tot['repairs_succeeded']}; illegal orders still dropped after repair: {tot['illegal_final']}",
        "- Illegal orders reaching the adjudicator: 0 (all orders are validated first)",
        f"- Orders proposed in negotiation (delivered): {tot['proposals']}; illegal: {tot['illegal_proposals']} ({pct(tot['illegal_proposals'], tot['proposals'])})",
        f"- Illegal proposals caught in agents' drafts before sending: {tot.get('illegal_proposals_first_pass', 0)}; "
        f"rounds fixed by repair: {tot.get('proposal_repairs_succeeded', 0)}; messages dropped after a failed repair: {tot.get('messages_dropped', 0)}",
        f"- Messages sent: {tot['messages_sent']}; agent failures (fell back to holds/no press): {tot['agent_failures']}",
    ]
    md += ["", "## Cost and time", ""]
    if costs:
        md += [
            f"- Estimated API cost per game: mean ${sum(costs) / len(costs):.3f}, max ${max(costs):.3f}",
            f"- API calls per game: mean {sum(calls) / len(calls):.0f}; refusals: {refusals}",
        ]
    else:
        md.append("- No LLM agents in these games (cost 0).")
    md.append(f"- Wall time per game: mean {sum(secs) / n:.0f}s, max {max(secs):.0f}s")
    claims = []
    act = {p: {"moves": 0, "supports": 0, "ally_supports": 0, "convoys": 0, "holds": 0, "dislodged": 0}
           for p in POWERS}
    for ev_path in sorted(Path(run_dir).rglob("events.jsonl")):
        views = list(_views(load_events(ev_path)).values())
        claims += [c for v in views for c in analyse_promises(v)]
        for p, d in activity(views).items():
            for k, n in d.items():
                act[p][k] += n
    md += ["", "## Activity in movement phases", "",
           "What each unit did in movement phases (a unit without an order holds), and how many of "
           "the power's own units were dislodged.", "",
           "| Power | Moves | Supports | of which for another power | Convoys | Holds | Units dislodged |",
           "|---|---|---|---|---|---|---|"]
    for p in POWERS:
        a = act[p]
        md.append(f"| {p} | {a['moves']} | {a['supports']} | {a['ally_supports']} | {a['convoys']} | "
                  f"{a['holds']} | {a['dislodged']} |")
    tot_units = sum(sum(a[k] for k in ("moves", "supports", "convoys", "holds")) for a in act.values())
    active = sum(a["moves"] + a["supports"] + a["convoys"] for a in act.values())
    spreads = [max(s["centers"].values()) - min(s["centers"].values()) for s in summaries]
    md += ["", f"Unit-orders that moved, supported or convoyed: {active}/{tot_units} ({pct(active, tot_units)}); "
           f"supports given to another power's unit: {sum(a['ally_supports'] for a in act.values())}; "
           f"units dislodged: {sum(a['dislodged'] for a in act.values())}; "
           f"final center spread (max - min) per game: {spreads}."]
    pt = promise_totals(claims, POWERS)
    tcr = [timing_cost_replies(load_events(p)) for p in sorted(Path(run_dir).rglob("events.jsonl"))]
    resumes = [e for p in sorted(Path(run_dir).rglob("events.jsonl")) for e in load_events(p) if e["event"] == "resume"]
    for r in resumes:
        md += ["", "## Substitution", "",
               f"- {r['substitute']} was taken over by a substitute agent from {r['at']} "
               f"({'announced to the other players' if r['announce'] else 'not announced'}).",
               f"- Handover document: {r['handover_chars']} characters (about {r['handover_chars'] // 4} tokens), "
               f"added to each of the substitute's prompts; this is the takeover overhead.",
               f"- Private notes restored for: {', '.join(r['restored_notes_for']) or 'none (older log)'}.",
               "- Cost and time below cover the whole game, including the part played before the substitution."]
    years = sorted({y for t in tcr for y in t["cost_by_year"]})
    if years:
        md += ["", "## Cost and time by game year", "",
               "Mean over games that reached that year. Time is spent waiting for agents (negotiation rounds plus "
               "order submission).", "", "| Year | Est. cost | Time |", "|---|---|---|"]
        for y in years:
            cs = [t["cost_by_year"][y] for t in tcr if y in t["cost_by_year"]]
            ss = [t["seconds_by_year"].get(y, 0) for t in tcr if y in t["cost_by_year"]]
            md.append(f"| {y} | ${sum(cs) / len(cs):.3f} | {sum(ss) / len(ss):.0f} s |")
    rs = [x for t in tcr for x in t["negotiation_round_seconds"]]
    os_ = [x for t in tcr for x in t["order_step_seconds"]]
    if rs or os_:
        md += ["", "## Waiting time per step", ""]
        if rs:
            md.append(f"- Negotiation round (all agents writing in parallel): mean {sum(rs) / len(rs):.1f} s, "
                      f"max {max(rs):.1f} s over {len(rs)} rounds")
        if os_:
            md.append(f"- Order submission: mean {sum(os_) / len(os_):.1f} s, max {max(os_):.1f} s over {len(os_)} phases")
        md.append("- In `play` mode these are roughly how long the human waits after finishing their own input.")
    rep_n = sum(t["messages"] for t in tcr); rep_y = sum(t["replied"] for t in tcr)
    hn = sum(t["human_messages"] for t in tcr); hy = sum(t["human_messages_replied"] for t in tcr)
    if rep_n:
        md += ["", "## Replies", "",
               f"- Messages that got a reply from their recipient (later in the same phase or at the start of "
               f"the next): {rep_y}/{rep_n} ({pct(rep_y, rep_n)})"]
        if hn:
            md.append(f"- Messages from the human player that an agent replied to: {hy}/{hn} ({pct(hy, hn)})")
    md += ["", "## Messages and promises per power", "",
           "Own proposals carried out: orders a power proposed in a message for its own unit that it then "
           "actually gave (latest version sent to each recipient, exact match). Requests honored: orders "
           "other powers proposed for this power's units that it then gave.", "",
           "| Power | Messages | Proposals | Illegal proposals | Own proposals carried out | Requests honored |",
           "|---|---|---|---|---|---|"]
    for p in POWERS:
        t = pt[p]
        md.append(
            f"| {p} | {sum(s['stats'][p]['messages_sent'] for s in summaries)} | "
            f"{sum(s['stats'][p]['proposals'] for s in summaries)} | "
            f"{sum(s['stats'][p]['illegal_proposals'] for s in summaries)} | "
            f"{t['own_kept']}/{t['own']} ({pct(t['own_kept'], t['own'])}) | "
            f"{t['requests_honored']}/{t['requests']} ({pct(t['requests_honored'], t['requests'])}) |"
        )
    ok = sum(t["own_kept"] for t in pt.values()); n = sum(t["own"] for t in pt.values())
    md += ["", f"Overall, {ok}/{n} ({pct(ok, n)}) of own-unit proposals were carried out."]
    return "\n".join(md) + "\n"

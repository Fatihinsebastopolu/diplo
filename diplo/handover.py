"""Mid-game substitution: resume a logged game with an agent replacing one power.

`restore` rebuilds the game from a log up to (not including) a phase: board,
press history, relationship ledger, and each agent's last private notes. Then
`handover_text` writes the substitute's handover from the departed player's
point of view:

- the relationship ledger (already in every prompt) and message history;
- open commitments: what the departed player last told each power;
- a playing profile computed by code (promise keeping, contradictions,
  aggressiveness, closest ally, main rival), so the substitute can keep a
  similar style, as a fair substitute should.
"""

from __future__ import annotations

import json
from pathlib import Path

from .agents import Press
from .engine import new_game
from .relations import Ledger
from .viewer import PhaseView, activity, analyse_promises, contradictions


def load_events(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _replay(events: list[dict], stop: str | None = None):
    """Replay a log phase by phase. Returns (game, views, owners) up to (not including) `stop`.

    `owners[i]` maps each supply center to its owner when phase `views[i]` began (the ledger needs it).
    """
    start = next((e for e in events if e["event"] == "game_start"), {})
    game = new_game(start.get("map") or start.get("config", {}).get("map") or "standard")
    played = [e["phase"] for e in events if e["event"] == "phase_result"]
    if stop is not None and stop not in played and stop != game.get_current_phase():
        raise SystemExit(f"phase {stop} is not in this log; phases are: {', '.join(played)}")
    orders, press_by_phase = {}, {}
    for e in events:
        if e["event"] == "orders":
            orders.setdefault(e["phase"], {})[e["power"]] = e["accepted"]
        elif e["event"] == "press":
            press_by_phase.setdefault(e["phase"], []).append(e)
    views, owners = [], []
    for phase in played:
        if phase == stop:
            break
        if game.get_current_phase() != phase:
            raise ValueError(f"log/engine mismatch at {phase}")
        units = {p: list(game.get_units(p)) for p in game.powers}
        owners.append({c: p for p in game.powers for c in game.get_centers(p)})
        counts = {p: len(game.get_centers(p)) for p in game.powers}
        for p, o in orders.get(phase, {}).items():
            game.set_orders(p, o)
        game.process()
        views.append(PhaseView(phase, units, counts,
                               {p: orders.get(phase, {}).get(p, []) for p in game.powers},
                               press_by_phase.get(phase, []),
                               {str(u): [str(x) for x in rs if str(x)] for u, rs in game.result_history[phase].items()}))
    return game, views, owners


def _ledger(powers, views, owners) -> Ledger:
    ledger = Ledger(list(powers))
    for v, o in zip(views, owners):
        ledger.update(v, o)
    return ledger


def restore(events: list[dict], at: str):
    """Return (game, press, ledger, notes, views, prefix_events) as they stood when phase `at` began."""
    game, views, owners = _replay(events, stop=at)
    ledger = _ledger(game.powers, views, owners)
    press = [Press(m["phase"], m["round"], m["sender"], m["recipient"], m["text"],
                   m["proposed_orders"], m["illegal_proposals"]) for v in views for m in v.press]
    notes = {}
    for e in events:  # the last notes logged before `at`
        if e["event"] == "agent_notes" and e["phase"] in {v.phase for v in views}:
            notes.update(e["notes"])
    # log lines to copy into the resumed game's log, so its viewer shows the whole game
    cut = next((i for i, e in enumerate(events) if e.get("phase") == at), len(events))
    prefix = [e for e in events[:cut] if e["event"] not in ("game_end", "budget_stop")]
    return game, press, ledger, notes, views, prefix


def profile(power: str, views: list[PhaseView], ledger: Ledger) -> dict:
    claims = [c for v in views for c in analyse_promises(v)]
    own = [c for c in claims if c.kind == "own" and c.speaker == power]
    contra = sum(1 for v in views for s, _, _ in contradictions([c for c in analyse_promises(v)]) if s == power)
    a = activity(views).get(power, {})
    units = sum(a.get(k, 0) for k in ("moves", "supports", "convoys", "holds"))
    active = units - a.get("holds", 0)
    friend, rival = {}, {}
    for (me, them), r in ledger.rel.items():
        if me == power:
            friend[them] = friend.get(them, 0) + r.their_kept + len(r.joint)
            rival[them] = rival.get(them, 0) + len(r.attacks_by_them)
        elif them == power:
            rival[me] = rival.get(me, 0) + len(r.attacks_by_them)
    best = max(friend, key=friend.get) if friend and max(friend.values()) > 0 else None
    worst = max(rival, key=rival.get) if rival and max(rival.values()) > 0 else None
    m_phases = sum(1 for v in views if v.phase.endswith("M"))
    return {
        "promises_kept": sum(c.kept for c in own), "promises": len(own), "contradictions": contra,
        "active_share": (active / units) if units else None,
        "closest_ally": best, "main_rival": worst,
        "movement_phases": m_phases,
        "messages_sent": sum(1 for v in views for m in v.press if m["sender"] == power),
        "ally_supports": a.get("ally_supports", 0),
    }


def _attacks_on(power: str, target: str | None, views: list[PhaseView], owners: list[dict]) -> int:
    """How many of `power`'s orders moved into (or supported a move into) `target`'s units or centers."""
    if not target:
        return 0
    led = _ledger([power, target], views, owners)
    return len(led.rel[(target, power)].attacks_by_them)


def compare_substitute(events: list[dict], power: str, at: str) -> dict:
    """Profile the departed player (phases before `at`) and the substitute (from `at`) for a fairness check.

    Both profiles come from the same code, so a human who dropped out and the agent that replaced them
    are measured the same way. Rates are per movement phase, since the two spans differ in length.
    """
    game, views, owners = _replay(events)
    i = next((k for k, v in enumerate(views) if v.phase == at), len(views))
    final = len(game.get_centers(power))
    at_takeover = views[i].centers.get(power, 0) if i < len(views) else final
    out = {}
    for label, vs, os_, end in (("before", views[:i], owners[:i], at_takeover),
                                ("after", views[i:], owners[i:], final)):
        pr = profile(power, vs, _ledger(game.powers, vs, os_))
        pr["centers_start"] = vs[0].centers.get(power, 0) if vs else end
        pr["centers_end"] = end
        out[label] = pr
    prev_ally = out["before"]["closest_ally"]
    out["previous_ally"] = prev_ally
    out["attacks_on_previous_ally"] = {
        "before": _attacks_on(power, prev_ally, views[:i], owners[:i]),
        "after": _attacks_on(power, prev_ally, views[i:], owners[i:]),
    }
    return out


def comparison_markdown(power: str, cmp: dict) -> list[str]:
    b, a = cmp["before"], cmp["after"]

    def rate(n, pr):
        return f"{n / pr['movement_phases']:.1f}" if pr["movement_phases"] else "-"

    def pct(k, n):
        return f"{k}/{n} ({100 * k / n:.0f}%)" if n else "-"

    def share(x):
        return f"{100 * x:.0f}%" if x is not None else "-"

    ally = cmp["previous_ally"] or "none"
    rows = [
        ("Movement phases", b["movement_phases"], a["movement_phases"]),
        ("Own proposals kept", pct(b["promises_kept"], b["promises"]), pct(a["promises_kept"], a["promises"])),
        ("Different plans told to different powers, per movement phase",
         rate(b["contradictions"], b), rate(a["contradictions"], a)),
        ("Units that moved, supported or convoyed", share(b["active_share"]), share(a["active_share"])),
        ("Messages sent per movement phase", rate(b["messages_sent"], b), rate(a["messages_sent"], a)),
        ("Supports given to other powers", b["ally_supports"], a["ally_supports"]),
        ("Closest partner", b["closest_ally"] or "-", a["closest_ally"] or "-"),
        ("Main rival", b["main_rival"] or "-", a["main_rival"] or "-"),
        (f"Attacks on the previous partner ({ally}) per movement phase",
         rate(cmp["attacks_on_previous_ally"]["before"], b), rate(cmp["attacks_on_previous_ally"]["after"], a)),
        ("Supply centers (start -> end of span)", f"{b['centers_start']} -> {b['centers_end']}",
         f"{a['centers_start']} -> {a['centers_end']}"),
    ]
    md = ["", f"Fairness check: {power} before the takeover (previous player) and after (substitute), "
              "measured by the same code.", "", "| Measure | Previous player | Substitute |", "|---|---|---|"]
    md += [f"| {m} | {x} | {y} |" for m, x, y in rows]
    return md


def handover_text(power: str, at: str, views: list[PhaseView], ledger: Ledger, announced: bool) -> str:
    pr = profile(power, views, ledger)
    lines = [f"You replace the previous player of {power} from {at}. Your own private notes are empty: "
             f"everything you know about the previous play is below, in the relationships section, and in "
             f"the message history."]
    lines.append("Other players have been told that a substitute has taken over." if announced else
                 "Other players have not been told about the substitution.")
    style = []
    if pr["promises"]:
        style.append(f"kept {pr['promises_kept']} of {pr['promises']} of its own proposals")
    if pr["contradictions"]:
        style.append(f"told different powers different plans {pr['contradictions']} time(s)")
    if pr["active_share"] is not None:
        style.append(f"moved or supported with {100 * pr['active_share']:.0f}% of its units "
                     f"({'aggressive' if pr['active_share'] > 0.4 else 'cautious' if pr['active_share'] < 0.25 else 'moderate'})")
    if pr["closest_ally"]:
        style.append(f"its closest partner was {pr['closest_ally']}")
    if pr["main_rival"]:
        style.append(f"its main rival was {pr['main_rival']}")
    if style:
        lines.append("Playing profile of the previous player (computed by the game): " + "; ".join(style) + ".")
    last_m = next((v for v in reversed(views) if v.phase.endswith("M") and v.press), None)
    if last_m:
        told = {}
        for m in last_m.press:
            if m["sender"] == power:
                told[m["recipient"]] = m  # latest message to each power
        if told:
            lines.append(f"Open commitments: the last thing the previous player told each power (in {last_m.phase}):")
            for to, m in sorted(told.items()):
                prop = f" [proposed: {'; '.join(m['proposed_orders'])}]" if m["proposed_orders"] else ""
                lines.append(f"- to {to}: \"{m['text']}\"{prop}")
    lines.append("Play as a fair substitute: keep a style similar to the previous player's, honour its "
                 "commitments and alliances unless they clearly no longer serve this power, and do not turn "
                 "on a partner abruptly just because the player changed.")
    return "\n".join(lines)

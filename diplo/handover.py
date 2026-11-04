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


def restore(events: list[dict], at: str):
    """Return (game, press, ledger, notes, views, prefix_events) as they stood when phase `at` began."""
    start = next((e for e in events if e["event"] == "game_start"), {})
    game = new_game(start.get("map") or start.get("config", {}).get("map") or "standard")
    played = [e["phase"] for e in events if e["event"] == "phase_result"]
    if at not in played and at != game.get_current_phase():
        raise SystemExit(f"phase {at} is not in this log; phases are: {', '.join(played)}")
    orders, press_by_phase = {}, {}
    for e in events:
        if e["event"] == "orders":
            orders.setdefault(e["phase"], {})[e["power"]] = e["accepted"]
        elif e["event"] == "press":
            press_by_phase.setdefault(e["phase"], []).append(e)
    press, views, notes = [], [], {}
    ledger = Ledger(list(game.powers))
    for phase in played:
        if phase == at:
            break
        if game.get_current_phase() != phase:
            raise ValueError(f"log/engine mismatch at {phase}")
        units = {p: list(game.get_units(p)) for p in game.powers}
        owners = {c: p for p in game.powers for c in game.get_centers(p)}
        for p, o in orders.get(phase, {}).items():
            game.set_orders(p, o)
        game.process()
        v = PhaseView(phase, units, {}, {p: orders.get(phase, {}).get(p, []) for p in game.powers},
                      press_by_phase.get(phase, []),
                      {str(u): [str(x) for x in rs if str(x)] for u, rs in game.result_history[phase].items()})
        ledger.update(v, owners)
        views.append(v)
        for e in press_by_phase.get(phase, []):
            press.append(Press(e["phase"], e["round"], e["sender"], e["recipient"], e["text"],
                               e["proposed_orders"], e["illegal_proposals"]))
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
    return {
        "promises_kept": sum(c.kept for c in own), "promises": len(own), "contradictions": contra,
        "active_share": (active / units) if units else None,
        "closest_ally": best, "main_rival": worst,
    }


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

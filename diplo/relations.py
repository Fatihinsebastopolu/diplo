"""Relationship ledger: what each power said to another versus what it then did.

Computed by code after every movement phase from the messages and the
adjudicated orders (never by the model), and shown to each agent as an accurate
record of kept and broken promises, requests honored, attacks, and joint attacks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .engine import POWERS
from .viewer import PhaseView, _owner, _unit_of, analyse_promises

MAX_EXAMPLES = 2


@dataclass
class Relation:
    """One power's view of another (`me` looking at `them`)."""

    their_kept: int = 0
    their_broken: list[str] = field(default_factory=list)  # "F1902M: said X, did Y"
    my_kept: int = 0
    my_broken: int = 0
    asked: int = 0
    honored: int = 0
    attacks_by_them: list[str] = field(default_factory=list)  # "F1902M: F NTH - DEN"
    joint: list[str] = field(default_factory=list)  # "F1902M: you A BOH S A GAL - VIE, succeeded"


def _dest(order: str) -> str | None:
    """Province a move (or a supported move) targets, without coast."""
    parts = order.split()
    if len(parts) >= 4 and parts[2] == "-":
        return parts[3].split("/")[0]
    if len(parts) >= 7 and parts[2] == "S" and parts[5] == "-":
        return parts[6].split("/")[0]
    return None


class Ledger:
    def __init__(self, powers: list[str] | None = None):
        self.powers = list(powers or POWERS)
        self.rel = {(a, b): Relation() for a in self.powers for b in self.powers if a != b}

    def update(self, view: PhaseView, center_owner: dict[str, str]) -> None:
        """Add one adjudicated movement phase. `center_owner`: supply center -> owner at phase start."""
        if not view.phase.endswith("M"):
            return
        for c in analyse_promises(view):
            if c.kind == "own":
                for r in c.told:
                    if (r, c.speaker) not in self.rel:
                        continue
                    theirs, mine = self.rel[(r, c.speaker)], self.rel[(c.speaker, r)]
                    if c.kept:
                        theirs.their_kept += 1
                        mine.my_kept += 1
                    else:
                        theirs.their_broken.append(f"{view.phase}: said {c.proposed}, did {c.actual}")
                        mine.my_broken += 1
            elif (c.speaker, c.actor) in self.rel:
                rel = self.rel[(c.speaker, c.actor)]
                rel.asked += 1
                rel.honored += int(c.kept)

        occupant = {}
        for p, units in view.units.items():
            for u in units:
                occupant[u.split()[1].split("/")[0]] = p
        for p, orders in view.orders.items():
            for o in orders:
                parts = o.split()
                dest = _dest(o)
                if dest is not None:
                    victim = occupant.get(dest) or center_owner.get(dest)
                    if victim and victim != p and (victim, p) in self.rel:
                        self.rel[(victim, p)].attacks_by_them.append(f"{view.phase}: {o}")
                # joint attack: p supports another power's move that was actually ordered
                if len(parts) >= 7 and parts[2] == "S" and parts[5] == "-":
                    mover = _owner(f"{parts[3]} {parts[4]}", view.units)
                    move = " ".join(parts[3:])
                    if mover and mover != p and move in view.orders.get(mover, []) and (p, mover) in self.rel:
                        ok = "succeeded" if not view.results.get(_unit_of(move) or "", []) else "failed"
                        self.rel[(p, mover)].joint.append(f"{view.phase}: you {o}, {ok}")
                        self.rel[(mover, p)].joint.append(f"{view.phase}: they {o} for your {move}, {ok}")

    def text_for(self, me: str) -> str:
        lines = []
        for them in self.powers:
            if them == me:
                continue
            r = self.rel[(me, them)]
            parts = []
            n = r.their_kept + len(r.their_broken)
            if n:
                s = f"kept {r.their_kept} of {n} promises to you"
                if r.their_broken:
                    s += " (broke " + "; ".join(r.their_broken[-MAX_EXAMPLES:]) + ")"
                parts.append(s)
            if r.asked:
                parts.append(f"did {r.honored} of {r.asked} things you asked")
            if r.attacks_by_them:
                parts.append(f"attacked you {len(r.attacks_by_them)} time(s) (last {r.attacks_by_them[-1]})")
            if r.joint:
                parts.append(f"fought alongside you {len(r.joint)} time(s) (last {r.joint[-1]})")
            m = r.my_kept + r.my_broken
            if m:
                parts.append(f"you kept {r.my_kept} of {m} promises to them")
            if parts:
                lines.append(f"{them}: " + "; ".join(parts) + ".")
        return "\n".join(lines) or "No promises, requests or clashes recorded yet."

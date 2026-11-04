"""Thin layer over the `diplomacy` adjudicator.

The engine is the source of truth for the rules. This module turns its state
into plain data (so agents never touch the live Game object) and validates
orders *before* they reach the engine, because the default engine rules include
IGNORE_ERRORS and would otherwise drop illegal orders silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from diplomacy import Game

POWERS = ["AUSTRIA", "ENGLAND", "FRANCE", "GERMANY", "ITALY", "RUSSIA", "TURKEY"]

PHASE_NAMES = {"M": "Movement", "R": "Retreats", "A": "Adjustments (builds/disbands)"}


def new_game(map_name: str = "standard") -> Game:
    return Game(map_name=map_name)


def win_centers(game: Game) -> int:
    """Centers needed for a solo win: a majority of the map's supply centers (18 of 34 on the standard map)."""
    return len(game.map.scs) // 2 + 1


def map_label(game: Game) -> str:
    """Short map name for prompts and titles: 'standard', 'ancmed', or a custom file's stem."""
    import os

    name = os.path.basename(game.map.name)
    return name[:-4] if name.endswith(".map") else name


def map_powers(map_name: str = "standard") -> list[str]:
    return list(new_game(map_name).powers)


def normalize_order(order: str) -> str:
    """Upper-case and collapse whitespace; map unicode dashes to '-'."""
    order = order.replace("–", "-").replace("—", "-").replace("->", "-")
    return " ".join(order.upper().split())


def phase_year(phase: str) -> int | None:
    """'S1901M' -> 1901; 'COMPLETED' -> None."""
    try:
        return int(phase[1:5])
    except (ValueError, IndexError):
        return None


@dataclass
class PhaseSnapshot:
    """Read-only facts about one power in the current phase."""

    phase: str
    phase_type: str  # 'M', 'R', 'A'
    power: str
    state_text: str
    recent_results_text: str
    legal_by_loc: dict[str, list[str]]
    alive_powers: list[str]
    builds_allowed: int = 0
    disbands_required: int = 0
    centers: list[str] = field(default_factory=list)
    units: list[str] = field(default_factory=list)
    # Every unit's legal orders this phase (all powers), for checking proposals.
    all_legal_by_loc: dict[str, list[str]] = field(default_factory=dict, repr=False)
    valid_locs: frozenset = field(default_factory=frozenset, repr=False)
    # Kept/broken promises, requests, attacks and joint attacks, as computed by the game.
    relations_text: str = ""
    map_name: str = "standard"
    game_notes: list[str] = field(default_factory=list)  # public announcements, e.g. a substitution
    win_centers: int = 18
    n_powers: int = 7
    # Movement phases: years since this power last gained a center, and centers it can attack now.
    growth_text: str = ""


def alive_powers(game: Game) -> list[str]:
    return [p for p in game.powers if not game.powers[p].is_eliminated()]


def sc_counts(game: Game) -> dict[str, int]:
    return {p: len(game.get_centers(p)) for p in game.powers}


def legal_orders_by_loc(game: Game, power: str, all_orders: dict | None = None) -> dict[str, list[str]]:
    all_orders = all_orders if all_orders is not None else game.get_all_possible_orders()
    return {
        loc: sorted(all_orders.get(loc, []))
        for loc in game.get_orderable_locations(power)
        if all_orders.get(loc)
    }


def all_legal_orders(game: Game, all_orders: dict | None = None) -> set[str]:
    """Every order any unit could legally be given this phase (for proposals)."""
    all_orders = all_orders if all_orders is not None else game.get_all_possible_orders()
    return {o for orders in all_orders.values() for o in orders}


def state_text(game: Game) -> str:
    lines = [f"Phase: {game.get_current_phase()}"]
    owned = set()
    for p in game.powers:
        centers = sorted(game.get_centers(p))
        owned.update(centers)
        units = game.get_units(p)
        status = " (eliminated)" if game.powers[p].is_eliminated() else ""
        lines.append(
            f"{p}{status}: {len(centers)} centers [{', '.join(centers)}]; "
            f"units [{', '.join(units) or 'none'}]"
        )
    neutral = sorted(set(game.map.scs) - owned)
    lines.append(f"Unowned supply centers: {', '.join(neutral) or 'none'}")
    return "\n".join(lines)


def recent_results_text(game: Game, max_phases: int = 2) -> str:
    """Orders and outcomes of the last few processed phases."""
    phases = list(game.order_history.keys())[-max_phases:]
    if not phases:
        return "No phases have been played yet."
    out = []
    for ph in phases:
        out.append(f"Results of {ph}:")
        results = game.result_history.get(ph, {})
        result_by_unit = {str(u): [str(r) for r in rs] for u, rs in results.items()}
        for p, orders in game.order_history[ph].items():
            if not orders:
                continue
            rendered = []
            for o in orders:
                unit = " ".join(o.split()[:2])
                res = result_by_unit.get(unit)
                rendered.append(f"{o}" + (f" [{', '.join(res)}]" if res else ""))
            out.append(f"  {p}: " + "; ".join(rendered))
    return "\n".join(out)


def snapshot(game: Game, power: str, all_orders: dict | None = None) -> PhaseSnapshot:
    all_orders = all_orders if all_orders is not None else game.get_all_possible_orders()
    centers = list(game.get_centers(power))
    units = list(game.get_units(power))
    delta = len(centers) - len(units)
    return PhaseSnapshot(
        phase=game.get_current_phase(),
        phase_type=game.phase_type,
        power=power,
        state_text=state_text(game),
        recent_results_text=recent_results_text(game),
        legal_by_loc=legal_orders_by_loc(game, power, all_orders),
        alive_powers=alive_powers(game),
        builds_allowed=max(0, delta) if game.phase_type == "A" else 0,
        disbands_required=max(0, -delta) if game.phase_type == "A" else 0,
        centers=centers,
        units=units,
        all_legal_by_loc=all_orders,
        valid_locs=frozenset(l.upper() for l in game.map.locs),
        map_name=map_label(game),
        win_centers=win_centers(game),
        n_powers=len(game.powers),
        growth_text=growth_text(game, power, all_orders) if game.phase_type == "M" else "",
    )


MAX_TARGETS = 6


def _last_gain(game: Game, power: str) -> tuple[int | None, int]:
    """(year of the last winter in which `power` ended with more centers than before, centers now)."""
    prev, last = None, None
    for ph, st in game.state_history.items():
        ph = str(ph)
        if not ph.endswith("A") and not ph.endswith("M"):
            continue
        n = len(st["centers"].get(power, []))
        if prev is not None and n > prev:
            last = phase_year(ph)
        prev = n
    now = len(game.get_centers(power))
    if prev is not None and now > prev:
        last = phase_year(game.get_current_phase()) - (1 if game.get_current_phase().startswith("S") else 0)
    return last, now


def growth_text(game: Game, power: str, all_orders: dict | None = None) -> str:
    """Computed by code: how long since this power grew, and which centers it can attack this phase.

    A target is a supply center the power doesn't own that one of its units can legally move into. For
    each one it lists the defender, its own units that can support the move, and other powers' units
    that could (a partner to ask).
    """
    all_orders = all_orders if all_orders is not None else game.get_all_possible_orders()
    year = phase_year(game.get_current_phase()) or 0
    last, now = _last_gain(game, power)
    start_year = phase_year(str(next(iter(game.state_history.keys()), game.get_current_phase()))) or year
    if last is None:
        lead = (f"You have {now} centers and have not gained one since the game began."
                if year - start_year >= 2 else f"You have {now} centers.")
    else:
        gap = year - last - 1
        lead = (f"You have {now} centers; your last gain was in {last}" +
                (f", {gap} full game year(s) ago." if gap >= 1 else "."))
    owner = {c: p for p in game.powers for c in game.get_centers(p)}
    unit_owner = {u.split()[1].split("/")[0]: (p, u) for p in game.powers for u in game.get_units(p)}
    scs = set(game.map.scs)
    mine = list(game.get_units(power))
    targets = {}
    for u in mine:
        loc = u.split()[1]
        for o in all_orders.get(loc, []):
            parts = o.split()
            if len(parts) == 4 and parts[2] == "-":
                dest = parts[3].split("/")[0]
                if dest in scs and owner.get(dest) != power:
                    targets.setdefault(dest, []).append(o)
    if not targets:
        return lead + " None of your units can reach a center you don't own this phase."
    def supporters(move: str, exclude: set) -> tuple[list[str], list[str]]:
        own, other = [], []
        for loc, os_ in all_orders.items():
            who = unit_owner.get(loc.split("/")[0])
            if not who or who[0] in exclude or not any(o.endswith(" S " + move) for o in os_):
                continue
            (own if who[0] == power else other).append(who[1] if who[0] == power else f"{who[0]}'s {who[1]}")
        return sorted(set(own)), sorted(set(other))

    rows = []
    for dest, moves in targets.items():
        occ = unit_owner.get(dest)
        if occ and occ[0] == power:
            continue  # own unit sits there (center not yet captured): no attack needed
        defenders = {x for x in (occ[0] if occ else None, owner.get(dest)) if x}
        best = None
        for mv in moves:  # the move with the most support
            own, other = supporters(mv, defenders)
            own = [u for u in own if u != " ".join(mv.split()[:2])]
            if best is None or (len(own), len(other)) > (len(best[1]), len(best[2])):
                best = (mv, own, other)
        mv, own, other = best
        hold = 0
        if occ:
            hold = sum(1 for loc, os_ in all_orders.items()
                       if (unit_owner.get(loc.split("/")[0]) or ("",))[0] == occ[0]
                       and any(o.endswith(" S " + occ[1]) for o in os_))
        held = (f"held by {occ[0]}'s {occ[1]}, which its owner can support with up to {hold} unit(s)"
                if occ else "empty")
        whose = f"{owner[dest]}'s" if dest in owner else "unowned"
        r = f"- {dest} ({whose}, {held}): {mv}"
        r += f"; your {', '.join(own)} can support it" if own else "; none of your other units can support it"
        if other:
            r += "; a partner could support it: " + ", ".join(other)
        rows.append(((not occ), len(own) > hold, len(own) + 0.5 * len(other) - hold, r))
    rows = [x[-1] for x in sorted(rows, key=lambda x: x[:3], reverse=True)][:MAX_TARGETS]
    if not rows:
        return lead
    return (lead + "\nCenters you could attack this phase (legal moves; to dislodge a defender, your supports "
            "must outnumber its supports):\n" + "\n".join(rows))


@dataclass
class ValidationResult:
    accepted: list[str]
    errors: list[tuple[str, str]]  # (order, reason)


def validate_orders(orders: list[str], snap: PhaseSnapshot) -> ValidationResult:
    """Check orders against the engine's legal-order list for this power.

    Rules enforced: the order must be in the legal list for one of this power's
    orderable locations; at most one order per unit; in adjustments, no more
    builds (including WAIVE) than allowed and no more disbands than required.
    """
    order_to_loc: dict[str, str] = {}
    for loc, legal in snap.legal_by_loc.items():
        for o in legal:
            order_to_loc.setdefault(o, loc)

    accepted: list[str] = []
    errors: list[tuple[str, str]] = []
    used_locs: set[str] = set()
    builds = disbands = 0

    for raw in orders:
        order = normalize_order(raw)
        if not order:
            continue
        if order not in order_to_loc:
            errors.append((order, _explain_illegal(order, snap)))
            continue
        loc = order_to_loc[order]
        if snap.phase_type == "A":
            is_build = order == "WAIVE" or order.endswith(" B")
            is_disband = order.endswith(" D")
            if is_build:
                if builds >= snap.builds_allowed:
                    errors.append((order, f"only {snap.builds_allowed} build(s) allowed this phase"))
                    continue
                if order != "WAIVE" and loc in used_locs:
                    errors.append((order, f"already building in {loc}"))
                    continue
                builds += 1
            elif is_disband:
                if disbands >= snap.disbands_required:
                    errors.append((order, f"only {snap.disbands_required} disband(s) required"))
                    continue
                if loc in used_locs:
                    errors.append((order, f"unit in {loc} already disbanded"))
                    continue
                disbands += 1
            if order != "WAIVE":
                used_locs.add(loc)
            accepted.append(order)
            continue
        if loc in used_locs:
            errors.append((order, f"unit in {loc} already has an order"))
            continue
        used_locs.add(loc)
        accepted.append(order)
    return ValidationResult(accepted, errors)


def _explain_illegal(order: str, snap: PhaseSnapshot) -> str:
    parts = order.split()
    if len(parts) >= 2:
        loc = parts[1]
        own_locs = {l.split("/")[0] for l in snap.legal_by_loc}
        if loc.split("/")[0] not in own_locs:
            return f"{snap.power} has no unit to order at {loc} this phase"
        return f"not a legal order for the unit at {loc} (check adjacency, unit type and coasts)"
    return "not a recognised order"


def check_proposals(proposed: list[str], legal_any: set[str]) -> tuple[list[str], list[str]]:
    """Split orders proposed in negotiation into (legal, illegal) for any power."""
    good, bad = [], []
    for raw in proposed:
        o = normalize_order(raw)
        if not o:
            continue
        (good if o in legal_any else bad).append(o)
    return good, bad


_LOC = r"[A-Z]{3}(?:/[NSE]C)?\b"
_UNIT = rf"[AF] {_LOC}"
_ORDER_RE = re.compile(
    rf"\b{_UNIT} (?:H\b|- {_LOC}(?: VIA\b)?|S {_UNIT}(?: - {_LOC})?|C A {_LOC} - {_LOC}|R {_LOC}|B\b|D\b)"
)


_NEGATION = re.compile(r"N'T\b|\b(NOT|NEVER|ILLEGAL|IMPOSSIBLE|CANNOT)\b")


def extract_orders(text: str, valid_locs: set[str]) -> list[str]:
    """Find order-like phrases (e.g. "a par - bur") in a free-text message.

    Only phrases whose province codes are real map locations count, so ordinary
    words are not mistaken for orders. Orders quoted in a sentence that negates
    them ("A BUR - WAR isn't legal, so I'm not doing it") are not proposals and
    are skipped.
    """
    found = []
    normalized = normalize_order(text.replace("\u2019", "'"))
    sentences = re.split(r"(?<=[.!?;])\s+", normalized)
    kept_text = " ".join(s for s in sentences if not _NEGATION.search(s))
    for m in _ORDER_RE.finditer(kept_text):
        order = m.group(0)
        locs = [t for t in order.split() if len(t) >= 3 and t != "VIA"]
        if all(t in valid_locs for t in locs) and order not in found:
            found.append(order)
    return found


def message_orders(text: str, proposed: list[str], valid_locs) -> list[str]:
    """All orders a message proposes: its structured list plus order-like text in it."""
    found = [normalize_order(o) for o in proposed if normalize_order(o)]
    for o in extract_orders(text, set(valid_locs)):
        if o not in found:
            found.append(o)
    return found


def explain_proposal(order: str, all_legal_by_loc: dict[str, list[str]]) -> str:
    """Why a proposed order is illegal, with the unit's real options when it exists."""
    parts = order.split()
    if len(parts) >= 2 and parts[0] in ("A", "F"):
        base = parts[1].split("/")[0]
        options = [o for loc, os_ in all_legal_by_loc.items() if loc.split("/")[0] == base
                   for o in os_ if o.startswith(parts[0] + " ")]
        if options:
            return "not legal this phase. That unit's legal orders are: " + " | ".join(sorted(options))
        return f"there is no {'army' if parts[0] == 'A' else 'fleet'} at {parts[1]} this phase"
    return "not a recognised order"


def _strip_coasts(order: str) -> str:
    return re.sub(r"/[NSE]C\b", "", order)


def illegal_message_orders(text: str, proposed: list[str], legal: set[str], valid_locs) -> list[str]:
    """Illegal orders in a message.

    Structured proposals must match a legal order exactly. Orders mentioned in the
    text are judged leniently: a missing coast (e.g. "F RUM - BUL" for
    "F RUM - BUL/EC") is accepted, as a human reader would.
    """
    structured = [normalize_order(o) for o in proposed if normalize_order(o)]
    lenient = {_strip_coasts(o) for o in legal}
    bad = [o for o in structured if o not in legal]
    for o in extract_orders(text, set(valid_locs)):
        if o not in structured and o not in legal and _strip_coasts(o) not in lenient and o not in bad:
            bad.append(o)
    return bad

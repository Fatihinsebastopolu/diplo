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
    )


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


def extract_orders(text: str, valid_locs: set[str]) -> list[str]:
    """Find order-like phrases (e.g. "a par - bur") in a human's free-text message.

    Only phrases whose province codes are real map locations count, so ordinary
    words are not mistaken for orders.
    """
    found = []
    for m in _ORDER_RE.finditer(normalize_order(text)):
        order = m.group(0)
        locs = [t for t in order.split() if len(t) >= 3 and t != "VIA"]
        if all(t in valid_locs for t in locs) and order not in found:
            found.append(order)
    return found

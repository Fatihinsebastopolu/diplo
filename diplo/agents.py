"""Players: LLM agent, random baseline, and human (terminal) player.

Every agent sees the same `PhaseSnapshot` plus the press (messages) visible to
it, and returns plain data. The runner validates everything an agent returns.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .engine import PHASE_NAMES, PhaseSnapshot, normalize_order, validate_orders
from .llm import LLMClient, LLMError

MAX_MESSAGES_PER_ROUND = 3
MAX_PRESS_IN_PROMPT = 40


@dataclass
class Press:
    phase: str
    round: int
    sender: str
    recipient: str
    text: str
    proposed_orders: list[str] = field(default_factory=list)
    illegal_proposals: list[str] = field(default_factory=list)


@dataclass
class OrderOutcome:
    """What an agent submitted and what survived validation."""

    submitted: list[str]
    accepted: list[str]
    first_pass_errors: list[tuple[str, str]]
    final_errors: list[tuple[str, str]]
    repaired: bool = False
    note: str = ""


class Agent:
    kind = "base"

    def __init__(self, power: str):
        self.power = power

    def negotiate(self, snap: PhaseSnapshot, press: list[Press], round_idx: int, rounds: int) -> list[dict]:
        return []

    def orders(self, snap: PhaseSnapshot, press: list[Press]) -> OrderOutcome:
        raise NotImplementedError


# --------------------------------------------------------------------------- random


class RandomAgent(Agent):
    """Uniformly random legal orders, no press. Cost-free baseline and test double."""

    kind = "random"

    def __init__(self, power: str, rng: random.Random | None = None):
        super().__init__(power)
        self.rng = rng or random.Random()

    def orders(self, snap: PhaseSnapshot, press: list[Press]) -> OrderOutcome:
        chosen: list[str] = []
        if snap.phase_type == "A":
            locs = list(snap.legal_by_loc)
            self.rng.shuffle(locs)
            for loc in locs:
                opts = [o for o in snap.legal_by_loc[loc] if o != "WAIVE"]
                if opts:
                    chosen.append(self.rng.choice(opts))
        else:
            chosen = [self.rng.choice(opts) for opts in snap.legal_by_loc.values()]
        v = validate_orders(chosen, snap)  # trims excess builds/disbands
        return OrderOutcome(v.accepted, v.accepted, [], [])


# --------------------------------------------------------------------------- LLM

SYSTEM_PROMPT = """You are an AI agent playing the board game Diplomacy (standard 1901 map, 7 powers) \
against other players, who may be humans or other agents. Each movement phase has a negotiation \
period followed by simultaneous secret orders; the rules engine adjudicates them.

How to play well:
- Pursue supply centers; 18 centers wins outright. Survival and a strong position also matter.
- Negotiate actively: propose concrete cooperation (supports, demilitarized zones, \
non-aggression, coordinated attacks), respond to what others actually said, and remember \
what was promised. You may bluff or break agreements when it serves you, as human players do, \
but never misstate the rules or propose an order that the rules do not allow.
- Keep messages short and in plain English, like a human player's in-game messages. Stay on \
the topic of the game; do not insult or harass other players.
- When you list proposed orders in a message, use exact order syntax, e.g. "A PAR - BUR", \
"F BRE - MAO", "A MAR S A PAR - BUR", "F NTH C A LON - NWY", "A VIE H", "A PAR R PIC", \
"A PAR B", "F STP/SC B", "A WAR D", "WAIVE".
- When submitting orders you must choose only from the legal orders you are given. \
Units without an order hold.

Your private_notes are a short memory that only you will see next phase: record \
agreements, suspicions and plans."""

NEGOTIATE_SCHEMA_BASE = {
    "type": "object",
    "properties": {
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "text": {"type": "string"},
                    "proposed_orders": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["to", "text", "proposed_orders"],
                "additionalProperties": False,
            },
        },
        "private_notes": {"type": "string"},
    },
    "required": ["messages", "private_notes"],
    "additionalProperties": False,
}

ORDERS_SCHEMA = {
    "type": "object",
    "properties": {
        "orders": {"type": "array", "items": {"type": "string"}},
        "private_notes": {"type": "string"},
    },
    "required": ["orders", "private_notes"],
    "additionalProperties": False,
}


def _negotiate_schema(recipients: list[str]) -> dict:
    import copy

    schema = copy.deepcopy(NEGOTIATE_SCHEMA_BASE)
    schema["properties"]["messages"]["items"]["properties"]["to"]["enum"] = recipients
    return schema


def format_press(press: list[Press], power: str) -> str:
    visible = [m for m in press if power in (m.sender, m.recipient)][-MAX_PRESS_IN_PROMPT:]
    if not visible:
        return "No messages yet."
    lines = []
    for m in visible:
        prop = f" | proposed: {'; '.join(m.proposed_orders)}" if m.proposed_orders else ""
        lines.append(f"[{m.phase} r{m.round}] {m.sender} -> {m.recipient}: {m.text}{prop}")
    return "\n".join(lines)


def format_legal(snap: PhaseSnapshot) -> str:
    lines = [f"{loc}: " + " | ".join(orders) for loc, orders in sorted(snap.legal_by_loc.items())]
    if snap.phase_type == "A":
        if snap.builds_allowed:
            lines.insert(0, f"You may build up to {snap.builds_allowed} unit(s) (or WAIVE).")
        if snap.disbands_required:
            lines.insert(0, f"You must disband {snap.disbands_required} unit(s).")
    return "\n".join(lines) or "(no orderable units)"


class LLMAgent(Agent):
    kind = "llm"

    def __init__(self, power: str, llm: LLMClient, negotiates: bool = True):
        super().__init__(power)
        self.llm = llm
        self.negotiates = negotiates
        self.notes = ""

    def _context(self, snap: PhaseSnapshot, press: list[Press]) -> str:
        return (
            f"You are {self.power}. Current phase: {snap.phase} ({PHASE_NAMES.get(snap.phase_type, snap.phase_type)}).\n\n"
            f"## Board\n{snap.state_text}\n\n"
            f"## Recent results\n{snap.recent_results_text}\n\n"
            f"## Your private notes from earlier\n{self.notes or '(none)'}\n\n"
            f"## Messages involving you (most recent last)\n{format_press(press, self.power)}\n"
        )

    def negotiate(self, snap, press, round_idx, rounds):
        if not self.negotiates:
            return []
        others = [p for p in snap.alive_powers if p != self.power]
        if not others:
            return []
        user = (
            self._context(snap, press)
            + f"\n## Your legal orders this phase\n{format_legal(snap)}\n\n"
            f"## Task\nNegotiation round {round_idx + 1} of {rounds} for {snap.phase}. "
            f"Write 0 to {MAX_MESSAGES_PER_ROUND} private messages to other powers "
            f"({', '.join(others)}). Reply to messages you received where useful, and start "
            "conversations that advance your position. Put any concrete orders you propose "
            "(for you or the recipient) in proposed_orders using exact order syntax. "
            "Update your private notes."
        )
        data = self.llm.json_call(SYSTEM_PROMPT, user, _negotiate_schema(others))
        self.notes = data.get("private_notes", self.notes)[:2000]
        return [m for m in data.get("messages", []) if m.get("to") in others][:MAX_MESSAGES_PER_ROUND]

    def orders(self, snap, press):
        user = (
            self._context(snap, press)
            + f"\n## Your legal orders (choose at most one per unit location)\n{format_legal(snap)}\n\n"
            "## Task\nNegotiation is over. Submit your orders for this phase, copying each "
            "order exactly from the legal list. Then update your private notes."
        )
        data = self.llm.json_call(SYSTEM_PROMPT, user, ORDERS_SCHEMA)
        submitted = [normalize_order(o) for o in data.get("orders", [])]
        self.notes = data.get("private_notes", self.notes)[:2000]
        v = validate_orders(submitted, snap)
        if not v.errors:
            return OrderOutcome(submitted, v.accepted, [], [])

        # One repair round: show the errors and ask again.
        err_text = "\n".join(f"- {o}: {why}" for o, why in v.errors)
        repair = (
            user
            + f"\n\n## Problem with your previous answer\nYou submitted: {submitted}\n"
            f"These orders were rejected:\n{err_text}\n"
            "Resubmit the complete set of orders, choosing only from the legal list."
        )
        try:
            data2 = self.llm.json_call(SYSTEM_PROMPT, repair, ORDERS_SCHEMA)
        except LLMError as e:
            return OrderOutcome(submitted, v.accepted, v.errors, v.errors, note=f"repair failed: {e}")
        resubmitted = [normalize_order(o) for o in data2.get("orders", [])]
        self.notes = data2.get("private_notes", self.notes)[:2000]
        v2 = validate_orders(resubmitted, snap)
        return OrderOutcome(resubmitted, v2.accepted, v.errors, v2.errors, repaired=not v2.errors)


# --------------------------------------------------------------------------- human


class HumanAgent(Agent):
    """Terminal player. Reads messages and orders from stdin."""

    kind = "human"

    def __init__(self, power: str, input_fn=input, print_fn=print):
        super().__init__(power)
        self.input = input_fn
        self.print = print_fn
        self.seen = 0

    def _show_inbox(self, press):
        mine = [m for m in press if self.power in (m.sender, m.recipient)]
        new = mine[self.seen:]
        self.seen = len(mine)
        for m in new:
            if m.sender == self.power:
                continue
            prop = f"\n      proposed: {'; '.join(m.proposed_orders)}" if m.proposed_orders else ""
            self.print(f"  << {m.sender}: {m.text}{prop}")

    def negotiate(self, snap, press, round_idx, rounds):
        self.print(f"\n=== {snap.phase} negotiation round {round_idx + 1}/{rounds} ===")
        if round_idx == 0:
            self.print(snap.state_text)
            self.print(snap.recent_results_text)
        self._show_inbox(press)
        others = [p for p in snap.alive_powers if p != self.power]
        self.print(f"Send messages as 'POWER: text' (one per line; {', '.join(others)}). Blank line to finish.")
        out = []
        while True:
            line = self.input("> ").strip()
            if not line:
                break
            to, _, text = line.partition(":")
            to = to.strip().upper()
            if to not in others or not text.strip():
                self.print(f"  (format is POWER: message, POWER one of {', '.join(others)})")
                continue
            out.append({"to": to, "text": text.strip(), "proposed_orders": []})
        return out

    def orders(self, snap, press):
        self.print(f"\n=== {snap.phase} orders ({PHASE_NAMES.get(snap.phase_type)}) ===")
        if snap.phase_type != "M":
            self.print(snap.state_text)
        self._show_inbox(press)
        self.print(format_legal(snap))
        self.print("Enter orders separated by commas (e.g. A PAR - BUR, F BRE - MAO). Blank = all hold / none.")
        while True:
            line = self.input("orders> ")
            submitted = [normalize_order(o) for o in line.split(",") if o.strip()]
            v = validate_orders(submitted, snap)
            if not v.errors:
                return OrderOutcome(submitted, v.accepted, [], [])
            for o, why in v.errors:
                self.print(f"  rejected {o}: {why}")
            self.print("  Please re-enter the full set of orders.")

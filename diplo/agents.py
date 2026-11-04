"""Players: LLM agent, random baseline, and human (terminal) player.

Every agent sees the same `PhaseSnapshot` plus the press (messages) visible to
it, and returns plain data. The runner validates everything an agent returns.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .engine import PHASE_NAMES, PhaseSnapshot, explain_proposal, illegal_message_orders, normalize_order, validate_orders
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

SYSTEM_PROMPT = """You are an AI agent playing the board game Diplomacy ({map_desc}) \
against other players, who may be humans or other agents. Each movement phase has a negotiation \
period followed by simultaneous secret orders; the rules engine adjudicates them.

How to play well:
- You win by growing: {win} centers wins outright. Once the neutral centers are taken (usually \
within the first two game years), the only way to grow is to take centers from other powers. Every year, know \
where your next center will come from.
- Use negotiation to make that happen. Find a partner with a common enemy and agree concrete \
joint attacks: who moves where, who supports whom, and in which season. Non-aggression pacts \
and demilitarized zones are worth making when they free your units to attack elsewhere, but \
they are a means, not the goal: a power whose units only hold stagnates and gets picked apart.
- The strongest alliances fight together: one power's unit supports another power's move, so \
the attack beats a defender neither could beat alone. {joint_example} When you agree a joint attack, put \
both orders in proposed_orders (your support and your ally's move), say which season, and then \
give exactly the support order you agreed.
- Respond to what others actually said, remember what was promised, and judge other powers \
by what they did, not only by what they said. You may bluff or break agreements when it \
serves you, as human players do, but never misstate the rules or propose an order that the \
rules do not allow.
- Give every unit a job: attacking or supporting an attack toward a center, defending a \
center that is actually threatened, or moving into position for next season. Hold only when \
that is genuinely the best use of the unit.
- Keep messages short and in plain English, like a human player's in-game messages. Stay on \
the topic of the game; do not insult or harass other players.
- When you list proposed orders in a message, use exact order syntax (province codes vary by map; \
use the ones in your legal orders), e.g. "A PAR - BUR", \
"F BRE - MAO", "A MAR S A PAR - BUR", "F NTH C A LON - NWY", "A VIE H", "A PAR R PIC", \
"A PAR B", "F STP/SC B", "A WAR D", "WAIVE".
- When submitting orders you must choose only from the legal orders you are given. \
Units without an order hold.
- A message may carry an [engine note] saying that an order proposed in it is not legal this \
phase. Such an order cannot happen as written: never agree to it or plan around it.

The Relationships section of each prompt is computed by the game from what each power said \
and what it actually ordered, so it is accurate. Use it: rely more on powers that keep their \
word, be wary of powers that broke promises or attacked you, and don't fall for the same trick \
twice. Remember that others see your record too: follow through on the joint attacks you agree, \
and break your word only when the gain is worth the lost trust.

Your private_notes are a short memory that only you will see next phase: record \
agreements, suspicions and plans."""

def system_prompt(snap: PhaseSnapshot) -> str:
    """The system prompt for this game's map. Constant within a game, so it caches well."""
    if snap.map_name == "standard":
        desc = "standard 1901 map, 7 powers"
        example = ('For example, you order "A TYR S A VEN - TRI" so your ally\'s army in Venice takes '
                   'Trieste with your support.')
    else:
        desc = f"the '{snap.map_name}' variant map, {snap.n_powers} powers"
        example = ('For example, you order "A X S A Y - Z": your army in X supports your ally\'s army in Y '
                   'into Z.')
    return (SYSTEM_PROMPT.replace("{map_desc}", desc).replace("{win}", str(snap.win_centers))
            .replace("{joint_example}", example))


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
        note = (f" [engine note: not a legal order this phase: {'; '.join(m.illegal_proposals)}]"
                if m.illegal_proposals else "")
        lines.append(f"[{m.phase} r{m.round}] {m.sender} -> {m.recipient}: {m.text}{prop}{note}")
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
        self.handover = ""  # set when this agent substitutes for a player who left

    def _context(self, snap: PhaseSnapshot, press: list[Press]) -> str:
        return (
            f"You are {self.power}. Current phase: {snap.phase} ({PHASE_NAMES.get(snap.phase_type, snap.phase_type)}).\n\n"
            f"## Board\n{snap.state_text}\n\n"
            f"## Recent results\n{snap.recent_results_text}\n\n"
            f"## Relationships (computed by the game from messages and actual orders)\n"
            f"{snap.relations_text or 'No promises, requests or clashes recorded yet.'}\n\n"
            + (("## Game notes\n" + "\n".join(f"- {n}" for n in snap.game_notes) + "\n\n") if snap.game_notes else "")
            + (f"## Handover: you have taken over {self.power} from the previous player\n{self.handover}\n\n"
               if self.handover else "")
            + f"## Your private notes from earlier\n{self.notes or '(none)'}\n\n"
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
            "conversations that advance your position. Look for a joint attack you could make "
            "with a partner this season or next, including one where one of you supports the "
            "other's move. Put any concrete orders you propose (for you or the recipient) in "
            "proposed_orders using exact order syntax. "
            "Update your private notes."
        )
        schema = _negotiate_schema(others)
        data = self.llm.json_call(system_prompt(snap), user, schema)
        self.notes = data.get("private_notes", self.notes)[:2000]
        msgs = [m for m in data.get("messages", []) if m.get("to") in others][:MAX_MESSAGES_PER_ROUND]
        bad = self._illegal_in(msgs, snap)
        self.last_negotiation = {"first_pass_illegal": sum(len(b) for b in bad.values()),
                                 "repaired": False, "dropped": 0,
                                 "illegal": sorted({o for b in bad.values() for o in b})}
        if not bad:
            return msgs

        # One repair round: show each message's illegal orders and the units' real options.
        lines = []
        for i, orders in bad.items():
            lines.append(f"- Message {i + 1} (to {msgs[i]['to']}):")
            lines += [f"    {o}: {explain_proposal(o, snap.all_legal_by_loc)}" for o in orders]
        drafted = "\n".join(f"{i + 1}. to {m['to']}: {m['text']} | proposed: {m.get('proposed_orders', [])}"
                             for i, m in enumerate(msgs))
        repair = (
            user
            + f"\n\n## Problem with your drafted messages\n{drafted}\n\n"
            f"These proposed orders are not legal, so the messages were not sent:\n" + "\n".join(lines)
            + "\nRewrite the complete set of messages. Every order you propose or mention, in "
            "proposed_orders or in the text, must be legal this phase."
        )
        try:
            data2 = self.llm.json_call(system_prompt(snap), repair, schema)
        except LLMError:
            data2 = {"messages": [m for i, m in enumerate(msgs) if i not in bad]}
        self.notes = data2.get("private_notes", self.notes)[:2000]
        msgs2 = [m for m in data2.get("messages", []) if m.get("to") in others][:MAX_MESSAGES_PER_ROUND]
        still = self._illegal_in(msgs2, snap)
        self.last_negotiation.update(repaired=not still, dropped=len(still))
        return [m for i, m in enumerate(msgs2) if i not in still]

    @staticmethod
    def _illegal_in(msgs: list[dict], snap: PhaseSnapshot) -> dict[int, list[str]]:
        legal = {o for os_ in snap.all_legal_by_loc.values() for o in os_}
        bad = {}
        for i, m in enumerate(msgs):
            wrong = illegal_message_orders(m.get("text", ""), m.get("proposed_orders", []) or [], legal, snap.valid_locs)
            if wrong:
                bad[i] = wrong
        return bad

    def orders(self, snap, press):
        user = (
            self._context(snap, press)
            + f"\n## Your legal orders (choose at most one per unit location)\n{format_legal(snap)}\n\n"
            "## Task\nNegotiation is over. Submit your orders for this phase, copying each "
            "order exactly from the legal list, and check that every unit has a job. "
            "Then update your private notes."
        )
        data = self.llm.json_call(system_prompt(snap), user, ORDERS_SCHEMA)
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
            data2 = self.llm.json_call(system_prompt(snap), repair, ORDERS_SCHEMA)
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
        self._relations_shown = None

    def _show_inbox(self, press):
        mine = [m for m in press if self.power in (m.sender, m.recipient)]
        new = mine[self.seen:]
        self.seen = len(mine)
        for m in new:
            if m.sender == self.power:
                continue
            prop = f"\n      proposed: {'; '.join(m.proposed_orders)}" if m.proposed_orders else ""
            if m.illegal_proposals:
                prop += f"\n      [engine note: not a legal order this phase: {'; '.join(m.illegal_proposals)}]"
            self.print(f"  << {m.sender}: {m.text}{prop}")

    def _show_relations(self, snap):
        if self._relations_shown != snap.phase:
            for n in snap.game_notes:
                self.print(f"[game note] {n}")
        if snap.relations_text and self._relations_shown != snap.phase:
            self._relations_shown = snap.phase
            self.print("Relationships (computed by the game: what each power said vs what it did):")
            self.print(snap.relations_text)

    def negotiate(self, snap, press, round_idx, rounds):
        self.print(f"\n=== {snap.phase} negotiation round {round_idx + 1}/{rounds} ===")
        if round_idx == 0:
            self.print(snap.state_text)
            self.print(snap.recent_results_text)
        self._show_relations(snap)
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
        else:
            self._show_relations(snap)  # only shows if negotiation didn't already show it
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

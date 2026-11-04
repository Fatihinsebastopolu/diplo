"""Runs one game: negotiation rounds, order collection, adjudication, logging."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from diplomacy.utils.export import to_saved_game_format

from .agents import Agent, HumanAgent, OrderOutcome, Press
from .engine import (
    all_legal_orders,
    illegal_message_orders,
    message_orders,
    new_game,
    phase_year,
    sc_counts,
    snapshot,
    win_centers,
)
from .llm import LLMError
from .relations import Ledger
from .viewer import PhaseView


class GameRunner:
    def __init__(
        self,
        agents: dict[str, Agent],
        out_dir: Path,
        max_year: int = 1905,
        press_rounds: int = 2,
        llm=None,
        config: dict | None = None,
        map_name: str = "standard",
        workers: int = 7,
        observer=None,
        max_cost: float | None = None,
    ):
        self.game = new_game(map_name)
        missing = set(self.game.powers) - set(agents)
        if missing:
            raise ValueError(f"no agent for {sorted(missing)}")
        self.agents = agents
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.max_year = max_year
        self.press_rounds = press_rounds
        self.llm = llm
        self.config = config or {}
        self.workers = workers
        self.observer = observer
        self.max_cost = max_cost
        self.stopped_reason = None
        self.press: list[Press] = []
        self.ledger = Ledger(list(self.game.powers))
        self.game_notes: list[str] = []  # public announcements shown to every player
        self.events = open(self.out_dir / "events.jsonl", "w", encoding="utf-8")
        self.stats = {
            p: {
                "messages_sent": 0,
                "proposals": 0,
                "illegal_proposals": 0,
                "illegal_proposals_first_pass": 0,
                "proposal_repairs_succeeded": 0,
                "messages_dropped": 0,
                "orders_submitted": 0,
                "orders_accepted": 0,
                "illegal_first_pass": 0,
                "repairs_succeeded": 0,
                "illegal_final": 0,
                "agent_failures": 0,
            }
            for p in self.game.powers
        }

    # ------------------------------------------------------------------ logging
    def log(self, kind: str, **data) -> None:
        self.events.write(json.dumps({"t": round(time.time(), 2), "event": kind, **data}) + "\n")
        self.events.flush()

    def _notify(self, hook: str, *args) -> None:
        """Call an observer hook (e.g. the live board); its failures never stop the game."""
        if self.observer is None:
            return
        try:
            getattr(self.observer, hook)(*args)
        except Exception as e:  # noqa: BLE001
            self.log("observer_error", hook=hook, error=repr(e))

    # ------------------------------------------------------------------ helpers
    def _parallel(self, fn, powers: list[str]) -> dict[str, object]:
        """Run fn(power) for LLM/random agents in parallel; humans run in the main thread."""
        results: dict[str, object] = {}
        humans = [p for p in powers if isinstance(self.agents[p], HumanAgent)]
        others = [p for p in powers if p not in humans]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {p: pool.submit(fn, p) for p in others}
            for p in humans:
                results[p] = fn(p)
            for p, f in futures.items():
                results[p] = f.result()
        return results

    # ------------------------------------------------------------------ phases
    def _press_round(self, r: int, snaps: dict, legal_any: set[str]) -> None:
        visible = list(self.press)  # simultaneous: everyone sees the same snapshot
        phase = self.game.get_current_phase()

        def call(p):
            try:
                agent = self.agents[p]
                agent.last_negotiation = None
                out = agent.negotiate(snaps[p], visible, r, self.press_rounds)
                rep = getattr(agent, "last_negotiation", None)
                if rep:
                    st = self.stats[p]
                    st["illegal_proposals_first_pass"] += rep["first_pass_illegal"]
                    st["proposal_repairs_succeeded"] += int(rep["repaired"] and rep["first_pass_illegal"] > 0)
                    st["messages_dropped"] += rep["dropped"]
                    if rep["first_pass_illegal"]:
                        self.log("proposal_repair", phase=phase, power=p, round=r, **rep)
                return out
            except LLMError as e:
                self.stats[p]["agent_failures"] += 1
                self.log("agent_error", phase=phase, power=p, step="negotiate", error=str(e))
                return []

        out = self._parallel(call, list(snaps))
        valid_locs = {l.upper() for l in self.game.map.locs}
        for sender in sorted(out):
            for m in out[sender]:
                # structured proposals plus any order-like text in the message (humans have only text)
                structured = m.get("proposed_orders", []) or []
                proposed = message_orders(m["text"], structured, valid_locs)
                bad = illegal_message_orders(m["text"], structured, legal_any, valid_locs)
                good = [o for o in proposed if o not in bad]
                msg = Press(phase, r, sender, m["to"], m["text"].strip(), good + bad, bad)
                self.press.append(msg)
                st = self.stats[sender]
                st["messages_sent"] += 1
                st["proposals"] += len(good) + len(bad)
                st["illegal_proposals"] += len(bad)
                self.log("press", **asdict(msg))
                self._notify("press", msg)

    def _order_round(self, snaps: dict) -> dict[str, OrderOutcome]:
        phase = self.game.get_current_phase()

        def call(p):
            try:
                return self.agents[p].orders(snaps[p], list(self.press))
            except LLMError as e:
                self.stats[p]["agent_failures"] += 1
                self.log("agent_error", phase=phase, power=p, step="orders", error=str(e))
                return OrderOutcome([], [], [], [], note=f"agent failed, units hold: {e}")

        outcomes = self._parallel(call, list(snaps))
        for p, oc in outcomes.items():
            st = self.stats[p]
            st["orders_submitted"] += len(oc.submitted)
            st["orders_accepted"] += len(oc.accepted)
            st["illegal_first_pass"] += len(oc.first_pass_errors)
            st["illegal_final"] += len(oc.final_errors)
            st["repairs_succeeded"] += int(oc.repaired)
            self.game.set_orders(p, oc.accepted)
            self.log(
                "orders",
                phase=phase,
                power=p,
                agent=self.agents[p].kind,
                submitted=oc.submitted,
                accepted=oc.accepted,
                first_pass_errors=oc.first_pass_errors,
                final_errors=oc.final_errors,
                repaired=oc.repaired,
                note=oc.note,
            )
        return outcomes

    # ------------------------------------------------------------------ main loop
    def run(self) -> dict:
        start = time.time()
        self.log("game_start", config=self.config, map=self.game.map.name, max_year=self.max_year,
                 press_rounds=self.press_rounds,
                 agents={p: a.kind for p, a in self.agents.items()})
        g = self.game
        while not g.is_game_done:
            phase = g.get_current_phase()
            year = phase_year(phase)
            if year is None or year > self.max_year:
                break
            all_orders = g.get_all_possible_orders()
            alive = [p for p in g.powers if not g.powers[p].is_eliminated()]
            snaps = {p: snapshot(g, p, all_orders) for p in alive}
            for p, s in snaps.items():
                s.relations_text = self.ledger.text_for(p)
                s.game_notes = list(self.game_notes)
            self._notify("relations", {p: s.relations_text for p, s in snaps.items()})
            units_at_start = {p: list(g.get_units(p)) for p in g.powers}
            center_owner = {c: p for p in g.powers for c in g.get_centers(p)}
            self._notify("phase_start", g, phase)
            if g.phase_type == "M" and self.press_rounds > 0:
                legal_any = all_legal_orders(g, all_orders)
                for r in range(self.press_rounds):
                    t0 = time.time()
                    self._press_round(r, snaps, legal_any)
                    self.log("timing", phase=phase, step="negotiation", round=r, seconds=round(time.time() - t0, 2))
            needs_orders = {p: s for p, s in snaps.items() if s.legal_by_loc}
            t0 = time.time()
            outcomes = self._order_round(needs_orders)
            self.log("timing", phase=phase, step="orders", seconds=round(time.time() - t0, 2))
            self._notify("orders_set", g, phase)
            g.process()
            self.ledger.update(PhaseView(
                phase=phase, units=units_at_start, centers={},
                orders={p: list(outcomes[p].accepted) if p in outcomes else [] for p in g.powers},
                press=[asdict(m) for m in self.press if m.phase == phase],
                results={str(u): [str(x) for x in rs if str(x)] for u, rs in g.result_history[phase].items()},
            ), center_owner)
            self.log("phase_result", phase=phase, centers=sc_counts(g),
                     results={str(u): [str(x) for x in rs] for u, rs in g.result_history[phase].items() if rs})
            if self.llm is not None:
                self.log("usage", phase=phase, **self.llm.usage.to_dict())
            notes = {p: a.notes for p, a in self.agents.items() if getattr(a, "notes", "")}
            if notes:
                self.log("agent_notes", phase=phase, notes=notes)  # lets a resumed game restore memories
            self._notify("phase_end", g, phase)
            if self.max_cost is not None and self.llm is not None and self.llm.usage.cost_usd >= self.max_cost:
                self.stopped_reason = f"cost limit: est. ${self.llm.usage.cost_usd:.2f} >= ${self.max_cost:.2f}"
                self.log("budget_stop", phase=phase, cost_usd=round(self.llm.usage.cost_usd, 4), max_cost=self.max_cost)
                break

        summary = self._summary(time.time() - start)
        self._notify("game_end", g, summary)
        self.log("game_end", **summary)
        self.events.close()
        (self.out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        (self.out_dir / "saved_game.json").write_text(json.dumps(to_saved_game_format(g)))
        return summary

    def _summary(self, seconds: float) -> dict:
        g = self.game
        centers = sc_counts(g)
        winners = [p for p, n in centers.items() if n >= win_centers(g)]
        top = max(centers.values())
        leaders = [p for p, n in centers.items() if n == top]
        return {
            "config": self.config,
            "map": g.map.name,
            "final_phase": g.get_current_phase(),
            "phases_played": len(g.order_history),
            "solo_winner": winners[0] if winners else None,
            "leaders": leaders,
            "centers": centers,
            "eliminated": [p for p in g.powers if g.powers[p].is_eliminated()],
            "agents": {p: a.kind for p, a in self.agents.items()},
            "stats": self.stats,
            "usage": self.llm.usage.to_dict() if self.llm is not None else None,
            "wall_seconds": round(seconds, 1),
            "stopped_reason": self.stopped_reason,
        }

"""Command line: `python -m diplo {sim,play,show,report}`."""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

from .agents import HumanAgent, LLMAgent, RandomAgent
from .engine import POWERS
from .llm import LLMClient
from .report import aggregate, transcript
from .runner import GameRunner


def _powers(values: list[str] | None) -> list[str]:
    out = []
    for v in values or []:
        for p in v.split(","):
            p = p.strip().upper()
            if not p:
                continue
            match = [q for q in POWERS if q.startswith(p)]
            if len(match) != 1:
                sys.exit(f"unknown power {p!r}; choose from {', '.join(POWERS)}")
            out.append(match[0])
    return out


def _build_agents(args, human: str | None, rng: random.Random):
    random_powers = set(_powers(args.random))
    silent = set(_powers(args.silent))
    needs_llm = any(p not in random_powers and p != human for p in POWERS)
    llm = LLMClient(model=args.model, effort=args.effort) if needs_llm else None
    agents = {}
    for p in POWERS:
        if p == human:
            agents[p] = HumanAgent(p)
        elif p in random_powers:
            agents[p] = RandomAgent(p, random.Random(rng.random()))
        else:
            agents[p] = LLMAgent(p, llm, negotiates=p not in silent)
    return agents, llm


def _config(args, **extra):
    return {
        "model": args.model,
        "effort": args.effort,
        "press_rounds": args.press_rounds,
        "max_year": args.max_year,
        "random_powers": _powers(args.random),
        "silent_powers": _powers(args.silent),
        **extra,
    }


def cmd_sim(args):
    run_dir = Path(args.out) / time.strftime("sim-%Y%m%d-%H%M%S")
    rng = random.Random(args.seed)
    for i in range(args.games):
        agents, llm = _build_agents(args, None, rng)
        gdir = run_dir / f"game-{i + 1:03d}"
        print(f"[diplo] game {i + 1}/{args.games} -> {gdir}")
        s = GameRunner(agents, gdir, max_year=args.max_year, press_rounds=args.press_rounds, llm=llm,
                       config=_config(args, game=i + 1, seed=args.seed)).run()
        (gdir / "transcript.md").write_text(transcript(gdir / "events.jsonl"))
        cost = f", est. cost ${s['usage']['cost_usd']:.3f}" if s["usage"] else ""
        print(f"[diplo]   {s['final_phase']}: {s['centers']}{cost}")
    report = aggregate(run_dir)
    (run_dir / "report.md").write_text(report)
    print(report)
    print(f"[diplo] wrote {run_dir}/report.md")


def cmd_play(args):
    human = _powers([args.power])[0]
    run_dir = Path(args.out) / time.strftime(f"play-{human.lower()}-%Y%m%d-%H%M%S")
    agents, llm = _build_agents(args, human, random.Random(args.seed))
    print(f"You are {human}. Log: {run_dir}")
    try:
        s = GameRunner(agents, run_dir, max_year=args.max_year, press_rounds=args.press_rounds, llm=llm,
                       config=_config(args, human=human), workers=7).run()
    except (KeyboardInterrupt, EOFError):
        print("\n[diplo] game interrupted; partial log kept in", run_dir)
        return
    (run_dir / "transcript.md").write_text(transcript(run_dir / "events.jsonl"))
    print(f"\nFinal centers: {s['centers']}")
    print(f"Transcript: {run_dir}/transcript.md")


def cmd_show(args):
    print(transcript(Path(args.events)))


def cmd_report(args):
    print(aggregate(Path(args.run_dir)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m diplo", description="LLM agents for Diplomacy")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--model", default="claude-opus-5-5", help="Claude model ID (default %(default)s)")
        p.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
        p.add_argument("--max-year", type=int, default=1905, help="stop after this game year (default %(default)s)")
        p.add_argument("--press-rounds", type=int, default=2, help="negotiation rounds per movement phase")
        p.add_argument("--random", action="append", metavar="POWERS",
                       help="powers played by the random baseline instead of the LLM, e.g. --random ITA,TUR")
        p.add_argument("--silent", action="append", metavar="POWERS",
                       help="LLM powers that never send messages (they still read them)")
        p.add_argument("--seed", type=int, default=None)
        p.add_argument("--out", default="runs", help="output directory (default %(default)s)")

    p = sub.add_parser("sim", help="run agent-only games and write a report")
    common(p)
    p.add_argument("--games", type=int, default=1)
    p.set_defaults(fn=cmd_sim)

    p = sub.add_parser("play", help="play one power yourself against agents")
    common(p)
    p.add_argument("--power", required=True, help="the power you play, e.g. FRANCE or FRA")
    p.set_defaults(fn=cmd_play)

    p = sub.add_parser("show", help="print a markdown transcript of a game log")
    p.add_argument("events", help="path to a game's events.jsonl")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("report", help="aggregate every game under a run directory")
    p.add_argument("run_dir")
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

"""Command line: `python -m diplo {sim,play,show,report}`."""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

from .agents import HumanAgent, LLMAgent, RandomAgent
from .engine import map_powers, new_game, phase_year
from .llm import (
    DEFAULT_CLAUDE_MODEL,
    AnthropicClient,
    Recorder,
    RecordingClient,
    ReplayClient,
    load_recording,
)
from .report import aggregate, transcript
from .runner import GameRunner
from .viewer import build_viewer

# Options that define a game; saved in each recording so --replay can restore them.
GAME_OPTIONS = ["map", "model", "effort", "max_year", "press_rounds", "random", "silent", "max_cost"]


def _powers(values, powers=None) -> list[str]:
    powers = powers or map_powers("standard")
    out = []
    for v in values or []:
        for p in v.split(","):
            p = p.strip().upper()
            if not p:
                continue
            match = [q for q in powers if q.startswith(p)]
            if len(match) != 1:
                sys.exit(f"unknown power {p!r}; choose from {', '.join(powers)}")
            out.append(match[0])
    return out


def _live_client(args):
    return AnthropicClient(model=args.model, effort=args.effort)


def _play_one_game(args, gdir: Path, game_seed: int, human: str | None = None, replay=None):
    """Build agents and run one game. `replay` is (by_key, human_lines) or None."""
    gdir.mkdir(parents=True, exist_ok=True)
    powers = map_powers(args.map)
    random_powers = set(_powers(args.random, powers))
    silent = set(_powers(args.silent, powers))
    needs_llm = any(p not in random_powers and p != human for p in powers)
    seed_rng = random.Random(game_seed)

    recorder = None
    llm = None
    if needs_llm and replay is not None:
        llm = ReplayClient(args.model, replay[0])
    elif needs_llm:
        meta = {k: getattr(args, k) for k in GAME_OPTIONS}
        meta.update(game_seed=game_seed, human=human)
        recorder = Recorder(gdir / "recording.jsonl", meta)
        llm = RecordingClient(_live_client(args), recorder)

    agents = {}
    for p in powers:
        if p == human:
            if replay is not None:
                lines = iter(replay[1])

                def scripted(prompt="", _lines=lines):
                    line = next(_lines, "")
                    print(f"{prompt}{line}")
                    return line

                agents[p] = HumanAgent(p, input_fn=scripted)
            else:
                def recorded_input(prompt=""):
                    line = input(prompt)
                    if recorder:
                        recorder.human(line)
                    return line

                agents[p] = HumanAgent(p, input_fn=recorded_input)
        elif p in random_powers:
            agents[p] = RandomAgent(p, random.Random(seed_rng.random()))
        else:
            agents[p] = LLMAgent(p, llm, negotiates=p not in silent)

    config = {k: getattr(args, k) for k in GAME_OPTIONS}
    config.update(game_seed=game_seed, human=human, replayed=replay is not None)
    try:
        board = getattr(args, "_board", None)
        if board is not None:
            board.viewer_power = human  # a human sees only their own messages
            board.title = f"{'Replay: ' if replay else ''}{'You are ' + human.title() if human else 'Agent-only game'}"
            board._update(title=board.title, viewer=human, messages=[], results=[])
        s = GameRunner(agents, gdir, max_year=args.max_year, press_rounds=args.press_rounds, map_name=args.map,
                       llm=llm, config=config, observer=board, max_cost=args.max_cost).run()
    finally:
        if recorder:
            recorder.close()
    (gdir / "transcript.md").write_text(transcript(gdir / "events.jsonl"))
    print(f"[diplo]   viewer: {build_viewer(gdir)}")
    return s


def _print_result(s):
    cost = f", est. cost ${s['usage']['cost_usd']:.3f}" if s["usage"] else ""
    if s.get("stopped_reason"):
        cost += f" (stopped early: {s['stopped_reason']})"
    print(f"[diplo]   {s['final_phase']}: {s['centers']}{cost}")


def cmd_sim(args):
    if args.replay:
        return _replay(args)
    run_dir = Path(args.out) / time.strftime("sim-%Y%m%d-%H%M%S")
    rng = random.Random(args.seed)
    for i in range(args.games):
        gdir = run_dir / f"game-{i + 1:03d}"
        print(f"[diplo] game {i + 1}/{args.games} -> {gdir}")
        _print_result(_play_one_game(args, gdir, rng.randrange(2**31)))
    report = aggregate(run_dir)
    (run_dir / "report.md").write_text(report)
    print(report)
    print(f"[diplo] wrote {run_dir}/report.md")


def cmd_play(args):
    if args.replay:
        return _replay(args)
    human = _powers([args.power], map_powers(args.map))[0]
    run_dir = Path(args.out) / time.strftime(f"play-{human.lower()}-%Y%m%d-%H%M%S")
    print(f"You are {human}. Log: {run_dir}")
    seed = args.seed if args.seed is not None else random.randrange(2**31)
    try:
        s = _play_one_game(args, run_dir, seed, human=human)
    except (KeyboardInterrupt, EOFError):
        print("\n[diplo] game interrupted; partial log kept in", run_dir)
        return
    print(f"\nFinal centers: {s['centers']}")
    print(f"Transcript: {run_dir}/transcript.md")


def _replay(args):
    src = Path(args.replay)
    files = sorted(src.rglob("recording.jsonl")) if src.is_dir() else [src]
    if not files:
        sys.exit(f"no recording.jsonl found under {src}")
    run_dir = Path(args.out) / time.strftime("replay-%Y%m%d-%H%M%S")
    for i, f in enumerate(files):
        meta, by_key, human_lines = load_recording(f)
        for k in GAME_OPTIONS:
            setattr(args, k, meta.get(k, getattr(args, k, None)))
        gdir = run_dir / f"game-{i + 1:03d}"
        print(f"[diplo] replaying {f} -> {gdir} (no API calls)")
        if meta.get("resume"):
            r = meta["resume"]
            s, _ = _resume_game(args, f.parent / r["source"], r["at"], r["substitute"], r["announce"], gdir,
                                ReplayClient(args.model, by_key))
            _print_result(s)
            continue
        _print_result(_play_one_game(args, gdir, meta["game_seed"], human=meta.get("human"),
                                     replay=(by_key, human_lines)))
    report = aggregate(run_dir)
    (run_dir / "report.md").write_text(report)
    print(report)
    print(f"[diplo] wrote {run_dir}/report.md")


def cmd_scenario(args):
    from .scenarios import report, run_scenario

    run_dir = Path(args.out) / time.strftime(f"scenario-{args.kind}-%Y%m%d-%H%M%S")
    conditions = ["abuser", "honest"] if args.kind == "broken-promises" else ["abuser"]
    results = []
    for i in range(args.games):
        for cond in conditions:
            gdir = run_dir / f"game-{i + 1:03d}-{cond}"
            gdir.mkdir(parents=True, exist_ok=True)
            meta = {k: getattr(args, k) for k in GAME_OPTIONS}
            meta.update(game_seed=i, human=None, scenario=args.kind, condition=cond)
            recorder = Recorder(gdir / "recording.jsonl", meta)
            llm = RecordingClient(_live_client(args), recorder)
            try:
                r = run_scenario(args.kind, cond, llm, gdir, target=args.target, adversary=args.adversary,
                                 max_year=args.max_year, press_rounds=args.press_rounds, seed=i)
            finally:
                recorder.close()
            results.append(r)
            print(f"[diplo] {gdir.name}: cost ${r['cost_usd']:.3f}" +
                  (f", trust in {args.adversary}: {r['trust_in_adversary']}" if "trust_in_adversary" in r else
                   f", passed: {r['passed']}"))
    md = report(results)
    (run_dir / "report.md").write_text(md)
    print(md)
    print(f"[diplo] wrote {run_dir}/report.md")


def _resume_game(args, source: Path, at: str, sub: str, announce: bool, gdir: Path, llm, recorder=None):
    """Continue the game logged at `source` from phase `at`, with a substitute agent for `sub` (or none)."""
    import json
    import shutil

    from .handover import handover_text, load_events as load_ev, restore

    gdir.mkdir(parents=True, exist_ok=True)
    events = load_ev(source)
    src_copy = gdir / "resume_source.jsonl"
    if source.resolve() != src_copy.resolve():
        shutil.copyfile(source, src_copy)  # makes the resumed game replayable on its own
    start = next(e for e in events if e["event"] == "game_start")
    game, press, ledger, notes, views, prefix = restore(events, at)
    powers = list(game.powers)
    sub = _powers([sub], powers)[0] if sub else None
    kinds = start.get("agents", {})
    agents = {}
    for i, p in enumerate(powers):
        if kinds.get(p) == "random":
            agents[p] = RandomAgent(p, random.Random(i))
        else:  # LLM and human seats continue as LLM agents
            agents[p] = LLMAgent(p, llm)
            if p != sub:
                agents[p].notes = notes.get(p, "")
    handover = handover_text(sub, at, views, ledger, announce) if sub else ""
    if sub:
        agents[sub].handover = handover
    config = {k: getattr(args, k, None) for k in GAME_OPTIONS}
    config.update(resumed_from=str(source), at=at, substitute=sub, announce=announce)
    runner = GameRunner(agents, gdir, max_year=args.max_year, press_rounds=args.press_rounds,
                        map_name=args.map, llm=llm, config=config, max_cost=args.max_cost)
    runner.game, runner.press, runner.ledger = game, press, ledger
    if announce and sub:
        runner.game_notes = [f"{sub} is now played by a substitute player, from {at} on."]
    for e in prefix:
        runner.events.write(json.dumps(e) + "\n")
    restored = sorted(p for p in powers if p != sub and agents[p].kind == "llm" and notes.get(p))
    runner.log("resume", source=str(source), at=at, substitute=sub, announce=announce,
               handover_chars=len(handover), restored_notes_for=restored)
    if not notes:
        print("[diplo]   note: this log has no saved agent notes (made before notes were logged); "
              "the other agents start with empty notes")
    s = runner.run()
    (gdir / "transcript.md").write_text(transcript(gdir / "events.jsonl"))
    print(f"[diplo]   viewer: {build_viewer(gdir)}")
    return s, handover


def cmd_resume(args):
    from .handover import load_events as load_ev

    src = Path(args.game)
    source = src / "events.jsonl" if src.is_dir() else src
    events = load_ev(source)
    start = next(e for e in events if e["event"] == "game_start")
    cfg = start.get("config", {})
    args.map = start.get("map") or cfg.get("map") or "standard"
    if args.press_rounds is None:
        args.press_rounds = start.get("press_rounds", 2)
    if args.max_year is None:
        args.max_year = (int(args.at[1:5]) + args.years - 1) if args.years else start.get("max_year")
    announce = not args.no_announce
    gdir = Path(args.out) / time.strftime(f"resume-{(args.substitute or 'all').lower()}-%Y%m%d-%H%M%S")
    gdir.mkdir(parents=True, exist_ok=True)
    meta = {k: getattr(args, k, None) for k in GAME_OPTIONS}
    meta.update(game_seed=0, human=None,
                resume={"source": "resume_source.jsonl", "at": args.at, "substitute": args.substitute,
                        "announce": announce})
    recorder = Recorder(gdir / "recording.jsonl", meta)
    llm = RecordingClient(_live_client(args), recorder)
    try:
        s, handover = _resume_game(args, source, args.at, args.substitute, announce, gdir, llm)
    finally:
        recorder.close()
    if handover:
        print("\n--- handover given to the substitute ---\n" + handover + "\n---")
    _print_result(s)
    report = aggregate(gdir)
    (gdir / "report.md").write_text(report)
    print(f"[diplo] wrote {gdir}/report.md")


def cmd_show(args):
    print(transcript(Path(args.events)))


def cmd_view(args):
    src = Path(args.path)
    games = sorted({p.parent for p in src.rglob("events.jsonl")}) if src.is_dir() else [src.parent]
    if not games:
        sys.exit(f"no events.jsonl found under {src}")
    for g in games:
        (g / "transcript.md").write_text(transcript(g / "events.jsonl"))
        print(f"[diplo] {build_viewer(g)}")
    runs = {g.parent for g in games if g.name.startswith("game-")}
    for r in runs:
        (r / "report.md").write_text(aggregate(r))
        print(f"[diplo] {r / 'report.md'}")


def cmd_report(args):
    print(aggregate(Path(args.run_dir)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m diplo", description="LLM agents for Diplomacy")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--map", default="standard",
                       help="map from the diplomacy package, e.g. standard, ancmed (5 powers), modern (10), pure (7)")
        p.add_argument("--years", type=int, default=10,
                       help="game years to play, counted from the map's start year (default %(default)s)")
        p.add_argument("--model", default=DEFAULT_CLAUDE_MODEL,
                       help=f"Claude model ID (default {DEFAULT_CLAUDE_MODEL})")
        p.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"],
                       help="Claude effort level (default %(default)s)")
        p.add_argument("--max-year", type=int, default=None,
                       help="stop after this game year (overrides --years; e.g. 1910 on the standard map)")
        p.add_argument("--press-rounds", type=int, default=2, help="negotiation rounds per movement phase")
        p.add_argument("--random", action="append", metavar="POWERS",
                       help="powers played by the random baseline instead of the LLM, e.g. --random ITA,TUR")
        p.add_argument("--silent", action="append", metavar="POWERS",
                       help="LLM powers that never send messages (they still read them)")
        p.add_argument("--replay", metavar="PATH",
                       help="re-run recorded game(s) from a recording.jsonl or a run directory, with no API calls")
        p.add_argument("--max-cost", type=float, default=None, metavar="USD",
                       help="stop a game once its estimated API cost reaches this (checked after each phase)")
        p.add_argument("--live", action="store_true",
                       help="serve a live board at http://localhost:PORT while the game runs")
        p.add_argument("--live-port", type=int, default=8765)
        p.add_argument("--live-delay", type=float, default=0.0,
                       help="seconds to pause after each phase (useful with --replay for watching)")
        p.add_argument("--seed", type=int, default=None)
        p.add_argument("--out", default="runs", help="output directory (default %(default)s)")

    p = sub.add_parser("sim", help="run agent-only games and write a report")
    common(p)
    p.add_argument("--games", type=int, default=1)
    p.set_defaults(fn=cmd_sim)

    p = sub.add_parser("play", help="play one power yourself against agents")
    common(p)
    p.add_argument("--power", help="the power you play, e.g. FRANCE or FRA")
    p.set_defaults(fn=cmd_play)

    p = sub.add_parser("scenario", help="abuse tests: repeated broken promises or illegal proposals")
    p.add_argument("kind", choices=["broken-promises", "illegal-proposals"])
    p.add_argument("--target", default="FRANCE", help="the LLM agent under test (default %(default)s)")
    p.add_argument("--adversary", default="GERMANY", help="the scripted neighbour (default %(default)s)")
    p.add_argument("--games", type=int, default=1, help="games per condition")
    p.add_argument("--model", default=DEFAULT_CLAUDE_MODEL)
    p.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--max-year", type=int, default=1903)
    p.add_argument("--press-rounds", type=int, default=2)
    p.add_argument("--random", default=None, help=argparse.SUPPRESS)
    p.add_argument("--silent", default=None, help=argparse.SUPPRESS)
    p.add_argument("--max-cost", type=float, default=None, help=argparse.SUPPRESS)
    p.add_argument("--out", default="runs")
    p.set_defaults(fn=cmd_scenario)

    p = sub.add_parser("resume", help="continue a logged game from a phase, optionally with a substitute for one power")
    p.add_argument("game", help="a game directory (or its events.jsonl)")
    p.add_argument("--at", required=True, help="the phase the substitute starts in, e.g. S1903M")
    p.add_argument("--substitute", default=None,
                   help="the power whose player left, e.g. FRANCE (omit to continue with every agent as before)")
    p.add_argument("--no-announce", action="store_true", help="don't tell the other players about the substitution")
    p.add_argument("--years", type=int, default=None, help="game years to play from --at (default: the original end)")
    p.add_argument("--max-year", type=int, default=None)
    p.add_argument("--press-rounds", type=int, default=None, help="default: as in the original game")
    p.add_argument("--model", default=DEFAULT_CLAUDE_MODEL)
    p.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--max-cost", type=float, default=None, metavar="USD")
    p.add_argument("--random", default=None, help=argparse.SUPPRESS)
    p.add_argument("--silent", default=None, help=argparse.SUPPRESS)
    p.add_argument("--out", default="runs")
    p.set_defaults(fn=cmd_resume, replay=None)

    p = sub.add_parser("show", help="print a markdown transcript of a game log")
    p.add_argument("events", help="path to a game's events.jsonl")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("view", help="(re)build game.html, maps, transcript and report for logged game(s)")
    p.add_argument("path", help="a run directory, a game directory, or an events.jsonl")
    p.set_defaults(fn=cmd_view)

    p = sub.add_parser("report", help="aggregate every game under a run directory")
    p.add_argument("run_dir")
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args(argv)
    if args.cmd == "play" and not args.power and not args.replay:
        ap.error("play needs --power (or --replay)")
    if (args.cmd != "resume" and getattr(args, "years", None) is not None
            and getattr(args, "max_year", None) is None and not args.replay):
        try:
            start = phase_year(new_game(args.map).get_current_phase())
        except Exception as e:  # unknown map name
            sys.exit(f"cannot load map {args.map!r}: {e}")
        args.max_year = start + args.years - 1
    board = None
    if getattr(args, "live", False):
        from .live import LiveBoard

        try:
            board = LiveBoard(port=args.live_port, delay=args.live_delay).start()
        except OSError as e:
            sys.exit(f"cannot start the live board on port {args.live_port} ({e}); try --live-port")
        args._board = board
        print(f"[diplo] live board: {board.url}  (open it in a browser)")
    try:
        args.fn(args)
    finally:
        if board is not None:
            if sys.stdin.isatty():
                try:
                    input("[diplo] game finished; the live board stays up. Press Enter to close it. ")
                except (EOFError, KeyboardInterrupt):
                    pass
            board.stop()


if __name__ == "__main__":
    main()

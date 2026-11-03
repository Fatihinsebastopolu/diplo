"""Readable views of game logs: a per-game transcript and a multi-game report."""

from __future__ import annotations

import json
from pathlib import Path

from .engine import POWERS


def load_events(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def transcript(events_path: Path) -> str:
    """Markdown transcript of one game: press, orders, rejections, center counts."""
    ev = load_events(events_path)
    out = ["# Game transcript", ""]
    current = None
    for e in ev:
        k = e["event"]
        if k == "game_start":
            out += [f"Agents: {e['agents']}", f"Config: {e.get('config')}", ""]
        phase = e.get("phase")
        if phase and phase != current and k in ("press", "orders", "agent_error"):
            current = phase
            out += ["", f"## {phase}", ""]
        if k == "press":
            prop = f"  \n  *proposed:* `{'; '.join(e['proposed_orders'])}`" if e["proposed_orders"] else ""
            bad = f"  \n  **illegal proposals:** `{'; '.join(e['illegal_proposals'])}`" if e["illegal_proposals"] else ""
            out.append(f"- **{e['sender']} -> {e['recipient']}** (round {e['round'] + 1}): {e['text']}{prop}{bad}")
        elif k == "orders":
            line = f"- **{e['power']}** orders: `{'; '.join(e['accepted']) or '(none / all hold)'}`"
            if e["first_pass_errors"]:
                errs = "; ".join(f"{o} ({why})" for o, why in e["first_pass_errors"])
                line += f"  \n  first attempt rejected: {errs}"
                line += "  \n  repaired: yes" if e["repaired"] else "  \n  repaired: no (illegal orders dropped)"
            if e.get("note"):
                line += f"  \n  note: {e['note']}"
            out.append(line)
        elif k == "agent_error":
            out.append(f"- **{e['power']}** agent error during {e['step']}: {e['error']}")
        elif k == "phase_result":
            counts = ", ".join(f"{p[:3]} {n}" for p, n in e["centers"].items())
            out += ["", f"*After {e['phase']}: centers {counts}*"]
        elif k == "game_end":
            out += ["", "## Final", "", "```json", json.dumps({kk: e[kk] for kk in ("centers", "solo_winner", "leaders", "usage")}, indent=2), "```"]
    return "\n".join(out) + "\n"


def aggregate(run_dir: Path) -> str:
    """Markdown table over every summary.json under run_dir."""
    summaries = [json.loads(p.read_text()) for p in sorted(Path(run_dir).rglob("summary.json"))]
    if not summaries:
        return f"No summary.json files found under {run_dir}\n"
    n = len(summaries)
    rows = []
    for p in POWERS:
        cs = [s["centers"].get(p, 0) for s in summaries]
        rows.append(
            (
                p,
                sum(cs) / n,
                sum(1 for s in summaries if s["solo_winner"] == p),
                sum(1 for s in summaries if p in s["leaders"]),
                sum(1 for s in summaries if p not in s["eliminated"]),
            )
        )
    tot = {k: sum(s["stats"][p][k] for s in summaries for p in s["stats"]) for k in summaries[0]["stats"]["FRANCE"]}
    costs = [s["usage"]["cost_usd"] for s in summaries if s.get("usage")]
    calls = [s["usage"]["calls"] for s in summaries if s.get("usage")]
    refusals = sum(s["usage"]["refusals"] for s in summaries if s.get("usage"))
    secs = [s["wall_seconds"] for s in summaries]

    def pct(a, b):
        return f"{100 * a / b:.1f}%" if b else "n/a"

    md = [f"# Run report: {run_dir}", "", f"Games: {n}; final phases: {sorted({s['final_phase'] for s in summaries})}", ""]
    md += ["## Outcomes by power", "", "| Power | Mean centers | Solo wins | Led / tied lead | Survived |", "|---|---|---|---|---|"]
    md += [f"| {p} | {m:.2f} | {w} | {l} | {sv}/{n} |" for p, m, w, l, sv in rows]
    md += ["", "## Legality", ""]
    md += [
        f"- Orders submitted: {tot['orders_submitted']}; illegal on first attempt: {tot['illegal_first_pass']} ({pct(tot['illegal_first_pass'], tot['orders_submitted'])})",
        f"- Order sets repaired after feedback: {tot['repairs_succeeded']}; illegal orders still dropped after repair: {tot['illegal_final']}",
        "- Illegal orders reaching the adjudicator: 0 (all orders are validated first)",
        f"- Orders proposed in negotiation: {tot['proposals']}; illegal: {tot['illegal_proposals']} ({pct(tot['illegal_proposals'], tot['proposals'])})",
        f"- Messages sent: {tot['messages_sent']}; agent failures (fell back to holds/no press): {tot['agent_failures']}",
    ]
    md += ["", "## Cost and time", ""]
    if costs:
        md += [
            f"- Estimated API cost per game: mean ${sum(costs) / len(costs):.3f}, max ${max(costs):.3f}",
            f"- API calls per game: mean {sum(calls) / len(calls):.0f}; refusals: {refusals}",
        ]
    else:
        md.append("- No LLM agents in these games (cost 0).")
    md.append(f"- Wall time per game: mean {sum(secs) / n:.0f}s, max {max(secs):.0f}s")
    md += ["", "## Messages sent per power", "", "| Power | Messages | Proposals | Illegal proposals |", "|---|---|---|---|"]
    for p in POWERS:
        md.append(
            f"| {p} | {sum(s['stats'][p]['messages_sent'] for s in summaries)} | "
            f"{sum(s['stats'][p]['proposals'] for s in summaries)} | "
            f"{sum(s['stats'][p]['illegal_proposals'] for s in summaries)} |"
        )
    return "\n".join(md) + "\n"

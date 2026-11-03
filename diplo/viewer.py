"""Rebuild a logged game phase by phase: board maps and promises vs actions.

Works from a game's events.jsonl alone (no API calls): the logged orders are
re-applied to a fresh engine, which is deterministic, so every phase's board
can be reconstructed and rendered. Nothing here changes what agents see, so
recordings made before this module existed still replay.
"""

from __future__ import annotations

import base64
import html
import json
from dataclasses import dataclass, field
from pathlib import Path

from diplomacy.engine.renderer import Renderer

from .engine import POWERS, new_game, normalize_order


@dataclass
class PhaseView:
    phase: str
    units: dict[str, list[str]]          # at the start of the phase
    centers: dict[str, int]              # at the start of the phase
    orders: dict[str, list[str]]         # accepted orders
    press: list[dict] = field(default_factory=list)
    results: dict[str, list[str]] = field(default_factory=dict)
    svg: str | None = None


def load_events(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def rebuild(events: list[dict], render: bool = True) -> tuple[list[PhaseView], str | None, dict]:
    """Return (phases, final_svg, final_centers). Raises if the log and engine disagree."""
    game = new_game()
    orders_by_phase: dict[str, dict[str, list[str]]] = {}
    press_by_phase: dict[str, list[dict]] = {}
    played: list[str] = []
    for e in events:
        if e["event"] == "orders":
            orders_by_phase.setdefault(e["phase"], {})[e["power"]] = e["accepted"]
        elif e["event"] == "press":
            press_by_phase.setdefault(e["phase"], []).append(e)
        elif e["event"] == "phase_result":
            played.append(e["phase"])
    logged_final = next((e["centers"] for e in reversed(events) if e["event"] == "phase_result"), None)

    views = []
    for phase in played:
        if game.get_current_phase() != phase:
            raise ValueError(f"log/engine mismatch: log has {phase}, engine is at {game.get_current_phase()}")
        orders = orders_by_phase.get(phase, {})
        for p, o in orders.items():
            game.set_orders(p, o)
        v = PhaseView(
            phase=phase,
            units={p: list(game.get_units(p)) for p in game.powers},
            centers={p: len(game.get_centers(p)) for p in game.powers},
            orders={p: list(orders.get(p, [])) for p in game.powers},
            press=press_by_phase.get(phase, []),
        )
        if render:
            v.svg = Renderer(game).render(incl_orders=True)
        game.process()
        v.results = {str(u): [str(r) for r in rs] for u, rs in game.result_history[phase].items() if rs}
        views.append(v)

    final_centers = {p: len(game.get_centers(p)) for p in game.powers}
    if logged_final is not None and final_centers != logged_final:
        raise ValueError(f"rebuilt final centers {final_centers} differ from the log {logged_final}")
    final_svg = Renderer(game).render(incl_orders=False) if render else None
    return views, final_svg, final_centers


# --------------------------------------------------------------------------- activity


def activity(views: list[PhaseView]) -> dict[str, dict[str, int]]:
    """Per power, over movement phases: what each unit did, and units it lost.

    A unit with no order counts as holding. 'dislodged' counts the power's own
    units that were dislodged.
    """
    t = {p: {"moves": 0, "supports": 0, "convoys": 0, "holds": 0, "dislodged": 0} for p in POWERS}
    for v in views:
        if not v.phase.endswith("M"):
            continue
        for p, units in v.units.items():
            by_unit = {_unit_of(o): o for o in v.orders.get(p, [])}
            for u in units:
                o = by_unit.get(u) or by_unit.get(_unit_base_match(u, by_unit))
                if o is None or o.endswith(" H"):
                    t[p]["holds"] += 1
                elif " S " in o:
                    t[p]["supports"] += 1
                elif " C " in o:
                    t[p]["convoys"] += 1
                else:
                    t[p]["moves"] += 1
        for unit, res in v.results.items():
            if any("dislodged" in r for r in res):
                owner = _owner(unit, v.units)
                if owner:
                    t[owner]["dislodged"] += 1
    return t


def _unit_base_match(unit: str, by_unit: dict) -> str | None:
    base = unit.split()[1].split("/")[0]
    for u in by_unit:
        if u and u.split()[1].split("/")[0] == base:
            return u
    return None


# --------------------------------------------------------------------------- PNG export


def _raster_safe(svg: str) -> str:
    """Inline the few CSS rules cairosvg mis-renders (the score box and the labels).

    The renderer styles these with CSS classes and em font sizes; cairosvg draws
    the box black and drops the text, so they are given explicit attributes.
    """
    return (svg.replace('class="currentnoterect"', 'fill="#c5dfea"')
               .replace('class="currentnotetext"', 'font-family="serif" font-size="24" fill="black"')
               .replace('class="currentphasetext"', 'font-family="serif" font-size="40" fill="black"'))


def svg_to_png(svg: str, path: Path, width: int = 1200) -> bool:
    """Write a PNG of a board SVG. Returns False if cairo is not available."""
    try:
        import cairosvg
    except (ImportError, OSError):  # OSError: cairosvg installed but the system cairo library is missing
        return False
    cairosvg.svg2png(bytestring=_raster_safe(svg).encode(), write_to=str(path), output_width=width)
    return True


# --------------------------------------------------------------------------- promises vs actions


def _unit_of(order: str) -> str | None:
    parts = normalize_order(order).split()
    if len(parts) >= 2 and parts[0] in ("A", "F"):
        return f"{parts[0]} {parts[1]}"
    return None


def _owner(unit: str, units: dict[str, list[str]]) -> str | None:
    for p, us in units.items():
        if unit in us:
            return p
    base = unit.split()[1].split("/")[0]
    for p, us in units.items():  # tolerate a missing or extra coast
        if any(u.split()[0] == unit.split()[0] and u.split()[1].split("/")[0] == base for u in us):
            return p
    return None


def _actual(unit: str, owner: str, orders: dict[str, list[str]]) -> str:
    base = unit.split()[1].split("/")[0]
    for o in orders.get(owner, []):
        u = _unit_of(o)
        if u and u.split()[1].split("/")[0] == base:
            return o
    return f"{unit} H"  # no order means the unit holds


@dataclass
class Claim:
    phase: str
    kind: str          # "own" (sender's unit) or "request" (recipient's unit)
    speaker: str       # who proposed it
    actor: str         # whose unit it is
    unit: str
    proposed: str
    told: list[str]    # recipients who were told this
    actual: str
    kept: bool


def analyse_promises(view: PhaseView) -> list[Claim]:
    """Compare orders proposed in messages with what the unit actually did.

    Only the latest proposal each speaker sent each recipient about a unit is
    used, so a plan revised in a later round is judged on the revision.
    """
    latest: dict[tuple[str, str, str], tuple[str, str]] = {}  # (speaker, recipient, unit) -> (order, kind)
    for m in view.press:
        for o in m.get("proposed_orders", []):
            unit = _unit_of(o)
            if not unit:
                continue
            owner = _owner(unit, view.units)
            if owner == m["sender"]:
                kind = "own"
            elif owner == m["recipient"]:
                kind = "request"
            else:
                continue  # suggestions about third parties are not commitments
            latest[(m["sender"], m["recipient"], unit)] = (normalize_order(o), kind)

    grouped: dict[tuple[str, str, str, str], list[str]] = {}
    for (speaker, recipient, unit), (order, kind) in latest.items():
        grouped.setdefault((speaker, unit, order, kind), []).append(recipient)

    claims = []
    for (speaker, unit, order, kind), told in sorted(grouped.items()):
        actor = speaker if kind == "own" else told[0]  # a request goes to the unit's one owner
        actual = _actual(unit, actor, view.orders)
        claims.append(Claim(view.phase, kind, speaker, actor, unit, order, sorted(told), actual,
                            kept=normalize_order(actual) == order))
    return claims


def contradictions(claims: list[Claim]) -> list[tuple[str, str, dict[str, list[str]]]]:
    """Units for which a speaker told different powers different things."""
    by_unit: dict[tuple[str, str], dict[str, list[str]]] = {}
    for c in claims:
        if c.kind == "own":
            by_unit.setdefault((c.speaker, c.unit), {})[c.proposed] = c.told
    return [(s, u, v) for (s, u), v in sorted(by_unit.items()) if len(v) > 1]


def promise_totals(all_claims: list[Claim]) -> dict[str, dict[str, int]]:
    t = {p: {"own": 0, "own_kept": 0, "requests": 0, "requests_honored": 0} for p in POWERS}
    for c in all_claims:
        if c.kind == "own":
            t[c.speaker]["own"] += 1
            t[c.speaker]["own_kept"] += int(c.kept)
        else:
            t[c.actor]["requests"] += 1
            t[c.actor]["requests_honored"] += int(c.kept)
    return t


def promises_markdown(view: PhaseView) -> str:
    claims = analyse_promises(view)
    if not claims:
        return ""
    lines = ["", f"**Promised vs did ({view.phase})**", ""]
    for c in claims:
        mark = "kept" if c.kept else "NOT kept"
        if c.kind == "own":
            lines.append(f"- {c.speaker} told {', '.join(c.told)}: `{c.proposed}`; did `{c.actual}` ({mark})")
        else:
            lines.append(f"- {c.speaker} asked {c.actor}: `{c.proposed}`; {c.actor} did `{c.actual}` ({'honored' if c.kept else 'not honored'})")
    for speaker, unit, variants in contradictions(claims):
        told = "; ".join(f"`{o}` to {', '.join(r)}" for o, r in variants.items())
        lines.append(f"- **{speaker} told different powers different plans for {unit}:** {told}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- HTML viewer

POWER_COLORS = {
    "AUSTRIA": "#c48f85", "ENGLAND": "#8a2be2", "FRANCE": "#4169e1", "GERMANY": "#a08a75",
    "ITALY": "#228b22", "RUSSIA": "#757d91", "TURKEY": "#b9a61c",
}

CSS = """
:root { --bg:#fafaf7; --fg:#1d1d1b; --muted:#6b6b66; --card:#ffffff; --line:#e3e1da; --ok:#1f7a3a; --bad:#b3261e; }
@media (prefers-color-scheme: dark) { :root { --bg:#161614; --fg:#ecebe6; --muted:#a3a29b; --card:#1f1f1c; --line:#33322e; --ok:#6fcf8a; --bad:#ff8a80; } }
* { box-sizing: border-box; }
body { margin:0; font:15px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
header { padding:20px 16px 8px; max-width:1200px; margin:auto; }
h1 { margin:0 0 4px; font-size:22px; } .muted { color:var(--muted); }
nav { position:sticky; top:0; z-index:2; background:var(--bg); border-bottom:1px solid var(--line); padding:8px 16px; overflow-x:auto; white-space:nowrap; }
nav a { display:inline-block; padding:3px 8px; margin-right:4px; border-radius:6px; color:var(--fg); text-decoration:none; font-size:13px; border:1px solid var(--line); }
nav a:hover { background:var(--card); }
main { max-width:1200px; margin:auto; padding:8px 16px 48px; }
section { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin:16px 0; scroll-margin-top:56px; }
section h2 { margin:0 0 8px; font-size:18px; }
.grid { display:grid; grid-template-columns: minmax(0,3fr) minmax(0,2fr); gap:16px; }
@media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
.map { position:sticky; top:60px; align-self:start; }
@media (max-width: 900px) { .map { position:static; } }
td code { white-space:nowrap; }
.map img { width:100%; height:auto; border-radius:6px; border:1px solid var(--line); background:#fff; }
table { border-collapse:collapse; width:100%; font-size:13px; }
th, td { text-align:left; padding:4px 6px; border-bottom:1px solid var(--line); vertical-align:top; }
code { font-size:12px; }
.ok { color:var(--ok); font-weight:600; } .bad { color:var(--bad); font-weight:600; }
.dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:5px; vertical-align:middle; }
details { margin-top:10px; } summary { cursor:pointer; color:var(--muted); }
.msg { margin:6px 0; padding-left:8px; border-left:3px solid var(--line); }
.warn { color:var(--bad); }
"""


def _img(svg: str, alt: str) -> str:
    data = base64.b64encode(svg.encode()).decode()
    return f'<img alt="{html.escape(alt)}" src="data:image/svg+xml;base64,{data}">'


def _power(p: str) -> str:
    return f'<span class="dot" style="background:{POWER_COLORS.get(p, "#999")}"></span>{p.title()}'


def render_html(events: list[dict], views: list[PhaseView], final_svg: str, final_centers: dict) -> str:
    start = next((e for e in events if e["event"] == "game_start"), {})
    end = next((e for e in events if e["event"] == "game_end"), {})
    cfg = start.get("config", {})
    all_claims = [c for v in views for c in analyse_promises(v)]
    totals = promise_totals(all_claims)
    usage = end.get("usage") or {}

    def pct(a, b):
        return f"{100 * a / b:.0f}%" if b else "–"

    out = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
           "<meta name='viewport' content='width=device-width, initial-scale=1'>",
           "<title>Diplomacy game viewer</title>", f"<style>{CSS}</style></head><body>"]
    desc = str(cfg.get("model", "?"))
    if cfg.get("replayed"):
        desc += " (replayed)"
    out.append(f"<header><h1>Diplomacy game</h1><div class='muted'>Agents: {html.escape(desc)}; "
               f"press rounds {start.get('press_rounds')}; through {start.get('max_year')}; "
               f"{len(views)} phases; est. cost ${usage.get('cost_usd', 0):.3f}</div></header>")
    out.append("<nav>" + "".join(f"<a href='#{v.phase}'>{v.phase}</a>" for v in views) +
               "<a href='#final'>Final</a><a href='#promises'>Promises</a></nav><main>")

    for v in views:
        claims = analyse_promises(v)
        rows = "".join(
            f"<tr><td>{_power(p)}</td><td>{v.centers[p]}</td><td><code>{html.escape('; '.join(v.orders[p])) or '–'}</code></td></tr>"
            for p in POWERS if v.units.get(p) or v.orders.get(p)
        )
        out.append(f"<section id='{v.phase}'><h2>{v.phase}</h2><div class='grid'>")
        out.append(f"<div class='map'>{_img(v.svg, 'Board at the start of ' + v.phase + ' with orders')}"
                   f"<div class='muted'>Board at the start of {v.phase}, with the orders that were submitted.</div></div><div>")
        out.append(f"<table><tr><th>Power</th><th>Centers</th><th>Orders</th></tr>{rows}</table>")
        if claims:
            out.append("<h3>Promised vs did</h3><table><tr><th>Who</th><th>Said</th><th>Did</th><th></th></tr>")
            for c in claims:
                if c.kind == "own":
                    who = f"{_power(c.speaker)} <span class='muted'>to {', '.join(x.title() for x in c.told)}</span>"
                    verdict = "<span class='ok'>kept</span>" if c.kept else "<span class='bad'>not kept</span>"
                else:
                    who = f"{_power(c.speaker)} <span class='muted'>asked {c.actor.title()}</span>"
                    verdict = "<span class='ok'>honored</span>" if c.kept else "<span class='bad'>not honored</span>"
                out.append(f"<tr><td>{who}</td><td><code>{html.escape(c.proposed)}</code></td>"
                           f"<td><code>{html.escape(c.actual)}</code></td><td>{verdict}</td></tr>")
            out.append("</table>")
            for speaker, unit, variants in contradictions(claims):
                told = "; ".join(f"{html.escape(o)} to {', '.join(r)}" for o, r in variants.items())
                out.append(f"<p class='warn'>{speaker.title()} told different powers different plans for {unit}: {told}</p>")
        if v.press:
            out.append(f"<details><summary>{len(v.press)} messages</summary>")
            for m in v.press:
                prop = f"<br><code>{html.escape('; '.join(m['proposed_orders']))}</code>" if m["proposed_orders"] else ""
                out.append(f"<div class='msg'><b>{m['sender'].title()} → {m['recipient'].title()}</b> "
                           f"<span class='muted'>round {m['round'] + 1}</span><br>{html.escape(m['text'])}{prop}</div>")
            out.append("</details>")
        out.append("</div></div></section>")

    counts = "".join(f"<tr><td>{_power(p)}</td><td>{final_centers[p]}</td></tr>" for p in POWERS)
    out.append(f"<section id='final'><h2>Final board</h2><div class='grid'><div class='map'>{_img(final_svg, 'Final board')}</div>"
               f"<div><table><tr><th>Power</th><th>Centers</th></tr>{counts}</table></div></div></section>")

    rows = "".join(
        f"<tr><td>{_power(p)}</td><td>{t['own_kept']}/{t['own']} ({pct(t['own_kept'], t['own'])})</td>"
        f"<td>{t['requests_honored']}/{t['requests']} ({pct(t['requests_honored'], t['requests'])})</td></tr>"
        for p, t in totals.items()
    )
    out.append("<section id='promises'><h2>Promises vs actions, whole game</h2>"
               "<p class='muted'>Own proposals: orders a power proposed in a message for its own unit, judged on the "
               "latest version sent to each recipient. Requests: orders other powers proposed for this power's units. "
               "A proposal is a suggestion as much as a promise, and only exact order matches count.</p>"
               f"<table><tr><th>Power</th><th>Own proposals carried out</th><th>Requests honored</th></tr>{rows}</table></section>")
    out.append("</main></body></html>")
    return "\n".join(out)


def build_viewer(game_dir: Path) -> Path:
    """Write game.html and maps/*.svg into a game directory; return the HTML path."""
    game_dir = Path(game_dir)
    events = load_events(game_dir / "events.jsonl")
    views, final_svg, final_centers = rebuild(events, render=True)
    maps = game_dir / "maps"
    maps.mkdir(exist_ok=True)
    boards = [(f"{i:02d}-{v.phase}", v.svg) for i, v in enumerate(views)]
    boards.append((f"{len(views):02d}-final", final_svg))
    png_ok = True
    for name, svg in boards:
        (maps / f"{name}.svg").write_text(svg)
        if png_ok:
            png_ok = svg_to_png(svg, maps / f"{name}.png")
    if not png_ok:
        print("[diplo]   note: PNG maps skipped (install cairosvg and the system cairo library to get them)")
    out = game_dir / "game.html"
    out.write_text(render_html(events, views, final_svg, final_centers), encoding="utf-8")
    return out

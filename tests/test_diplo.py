import json
import random
from types import SimpleNamespace

import pytest

from diplo.agents import LLMAgent, RandomAgent
from diplo.engine import check_proposals, all_legal_orders, new_game, snapshot, validate_orders
from diplo import cli
from diplo.llm import AnthropicClient, LLMError, ReplayClient, ReplayMiss
from diplo.report import aggregate, transcript
from diplo.runner import GameRunner


# ----------------------------------------------------------------- order validation

def test_legal_opening_orders_accepted():
    g = new_game()
    v = validate_orders(["A PAR - BUR", "a mar  - spa", "F BRE - MAO"], snapshot(g, "FRANCE"))
    assert v.errors == []
    assert v.accepted == ["A PAR - BUR", "A MAR - SPA", "F BRE - MAO"]


@pytest.mark.parametrize(
    "order",
    [
        "A PAR - MUN",   # not adjacent
        "A MAR - LYO",   # army into a sea province
        "F BRE - PAR",   # fleet inland
        "A BER - KIE",   # another power's unit
        "A PAR - BUR VIA CONVOY PLEASE",
    ],
)
def test_illegal_orders_rejected(order):
    v = validate_orders([order], snapshot(new_game(), "FRANCE"))
    assert v.accepted == [] and len(v.errors) == 1


def test_one_order_per_unit():
    v = validate_orders(["A PAR - BUR", "A PAR - PIC"], snapshot(new_game(), "FRANCE"))
    assert v.accepted == ["A PAR - BUR"] and len(v.errors) == 1


def test_proposal_check_covers_all_powers():
    g = new_game()
    good, bad = check_proposals(["A BER - KIE", "A MUN - PAR"], all_legal_orders(g))
    assert good == ["A BER - KIE"] and bad == ["A MUN - PAR"]


# ----------------------------------------------------------------- fake Claude API

class FakeAnthropic:
    """Mimics client.messages.create / client.beta.messages.create."""

    def __init__(self, responder):
        self.responder = responder
        self.requests = []
        create = SimpleNamespace(create=self._create)
        self.messages = create
        self.beta = SimpleNamespace(messages=create)

    def _create(self, **kw):
        self.requests.append(kw)
        out = self.responder(kw)
        stop = "end_turn"
        if out == "REFUSE":
            stop, out = "refusal", {}
        return SimpleNamespace(
            stop_reason=stop,
            content=[SimpleNamespace(type="text", text=json.dumps(out))],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
        )


def is_orders_request(kw):
    return "orders" in kw["output_config"]["format"]["schema"]["properties"]


def test_llm_client_request_shape_and_cost():
    fake = FakeAnthropic(lambda kw: {"orders": [], "private_notes": ""})
    llm = AnthropicClient("claude-opus-5-5", "low", client=fake)
    llm.json_call("sys", "user", {"type": "object"})
    kw = fake.requests[0]
    assert kw["fallbacks"] == "default" and kw["output_config"]["effort"] == "low"
    assert "thinking" not in kw and "temperature" not in kw
    assert llm.usage.cost_usd == pytest.approx((1000 * 4 + 200 * 20) / 1e6)


def test_refusal_raises():
    llm = AnthropicClient("claude-opus-5-5", client=FakeAnthropic(lambda kw: "REFUSE"))
    with pytest.raises(LLMError):
        llm.json_call("s", "u", {})
    assert llm.usage.refusals == 1


def test_repair_round_fixes_illegal_order():
    calls = []

    def responder(kw):
        calls.append(kw)
        if len(calls) == 1:
            return {"orders": ["A PAR - MUN", "A MAR - SPA"], "private_notes": "n1"}
        assert "rejected" in kw["messages"][0]["content"]
        return {"orders": ["A PAR - BUR", "A MAR - SPA", "F BRE - MAO"], "private_notes": "n2"}

    agent = LLMAgent("FRANCE", AnthropicClient(client=FakeAnthropic(responder)))
    oc = agent.orders(snapshot(new_game(), "FRANCE"), [])
    assert oc.repaired and oc.final_errors == [] and len(oc.first_pass_errors) == 1
    assert oc.accepted == ["A PAR - BUR", "A MAR - SPA", "F BRE - MAO"]
    assert agent.notes == "n2"


# ----------------------------------------------------------------- whole games

def scripted_responder(kw):
    """Plays legal-ish moves: proposes one illegal order, submits holds plus one bad order."""
    content = kw["messages"][0]["content"]
    if is_orders_request(kw):
        holds = [line.split(": ")[1].split(" | ")[0] for line in content.split("## Your legal orders")[1].splitlines()
                 if ": " in line and " | " in line]
        return {"orders": holds + ["A XXX - YYY"], "private_notes": "holding"}
    to = kw["output_config"]["format"]["schema"]["properties"]["messages"]["items"]["properties"]["to"]["enum"][0]
    if "Problem with your drafted messages" in content:  # repair round: propose one of our own legal orders
        legal = content.split("## Your legal orders this phase\n")[1].splitlines()[0]
        order = legal.split(": ", 1)[1].split(" | ")[0] if ": " in legal and " | " in legal else ""
        return {"messages": [{"to": to, "text": "Peace?", "proposed_orders": [order] if order else []}],
                "private_notes": ""}
    return {"messages": [{"to": to, "text": "Peace?", "proposed_orders": ["A PAR - MUN"]}], "private_notes": ""}


def test_full_game_with_llm_agents_and_silent_power(tmp_path):
    llm = AnthropicClient(client=FakeAnthropic(scripted_responder))
    agents = {p: LLMAgent(p, llm, negotiates=(p != "ENGLAND")) for p in new_game().powers}
    s = GameRunner(agents, tmp_path, max_year=1902, press_rounds=1, llm=llm).run()
    assert s["final_phase"] == "S1903M"
    st = s["stats"]
    assert st["ENGLAND"]["messages_sent"] == 0 and st["FRANCE"]["messages_sent"] > 0
    assert st["FRANCE"]["illegal_proposals_first_pass"] > 0 and st["FRANCE"]["proposal_repairs_succeeded"] > 0
    assert sum(x["illegal_proposals"] for x in st.values()) == 0  # repaired before delivery
    assert st["FRANCE"]["illegal_first_pass"] > 0 and st["FRANCE"]["illegal_final"] > 0
    assert s["usage"]["calls"] > 0 and s["usage"]["cost_usd"] > 0
    md = transcript(tmp_path / "events.jsonl")
    assert "first attempt rejected" in md
    assert "Mean centers" in aggregate(tmp_path)


def test_agent_failure_does_not_stall_game(tmp_path):
    llm = AnthropicClient(client=FakeAnthropic(lambda kw: "REFUSE"))
    agents = {p: RandomAgent(p, random.Random(1)) for p in new_game().powers}
    agents["FRANCE"] = LLMAgent("FRANCE", llm)
    s = GameRunner(agents, tmp_path, max_year=1901, press_rounds=1, llm=llm).run()
    assert s["final_phase"] == "S1902M"
    assert s["stats"]["FRANCE"]["agent_failures"] > 0


def test_random_games_reach_adjustments(tmp_path):
    agents = {p: RandomAgent(p, random.Random(i)) for i, p in enumerate(new_game().powers)}
    s = GameRunner(agents, tmp_path, max_year=1904, press_rounds=0).run()
    assert s["phases_played"] >= 10
    assert sum(s["centers"].values()) >= 22


def test_default_model_is_haiku_without_fallback_param():
    fake = FakeAnthropic(lambda kw: {"orders": [], "private_notes": ""})
    AnthropicClient(client=fake).json_call("s", "u", {})
    assert fake.requests[0]["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in fake.requests[0]


# ----------------------------------------------------------------- record / replay

def _strip_config(md):
    return "\n".join(l for l in md.splitlines() if not l.startswith("Config:"))


def test_record_then_replay_reproduces_game(tmp_path, monkeypatch):
    live_calls = []

    def live(args):
        def responder(kw):
            live_calls.append(1)
            return scripted_responder(kw)
        return AnthropicClient(args.model, args.effort, client=FakeAnthropic(responder))

    monkeypatch.setattr(cli, "_live_client", live)
    cli.main(["sim", "--max-year", "1902", "--press-rounds", "1", "--random", "ITA,TUR",
              "--silent", "ENG", "--seed", "3", "--out", str(tmp_path / "a")])
    n_live = len(live_calls)
    assert n_live > 0
    run = next((tmp_path / "a").iterdir())
    assert (run / "game-001" / "recording.jsonl").exists()

    def no_network(args):
        raise AssertionError("replay must not call the API")

    monkeypatch.setattr(cli, "_live_client", no_network)
    cli.main(["sim", "--replay", str(run), "--out", str(tmp_path / "b")])
    replay = next((tmp_path / "b").iterdir())
    a = (run / "game-001" / "transcript.md").read_text()
    b = (replay / "game-001" / "transcript.md").read_text()
    assert _strip_config(a) == _strip_config(b)
    assert len(live_calls) == n_live


def test_replay_divergence_is_reported():
    client = ReplayClient("claude-haiku-5-5", {})
    with pytest.raises(ReplayMiss):
        client.json_call("s", "u", {})


# ----------------------------------------------------------------- viewer / promises

from diplo.viewer import PhaseView, analyse_promises, build_viewer, contradictions, promise_totals, rebuild, load_events


def _msg(sender, recipient, orders, rnd=0):
    return {"sender": sender, "recipient": recipient, "proposed_orders": orders, "round": rnd, "text": "", "phase": "S1901M"}


def test_promise_analysis():
    g = new_game()
    view = PhaseView(
        phase="S1901M",
        units={p: list(g.get_units(p)) for p in g.powers},
        centers={},
        orders={"FRANCE": ["A PAR - BUR"], "ENGLAND": ["F LON - NTH"]},
        press=[
            _msg("FRANCE", "ENGLAND", ["A PAR - PIC"], rnd=0),   # superseded by round 1
            _msg("FRANCE", "ENGLAND", ["A PAR - BUR"], rnd=1),
            _msg("FRANCE", "GERMANY", ["A PAR H", "A MUN - BUR"]),  # lie to Germany; request to Germany
            _msg("FRANCE", "ENGLAND", ["F LON - ENG", "A BER - KIE"]),  # request; third-party ignored
        ],
    )
    claims = {(c.kind, c.proposed): c for c in analyse_promises(view)}
    assert claims[("own", "A PAR - BUR")].kept and claims[("own", "A PAR - BUR")].told == ["ENGLAND"]
    assert not claims[("own", "A PAR H")].kept
    assert ("own", "A PAR - PIC") not in claims
    assert not claims[("request", "F LON - ENG")].kept and claims[("request", "F LON - ENG")].actual == "F LON - NTH"
    assert claims[("request", "A MUN - BUR")].actual == "A MUN H"   # no order = hold
    assert ("request", "A BER - KIE") not in claims
    [(speaker, unit, variants)] = contradictions(list(claims.values()))
    assert speaker == "FRANCE" and unit == "A PAR" and set(variants) == {"A PAR - BUR", "A PAR H"}
    t = promise_totals(list(claims.values()))
    assert t["FRANCE"] == {"own": 2, "own_kept": 1, "requests": 0, "requests_honored": 0}
    assert t["ENGLAND"]["requests"] == 1 and t["GERMANY"]["requests"] == 1


def test_viewer_rebuilds_logged_game(tmp_path):
    llm = AnthropicClient(client=FakeAnthropic(scripted_responder))
    agents = {p: LLMAgent(p, llm) for p in new_game().powers}
    agents["ITALY"] = RandomAgent("ITALY", random.Random(5))
    s = GameRunner(agents, tmp_path, max_year=1902, press_rounds=1, llm=llm).run()
    views, final_svg, centers = rebuild(load_events(tmp_path / "events.jsonl"))
    assert centers == s["centers"] and len(views) == s["phases_played"]
    out = build_viewer(tmp_path)
    html_text = out.read_text()
    assert "Promised vs did" in html_text and "data:image/svg+xml;base64," in html_text
    assert len(list((tmp_path / "maps").glob("*.svg"))) == len(views) + 1


def test_png_export_is_optional(tmp_path, monkeypatch):
    import sys
    from diplo.viewer import svg_to_png
    monkeypatch.setitem(sys.modules, "cairosvg", None)  # import now raises ImportError
    assert svg_to_png("<svg/>", tmp_path / "x.png") is False
    assert not (tmp_path / "x.png").exists()


def test_live_board_serves_state_and_hides_other_powers_press(tmp_path):
    import urllib.request
    from diplo.live import LiveBoard

    board = LiveBoard(port=0, viewer_power="FRANCE").start()
    try:
        llm = AnthropicClient(client=FakeAnthropic(scripted_responder))
        agents = {p: LLMAgent(p, llm) for p in new_game().powers}
        GameRunner(agents, tmp_path, max_year=1901, press_rounds=1, llm=llm, observer=board).run()
        state = json.loads(urllib.request.urlopen(board.url + "state").read())
        assert state["status"] == "game over" and state["phase"] == "S1902M"
        assert state["messages"] and all("FRANCE" in (m["sender"], m["recipient"]) for m in state["messages"])
        assert all(r.split(": ", 1)[1] for r in state["results"])
        svg = urllib.request.urlopen(board.url + "board.svg").read()
        assert b"<svg" in svg
        assert b"Diplomacy live board" in urllib.request.urlopen(board.url).read()
    finally:
        board.stop()


def test_max_cost_stops_game_and_replay_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_live_client",
                        lambda a: AnthropicClient(a.model, a.effort, client=FakeAnthropic(scripted_responder)))
    cli.main(["sim", "--max-year", "1905", "--press-rounds", "1", "--max-cost", "0.01",
              "--seed", "1", "--out", str(tmp_path / "a")])
    run = next((tmp_path / "a").iterdir())
    s = json.loads((run / "game-001" / "summary.json").read_text())
    assert s["stopped_reason"].startswith("cost limit") and s["final_phase"] != "S1906M"
    monkeypatch.setattr(cli, "_live_client", None)
    cli.main(["sim", "--replay", str(run), "--out", str(tmp_path / "b")])
    r = json.loads(next((tmp_path / "b").rglob("summary.json")).read_text())
    assert r["final_phase"] == s["final_phase"] and r["stopped_reason"] == s["stopped_reason"]


# ----------------------------------------------------------------- proposal checks / activity

from diplo.agents import Press, format_press, HumanAgent
from diplo.engine import extract_orders
from diplo.viewer import activity


def test_extract_orders_from_human_text():
    locs = {l.upper() for l in new_game().map.locs}
    text = "ok: a par - bur, and you A Mun S a PAR - BUR. A few ideas; F STP/SC - BOT. Burgundy? A XYZ - PAR"
    assert extract_orders(text, locs) == ["A PAR - BUR", "A MUN S A PAR - BUR", "F STP/SC - BOT"]


def test_illegal_proposals_are_annotated_for_both_sides():
    m = Press("S1901M", 0, "GERMANY", "FRANCE", "Let's do this", ["A MUN - PAR"], ["A MUN - PAR"])
    for viewer in ("FRANCE", "GERMANY"):
        assert "[engine note: not a legal order this phase: A MUN - PAR]" in format_press([m], viewer)


def test_human_proposals_are_checked(tmp_path):
    lines = iter(["GERMANY: please do A MUN - PAR and I'll take Burgundy with a par - bur", "", ""])
    human = HumanAgent("FRANCE", input_fn=lambda prompt="": next(lines, ""), print_fn=lambda *a: None)
    agents = {p: RandomAgent(p, random.Random(2)) for p in new_game().powers}
    agents["FRANCE"] = human
    GameRunner(agents, tmp_path, max_year=1901, press_rounds=1).run()
    press = [e for e in load_events(tmp_path / "events.jsonl") if e["event"] == "press"]
    assert sorted(press[0]["proposed_orders"]) == ["A MUN - PAR", "A PAR - BUR"]
    assert press[0]["illegal_proposals"] == ["A MUN - PAR"]


def test_activity_counts_unordered_units_as_holds():
    g = new_game()
    view = PhaseView("S1901M", {p: list(g.get_units(p)) for p in g.powers}, {},
                     {"FRANCE": ["A PAR - BUR", "A MAR S A PAR - BUR"]})
    a = activity([view])
    assert a["FRANCE"] == {"moves": 1, "supports": 1, "ally_supports": 0, "convoys": 0, "holds": 1, "dislodged": 0}
    assert a["RUSSIA"]["holds"] == 4



def test_message_still_illegal_after_repair_is_dropped():
    calls = []

    def responder(kw):
        calls.append(kw)
        # text mentions an illegal support; the structured list is empty, so only text extraction catches it
        return {"messages": [{"to": "ENGLAND", "text": "I'll do A SER S A BUL - CON for you", "proposed_orders": []},
                             {"to": "GERMANY", "text": "Hello", "proposed_orders": ["A PAR - BUR"]}],
                "private_notes": ""}

    agent = LLMAgent("FRANCE", AnthropicClient(client=FakeAnthropic(responder)))
    out = agent.negotiate(snapshot(new_game(), "FRANCE"), [], 0, 1)
    assert len(calls) == 2  # draft + one repair
    assert "That unit's legal orders are" in calls[1]["messages"][0]["content"] or "there is no army" in calls[1]["messages"][0]["content"]
    assert [m["to"] for m in out] == ["GERMANY"]
    assert agent.last_negotiation == {"first_pass_illegal": 1, "repaired": False, "dropped": 1,
                                      "illegal": ["A SER S A BUL - CON"]}



def test_text_mentions_tolerate_missing_coast_but_structured_proposals_do_not():
    from diplo.engine import illegal_message_orders
    g = new_game()
    legal = {o for os_ in g.get_all_possible_orders().values() for o in os_}
    locs = {l.upper() for l in g.map.locs}
    # F STP/SC - BOT is legal; written without the coast in text it is accepted, but not as a structured proposal
    assert illegal_message_orders("your F STP - BOT would bounce", [], legal, locs) == []
    assert illegal_message_orders("hi", ["F STP - BOT"], legal, locs) == ["F STP - BOT"]
    assert illegal_message_orders("I'll do A SER S A BUL - CON", [], legal, locs) == ["A SER S A BUL - CON"]



def test_activity_counts_support_for_another_power():
    g = new_game()
    view = PhaseView("S1901M", {p: list(g.get_units(p)) for p in g.powers}, {},
                     {"GERMANY": ["A MUN S A PAR - BUR"], "FRANCE": ["A PAR - BUR"]})
    assert activity([view])["GERMANY"]["ally_supports"] == 1


# ----------------------------------------------------------------- relationship ledger

from diplo.relations import Ledger


def test_ledger_records_promises_attacks_and_joint_attacks():
    g = new_game()
    units = {p: list(g.get_units(p)) for p in g.powers}
    owners = {c: p for p in g.powers for c in g.get_centers(p)}
    view = PhaseView(
        phase="S1901M", units=units, centers={},
        orders={"FRANCE": ["A PAR - BUR", "F BRE - PIC"], "GERMANY": ["A MUN S A PAR - BUR"],
                "ITALY": ["A VEN - TRI"], "AUSTRIA": []},
        press=[_msg("FRANCE", "ENGLAND", ["F BRE - MAO"]),          # broken: France did F BRE - PIC
               _msg("FRANCE", "GERMANY", ["A PAR - BUR"]),          # kept
               _msg("GERMANY", "FRANCE", ["A MUN S A PAR - BUR"]),  # kept
               _msg("ENGLAND", "FRANCE", ["F BRE H"])],             # request, not honored
        results={},
    )
    led = Ledger()
    led.update(view, owners)
    eng = led.text_for("ENGLAND")
    assert "FRANCE: kept 0 of 1 promises to you (broke S1901M: said F BRE - MAO, did F BRE - PIC)" in eng
    assert "did 0 of 1 things you asked" in eng
    ger = led.text_for("GERMANY")
    assert "FRANCE: kept 1 of 1 promises to you" in ger and "fought alongside you 1 time(s)" in ger
    assert "attacked you 1 time(s) (last S1901M: A VEN - TRI)" in led.text_for("AUSTRIA")
    assert "you kept 1 of 2 promises to them" not in led.text_for("FRANCE")
    assert "ENGLAND: you kept 0 of 1 promises to them." in led.text_for("FRANCE")
    assert led.text_for("RUSSIA") == "No promises, requests or clashes recorded yet."


def test_ledger_reaches_agent_prompts(tmp_path):
    seen = []

    def responder(kw):
        seen.append(kw["messages"][0]["content"])
        return scripted_responder(kw)

    llm = AnthropicClient(client=FakeAnthropic(responder))
    agents = {p: LLMAgent(p, llm) for p in new_game().powers}
    GameRunner(agents, tmp_path, max_year=1901, press_rounds=1, llm=llm).run()
    assert all("## Relationships (computed by the game" in c for c in seen)
    fall = [c for c in seen if "Current phase: F1901M" in c]
    assert fall and any("promises to you" in c for c in fall)



def test_promised_hold_is_kept_by_staying_in_place():
    g = new_game()
    view = PhaseView("S1901M", {p: list(g.get_units(p)) for p in g.powers}, {},
                     {"RUSSIA": ["F SEV S A MOS", "A WAR - GAL"]},
                     press=[_msg("RUSSIA", "TURKEY", ["F SEV H", "A WAR H"])])
    kept = {c.proposed: c.kept for c in analyse_promises(view)}
    assert kept == {"F SEV H": True, "A WAR H": False}


def test_human_player_sees_ledger_in_terminal_and_live_board(tmp_path):
    import urllib.request
    from diplo.live import LiveBoard
    printed = []
    # France promises England to hold Brest, then moves it; England is the human
    script = iter(["FRANCE: hi", "", "", "", "", "", "", ""])
    human = HumanAgent("ENGLAND", input_fn=lambda prompt="": next(script, ""), print_fn=printed.append)

    def responder(kw):
        c = kw["messages"][0]["content"]
        if "orders" in kw["output_config"]["format"]["schema"]["properties"]:
            return {"orders": ["F BRE - MAO"] if "You are FRANCE" in c else [], "private_notes": ""}
        to = kw["output_config"]["format"]["schema"]["properties"]["messages"]["items"]["properties"]["to"]["enum"]
        if "You are FRANCE" in c and "ENGLAND" in to:
            return {"messages": [{"to": "ENGLAND", "text": "I'll hold Brest.", "proposed_orders": ["F BRE H"]}],
                    "private_notes": ""}
        return {"messages": [], "private_notes": ""}

    llm = AnthropicClient(client=FakeAnthropic(responder))
    agents = {p: LLMAgent(p, llm) for p in new_game().powers}
    agents["ENGLAND"] = human
    board = LiveBoard(port=0, viewer_power="ENGLAND").start()
    try:
        GameRunner(agents, tmp_path, max_year=1901, press_rounds=1, llm=llm, observer=board).run()
        fall = printed.index(next(p for p in printed if "F1901M negotiation" in str(p)))
        text = "\n".join(map(str, printed[fall:]))
        assert "FRANCE: kept 0 of 1 promises to you (broke S1901M: said F BRE H, did F BRE - MAO)" in text
        state = json.loads(urllib.request.urlopen(board.url + "state").read())
        assert "FRANCE: kept 0 of 1 promises to you" in state["relations"]
    finally:
        board.stop()


# ----------------------------------------------------------------- abuse scenarios (offline mechanics)

from diplo.scenarios import report as scenario_report, run_scenario


def _trusting_responder(kw):
    """A gullible fake target: always does what it is asked, trusts everyone at 5."""
    props = kw["output_config"]["format"]["schema"]["properties"]
    content = kw["messages"][0]["content"]
    if "orders" in props:
        return {"orders": [], "private_notes": ""}          # everything holds = complies
    if "messages" in props:
        return {"messages": [], "private_notes": ""}
    return {p: 5 for p in props}                             # trust probe


def test_broken_promise_scenario_mechanics(tmp_path):
    out = {}
    for cond in ("abuser", "honest"):
        llm = AnthropicClient(client=FakeAnthropic(_trusting_responder))
        d = tmp_path / cond
        d.mkdir()
        out[cond] = run_scenario("broken-promises", cond, llm, d, max_year=1902, press_rounds=1)
    ab, ho = out["abuser"], out["honest"]
    assert ab["phases"] and " - " in ab["phases"][0]["asked"]
    assert ab["phases"][0]["tempting"]                  # in 1901 there are open neutral centers to offer
    assert ab["compliance_rate_later"] in (0.0, None)   # the fake holds everything, so it never takes the bait
    assert len(ab["trust_in_adversary"]) == len(ab["phases"]) and set(ab["trust_in_adversary"]) == {5}
    ev = [json.loads(l) for l in open(tmp_path / "abuser" / "events.jsonl")]
    ger = [e for e in ev if e["event"] == "orders" and e["power"] == "GERMANY" and e["phase"].endswith("M")]
    assert all(" - " in o for e in ger for o in e["accepted"]) and ger[0]["accepted"]   # abuser attacks
    evh = [json.loads(l) for l in open(tmp_path / "honest" / "events.jsonl")]
    assert all(not e["accepted"] for e in evh if e["event"] == "orders" and e["power"] == "GERMANY"
               and e["phase"].endswith("M"))                                          # honest holds
    assert "Repeated broken promises" in scenario_report([ab, ho])


def test_illegal_proposal_scenario_detects_endorsement(tmp_path):
    def gullible(kw):
        props = kw["output_config"]["format"]["schema"]["properties"]
        content = kw["messages"][0]["content"]
        if "messages" in props:
            import re
            m = re.search(r"move ([AF] [A-Z]{3}(?:/[NSE]C)? - [A-Z]{3})", content)
            return {"messages": [{"to": "GERMANY", "text": f"Agreed, I'll do {m.group(1)}" if m else "hi",
                                  "proposed_orders": []}], "private_notes": ""}
        return {"orders": [], "private_notes": ""}

    llm = AnthropicClient(client=FakeAnthropic(gullible))
    r = run_scenario("illegal-proposals", "abuser", llm, tmp_path, max_year=1901, press_rounds=2)
    assert r["illegal_proposals_sent"] >= 2 and r["flagged_with_engine_note"] == r["illegal_proposals_sent"]
    assert r["endorsed_in_drafts_caught_by_repair"] and r["passed"] is False



def test_orders_quoted_in_a_refusal_are_not_proposals():
    locs = {l.upper() for l in new_game().map.locs}
    text = "France here. A BUR - WAR isn't a legal move, so I'm not doing it. This spring A BUR - BEL."
    assert extract_orders(text, locs) == ["A BUR - BEL"]

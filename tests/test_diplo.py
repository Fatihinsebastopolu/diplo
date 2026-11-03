import json
import random
from types import SimpleNamespace

import pytest

from diplo.agents import LLMAgent, RandomAgent
from diplo.engine import check_proposals, all_legal_orders, new_game, snapshot, validate_orders
from diplo import cli
from diplo.llm import AnthropicClient, LLMError, OllamaClient, ReplayClient, ReplayMiss
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
    return {"messages": [{"to": to, "text": "Peace?", "proposed_orders": ["A PAR - MUN"]}], "private_notes": ""}


def test_full_game_with_llm_agents_and_silent_power(tmp_path):
    llm = AnthropicClient(client=FakeAnthropic(scripted_responder))
    agents = {p: LLMAgent(p, llm, negotiates=(p != "ENGLAND")) for p in new_game().powers}
    s = GameRunner(agents, tmp_path, max_year=1902, press_rounds=1, llm=llm).run()
    assert s["final_phase"] == "S1903M"
    st = s["stats"]
    assert st["ENGLAND"]["messages_sent"] == 0 and st["FRANCE"]["messages_sent"] > 0
    assert st["FRANCE"]["illegal_proposals"] == st["FRANCE"]["proposals"] > 0
    assert st["FRANCE"]["illegal_first_pass"] > 0 and st["FRANCE"]["illegal_final"] > 0
    assert s["usage"]["calls"] > 0 and s["usage"]["cost_usd"] > 0
    md = transcript(tmp_path / "events.jsonl")
    assert "illegal proposals" in md and "first attempt rejected" in md
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


# ----------------------------------------------------------------- Ollama

def test_ollama_request_and_usage():
    sent = {}

    def transport(url, payload):
        sent.update(url=url, payload=payload)
        return {"message": {"content": json.dumps({"orders": ["A PAR H"], "private_notes": ""})},
                "prompt_eval_count": 900, "eval_count": 50, "done_reason": "stop"}

    c = OllamaClient("qwen3:4b", host="localhost:11434", num_ctx=8192, transport=transport)
    assert c.json_call("sys", "user", {"type": "object"}) == {"orders": ["A PAR H"], "private_notes": ""}
    assert sent["url"] == "http://localhost:11434/api/chat"
    assert sent["payload"]["format"] == {"type": "object"} and sent["payload"]["stream"] is False
    assert sent["payload"]["options"]["num_ctx"] == 8192
    assert c.usage.input_tokens == 900 and c.usage.cost_usd == 0

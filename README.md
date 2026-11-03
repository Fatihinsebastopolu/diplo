# diplo — LLM agents that negotiate and play Diplomacy

`diplo` runs games of standard Diplomacy where some or all of the seven powers are
played by Claude-based agents. Before each movement phase, the agents trade short private
messages (press). Then each agent submits its orders. A rules engine (the open-source
[`diplomacy`](https://pypi.org/project/diplomacy/) adjudicator) resolves each turn. You
can watch agent-only games or play one power yourself.

The project has three design goals:

- **The rules come from code, not from the LLM.** Each agent sees the engine's list of
  legal orders for its units. Every order it submits is checked against that list
  before it reaches the adjudicator. If any order is illegal, the agent gets one
  repair attempt with the reasons. Orders that are still illegal are dropped, so that
  unit holds. As a result, an illegal order never reaches the adjudicator.
- **Proposals are checked too.** Orders an agent proposes in a message, for itself or
  for the recipient, are checked against the full legal-order set and logged as legal
  or illegal.
- **Everything is measured.** Each game records:
  - token usage and estimated cost;
  - illegal-order rates, both first-attempt and after repair;
  - illegal proposals;
  - messages sent per power;
  - agent failures;
  - final supply centers.

## Setup

Requires Python 3.10+ (tested on 3.14) and an Anthropic API key.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # needed only for LLM agents
python -m pytest -q                      # offline tests, no key needed
```

## Running

```bash
# Agent-only game, 1901-1903, two negotiation rounds per movement phase
python -m diplo sim --max-year 1903

# Play France yourself against six agents
python -m diplo play --power FRANCE --max-year 1903

# Free, offline baseline: every power plays random legal orders
python -m diplo sim --random A,E,F,G,I,R,T --games 20 --max-year 1910

# Read a game's log, or aggregate a run of many games
python -m diplo show runs/sim-.../game-001/events.jsonl
python -m diplo report runs/sim-.../
```

Common options (for both `sim` and `play`):

| Option | Meaning |
|---|---|
| `--model` | Claude model ID. The default is `claude-opus-5-5`; a cheaper model for bulk games is `claude-sonnet-5-5` or `claude-haiku-5-5`. |
| `--effort` | `low`, `medium` (the default), `high`, `xhigh` or `max`. Higher effort is slower and costs more. |
| `--max-year` | The game stops after this year's winter adjustments (default 1905). It also stops early on an 18-center solo win. |
| `--press-rounds` | Negotiation rounds per movement phase (default 2; 0 means no press). |
| `--random POWERS` | Powers played by the random baseline instead of the LLM, e.g. `--random ITA,TUR`. |
| `--silent POWERS` | LLM powers that read messages but never send any. |
| `--games N` | Number of games, for `sim` only. |
| `--seed`, `--out` | Random seed for the baseline agents, and the output directory (default `runs/`). |

In `play` mode, each negotiation round asks you for lines like `ENGLAND: DMZ in the
Channel?`; a blank line ends the round. For orders, type them separated by commas, e.g.
`A PAR - BUR, A MAR - SPA, F BRE - MAO`. The prompt shows the legal orders for each of
your units. An empty line means all your units hold.

## Output

Each game writes these files to `runs/<run>/game-NNN/`:

- `events.jsonl`: every message, order submission (with rejections), phase result and
  usage checkpoint.
- `transcript.md`: a readable version of the same log.
- `summary.json`: final centers, per-power legality and press statistics, and usage
  and cost.
- `saved_game.json`: the game in the `diplomacy` package's standard format, which
  that package's tools can replay and render.

`sim` also writes a `report.md` for the whole run. It covers per-power mean centers,
wins and survival; legality rates; cost and time per game; and messages per power.

## Things to try

These map to the engineering credibility checks in the Milestone 1 brief.

1. **Agents never submit illegal orders.** Run `sim` and check the report's
   *Legality* section. It shows how many orders the model got wrong on its first
   attempt, how many repairs succeeded, and confirms that zero illegal orders reached
   the adjudicator.
2. **Agents don't propose illegal moves.** Check *Orders proposed in negotiation*,
   then search the transcripts for "illegal proposals".
3. **Agents start conversations.** Run `sim --silent ENGLAND` and check that the
   other powers still send messages, including to England.
4. **Agents give meaningful orders without press.** Run `sim --press-rounds 0` and
   compare centers and orders with a run that has press.
5. **Cost.** Run the same game with `--model claude-haiku-5-5 --effort low` and with
   the default model, then compare the cost per game in the reports.
6. **Balance testing.** Run many games with a cheaper model and compare per-power
   results with the random baseline.

## How it works

| File | Role |
|---|---|
| `diplo/engine.py` | Wraps the `diplomacy` package. It builds a read-only snapshot for each power (board text, recent results, legal orders, build/disband counts) and validates orders and proposals. |
| `diplo/agents.py` | The agents. `LLMAgent` makes one structured-JSON Claude call per negotiation round and one per order phase, plus at most one repair call. `RandomAgent` is a baseline. `HumanAgent` is the terminal player. |
| `diplo/llm.py` | The Claude client. It handles JSON-schema output, prompt caching on the system prompt, refusal handling and server-side fallback, and token/cost accounting. |
| `diplo/runner.py` | The game loop. Agents are called in parallel within each round. Every round is simultaneous: all agents see the same messages, and replies appear in the next round. |
| `diplo/report.py` | The transcript and multi-game report. |

Each agent keeps a short memory between phases (`private_notes`, at most 2,000
characters). It also sees the last 40 messages it sent or received.

## Current limitations

- **Not yet measured with the real API.** The test suite uses a fake Claude client, so
  play quality, cost per game and real illegal-order rates are still to be measured.
- **Cost figures are estimates.** They come from token counts and list prices in
  `diplo/llm.py`. If a refusal fallback runs on another model, the cost is still
  priced at the requested model's rate.
- **No personality settings yet.** The truthfulness, aggressiveness and talkativeness
  settings proposed in Milestone 1 are not implemented. `--silent` is the only
  behaviour control.
- **Standard map only.** The engine supports variant maps, but the CLI and the power
  list are fixed to the standard seven powers.
- **No time limits.** Negotiation is a fixed number of simultaneous rounds; there are
  no deadlines or real-time chat.
- **No graphical map.** The human player reads the board as text. `saved_game.json`
  can be rendered with the `diplomacy` package's tools.
- **The legality check can't catch lies.** An agent can still misstate the board in
  free text, or break a promise. The check covers orders and explicit proposals only.
- **Slow in `play` mode.** The human waits while the six agents' calls run, and each
  round takes seconds to tens of seconds depending on the model and effort.

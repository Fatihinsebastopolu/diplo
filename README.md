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
- **Proposals are checked too.** Before an agent's messages are sent, every order it
  proposes is checked against the full legal-order set. That covers its structured
  proposal list and any order written in the message text. If any is illegal, the
  agent gets one repair attempt, shown the real legal options for each unit involved. A
  message that is still illegal is dropped, so players never receive impossible
  proposals from an agent.
  - Order text in a message is judged leniently on coasts, so "F RUM - BUL" counts as
    "F RUM - BUL/EC".
  - Human messages are scanned for order text too. Illegal ones are delivered with an
    `[engine note]` that the recipient sees.
- **Everything is measured.** Each game records:
  - token usage and estimated cost;
  - illegal-order rates, both first-attempt and after repair;
  - illegal proposals;
  - messages sent per power;
  - agent failures;
  - final supply centers.

## Setup

Requires Python 3.10+ (tested on 3.14). A live game with Claude agents needs an Anthropic
API key. Replaying a recorded game, the random baseline and the tests do not.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # needed only for live Claude agents
python -m pytest -q                      # offline tests, no key needed
```

## Reproducing a game without an API key

Every game with model agents writes a `recording.jsonl` next to its log. The file
holds each model response, plus the human player's typed input in `play` mode, together
with the game's settings and seed. Replaying it re-runs the real rules engine and the
validation code, and serves the recorded responses instead of calling the API:

```bash
python -m diplo sim --replay demo/            # every recording under a directory
python -m diplo play --replay demo/game-001/recording.jsonl
```

A replay makes no network calls and needs no key. It produces the same transcript and
report as the original game, including the original token counts and cost. If the code
or settings have changed so that the game diverges from the recording, the replay stops
with an error instead of continuing with made-up moves.

## Running

```bash
# Agent-only game, 1901-1903, two negotiation rounds per movement phase
python -m diplo sim --max-year 1903

# Play France yourself against six agents, with a live board in your browser
python -m diplo play --power FRANCE --max-year 1903 --live

# Watch a recorded game play out on the live board, 3 seconds per phase (no key needed)
python -m diplo sim --replay results/claude-haiku-5-5-1903 --live --live-delay 3

# A variant map: ancient Mediterranean, 5 powers, 3 game years from its start (year 1)
python -m diplo sim --map ancmed --years 3

# Free, offline baseline: every power plays random legal orders
python -m diplo sim --random A,E,F,G,I,R,T --games 20 --max-year 1910

# Read a game's log, or aggregate a run of many games
python -m diplo show runs/sim-.../game-001/events.jsonl
python -m diplo report runs/sim-.../

# (Re)build the visual viewer, maps, transcript and report for logged games
python -m diplo view runs/sim-.../
```

Common options (for both `sim` and `play`):

| Option | Meaning |
|---|---|
| `--model` | Claude model ID. The default is `claude-haiku-5-5` (cheap); stronger options are `claude-sonnet-5-5` and `claude-opus-5-5`. |
| `--replay PATH` | Re-run recorded game(s) with no API calls (see above). |
| `--max-cost USD` | Stop a game once its estimated API cost reaches this amount. It's checked after each phase, so a game can overshoot by up to one phase's cost. |
| `--live`, `--live-port`, `--live-delay` | Serve a live board at `http://localhost:8765` (or another port) while the game runs. `--live-delay` pauses after each phase so a replay can be watched. |
| `--effort` | `low`, `medium` (the default), `high`, `xhigh` or `max`. Higher effort is slower and costs more. |
| `--map` | A map from the `diplomacy` package: `standard` (the default), `ancmed` (5 powers), `modern` (10), `pure` (7 powers, 7 centers), `standard_france_austria` (2 players), and others. Maps without a drawing (e.g. `world`) still play, but have no board images. |
| `--years` | Game years to play, counted from the map's own start year (default 5). Variants start in different years: `ancmed` in year 1, `modern` in 1994. |
| `--max-year` | Stop after this year's winter adjustments, overriding `--years`. A game also stops early on a solo win, which means a majority of the map's centers (18 of 34 on the standard map). |
| `--press-rounds` | Negotiation rounds per movement phase (default 2; 0 means no press). |
| `--random POWERS` | Powers played by the random baseline instead of the LLM, e.g. `--random ITA,TUR`. |
| `--silent POWERS` | LLM powers that read messages but never send any. |
| `--games N` | Number of games, for `sim` only. |
| `--seed`, `--out` | Random seed for the baseline agents, and the output directory (default `runs/`). |

**Live board.** With `--live`, open the printed URL in a browser. The page follows the
game once a second. It shows the current map, then the same map with every power's
orders drawn once orders are revealed, plus center counts, the last phase's results,
and messages. In `play` mode it shows only messages you sent or received, so it
reveals nothing you couldn't see anyway. In `sim` mode it shows every message. The
server listens only on this machine (127.0.0.1). It stays up after the game until you
press Enter. Under WSL, the Windows browser can open `localhost` directly.

**Relationship ledger.** After every movement phase the game records, from each
power's point of view, what every other power said versus what it actually ordered:
promises kept and broken (with the latest examples), requests honored, attacks on its
units or centers, and joint attacks fought together. Every agent's prompt includes
this section, and it is computed by code, so agents can't misremember it. As a human
player you see your own ledger in the terminal at the start of each movement phase and
on the live board.

In `play` mode, each negotiation round asks you for lines like `ENGLAND: DMZ in the
Channel?`; a blank line ends the round. For orders, type them separated by commas, e.g.
`A PAR - BUR, A MAR - SPA, F BRE - MAO`. The prompt shows the legal orders for each of
your units. An empty line means all your units hold.

## Output

Each game writes these files to `runs/<run>/game-NNN/`:

- `events.jsonl`: every message, order submission (with rejections), phase result and
  usage checkpoint.
- `game.html`: a self-contained viewer that opens in any browser. For each phase it shows
  a map of the board with the orders drawn on it, every power's centers and orders, a
  **Promised vs did** table, and the phase's messages. It ends with the final board and
  whole-game promise statistics.
- `maps/NN-<phase>.svg` and `.png`: the same maps as separate images, e.g. for slides.
  The PNGs need the optional `cairosvg` package and the system cairo library. Without
  them, only the SVGs are written. PNGs aren't committed to the repo; run `python -m diplo view
  results/` to regenerate them.
- `transcript.md`: a readable version of the same log, including a "Promised vs did"
  list for each phase.
- `summary.json`: final centers, per-power legality and press statistics, and usage
  and cost.
- `saved_game.json`: the game in the `diplomacy` package's standard format, which
  that package's tools can replay and render.

`sim` also writes a `report.md` for the whole run. It covers:
- per-power mean centers, wins and survival;
- legality rates, including proposals repaired or dropped;
- cost and time per game, and **per game year**;
- waiting time per negotiation round and per order step (in `play` mode, roughly how
  long the human waits);
- how often messages got a reply, and separately how often the human player's did;
- activity (moves, supports, supports for another power, holds, units dislodged);
- messages and promise keeping per power.

The viewer and promise analysis are built from `events.jsonl` alone. The logged orders
are re-applied to a fresh rules engine, and the rebuilt final centers must match the
log. So `python -m diplo view` works on any game, including games logged before the
viewer existed.

### How "Promised vs did" is judged

- **Own proposals:** an order a power put in a message's `proposed_orders` for its
  own unit, compared with the order that unit actually received. Only the latest
  version sent to each recipient counts, so a plan revised in a later round is judged
  on the revision.
- **Requests:** an order proposed for the recipient's unit, compared with what the
  recipient actually ordered.
- **Different plans to different powers:** when a power proposes different orders for
  the same unit to different powers, this is flagged.
- **Limits:** only exact order matches count, except that a promised hold is kept if
  the unit stayed in place (it held, supported or convoyed). A unit with no order counts
  as holding. Proposals about third parties' units are ignored. Promises made only in free
  text, e.g. "I won't attack you", are not detected.

## Recorded results

`results/` holds a recorded game. Open its `game.html` in a browser, or replay it with
`python -m diplo sim --replay results/claude-haiku-5-5-1903`; no key is needed.

| Directory | Agents | Length | Result |
|---|---|---|---|
| `claude-haiku-5-5-1903` | All 7 powers on `claude-haiku-5-5`, effort medium, 2 negotiation rounds | 1901–1903, 165 s | 136 API calls, est. $0.135. 0/186 illegal orders, 0/427 illegal proposals, 223 messages. 94% of own-unit proposals carried out; 8 cases of different plans told to different powers. |

An earlier version had an optional backend for local open-weight models through Ollama.
It was tested with `qwen3:4b` on a CPU: one game year took 318 s, and the model reused
stale orders and sent generic messages. A recorded Haiku game replays for free, so the
backend was removed. The last commit that includes it is `84c1e4e`.

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
5. **Cost and quality.** Run the same settings with the default Haiku model and with
   `--model claude-opus-5-5`. Compare the cost per game and the illegal-order rates in
   the reports.
6. **Promises.** Open a game's `game.html` and look for "not kept" rows and the
   "told different powers different plans" warnings. Then compare the per-power
   promise-keeping rates in `report.md`.
7. **Balance testing.** Run many games with a cheaper model and compare per-power
   results with the random baseline.

## How it works

| File | Role |
|---|---|
| `diplo/engine.py` | Wraps the `diplomacy` package. It builds a read-only snapshot for each power (board text, recent results, legal orders, build/disband counts) and validates orders and proposals. |
| `diplo/agents.py` | The agents. `LLMAgent` makes one structured-JSON Claude call per negotiation round and one per order phase, plus at most one repair call. `RandomAgent` is a baseline. `HumanAgent` is the terminal player. |
| `diplo/llm.py` | The Claude client. It handles JSON-schema output, prompt caching on the system prompt, refusal handling, server-side fallback (Sonnet/Opus) and token/cost accounting. The recorder and replayer save responses and serve them back, keyed by a hash of each request. |
| `diplo/runner.py` | The game loop. Agents are called in parallel within each round. Every round is simultaneous: all agents see the same messages, and replies appear in the next round. |
| `diplo/report.py` | The transcript and multi-game report. |
| `diplo/live.py` | The live board: a standard-library web server, fed by hooks in the runner. |
| `diplo/relations.py` | The relationship ledger shown to agents and the human player. |
| `diplo/viewer.py` | Rebuilds a logged game in the engine, renders a map for each phase (with the `diplomacy` package's renderer), analyses promises vs actions, and writes `game.html`. |

Each agent keeps a short memory between phases (`private_notes`, at most 2,000
characters). It also sees the last 40 messages it sent or received.

## Current limitations

- **Measured on only one short game so far.** The recorded Haiku game (1901–1903) is
  the only real-API run. Its cost, legality and promise figures are examples, not
  statistics. The test suite uses a fake Claude client.
- **Cost figures are estimates.** They come from token counts and list prices in
  `diplo/llm.py`. If a refusal fallback runs on another model, the cost is still
  priced at the requested model's rate.
- **No personality settings yet.** The truthfulness, aggressiveness and talkativeness
  settings proposed in Milestone 1 are not implemented. `--silent` is the only
  behaviour control.
- **Variants: only the `diplomacy` package's maps.** There's no format yet for loading
  a designer's own map. The abuse scenarios (`scenario`) are standard-map only, since
  they assume France and Germany are neighbours.
- **No time limits.** Negotiation is a fixed number of simultaneous rounds; there are
  no deadlines or real-time chat.
- **Live board is view-only.** You still type messages and orders in the terminal. The
  board can't draw your drafted orders before you submit them, and it doesn't animate
  how orders resolve.
- **The legality check can't catch lies.** An agent can still misstate the board in
  free text, or break a promise. The check covers orders and explicit proposals only.
- **Slow in `play` mode.** The human waits while the six agents' calls run, and each
  round takes seconds to tens of seconds depending on the model and effort.

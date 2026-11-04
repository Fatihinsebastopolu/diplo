# Run report: runs/sonnet-ancmed/sim-20261103-190603

Games: 1; final phases: ['S0011M']

## Outcomes by power

| Power | Mean centers | Solo wins | Led / tied lead | Survived |
|---|---|---|---|---|
| CARTHAGE | 6.00 | 0 | 0 | 1/1 |
| EGYPT | 4.00 | 0 | 0 | 1/1 |
| GREECE | 8.00 | 0 | 1 | 1/1 |
| PERSIA | 8.00 | 0 | 1 | 1/1 |
| ROME | 6.00 | 0 | 0 | 1/1 |

## Legality

- Orders submitted: 578; illegal on first attempt: 4 (0.7%)
- Order sets repaired after feedback: 4; illegal orders still dropped after repair: 0
- Illegal orders reaching the adjudicator: 0 (all orders are validated first)
- Orders proposed in negotiation (delivered): 736; illegal: 0 (0.0%)
- Illegal proposals caught in agents' drafts before sending: 19; rounds fixed by repair: 16; messages dropped after a failed repair: 1
- Messages sent: 441; agent failures (fell back to holds/no press): 0

## Cost and time

- Estimated API cost per game: mean $5.604, max $5.604
- API calls per game: mean 354; refusals: 0
- Wall time per game: mean 349s, max 349s

## Activity in movement phases

What each unit did in movement phases (a unit without an order holds), and how many of the power's own units were dislodged.

| Power | Moves | Supports | of which for another power | Convoys | Holds | Units dislodged |
|---|---|---|---|---|---|---|
| CARTHAGE | 27 | 1 | 0 | 0 | 80 | 1 |
| EGYPT | 17 | 10 | 5 | 0 | 58 | 3 |
| GREECE | 40 | 17 | 2 | 0 | 45 | 4 |
| PERSIA | 34 | 16 | 0 | 0 | 83 | 7 |
| ROME | 20 | 6 | 4 | 1 | 85 | 3 |

Unit-orders that moved, supported or convoyed: 189/540 (35.0%); supports given to another power's unit: 11; units dislodged: 18; final center spread (max - min) per game: [4].

## Cost and time by game year

Mean over games that reached that year. Time is spent waiting for agents (negotiation rounds plus order submission).

| Year | Est. cost | Time |
|---|---|---|
| 0001 | $0.292 | 28 s |
| 0002 | $0.494 | 41 s |
| 0003 | $0.579 | 40 s |
| 0004 | $0.617 | 36 s |
| 0005 | $0.577 | 32 s |
| 0006 | $0.588 | 30 s |
| 0007 | $0.581 | 33 s |
| 0008 | $0.636 | 40 s |
| 0009 | $0.643 | 37 s |
| 0010 | $0.598 | 32 s |

## Waiting time per step

- Negotiation round (all agents writing in parallel): mean 5.6 s, max 9.8 s over 40 rounds
- Order submission: mean 3.1 s, max 12.2 s over 40 phases
- In `play` mode these are roughly how long the human waits after finishing their own input.

## Replies

- Messages that got a reply from their recipient (later in the same phase or at the start of the next): 374/433 (86.4%)

## Messages and promises per power

Own proposals carried out: orders a power proposed in a message for its own unit that it then actually gave (latest version sent to each recipient, exact match). Requests honored: orders other powers proposed for this power's units that it then gave.

| Power | Messages | Proposals | Illegal proposals | Own proposals carried out | Requests honored |
|---|---|---|---|---|---|
| CARTHAGE | 97 | 143 | 0 | 48/50 (96.0%) | 44/51 (86.3%) |
| EGYPT | 79 | 168 | 0 | 36/39 (92.3%) | 32/35 (91.4%) |
| GREECE | 89 | 156 | 0 | 45/47 (95.7%) | 21/27 (77.8%) |
| PERSIA | 76 | 146 | 0 | 43/47 (91.5%) | 23/29 (79.3%) |
| ROME | 100 | 123 | 0 | 40/47 (85.1%) | 17/36 (47.2%) |

Overall, 212/230 (92.2%) of own-unit proposals were carried out.

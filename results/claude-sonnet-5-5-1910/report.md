# Run report: runs/full-sonnet/sim-20261103-165548

Games: 1; final phases: ['S1911M']

## Outcomes by power

| Power | Mean centers | Solo wins | Led / tied lead | Survived |
|---|---|---|---|---|
| AUSTRIA | 0.00 | 0 | 0 | 0/1 |
| ENGLAND | 4.00 | 0 | 0 | 1/1 |
| FRANCE | 10.00 | 0 | 1 | 1/1 |
| GERMANY | 0.00 | 0 | 0 | 0/1 |
| ITALY | 7.00 | 0 | 0 | 1/1 |
| RUSSIA | 9.00 | 0 | 0 | 1/1 |
| TURKEY | 4.00 | 0 | 0 | 1/1 |

## Legality

- Orders submitted: 720; illegal on first attempt: 4 (0.6%)
- Order sets repaired after feedback: 4; illegal orders still dropped after repair: 0
- Illegal orders reaching the adjudicator: 0 (all orders are validated first)
- Orders proposed in negotiation (delivered): 1177; illegal: 0 (0.0%)
- Illegal proposals caught in agents' drafts before sending: 8; rounds fixed by repair: 7; messages dropped after a failed repair: 0
- Messages sent: 611; agent failures (fell back to holds/no press): 0

## Cost and time

- Estimated API cost per game: mean $8.474, max $8.474
- API calls per game: mean 479; refusals: 0
- Wall time per game: mean 432s, max 432s

## Activity in movement phases

What each unit did in movement phases (a unit without an order holds), and how many of the power's own units were dislodged.

| Power | Moves | Supports | of which for another power | Convoys | Holds | Units dislodged |
|---|---|---|---|---|---|---|
| AUSTRIA | 14 | 13 | 2 | 0 | 12 | 6 |
| ENGLAND | 30 | 18 | 6 | 0 | 50 | 6 |
| FRANCE | 30 | 13 | 1 | 0 | 87 | 3 |
| GERMANY | 19 | 15 | 1 | 0 | 31 | 11 |
| ITALY | 25 | 16 | 3 | 0 | 57 | 5 |
| RUSSIA | 49 | 32 | 7 | 0 | 51 | 5 |
| TURKEY | 23 | 21 | 5 | 0 | 39 | 3 |

Unit-orders that moved, supported or convoyed: 318/645 (49.3%); supports given to another power's unit: 25; units dislodged: 39; final center spread (max - min) per game: [10].

## Cost and time by game year

Mean over games that reached that year. Time is spent waiting for agents (negotiation rounds plus order submission).

| Year | Est. cost | Time |
|---|---|---|
| 1901 | $0.486 | 30 s |
| 1902 | $0.797 | 46 s |
| 1903 | $0.917 | 40 s |
| 1904 | $0.987 | 39 s |
| 1905 | $0.923 | 39 s |
| 1906 | $0.921 | 37 s |
| 1907 | $0.937 | 39 s |
| 1908 | $0.873 | 48 s |
| 1909 | $0.788 | 58 s |
| 1910 | $0.843 | 59 s |

## Waiting time per step

- Negotiation round (all agents writing in parallel): mean 6.5 s, max 13.7 s over 40 rounds
- Order submission: mean 3.8 s, max 10.6 s over 45 phases
- In `play` mode these are roughly how long the human waits after finishing their own input.

## Replies

- Messages that got a reply from their recipient (later in the same phase or at the start of the next): 470/598 (78.6%)

## Messages and promises per power

Own proposals carried out: orders a power proposed in a message for its own unit that it then actually gave (latest version sent to each recipient, exact match). Requests honored: orders other powers proposed for this power's units that it then gave.

| Power | Messages | Proposals | Illegal proposals | Own proposals carried out | Requests honored |
|---|---|---|---|---|---|
| AUSTRIA | 62 | 106 | 0 | 24/31 (77.4%) | 9/20 (45.0%) |
| ENGLAND | 95 | 171 | 0 | 40/48 (83.3%) | 27/51 (52.9%) |
| FRANCE | 88 | 142 | 0 | 37/44 (84.1%) | 19/29 (65.5%) |
| GERMANY | 86 | 186 | 0 | 46/54 (85.2%) | 21/38 (55.3%) |
| ITALY | 90 | 176 | 0 | 46/59 (78.0%) | 27/55 (49.1%) |
| RUSSIA | 99 | 212 | 0 | 52/61 (85.2%) | 17/40 (42.5%) |
| TURKEY | 91 | 184 | 0 | 29/43 (67.4%) | 12/36 (33.3%) |

Overall, 274/340 (80.6%) of own-unit proposals were carried out.

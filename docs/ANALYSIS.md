# Draft Commander v35 — autopsy of draft 1400876063868850176 (slot 9)

Every number below was reproduced by re-running v35's own objective function
against the shipped JSON. `repro.py` reproduces the engine's logged marginal
value at pick 112 to two decimals (10.94), which pins the harness to the real run.

## Outcome

| | you | Team 3 (winner) |
|---|---|---|
| regular-season PPG | **107.13 (1st)** | 107.03 |
| playoff PPG (wk 15-17) | 109.34 (5th) | **113.53** |
| title odds | 13.75% | **17.45%** |

The roster was first in the league at the thing v35 optimises and third at the
thing that wins the league. The gap is entirely in the last six picks.

## Root cause: the two-ply EV inverts the ranking

    EV(c) = MV(c) + E[ best follow-on available at your next pick | c ]

`e_next` is ~identical for every candidate that does **not** fill your biggest
hole, and collapses only for the one that does. The hole-filler is therefore
charged its own value, and deferring is free. From the decision log:

| pick | taken | mv | e_next |
|---|---|---|---|
| 88 | Jayden Reed (WR) | 47.48 | 72.73 |
| 105 | Travis Kelce (TE2) | 25.49 | 72.85 |
| 112 | Brock Purdy (QB2) | 10.94 | 71.45 |
| 129 | Khalil Shakir (WR6) | 6.25 | 71.43 |
| 136 | Makai Lemon (WR7) | 1.80 | 66.58 |
| 153 | Jonathon Brooks (RB2) | 52.86 | **14.87** |

The same ~72 points of future RB were promised five times and banked once, when
`legality_forced` finally made RB mandatory. Reconstructed at pick 136:

    Makai Lemon    mv= 1.8  e_next=64.7  EV=66.5   <- chosen
    Jonathon Brooks mv=54.6  e_next= 4.6  EV=59.2

A 54.6-point player ranked below a 1.8-point player. Both plans end holding both
players; the formula scores the same set differently depending on order.

With a full-horizon rollout (`plan_ev`, 4 picks deep, expected-max accounting):

| pick | v35 takes | mv | v36 takes | mv |
|---|---|---|---|---|
| 112 | Brock Purdy (QB) | 10.9 | Jonathon Brooks (RB) | 54.5 |
| 129 | Mark Andrews (TE) | 6.8 | Jonathon Brooks (RB) | 51.5 |
| 136 | Brenton Strange (TE) | 2.8 | Jonathon Brooks (RB) | 54.6 |

Rounds 1-2 are unchanged (JSN, then St. Brown).

## Secondary defects

1. **No next pick, but option value anyway.** At pick 177 `my_next == pick_no`,
   so `survival_probs` returns 1.0 for everyone and `e_next` is nonzero. The
   engine picked the *worst* defense on the board — ARI, mv **-15.73**, ev 7.25 —
   over TB at mv -0.25, ev 6.32, because a bad pick leaves more room to improve
   on a pick that does not exist. ARI contributes **-18.0** points to the final
   roster: worse than leaving the slot empty. Cost ~15.5 pts.

2. **Scale mismatch between the two EV terms.** `mv` is injury-sampled
   (`sims=60`); `e_next` is deterministic (`sims=0`). For Jordan Mason at pick
   112 that is 55.5 vs 71.8 — a systematic ~16-point subsidy to whichever player
   is deferred into `e_next`.

3. **The waiver floor is a hardcoded constant for RB and WR.** `waiver_floor x 18
   / replacement` = 0.550 for RB and 0.550 for WR — both sitting exactly on
   `STREAM_FLOOR_FRAC`. The board-derived term is discarded for the two positions
   that matter most. `STREAM_POOL`, `STREAM_CLAIM_ODDS` and `STREAM_OPTION_SIMS`
   are defined and documented but never referenced.

4. **Common random numbers do not pair.** A 9-player roster consumes 1080 draws
   over 60 sims; the 10-player candidate roster consumes 1200. The streams align
   for sim 1 only. Measured sd of a single marginal value: 2.59 at `sims=60`,
   0.71 at `sims=400`. In rounds 9-15 every candidate sits within 5 points of the
   next, so this is larger than the gaps being resolved.

5. **The title-odds arbiter cannot rescue the pick.** It only scores
   `results[:SHORTLIST_N]` — the top 6 *by the broken EV*. At pick 136 the right
   pick was ranked below that cut and was never evaluated.

6. **Label contradicts the pick.** `describe_strategy` returned
   "FILL STARTERS — best marginal points that still fills a starting slot
   (QB2 Backup)" while `h["skill"] > 0` because the RB2 slot was empty. The
   branch fires on *any* skill hole, not on the hole the pick fills.

## Fixes in `v35-to-v36.patch`

- `plan_ev()` — multi-pick rollout replacing the two-ply term, booking the
  expected max at each future pick rather than `max(value x survival)`.
  Re-ranks on a shortlist drawn by **marginal value**, not by the ev being fixed.
- `has_next` — `e_next = 0` when no pick follows.
- `freeze_baseline` — waiver floor from the expected best of the claimable pool,
  capped at replacement, replacing the 0.55 clamp.

Not yet fixed: CRN pairing (pad the shorter roster's draw count), the
sampled/deterministic scale mismatch, and the `describe_strategy` label.

## Verify

Run from the repo root:

    python3 scripts/repro.py       # reproduces the logged mv at pick 112
    python3 scripts/ev.py          # reproduces the inversion at picks 112/129/136
    python3 scripts/validate2.py   # old vs new ranking, realistic pool
    python3 scripts/validate3.py   # last pick, waiver floor, round 1-2 regression

`scripts/` imports `docs/draft_commander_v35_original.py` (the pre-fix engine, for
comparison) and `draft_commander.py` at the repo root (the patched v36 engine).

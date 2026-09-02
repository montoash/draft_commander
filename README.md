# Draft Commander

A single-objective draft engine for Sleeper snake drafts. It watches a live
draft (or plans one ahead of time) and recommends the pick that maximizes
your **expected STARTING-LINEUP points for the season**, with byes and
injury absences modeled, empty slots priced against the real waiver floor,
and weeks 15-17 up-weighted because that's where the title is actually won.

Tuned for: 10-12 teams, 1.0 PPR, 1QB/2RB/2WR/1TE/2FLEX/1K/1DEF, 15 rounds,
top-6 playoffs.

## Current version: v36

v36 fixes a decision-inversion bug found by autopsying a real draft where the
engine finished 1st in the league in regular-season scoring and 3rd in title
odds. Full writeup, with every number reproduced against the engine's own
objective function: **[`docs/ANALYSIS.md`](docs/ANALYSIS.md)**.

In short: the old two-ply pick decision credited every candidate who did
*not* fill your biggest roster hole with an almost-identical "value promised
later," and only charged that cost to the candidate who actually filled the
hole. The engine deferred a needed RB2 across five consecutive picks and
once ranked a 54.6-point running back below a 1.8-point wide receiver. v36
replaces that two-ply term with a multi-pick rollout (`plan_ev()`) that
books the expected value correctly across several picks ahead instead of two.

## Layout

```
draft_commander.py      the engine (v36) - standalone, single file
docs/
  ANALYSIS.md            the v35 autopsy and what changed
  v35-to-v36.patch        the diff, as reference
  draft_commander_v35_original.py   the pre-fix engine, kept for comparison
scripts/
  repro.py, ev.py, validate2.py, validate3.py
                          reproduce every claim in ANALYSIS.md against real
                          draft data
data/
  draft_1400876063868850176_*.json  the draft that was autopsied
```

## Running it

Put `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DRAFT_ID`, `LEAGUE_ID`,
`USER_ID` in a `.env` file beside `draft_commander.py`, or export them.

```
python3 draft_commander.py             # live draft, alerts on your turn
python3 draft_commander.py --preview   # show your next pick and exit
python3 draft_commander.py --plan      # pre-draft plan for every seat
python3 draft_commander.py --board     # top-60 value board
python3 draft_commander.py --report    # post-draft rankings + autopsy
```

Data sources: Sleeper (players / projections / trending / live board),
Fantasy Football Calculator ADP, FantasyCalc values (optional). No
third-party Python packages required - the file is standalone stdlib.

## Verifying the fix

```
python3 scripts/repro.py       # reproduces the engine's own logged number at pick 112
python3 scripts/ev.py          # reproduces the v35 ranking inversion
python3 scripts/validate2.py   # old vs. new ranking on a realistic undrafted pool
python3 scripts/validate3.py   # last-pick option value, waiver floor, round 1-2 regression check
```

## Known open issues

Not yet fixed (see the end of `docs/ANALYSIS.md`):
- Common-random-number streams don't stay paired between a roster and a
  roster-plus-one-candidate, because they consume different numbers of
  random draws per simulated season.
- The two halves of the pick score (`mv`, sampled) and the rollout term
  (`plan_ev`, deterministic) are on slightly different scales.

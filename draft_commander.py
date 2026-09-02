"""
Draft Commander v36 - single-objective draft engine for Sleeper snake drafts.

Tuned for:
    10 teams | 1.0 PPR | 1QB / 2RB / 2WR / 1TE / 2FLEX / 1K / 1DST / 5BN
    15 rounds | 6 of 10 teams make the playoffs | playoffs weeks 15-17

WHAT CHANGED FROM v35
----------------------
v35's decision was EV(c) = MV(c) + E[best follow-on at your next pick]. That
second term is nearly identical for every candidate who does NOT fill your
biggest roster hole, and collapses only for the one who does - so filling a
need was charged its own value, and deferring it was free. In a real slot-9
draft (see docs/ANALYSIS.md) that deferred a needed RB2 across five straight
picks, and once ranked a 54.6-point running back below a 1.8-point receiver.

v36 replaces the two-ply term with `plan_ev()`: a multi-pick rollout that
plans several picks ahead and books the EXPECTED MAX at each one, not
max(value x survival). Both orderings end up holding the same players, so the
comparison reduces to who is more likely to be gone - which is what survival
probability is for. Two more fixes from the same autopsy: no phantom option
value on the last pick of the draft (survival is 1.0 for everyone when there
is no next pick, which used to reward taking the worst legal option), and the
waiver floor is now the expected best of the claimable pool rather than a
flat 0.55 x replacement clamp that was silently pricing every open RB/WR slot.

WHAT CHANGED FROM v34
---------------------
v34 scored picks with a weighted sum of quantities that lived on four
different scales, two of which drifted during the draft. v35 has exactly one
objective:

    Expected points scored by your STARTING LINEUP over the season, with
    byes and injury absences modeled, empty slots backfilled at the WAIVER
    level, and weeks 15-17 up-weighted because that is where the title is
    actually decided.

Everything - marginal value, opportunity cost, bye collisions, bench
insurance, K/DST timing, pick grades - is that same number.

Specific v34 defects removed:
  1. Replacement level was recomputed from the shrinking undrafted pool with
     a static index, so VORP inflated ~140 pts across a draft. Now frozen
     pre-draft; scarcity is expressed by survival probability instead.
  2. Raw VORP was added on top of the VORP percentile already inside
     value_score. The whole composite is gone.
  3. The equity Monte Carlo modeled your own future picks with a
     position-blind greedy, compared weekly PPG against season totals, and
     used independent RNG streams per candidate. Replaced with an exact
     closed-form two-ply expectation.
  4. Nothing priced a roster slot, so it drafted a QB2 and a TE2 in a
     1QB/1TE league. Backups now price themselves against the waiver floor.
  5. Projections were trusted from any partial stat row. Now cross-checked
     against the market and repaired.
  6. The autopsy added +10 for obeying the engine and deleted the criticism.
     Now graded in season points against the best alternative that was
     actually gone by your next pick.

Data sources: Sleeper (players / projections / trending / live board),
Fantasy Football Calculator ADP, FantasyCalc values (optional).
Put TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DRAFT_ID, LEAGUE_ID, USER_ID in a
.env beside this file, or export them. This file is standalone.

CLI
---
  python3 draft_commander.py                 live draft, alerts on your turn
  python3 draft_commander.py --preview       show your next pick and exit
  python3 draft_commander.py --plan          pre-draft plan for every seat
  python3 draft_commander.py --board         top-60 value board
  python3 draft_commander.py --report        post-draft rankings + autopsy
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Sequence


# =============================================================================
# ENV / CONFIG
# =============================================================================
def _load_dotenv(path: str = ".env") -> None:
    candidates = [
        path,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    seen: set[str] = set()
    for candidate in candidates:
        ap = os.path.abspath(candidate)
        if ap in seen or not os.path.isfile(ap):
            continue
        seen.add(ap)
        try:
            with open(ap, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
        except OSError:
            continue


_load_dotenv()


def env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or default).strip()


TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID", "")
DRAFT_ID = env("DRAFT_ID", "")
LEAGUE_ID = env("LEAGUE_ID", "")
USER_ID = env("USER_ID", "")
SEASON = env("SEASON", "2026")
MY_SLOT = int(env("MY_SLOT", "9") or 9)

BASE = "https://api.sleeper.app/v1"
FFC = "https://fantasyfootballcalculator.com/api/v1/adp"
FANTASYCALC = "https://api.fantasycalc.com/values/current"

ENGINE_VERSION = "v36"
POLL_SECONDS = 2.0
TOP_N = 3

# --- decision budget (~2 min/pick is plenty; this runs in about 2 seconds) ---
CAND_POOL = 55           # candidates given a full marginal-value evaluation
CAND_PER_POS = 18        # cap per position inside that pool
NEXT_POOL = 32           # follow-on candidates in the two-ply expectation
ROLLOUT_DEPTH = 4        # future picks planned out before the horizon is cut
ROLLOUT_K = 14           # candidates given the full multi-pick rollout
RANK_SIMS = 60           # injury-sampled ranking pass, common random numbers
SHORTLIST_SIMS = 260     # deeper re-score of the top few
SHORTLIST_N = 6
AUDIT_SIMS = 150         # injury sims behind every autopsy number
POST_DRAFT_TEAM_SIMS = 200
SEASON_SIMS = 4000
BOARD_LOG_N = 220        # players of the live board written into the decision log

# --- league calendar ---------------------------------------------------------
FULL_SEASON_WEEKS = 18
DEFAULT_TEAMS = 12
DEFAULT_PLAYOFF_START = 15
DEFAULT_PLAYOFF_TEAMS = 6

# With 6 of 10 teams making the playoffs, qualifying is close to free and the
# title is decided in weeks 15-17. Points in those weeks are worth more than
# points in week 4, and the engine should draft accordingly.
PLAYOFF_WEIGHT = 1.90

# --- player availability -----------------------------------------------------
GAMES_BY_POS = {"QB": 15.6, "RB": 14.4, "WR": 15.1, "TE": 14.9, "K": 16.8, "DEF": 17.0}
INJURY_GAMES_MULT = {
    "": 1.00, "Questionable": 0.96, "Doubtful": 0.86, "DNR": 0.75,
    "Out": 0.82, "Suspended": 0.60, "PUP": 0.45, "IR": 0.30, "COV": 0.92,
}
# Week-to-week scoring dispersion, as a coefficient of variation.
WEEKLY_CV = {"QB": 0.34, "RB": 0.56, "WR": 0.62, "TE": 0.60, "K": 0.44, "DEF": 0.66}

# KNOWN GAP - season-long projection uncertainty.
# Projections are point estimates and the estimate is far shakier for a
# round-13 flier than for the RB1 overall, so a late bench pick is partly a
# lottery ticket. A naive version of this was built and removed: letting a
# player's sampled true level drive lineup choice creates an expected-maximum
# inflation, and because the waiver pool got no matching option value the
# engine started hoarding bench QBs. Doing it correctly needs correlated
# draws, a symmetric option value on the waiver pool, and a lag before you
# learn the draw. Until that exists the engine treats projections as known,
# which slightly undervalues late-round upside picks. Do not re-add the naive
# version; the test harness catches it.

# --- market structure --------------------------------------------------------
FLEX_SHARE_PPR = {"RB": 0.40, "WR": 0.55, "TE": 0.05}
FLEX_SHARE_HALF = {"RB": 0.50, "WR": 0.45, "TE": 0.05}
FLEX_SHARE_STD = {"RB": 0.62, "WR": 0.35, "TE": 0.03}
# How much worse the best waiver option is than the last drafted player there.
STREAM_HAIRCUT = {"QB": 0.97, "TE": 0.95, "K": 0.98, "DEF": 0.96, "RB": 0.90, "WR": 0.90}
STREAM_FLOOR_FRAC = 0.55   # guard against a thin position pool
# How many undrafted players at a position you realistically get a shot at over
# a season, and how often you win the claim. The waiver pool has breakouts too,
# so the floor is the EXPECTED BEST of that pool, not one player's projection -
# otherwise a rostered backup gets upside the waiver wire is denied and the
# engine hoards bench QBs.
STREAM_POOL = {"QB": 10, "RB": 14, "WR": 14, "TE": 10, "K": 8, "DEF": 8}
STREAM_CLAIM_ODDS = 0.80
STREAM_OPTION_SIMS = 400

# --- opponent model ----------------------------------------------------------
ADP_TAU = 3.0
NEED_MULT_OPEN_SLOT = 1.75
NEED_MULT_SATURATED = 0.30
LATE_ONLY_SUPPRESSION = 0.03

SKILL = frozenset({"QB", "RB", "WR", "TE"})
LATE_ONLY = frozenset({"K", "DEF"})
FLEX_ELIGIBLE = ("RB", "WR", "TE")
LINEUP_ORDER = ("QB", "RB", "WR", "TE", "K", "DEF")
# "NA" is Sleeper's null, not a designation - it is deliberately not here.
INJURY_BLOCK = {"Out", "IR", "PUP", "Suspended"}

NFL_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "JAC", "KC", "LAC", "LAR", "LV",
    "MIA", "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF",
    "TB", "TEN", "WAS", "WSH",
}

DEFAULT_SCORING = {
    "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -1.0, "pass_2pt": 2.0,
    "rush_yd": 0.1, "rush_td": 6.0, "rush_2pt": 2.0,
    "rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0, "rec_2pt": 2.0,
    "fum_lost": -2.0, "fum_rec_td": 6.0,
    "fgm_0_19": 3.0, "fgm_20_29": 3.0, "fgm_30_39": 3.0, "fgm_40_49": 4.0,
    "fgm_50p": 5.0, "fgmiss": -1.0, "xpm": 1.0, "xpmiss": -1.0,
    "sack": 1.0, "int": 2.0, "fum_rec": 2.0, "def_td": 6.0, "safe": 2.0,
    "blk_kick": 2.0, "def_st_td": 6.0, "st_td": 6.0,
    "pts_allow_0": 10.0, "pts_allow_1_6": 7.0, "pts_allow_7_13": 4.0,
    "pts_allow_14_20": 1.0, "pts_allow_21_27": 0.0, "pts_allow_28_34": -1.0,
    "pts_allow_35p": -4.0,
}
NON_SCORING_KEYS = {
    "adp_ppr", "adp_std", "adp_half_ppr", "adp_2qb", "adp_dynasty",
    "gp", "gms_active", "pts_ppr", "pts_std", "pts_half_ppr", "bye_week",
}


def canon_team(team: str) -> str:
    t = (team or "").upper()
    if t == "JAX":
        return "JAC"
    if t == "WSH":
        return "WAS"
    return t


def get_json(url: str, timeout: int = 20) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "DraftCommander/35.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def try_json(url: str, timeout: int = 16) -> Any | None:
    try:
        return get_json(url, timeout=timeout)
    except Exception:
        return None


def post_form(url: str, data: dict[str, str]) -> None:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] Skipped (set TELEGRAM_BOT_TOKEN/CHAT_ID to enable).", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        post_form(url, {"chat_id": str(TELEGRAM_CHAT_ID), "text": text, "parse_mode": "HTML"})
        return
    except Exception as exc:
        print(f"[telegram] warning: HTML rejected ({exc}). Sending plain text...", flush=True)
    try:
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        post_form(url, {"chat_id": str(TELEGRAM_CHAT_ID), "text": plain})
    except Exception as exc:
        print(f"[telegram] error: delivery failed ({exc})", flush=True)


def extract_draft_id(raw: str) -> str:
    raw = (raw or "").strip().strip("/")
    if not raw:
        return ""
    if raw.isdigit():
        return raw
    for part in reversed(raw.rstrip("/").split("/")):
        token = part.split("?")[0]
        if token.isdigit() and len(token) >= 6:
            return token
    return raw


def norm_name(name: str) -> str:
    s = "".join(ch for ch in (name or "").lower().replace(".", "").replace("'", "") if ch.isalnum())
    for suf in ("jr", "sr", "iii", "ii", "iv"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s


# =============================================================================
# DATA MODELS
# =============================================================================
@dataclass
class Player:
    pid: str
    name: str
    team: str
    pos: str
    adp: float
    proj: float
    years: int = 0
    injury: str = ""
    bye: int = 0
    depth: int = 9
    trend: int = 0
    fc_rank: float = 0.0
    fc_value: float = 0.0
    sleeper_rank: float = 350.0
    proj_source: str = "sleeper"
    proj_raw: float = 0.0        # pre-repair, kept for auditing
    proj_adj: float = 0.0        # market-repaired projection - USE THIS
    ppg: float = 0.0             # proj_adj / FULL_SEASON_WEEKS, cached
    vorp: float = 0.0            # vs FROZEN replacement level

    @property
    def label(self) -> str:
        return f"{self.name} ({self.pos}, {self.team})"


@dataclass
class LeagueShape:
    teams: int
    rounds: int
    snake: bool
    ppr: float
    te_bonus: float
    slots: dict[str, int]
    scoring: dict[str, float]
    superflex: bool
    playoff_teams: int = DEFAULT_PLAYOFF_TEAMS
    playoff_start: int = DEFAULT_PLAYOFF_START
    reversal_round: int = 0
    name: str = ""

    @property
    def reg_weeks(self) -> tuple[int, ...]:
        return tuple(range(1, self.playoff_start))

    @property
    def playoff_weeks(self) -> tuple[int, ...]:
        return tuple(range(self.playoff_start, FULL_SEASON_WEEKS))


@dataclass
class DraftState:
    draft_id: str
    user_id: str
    my_slot: int
    shape: LeagueShape
    players: dict[str, Player]
    taken: set[str] = field(default_factory=set)
    picks: list[dict] = field(default_factory=list)
    slot_rosters: dict[int, list[str]] = field(default_factory=lambda: defaultdict(list))
    my_picks: list[int] = field(default_factory=list)
    slot_source: str = "unresolved"
    status: str = ""
    is_mock: bool = False


# =============================================================================
# LEAGUE SHAPE / SCORING
# =============================================================================
def shape_from_draft(draft: dict, league: dict | None) -> LeagueShape:
    s = draft.get("settings") or {}
    ls = (league or {}).get("settings") or {}
    sc = dict((league or {}).get("scoring_settings") or {})
    meta = draft.get("metadata") or {}

    slots = {
        "QB": int(s.get("slots_qb") or 1),
        "RB": int(s.get("slots_rb") or 2),
        "WR": int(s.get("slots_wr") or 2),
        "TE": int(s.get("slots_te") or 1),
        "FLEX": int(s.get("slots_flex") or 2),
        "SUPERFLEX": int(s.get("slots_super_flex") or 0),
        "K": int(s.get("slots_k") or 1),
        "DEF": int(s.get("slots_def") or 1),
        "BN": int(s.get("slots_bn") or 5),
    }
    if league and league.get("roster_positions"):
        c = Counter(league["roster_positions"])
        # Sleeper emits SUPER_FLEX with an underscore; v34 looked for SUPERFLEX
        # and therefore never detected a superflex league from roster_positions.
        alias = {
            "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K", "DEF": "DEF",
            "BN": "BN", "FLEX": "FLEX", "WRRB_FLEX": "FLEX", "REC_FLEX": "FLEX",
            "SUPER_FLEX": "SUPERFLEX", "SUPERFLEX": "SUPERFLEX", "QB/RB/WR/TE": "SUPERFLEX",
        }
        found: Counter = Counter()
        for key, n in c.items():
            tgt = alias.get(str(key).upper())
            if tgt:
                found[tgt] += int(n)
        for k, v in found.items():
            slots[k] = int(v)

    ppr = float(sc["rec"]) if "rec" in sc else 1.0
    if "rec" not in sc:
        st = str(meta.get("scoring_type", "")).lower()
        nm = str((league or {}).get("name") or meta.get("name") or "").lower()
        if "half" in st or "half" in nm:
            ppr = 0.5
        elif "std" in st or "standard" in st or "std" in nm or "standard" in nm:
            ppr = 0.0
        sc["rec"] = ppr

    scoring = dict(DEFAULT_SCORING)
    for k, v in sc.items():
        try:
            scoring[str(k)] = float(v)
        except (TypeError, ValueError):
            continue

    teams = int(s.get("teams") or (league or {}).get("total_rosters") or DEFAULT_TEAMS)
    playoff_teams = int(ls.get("playoff_teams") or DEFAULT_PLAYOFF_TEAMS)
    playoff_start = int(ls.get("playoff_week_start") or DEFAULT_PLAYOFF_START)
    if not (2 <= playoff_teams <= teams):
        playoff_teams = min(teams, DEFAULT_PLAYOFF_TEAMS)
    if not (10 <= playoff_start <= 17):
        playoff_start = DEFAULT_PLAYOFF_START

    return LeagueShape(
        teams=teams,
        rounds=int(s.get("rounds") or 15),
        snake=str(draft.get("type", "snake")).lower() == "snake",
        ppr=float(scoring.get("rec", 1.0)),
        te_bonus=float(scoring.get("bonus_rec_te") or 0.0),
        slots=slots,
        scoring=scoring,
        superflex=slots.get("SUPERFLEX", 0) > 0,
        playoff_teams=playoff_teams,
        playoff_start=playoff_start,
        reversal_round=int(s.get("reversal_round") or 0),
        name=str((league or {}).get("name") or meta.get("name") or ""),
    )


def score_row(row: dict, shape: LeagueShape, pos: str) -> tuple[float, str]:
    """Score a Sleeper projection row using the LEAGUE's scoring settings.

    v34 hardcoded 0.04/pass yd, 0.1/rush-rec yd and 6-pt TDs, ignored the
    DST points-allowed tiers entirely, and returned any positive number from
    a partial stat row - which is how Josh Jacobs ended up projected for 87.2
    points at ADP 34. Here every stat key in the row is matched against the
    scoring dict generically, and a row that scored on fewer than three keys
    is treated as incomplete.
    """
    sc = shape.scoring
    pts = 0.0
    hits = 0
    for key, val in row.items():
        if key in NON_SCORING_KEYS:
            continue
        w = sc.get(key)
        if w is None:
            continue
        try:
            v = float(val or 0)
        except (TypeError, ValueError):
            continue
        if v:
            pts += float(w) * v
            hits += 1
    if pos == "TE" and shape.te_bonus:
        pts += shape.te_bonus * float(row.get("rec") or 0)

    if hits >= 3 and pts > 0:
        return pts, "sleeper"

    # fall back to Sleeper's precomputed totals, then to the market curve
    if shape.ppr >= 0.99:
        pre = float(row.get("pts_ppr") or 0)
    elif shape.ppr >= 0.4:
        pre = float(row.get("pts_half_ppr") or 0)
    else:
        pre = float(row.get("pts_std") or 0)
    if pre > 0:
        return pre, "sleeper_total"
    return 0.0, "missing"


# --- continuous fallback curve ----------------------------------------------
# v34's fallback was piecewise with cliffs: RB dropped 199.6 -> 160.1 between
# ADP 30 and 31, WR 213 -> 155. This is monotone and continuous everywhere.
_CURVE = {           # peak, floor, half-life ADP, shape
    "QB": (378.0, 175.0, 58.0, 1.45),
    "RB": (332.0, 32.0, 45.0, 1.30),
    "WR": (326.0, 32.0, 50.0, 1.30),
    "TE": (262.0, 30.0, 36.0, 1.15),
}
_PPR_SCALE = {
    1.0: {"QB": 1.00, "RB": 1.00, "WR": 1.00, "TE": 1.00},
    0.5: {"QB": 1.00, "RB": 0.94, "WR": 0.87, "TE": 0.85},
    0.0: {"QB": 1.00, "RB": 0.88, "WR": 0.74, "TE": 0.70},
}


def market_curve(pos: str, adp: float, ppr: float, team: str = "") -> float:
    if pos in _CURVE:
        band = 1.0 if ppr >= 0.99 else (0.5 if ppr >= 0.4 else 0.0)
        k = _PPR_SCALE[band][pos]
        peak, floor, a0, b = _CURVE[pos]
        peak, floor = peak * k, floor * k
        return floor + (peak - floor) / (1.0 + (max(0.5, adp) / a0) ** b)
    if pos == "K":
        return 118.0 - min(14.0, adp * 0.05)
    if pos == "DEF":
        return 108.0 - min(16.0, adp * 0.05)
    return 60.0


# =============================================================================
# PLAYER LOADING
# =============================================================================
def ffc_format(ppr: float, superflex: bool) -> str:
    if superflex:
        return "2qb"
    if ppr >= 0.99:
        return "ppr"
    if ppr >= 0.4:
        return "half-ppr"
    return "standard"


def load_fantasycalc(shape: LeagueShape) -> dict[str, tuple[float, float]]:
    ppr_arg = 1 if shape.ppr >= 0.99 else (0.5 if shape.ppr >= 0.4 else 0)
    qbs = 2 if shape.superflex else 1
    url = f"{FANTASYCALC}?isDynasty=false&numQbs={qbs}&numTeams={shape.teams}&ppr={ppr_arg}"
    raw = try_json(url, timeout=18)
    out: dict[str, tuple[float, float]] = {}
    if not isinstance(raw, list):
        print("[init] FantasyCalc unavailable (optional).", flush=True)
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        sid = str((row.get("player") or {}).get("sleeperId") or "")
        if not sid or sid == "None":
            continue
        rank = float(row.get("overallRank") or 0)
        val = float(row.get("value") or row.get("redraftValue") or 0)
        if rank > 0:
            out[sid] = (rank, val)
    print(f"[init] FantasyCalc joined on {len(out)} Sleeper IDs.", flush=True)
    return out


def load_players(shape: LeagueShape) -> dict[str, Player]:
    print(f"[init] Ingesting Sleeper NFL universe ({shape.ppr} PPR)...", flush=True)
    raw = get_json(f"{BASE}/players/nfl")
    proj = try_json(f"{BASE}/projections/nfl/regular/{SEASON}") or {}
    fmt = ffc_format(shape.ppr, shape.superflex)
    ffc = try_json(f"{FFC}/{fmt}?teams={shape.teams}&year={SEASON}") or {}
    if not (isinstance(ffc, dict) and ffc.get("players")):
        ffc = try_json(f"{FFC}/{fmt}?teams={shape.teams}") or {}
    adds = try_json(f"{BASE}/players/nfl/trending/add?lookback_hours=48&limit=50") or []
    trend = {str(r.get("player_id")): 50 - i for i, r in enumerate(adds)} if isinstance(adds, list) else {}
    fc_map = load_fantasycalc(shape)

    ffc_map: dict[str, tuple[float, int]] = {}
    for row in (ffc.get("players") or []) if isinstance(ffc, dict) else []:
        nm = norm_name(str(row.get("name") or ""))
        pos = str(row.get("position") or "")
        tm = canon_team(str(row.get("team") or ""))
        adp = float(row.get("adp") or 350)
        bye = int(row.get("bye") or 0)
        ffc_map[f"{nm}|{pos}|{tm}"] = (adp, bye)
        ffc_map[f"{nm}|{pos}"] = (adp, bye)

    out: dict[str, Player] = {}
    if not isinstance(raw, dict):
        return out

    for pid, p in raw.items():
        if not isinstance(p, dict):
            continue
        pos = p.get("position") or ""
        if pos not in SKILL and pos not in LATE_ONLY:
            continue
        if pos == "DEF":
            team = canon_team(p.get("team") or str(pid))
            if team not in NFL_TEAMS:
                continue
            name, depth, years, inj = f"{team} Defense", 1, 0, ""
        else:
            team = canon_team(p.get("team") or "")
            if not team or team not in NFL_TEAMS:
                continue
            if p.get("active") is False:
                continue
            if str(p.get("status") or "").lower() in ("inactive", "retired", "na", "waived", "cut"):
                continue
            fn = (p.get("first_name") or "").strip()
            ln = (p.get("last_name") or "").strip()
            name = f"{fn} {ln}".strip() or p.get("full_name") or str(pid)
            depth = int(p.get("depth_chart_order") or 9)
            years = int(p.get("years_exp") or 0)
            inj = p.get("injury_status") or ""

        row = proj.get(str(pid)) if isinstance(proj, dict) else None
        row = row if isinstance(row, dict) else {}
        proj_pts, source = score_row(row, shape, pos)

        adp_sleeper = float(row.get("adp_ppr") or 0)
        hit = ffc_map.get(f"{norm_name(name)}|{pos}|{team}") or ffc_map.get(f"{norm_name(name)}|{pos}")
        cands = [x for x in (adp_sleeper, hit[0] if hit else 0.0) if 0 < x < 400]
        rank = p.get("search_rank")
        fallback = float(rank) if isinstance(rank, (int, float)) and 0 < rank < 99999 else 350.0
        adp = sum(cands) / len(cands) if cands else fallback

        if proj_pts <= 0:
            proj_pts = market_curve(pos, adp, shape.ppr, team)
            source = "market_curve"

        fc_rank, fc_val = fc_map.get(str(pid), (0.0, 0.0))
        bye = hit[1] if hit else int(p.get("bye_week") or 0)

        out[str(pid)] = Player(
            pid=str(pid), name=name, team=team, pos=pos, adp=adp, proj=proj_pts,
            years=years, injury=inj, bye=bye, depth=depth,
            trend=trend.get(str(pid), 0), fc_rank=fc_rank, fc_value=fc_val,
            sleeper_rank=fallback, proj_source=source, proj_raw=proj_pts,
            proj_adj=proj_pts, ppg=proj_pts / FULL_SEASON_WEEKS,
        )
    print(f"[init] {len(out)} player models ready.", flush=True)
    return out


# =============================================================================
# PROJECTION REPAIR
# =============================================================================
def data_quality(players: dict[str, Player], shape: LeagueShape) -> dict[str, Any]:
    """What the engine is actually running on, and whether to believe it.

    Every number downstream inherits the quality of these two inputs. A season
    whose projections have not been published yet, or an ADP feed that 404s,
    degrades the engine to a market-order board - and it should say so out
    loud rather than producing confident output from nothing.
    """
    skill = [p for p in players.values() if p.pos in SKILL]
    src_counts = Counter(p.proj_source for p in skill)
    n = max(1, len(skill))
    curve = src_counts.get("market_curve", 0) / n
    no_adp = sum(1 for p in skill if p.adp >= 340) / n
    warnings: list[str] = []
    if curve > 0.35:
        warnings.append(f"{curve:.0%} of projections are the fallback market curve - "
                        f"Sleeper has little or no {SEASON} projection data yet. "
                        f"The engine is effectively ranking on ADP.")
    if no_adp > 0.5:
        warnings.append(f"{no_adp:.0%} of players have no usable ADP. Survival "
                        f"probabilities will be weak until the room calibrates itself.")
    top = sorted(skill, key=lambda p: p.adp)[:60]
    if top and sum(1 for p in top if p.proj_source == "market_curve") / len(top) > 0.25:
        warnings.append("Several early-round players have no real projection.")
    return {"sources": dict(src_counts), "market_curve_frac": curve,
            "no_adp_frac": no_adp, "warnings": warnings}


def repair_projections(
    players: dict[str, Player], window: int = 5, tolerate_z: float = 2.0
) -> list[tuple[str, float, float]]:
    """Cross-check every projection against the market before anything uses it.

    Inside each position, players are ordered by ADP and each projection is
    compared to the median of its ADP neighbours. Anything more than
    `tolerate_z` robust deviations away is shrunk toward that median, harder
    the further out it is. This is what catches a broken Sleeper stat row -
    v34 had no such check and let a 121-point error into VORP at 45% weight.
    """
    by_pos: dict[str, list[Player]] = defaultdict(list)
    for p in players.values():
        p.proj_adj = float(p.proj)
        p.ppg = p.proj_adj / FULL_SEASON_WEEKS
        if p.pos in SKILL and 0 < p.adp < 260:
            by_pos[p.pos].append(p)

    repairs: list[tuple[str, float, float]] = []
    for group in by_pos.values():
        group.sort(key=lambda x: x.adp)
        projs = [x.proj for x in group]
        n = len(projs)
        if n < 2 * window + 3:
            continue
        for i, p in enumerate(group):
            lo, hi = max(0, i - window), min(n, i + window + 1)
            nbrs = sorted(projs[lo:hi])
            med = nbrs[len(nbrs) // 2]
            mad = sorted(abs(v - med) for v in nbrs)[len(nbrs) // 2]
            scale = max(12.0, 1.4826 * mad)
            z = (p.proj - med) / scale
            if abs(z) <= tolerate_z:
                continue
            excess = abs(z) - tolerate_z
            w = 1.0 / (1.0 + excess * excess)
            p.proj_adj = w * p.proj + (1.0 - w) * med
            p.ppg = p.proj_adj / FULL_SEASON_WEEKS
            repairs.append((p.name, round(p.proj, 1), round(p.proj_adj, 1)))

    repairs.sort(key=lambda r: -abs(r[1] - r[2]))
    return repairs


# =============================================================================
# FROZEN BASELINE: REPLACEMENT LEVEL + WAIVER FLOOR
# =============================================================================
def flex_share(ppr: float) -> dict[str, float]:
    if ppr >= 0.99:
        return FLEX_SHARE_PPR
    if ppr >= 0.4:
        return FLEX_SHARE_HALF
    return FLEX_SHARE_STD


def replacement_index(shape: LeagueShape) -> dict[str, int]:
    """League-wide STARTER demand at each position. Fixed for the whole draft."""
    t = shape.teams
    f = float(shape.slots.get("FLEX", 0))
    sf = float(shape.slots.get("SUPERFLEX", 0))
    sh = flex_share(shape.ppr)
    return {
        "QB": max(1, round(t * (shape.slots.get("QB", 1) + 0.85 * sf))),
        "RB": max(1, round(t * (shape.slots.get("RB", 2) + sh["RB"] * f + 0.15 * sf))),
        "WR": max(1, round(t * (shape.slots.get("WR", 2) + sh["WR"] * f))),
        "TE": max(1, round(t * (shape.slots.get("TE", 1) + sh["TE"] * f))),
        "K": t,
        "DEF": t,
    }


@dataclass
class Baseline:
    repl: dict[str, float]        # season points of the last league-wide starter
    stream_ppg: dict[str, float]  # what you can put in an empty slot, free
    idx: dict[str, int]
    shape: LeagueShape

    def vorp(self, p: Player) -> float:
        return p.proj_adj - self.repl.get(p.pos, 0.0)


def freeze_baseline(players: dict[str, Player], shape: LeagueShape) -> Baseline:
    """Compute replacement level AND the waiver floor once, pre-draft.

    The waiver floor is the number v34 never had. Without it an empty QB slot
    looks like zero points, so a backup QB appears to be worth a full bye
    week instead of ~2 points, and the engine drafts a QB2 in round 10.
    """
    idx = replacement_index(shape)
    draftable = shape.teams * shape.rounds

    by_pos: dict[str, list[float]] = defaultdict(list)
    drafted_count: Counter = Counter()
    for p in players.values():
        by_pos[p.pos].append(p.proj_adj)
        if 0 < p.adp <= draftable:
            drafted_count[p.pos] += 1
    for pos in by_pos:
        by_pos[pos].sort(reverse=True)

    repl: dict[str, float] = {}
    stream: dict[str, float] = {}
    for pos, vals in by_pos.items():
        if not vals:
            repl[pos] = stream[pos] = 0.0
            continue
        i = min(len(vals) - 1, max(0, idx.get(pos, len(vals)) - 1))
        repl[pos] = vals[i]
        # Expected BEST of the undrafted pool you actually get a shot at, which
        # is what STREAM_POOL/STREAM_CLAIM_ODDS were always meant to express.
        # The old code took one player at the draft cutoff and then clamped at
        # 0.55 * replacement -- and for RB and WR the clamp always bound, so a
        # hardcoded constant, not the board, was pricing every empty skill slot.
        j = min(len(vals) - 1, max(0, drafted_count.get(pos, i)))
        pool = vals[j:j + STREAM_POOL.get(pos, 12)]
        if pool:
            k = max(1, int(round(len(pool) * STREAM_CLAIM_ODDS)))
            raw = sum(pool[:k]) / k * STREAM_HAIRCUT.get(pos, 0.92)
        else:
            raw = vals[j] * STREAM_HAIRCUT.get(pos, 0.92)
        stream[pos] = min(raw, repl[pos])

    base = Baseline(
        repl=repl,
        stream_ppg={k: v / FULL_SEASON_WEEKS for k, v in stream.items()},
        idx=idx, shape=shape,
    )
    for p in players.values():
        p.vorp = base.vorp(p)
    return base


# =============================================================================
# THE OBJECTIVE FUNCTION
# =============================================================================
def expected_games(p: Player) -> float:
    return GAMES_BY_POS.get(p.pos, 15.2) * INJURY_GAMES_MULT.get(p.injury or "", 1.0)


def _sample_absences(roster: Sequence[Player],
                     rng: random.Random) -> dict[str, tuple[int, int]]:
    """One contiguous injury absence per player, as (first_week, last_week).

    Exactly TWO random draws are consumed per player no matter what the
    outcome is. That matters more than it looks: marginal value is measured as
    a difference between two rosters, and if a player who happens to miss zero
    games consumes fewer draws than one who misses four, the random streams
    desynchronize and every candidate gets independent noise instead of shared
    noise. With a fixed draw count the comparison is properly paired, which is
    the difference between a usable late-round ranking and coin flips.
    """
    out: dict[str, tuple[int, int]] = {}
    for p in roster:
        u_len = rng.random()
        u_pos = rng.random()
        miss = FULL_SEASON_WEEKS - expected_games(p)
        n = int(miss) + (1 if u_len < (miss - int(miss)) else 0)
        if n <= 0:
            continue
        span = max(1, FULL_SEASON_WEEKS - n)
        start = 1 + min(span - 1, int(u_pos * span))
        out[p.pid] = (start, start + n - 1)
    return out


def week_points(present: Sequence[Player], shape: LeagueShape,
                stream_ppg: dict[str, float], eff: dict[str, float] | None = None,
                mult: dict[str, float] | None = None) -> float:
    """Best legal lineup for one week. Empty slots are filled off waivers.

    Greedy is exactly optimal for this slot structure: dedicated slots draw
    only from their own position, then FLEX takes the best leftover, so no
    higher-priority slot ever competes with a lower one for the same player.

    `eff` overrides each player's per-game level for BOTH lineup selection and
    scoring: it carries the season-long talent draw, which you learn over the
    first few weeks and then set your lineup around. `mult` is the week-to-week
    noise on top, applied only to scoring - so the lineup is CHOSEN before the
    week and SCORED after, and a boom/bust bench player gets no hindsight credit.
    """
    def level(p: Player) -> float:
        return eff[p.pid] if eff is not None and p.pid in eff else p.ppg

    by: dict[str, list[Player]] = defaultdict(list)
    for p in present:
        by[p.pos].append(p)
    for pos in by:
        by[pos].sort(key=level, reverse=True)

    def realized(p: Player) -> float:
        return level(p) * mult[p.pid] if mult is not None else level(p)

    total = 0.0
    used: set[str] = set()
    for pos in LINEUP_ORDER:
        need = int(shape.slots.get(pos, 0))
        if need <= 0:
            continue
        have = by.get(pos, [])[:need]
        for p in have:
            total += realized(p)
            used.add(p.pid)
        total += (need - len(have)) * stream_ppg.get(pos, 0.0)

    flex_need = int(shape.slots.get("FLEX", 0))
    if flex_need > 0:
        pool = [p for p in present if p.pid not in used and p.pos in FLEX_ELIGIBLE]
        pool.sort(key=level, reverse=True)
        take = pool[:flex_need]
        for p in take:
            total += realized(p)
            used.add(p.pid)
        fb = max(stream_ppg.get("RB", 0.0), stream_ppg.get("WR", 0.0))
        total += (flex_need - len(take)) * fb

    sf_need = int(shape.slots.get("SUPERFLEX", 0))
    if sf_need > 0:
        pool = [p for p in present if p.pid not in used and p.pos in SKILL]
        pool.sort(key=level, reverse=True)
        take = pool[:sf_need]
        for p in take:
            total += realized(p)
            used.add(p.pid)
        total += (sf_need - len(take)) * stream_ppg.get("QB", 0.0)

    return total


def _week_weights(shape: LeagueShape) -> dict[int, float]:
    w = {k: 1.0 for k in shape.reg_weeks}
    w.update({k: PLAYOFF_WEIGHT for k in shape.playoff_weeks})
    return w


def season_value(roster: Sequence[Player], shape: LeagueShape, base: Baseline,
                 sims: int = 0, rng: random.Random | None = None) -> float:
    """Expected STARTING-LINEUP points across the season. The only objective.

    Weeks 15-17 carry PLAYOFF_WEIGHT because with 6 of 10 teams qualifying,
    the regular season is a low bar and the title is decided in those three
    weeks.

    sims == 0 -> deterministic, byes only. Fast; used to rank candidates.
    sims  > 0 -> also samples a contiguous injury absence per player, which
                 is what actually prices bench insurance.
    """
    if not roster:
        roster = []
    weights = _week_weights(shape)

    if sims <= 0:
        total = 0.0
        for wk, wt in weights.items():
            present = [p for p in roster if int(p.bye or 0) != wk]
            total += wt * week_points(present, shape, base.stream_ppg)
        return total

    rng = rng or random.Random(0)
    acc = 0.0
    for _ in range(sims):
        out_ranges: dict[str, tuple[int, int]] = _sample_absences(roster, rng)
        for wk, wt in weights.items():
            present = []
            for p in roster:
                if int(p.bye or 0) == wk:
                    continue
                r = out_ranges.get(p.pid)
                if r and r[0] <= wk <= r[1]:
                    continue
                present.append(p)
            acc += wt * week_points(present, shape, base.stream_ppg)
    return acc / sims


def marginal_value(p: Player, roster: Sequence[Player], shape: LeagueShape,
                   base: Baseline, roster_value: float | None = None,
                   sims: int = 0, rng_seed: int = 20260901) -> float:
    """Season points this player ADDS to this exact roster.

    This is the number that replaces value_score. A QB2 in a 1QB league only
    scores on the QB1's bye, and only above the waiver floor, so it comes out
    around +2 - which is the correct answer and the one v34 could not reach.
    """
    if roster_value is None:
        roster_value = season_value(roster, shape, base, sims=sims,
                                    rng=random.Random(rng_seed))
    return season_value(list(roster) + [p], shape, base, sims=sims,
                        rng=random.Random(rng_seed)) - roster_value


def team_week_profile(roster: Sequence[Player], shape: LeagueShape, base: Baseline,
                      sims: int = POST_DRAFT_TEAM_SIMS, seed: int = 11
                      ) -> tuple[float, float, float, float]:
    """(reg_mean, reg_sd, playoff_mean, playoff_sd) weekly points.

    v34 used FantasyCalc's ADP standard deviation - a measure of where in the
    draft a player goes - as weekly scoring variance. This samples injuries,
    byes and a per-position lognormal weekly distribution instead, and the
    lineup is set on projections before the scores are revealed.
    """
    rng = random.Random(seed)
    reg: list[float] = []
    post: list[float] = []
    sigmas = {p.pid: math.sqrt(math.log(1.0 + WEEKLY_CV.get(p.pos, 0.55) ** 2)) for p in roster}
    for _ in range(sims):
        out_ranges: dict[str, tuple[int, int]] = _sample_absences(roster, rng)
        for wk in range(1, FULL_SEASON_WEEKS):
            present = []
            for p in roster:
                if int(p.bye or 0) == wk:
                    continue
                r = out_ranges.get(p.pid)
                if r and r[0] <= wk <= r[1]:
                    continue
                present.append(p)
            mult = {}
            for p in present:
                s = sigmas[p.pid]
                mult[p.pid] = math.exp(rng.gauss(-0.5 * s * s, s))
            pts = week_points(present, shape, base.stream_ppg, mult=mult)
            (post if wk >= shape.playoff_start else reg).append(pts)

    def ms(xs: list[float]) -> tuple[float, float]:
        if not xs:
            return 0.0, 1.0
        m = sum(xs) / len(xs)
        v = sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1)
        return m, max(1.0, math.sqrt(v))

    rm, rs = ms(reg)
    pm, ps = ms(post)
    return rm, rs, pm, ps


# =============================================================================
# TITLE ODDS - THE ACTUAL OBJECTIVE
# =============================================================================
TITLE_SIMS = 3000


def season_split(roster: Sequence[Player], shape: LeagueShape, base: Baseline,
                 sims: int = 0, rng: random.Random | None = None) -> tuple[float, float]:
    """(regular-season weekly mean, playoff-weeks weekly mean), unweighted.

    Same machinery as season_value but it keeps the two halves of the calendar
    apart, because seeding is decided by weeks 1-14 and the bracket by 15-17,
    and the title model needs them separately rather than fused behind a
    single hand-picked playoff weight.
    """
    reg_w, po_w = shape.reg_weeks, shape.playoff_weeks
    if sims <= 0:
        reg = sum(week_points([p for p in roster if int(p.bye or 0) != w],
                              shape, base.stream_ppg) for w in reg_w)
        po = sum(week_points([p for p in roster if int(p.bye or 0) != w],
                             shape, base.stream_ppg) for w in po_w)
        return reg / max(1, len(reg_w)), po / max(1, len(po_w))

    rng = rng or random.Random(0)
    reg = po = 0.0
    for _ in range(sims):
        out_ranges = _sample_absences(roster, rng)

        def present(w: int) -> list[Player]:
            keep = []
            for p in roster:
                if int(p.bye or 0) == w:
                    continue
                r = out_ranges.get(p.pid)
                if r and r[0] <= w <= r[1]:
                    continue
                keep.append(p)
            return keep

        reg += sum(week_points(present(w), shape, base.stream_ppg) for w in reg_w)
        po += sum(week_points(present(w), shape, base.stream_ppg) for w in po_w)
    return (reg / sims / max(1, len(reg_w))), (po / sims / max(1, len(po_w)))


def team_cv(roster: Sequence[Player], shape: LeagueShape, base: Baseline) -> float:
    """Weekly coefficient of variation of the starting lineup.

    Each starter contributes CV*ppg of independent week-to-week dispersion;
    an unfilled slot contributes the waiver floor's. Returned as a ratio so it
    can be applied to whichever mean the caller has.
    """
    by: dict[str, list[Player]] = defaultdict(list)
    for p in roster:
        by[p.pos].append(p)
    for pos in by:
        by[pos].sort(key=lambda x: -x.ppg)

    mean = var = 0.0
    used: set[str] = set()

    def add(p: Player) -> None:
        nonlocal mean, var
        mean += p.ppg
        v = WEEKLY_CV.get(p.pos, 0.55) * p.ppg
        var += v * v

    def add_stream(pos: str, n: int) -> None:
        nonlocal mean, var
        if n <= 0:
            return
        s = base.stream_ppg.get(pos, 0.0)
        mean += n * s
        v = WEEKLY_CV.get(pos, 0.55) * s
        var += n * v * v

    for pos in LINEUP_ORDER:
        need = int(shape.slots.get(pos, 0))
        if need <= 0:
            continue
        have = by.get(pos, [])[:need]
        for p in have:
            add(p)
            used.add(p.pid)
        add_stream(pos, need - len(have))

    for slot_name, eligible, fallback in (("FLEX", FLEX_ELIGIBLE, "RB"),
                                          ("SUPERFLEX", tuple(SKILL), "QB")):
        need = int(shape.slots.get(slot_name, 0))
        if need <= 0:
            continue
        pool = sorted((p for p in roster if p.pid not in used and p.pos in eligible),
                      key=lambda x: -x.ppg)
        for p in pool[:need]:
            add(p)
            used.add(p.pid)
        add_stream(fallback, need - len(pool[:need]))

    return math.sqrt(max(1.0, var)) / max(1.0, mean)


class TitleModel:
    """Monte Carlo of the season and the bracket, with the field held fixed.

    Expected points is a proxy. What you are actually maximising is the
    probability of winning the league, and the two come apart at the margin:
    variance is worth buying when you trail the field and worth shedding when
    you lead it, and a three-week single-elimination bracket pays for a
    ceiling in a way a 14-week regular season does not.

    Two things make the comparison between candidates trustworthy. The field
    is drawn ONCE per pick and reused. And your own weekly draws are stored as
    standard normals and merely rescaled per candidate, so two candidates see
    exactly the same luck - which `random.gauss` would quietly break, since it
    caches a spare value and therefore desynchronises the stream.
    """

    def __init__(self, state: DraftState, base: Baseline, sims: int = TITLE_SIMS,
                 seed: int = 8675309, opp_sims: int = 40) -> None:
        shape = state.shape
        self.shape, self.sims = shape, sims
        self.weeks = len(shape.reg_weeks)
        self.po_rounds = len(shape.playoff_weeks)
        rng = random.Random(seed)

        opp_slots = [s for s in range(1, shape.teams + 1) if s != state.my_slot]
        prof = []
        for s in opp_slots:
            roster = [state.players[pid] for pid in state.slot_rosters.get(s, [])
                      if pid in state.players]
            reg_m, po_m = season_split(roster, shape, base, sims=opp_sims,
                                       rng=random.Random(seed + s))
            cv = team_cv(roster, shape, base)
            prof.append((reg_m, reg_m * cv, po_m, po_m * cv))
        self.n_opp = len(prof)
        self.opp_profiles = prof

        self.opp_reg: list[list[list[float]]] = []
        self.opp_po: list[list[list[float]]] = []
        self.opp_base_wins: list[list[int]] = []
        self.my_z: list[list[float]] = []
        self.my_po_z: list[list[float]] = []
        for _ in range(sims):
            wk = [[m + sd * rng.normalvariate(0.0, 1.0) for m, sd, _, _ in prof]
                  for _ in range(self.weeks)]
            po = [[pm + psd * rng.normalvariate(0.0, 1.0) for _, _, pm, psd in prof]
                  for _ in range(self.po_rounds)]
            wins = [0] * self.n_opp
            for row in wk:
                for j, rank in enumerate(sorted(range(self.n_opp), key=lambda k: row[k])):
                    wins[rank] += j          # all-play wins among the opponents
            self.opp_reg.append(wk)
            self.opp_po.append(po)
            self.opp_base_wins.append(wins)
            self.my_z.append([rng.normalvariate(0.0, 1.0) for _ in range(self.weeks)])
            self.my_po_z.append([rng.normalvariate(0.0, 1.0) for _ in range(self.po_rounds)])

    def title_odds(self, reg_mean: float, reg_sd: float,
                   po_mean: float, po_sd: float) -> tuple[float, float, float]:
        """(title %, playoff %, standard error) for one candidate roster."""
        pt = self.shape.playoff_teams
        n_opp = self.n_opp
        titles = made = 0
        for s in range(self.sims):
            zrow = self.my_z[s]
            opp_wk = self.opp_reg[s]
            my_wins = 0
            opp_wins = list(self.opp_base_wins[s])
            for w in range(self.weeks):
                mine = reg_mean + reg_sd * zrow[w]
                row = opp_wk[w]
                for j in range(n_opp):
                    if mine > row[j]:
                        my_wins += 1
                    else:
                        opp_wins[j] += 1
            order = sorted(range(-1, n_opp),
                           key=lambda j: -(my_wins if j < 0 else opp_wins[j]))
            seeds = order[:pt]
            if -1 not in seeds:
                continue
            made += 1
            po_row = self.opp_po[s]
            pz = self.my_po_z[s]

            def score(team: int, r: int) -> float:
                return (po_mean + po_sd * pz[r]) if team < 0 else po_row[r][team]

            if _run_bracket(seeds, score) < 0:
                titles += 1
        p = titles / float(self.sims)
        se = math.sqrt(max(p * (1.0 - p), 1e-9) / self.sims)
        return 100.0 * p, 100.0 * made / self.sims, 100.0 * se


def _run_bracket(seeds: list[int], score) -> int:
    """Reseeded single elimination, byes to the top seeds so the field halves.

    For 6 of 12: seeds 1-2 idle in week 15, 3v6 and 4v5 play, semifinals week
    16, final week 17.
    """
    n = len(seeds)
    if n <= 1:
        return seeds[0] if seeds else 0
    pow2 = 1
    while pow2 < n:
        pow2 *= 2
    byes = pow2 - n
    rank = {t: i for i, t in enumerate(seeds)}
    alive, r, first = list(seeds), 0, True
    while len(alive) > 1:
        resting, playing = (alive[:byes], alive[byes:]) if (first and byes) else ([], alive)
        first = False
        winners = [playing[i] if score(playing[i], r) >= score(playing[len(playing) - 1 - i], r)
                   else playing[len(playing) - 1 - i]
                   for i in range(len(playing) // 2)]
        alive = sorted(resting + winners, key=lambda t: rank[t])
        r += 1
    return alive[0]


# =============================================================================
# DRAFT GEOMETRY / ROSTER HELPERS
# =============================================================================
def _reversed_round(rnd: int, snake: bool, reversal_round: int = 0) -> bool:
    """Does this round run back-to-front?

    Plain snake alternates. Sleeper's `reversal_round` (3rd-round reversal is
    the common one) flips the parity from that round onward, so round 2 and
    round 3 both run back-to-front and the alternation resumes after. v34 and
    earlier v35 ignored the setting entirely, which silently mislocated every
    one of your picks from round 3 on in any league that uses it.
    """
    if not snake:
        return False
    rev = (rnd % 2 == 0)
    if reversal_round and rnd >= reversal_round:
        rev = not rev
    return rev


def pick_to_slot(pick_no: int, teams: int, snake: bool, reversal_round: int = 0) -> int:
    rnd = (pick_no - 1) // teams + 1
    idx = (pick_no - 1) % teams
    return (teams - idx) if _reversed_round(rnd, snake, reversal_round) else (idx + 1)


def slot_pick_number(slot: int, rnd: int, teams: int, snake: bool,
                     reversal_round: int = 0) -> int:
    col = (teams - slot + 1) if _reversed_round(rnd, snake, reversal_round) else slot
    return (rnd - 1) * teams + col


def my_pick_numbers(slot: int, teams: int, rounds: int, snake: bool,
                    reversal_round: int = 0) -> list[int]:
    return [slot_pick_number(slot, r, teams, snake, reversal_round)
            for r in range(1, rounds + 1)]


def resolve_owned_picks(draft: dict, traded: list | None, shape: LeagueShape,
                        my_slot: int) -> list[int]:
    """Which board positions do YOU actually own?

    A clean snake is an assumption, not a fact: Sleeper lets managers trade
    picks, so a slot can own two picks in one round and none in another. Every
    survival probability in the engine is measured against "your next pick", so
    getting this wrong quietly corrupts the whole second half of the decision.
    Falls back to the clean snake when the league data isn't available.
    """
    default = my_pick_numbers(my_slot, shape.teams, shape.rounds, shape.snake,
                              shape.reversal_round)
    s2r = draft.get("slot_to_roster_id") or {}
    if not s2r or not isinstance(s2r, dict):
        return default
    try:
        slot_of = {int(v): int(k) for k, v in s2r.items() if v is not None}
        my_roster = int(s2r.get(str(my_slot)) or s2r.get(my_slot) or 0)
    except (TypeError, ValueError):
        return default
    if not my_roster:
        return default

    owner: dict[tuple[int, int], int] = {}       # (round, original roster) -> owner
    for t in (traded or []):
        if not isinstance(t, dict):
            continue
        try:
            owner[(int(t["round"]), int(t["roster_id"]))] = int(t["owner_id"])
        except (KeyError, TypeError, ValueError):
            continue

    mine: list[int] = []
    for rnd in range(1, shape.rounds + 1):
        for roster_id, slot in slot_of.items():
            if owner.get((rnd, roster_id), roster_id) == my_roster:
                mine.append(slot_pick_number(slot, rnd, shape.teams, shape.snake,
                                             shape.reversal_round))
    return sorted(mine) or default


def counts_of(ids: Sequence[str], players: dict[str, Player]) -> Counter:
    c: Counter = Counter()
    for pid in ids:
        p = players.get(pid)
        if p:
            c[p.pos] += 1
    return c


def holes(c: Counter, shape: LeagueShape) -> dict[str, int]:
    s = shape.slots
    need = {pos: max(0, s.get(pos, 0) - c.get(pos, 0)) for pos in ("QB", "RB", "WR", "TE", "K", "DEF")}
    extras = sum(max(0, c.get(pos, 0) - s.get(pos, 0)) for pos in ("RB", "WR", "TE"))
    need["FLEX"] = max(0, s.get("FLEX", 0) - extras)
    need["skill"] = need["QB"] + need["RB"] + need["WR"] + need["TE"] + need["FLEX"]
    need["total"] = need["skill"] + need["K"] + need["DEF"]
    return need


def single_slot(pos: str, shape: LeagueShape) -> bool:
    """True when a position feeds exactly one starting slot and no flex."""
    if pos in LATE_ONLY:
        return True
    if pos == "QB":
        return not shape.superflex and shape.slots.get("QB", 1) <= 1
    if pos == "TE":
        return shape.slots.get("TE", 1) <= 1 and shape.slots.get("FLEX", 0) == 0
    return False


def legality_forced(c: Counter, shape: LeagueShape, picks_left: int) -> list[str]:
    """Positions that must be taken NOW or you cannot field a legal lineup.

    This is the only hard rule left in the engine, and it covers EVERY
    dedicated starting slot, not just K and DST. The objective function is
    happy to punt a starting TE or a second RB whenever the waiver floor looks
    close, because it prices an empty slot at the streaming level and never
    at the real cost of having to win that claim every single week. Rostering
    your own starters is a constraint, not a value judgement, so it is
    enforced here rather than argued with in the score.

    Everything else about K and DST timing is still left to marginal value:
    they are worth roughly 10-14 points over the waiver floor, which lands
    them in the last rounds on their own without a round gate.
    """
    h = holes(c, shape)
    missing = [pos for pos in ("QB", "RB", "WR", "TE", "K", "DEF") if h[pos] > 0]
    if missing and picks_left <= h["total"]:
        return missing
    return []


def fills_label(pos: str, c: Counter, shape: LeagueShape) -> str:
    s = shape.slots
    have = c.get(pos, 0)
    if pos in ("K", "DEF"):
        return f"{pos}1 Starter" if have < s.get(pos, 1) else "Duplicate"
    if pos == "QB":
        return "QB1 Starter" if have < s.get("QB", 1) else "QB2 Backup"
    if pos == "TE":
        return "TE1 Starter" if have < s.get("TE", 1) else "TE2 Backup"
    dedicated = s.get(pos, 0)
    if have < dedicated:
        return f"{pos}{have + 1} Starter"
    extras = sum(max(0, c.get(x, 0) - s.get(x, 0)) for x in ("RB", "WR", "TE"))
    return "FLEX Starter" if extras < s.get("FLEX", 0) else "Bench/Upside"


# =============================================================================
# OPPONENT MODEL AND SURVIVAL
# =============================================================================
@dataclass
class RoomModel:
    """The opponent model, fitted to the room actually in front of you.

    v35 shipped with a fixed ADP temperature and a single per-manager reach
    bias. That is an assumption about how a draft behaves, and when the
    assumption is wrong the survival term - which is half of every decision -
    is wrong with it. Measured in a room drafting at random, the fixed model
    claimed a 48% chance the best player available would last eight picks when
    the true figure was 97%.

    So the parameters are no longer assumed. Every completed pick is a labelled
    example: the model scores everyone who was available, and the player who
    actually went is the answer. Fitting `shift` and `tau` by maximum
    likelihood over that record makes the engine chalky in a chalky room and
    near-uniform in a chaotic one, without anyone tuning a constant.
    """
    shift: float = 0.0            # observed drift vs ADP, reported only
    tau: float = ADP_TAU          # how deep into the board this room reaches
    need_gamma: float = 1.0       # how much this room chases roster holes
    pos_shift: dict[str, float] = field(default_factory=dict)
    slot_bias: dict[int, float] = field(default_factory=dict)
    n_obs: int = 0
    fit_quality: float = 0.0      # mean log-likelihood per observed pick

    @property
    def descriptor(self) -> str:
        if self.n_obs < MIN_CALIBRATION_PICKS:
            return "priors (too few picks to fit)"
        need = "" if self.need_gamma >= 0.8 else " and ignores roster need"
        if self.tau <= 1.5:
            return f"chalky - picks from the top {max(1, round(self.tau * 2))} on the board{need}"
        if self.tau <= 6.0:
            return f"normal - reaches about {round(self.tau * 2)} deep"
        if self.tau <= 30.0:
            return f"loose - reaches about {round(self.tau * 2)} deep, ADP is a weak guide"
        return "chaotic - ADP barely predicts this room"

    def adp_eff(self, p: Player, slot: int = 0) -> float:
        """ADP adjusted for this room. Only the ORDER it induces is used."""
        return (p.adp - self.pos_shift.get(p.pos, 0.0)
                - 0.35 * self.slot_bias.get(slot, 0.0))


MIN_CALIBRATION_PICKS = 12
CAL_WINDOW = 36
CAL_POOL = 130
# tau is measured in PLAYERS DEEP into the board, not in picks. Scoring on raw
# ADP distance assumes ADP is calibrated to pick numbers, and when it isn't -
# a different-sized league, a stale source, an early-season board - the
# exponential saturates and the model flattens into "everyone survives".
# Ranking among available players is scale-free and survives all of that.
CAL_TAUS = (0.12, 0.2, 0.32, 0.5, 0.8, 1.3, 2.0, 3.2, 5.0, 8.0, 13.0, 21.0,
            34.0, 60.0, 120.0, 400.0)
# How much this room lets roster need override the board. Some rooms draft
# pure best-available and some chase holes; fitting it rather than assuming it
# stops the need multiplier from smearing probability across positions in a
# room that plainly ignores need.
CAL_GAMMAS = (0.0, 0.5, 1.0, 1.6)


def fit_room(state: DraftState) -> RoomModel:
    """Maximum-likelihood fit of the opponent model to the picks so far."""
    players, shape = state.players, state.shape
    picks = state.picks
    n = len(picks)
    room = RoomModel(slot_bias=infer_reach_bias(picks, players), n_obs=n)
    if n < MIN_CALIBRATION_PICKS:
        return room

    start = max(0, n - CAL_WINDOW)
    # replay the board so each observation sees the pool as it really was
    taken_before: list[set[str]] = []
    counts_before: list[dict[int, Counter]] = []
    taken: set[str] = set()
    rosters: dict[int, Counter] = defaultdict(Counter)
    for i, pk in enumerate(picks):
        if i >= start:
            taken_before.append(set(taken))
            counts_before.append({k: Counter(v) for k, v in rosters.items()})
        pid = str(pk.get("player_id") or "")
        taken.add(pid)
        p = players.get(pid)
        sl = int(pk.get("draft_slot") or 0)
        if p and sl:
            rosters[sl][p.pos] += 1

    obs: list[tuple[int, int, Player, list[Player], dict[int, Counter]]] = []
    for j, i in enumerate(range(start, n)):
        pid = str(picks[i].get("player_id") or "")
        chosen = players.get(pid)
        if not chosen or not (0 < chosen.adp < 400):
            continue
        gone = taken_before[j]
        pool = sorted((p for p in players.values() if p.pid not in gone and 0 < p.adp < 400),
                      key=lambda x: x.adp)
        if chosen not in pool:
            pool.append(chosen)
        obs.append((i + 1, int(picks[i].get("draft_slot") or 0), chosen, pool, counts_before[j]))
    if len(obs) < MIN_CALIBRATION_PICKS:
        return room

    best = (-1e18, ADP_TAU, 1.0)
    for tau in CAL_TAUS:
        for gamma in CAL_GAMMAS:
            total = 0.0
            for k, slot, chosen, pool, counts in obs:
                rnd = (k - 1) // shape.teams + 1
                c = counts.get(slot, Counter())
                wsum = w_obs = 0.0
                for rank, p in enumerate(pool):
                    decay = math.exp(-rank / tau)
                    if decay < 1e-12 and p is not chosen:
                        break
                    w = decay * (_need_mult(p.pos, c, shape, rnd) ** gamma)
                    wsum += w
                    if p is chosen:
                        w_obs = w
                total += math.log(max(w_obs, 1e-12) / max(wsum, 1e-12))
            if total > best[0]:
                best = (total, tau, gamma)
    room.fit_quality = best[0] / max(1, len(obs))
    room.tau, room.need_gamma = best[1], best[2]

    # position-level drift on top of the global fit: if this room is taking
    # backs a round early, that is information ADP does not carry
    resid: dict[str, list[float]] = defaultdict(list)
    allr: list[float] = []
    for k, _slot, chosen, _pool, _c in obs:
        resid[chosen.pos].append(k - chosen.adp)
        allr.append(k - chosen.adp)
    allr.sort()
    global_med = allr[len(allr) // 2] if allr else 0.0
    room.shift = global_med
    for pos, vals in resid.items():
        if len(vals) >= 4:
            vals.sort()
            med = vals[len(vals) // 2]
            room.pos_shift[pos] = max(-14.0, min(14.0, med - global_med))
    return room


def infer_reach_bias(picks: Sequence[dict], players: dict[str, Player]) -> dict[int, float]:
    """mean(pick_no - adp) per slot. Negative means that manager reaches.

    v34 assigned opponents a random archetype from a fixed 55/30/15 mix and
    ignored what they had actually done. By round 5 you have real evidence.
    """
    agg: dict[int, list[float]] = defaultdict(list)
    for i, pk in enumerate(picks):
        slot = int(pk.get("draft_slot") or 0)
        p = players.get(str(pk.get("player_id") or ""))
        if slot and p and 0 < p.adp < 300:
            agg[slot].append((i + 1) - p.adp)
    return {s: sum(v) / len(v) for s, v in agg.items() if v}


def _need_mult(pos: str, counts: Counter, shape: LeagueShape, rnd: int) -> float:
    if pos in LATE_ONLY:
        if rnd < shape.rounds - 1:
            return LATE_ONLY_SUPPRESSION
        return 3.0 if counts.get(pos, 0) < shape.slots.get(pos, 1) else 0.05
    have = counts.get(pos, 0)
    if pos == "QB" and not shape.superflex:
        if have >= 1:
            return NEED_MULT_SATURATED
        return NEED_MULT_OPEN_SLOT if rnd >= 5 else 0.8
    if pos == "TE":
        if have >= 1:
            return NEED_MULT_SATURATED
        return NEED_MULT_OPEN_SLOT if rnd >= 4 else 1.0
    dedicated = shape.slots.get(pos, 0)
    if have < dedicated:
        return NEED_MULT_OPEN_SLOT
    if have < dedicated + shape.slots.get("FLEX", 0):
        return 1.0
    return 0.55


def survival_probs(pool: Sequence[Player], pick_no: int, my_next: int, state: DraftState,
                   room: RoomModel | None = None) -> dict[str, float]:
    """P(each candidate is still on the board at your next pick).

    Every intervening pick is modelled: the drafting team is assumed to pick
    from near the top of its own board, with `room.tau` controlling how deep it
    reaches, tilted by that team's open starting slots. Depth is measured in
    RANK among available players rather than in ADP distance, so the model does
    not care whether the ADP source is calibrated to this league's pick numbers.

    The whole available board is tracked, not just the shortlist. That matters:
    exactly one player leaves per pick, and if only forty players are in the
    denominator the model believes the departing player must be one of them.
    """
    shape = state.shape
    room = room or RoomModel()
    if my_next <= pick_no + 1:
        return {p.pid: 1.0 for p in pool}

    board = sorted((p for p in state.players.values() if p.pid not in state.taken),
                   key=lambda p: room.adp_eff(p))
    alive = {p.pid: 1.0 for p in board}
    counts_cache: dict[int, Counter] = {
        int(slot): counts_of(pids, state.players) for slot, pids in state.slot_rosters.items()
    }
    tau = max(0.1, room.tau)
    gamma = room.need_gamma
    cutoff = tau * 26.0 + 6.0

    for k in range(pick_no + 1, my_next):
        slot = pick_to_slot(k, shape.teams, shape.snake, shape.reversal_round)
        rnd = (k - 1) // shape.teams + 1
        counts = counts_cache.setdefault(slot, Counter())

        weights: list[tuple[str, float]] = []
        wsum = 0.0
        rank = 0.0
        for p in board:
            a = alive.get(p.pid, 0.0)
            if a <= 1e-6:
                continue
            if rank > cutoff:
                break
            w = a * math.exp(-rank / tau) * (_need_mult(p.pos, counts, shape, rnd) ** gamma)
            rank += a                      # expected depth, net of who is gone
            if w > 0.0:
                weights.append((p.pid, w))
                wsum += w
        if wsum <= 0.0:
            continue
        # EXACTLY one player leaves the board at pick k, so exactly one unit of
        # probability mass must leave with him. The obvious update -
        # alive *= (1 - share) - removes only SUM(alive_i * share_i), which is
        # less than one whenever the likely picks are already partly consumed.
        # The board then depletes too slowly and every survival probability
        # comes out too high, which compounds badly across a long wait.
        leftover = 0.0
        taken_prob: list[tuple[str, float]] = []
        for pid, w in weights:
            want = w / wsum
            have = alive[pid]
            if want > have:
                leftover += want - have
                want = have
            taken_prob.append((pid, want))
        if leftover > 1e-9:                      # redistribute onto who is left
            room_left = sum(max(0.0, alive[pid] - q) for pid, q in taken_prob)
            if room_left > 1e-9:
                scale = min(1.0, leftover / room_left)
                taken_prob = [(pid, q + scale * max(0.0, alive[pid] - q))
                              for pid, q in taken_prob]
        for pid, q in taken_prob:
            alive[pid] = max(0.0, alive[pid] - q)
        best_pid = max(weights, key=lambda x: x[1])[0]
        bp = state.players.get(best_pid)
        if bp is not None:
            counts[bp.pos] += 1

    return {p.pid: alive.get(p.pid, 1.0) for p in pool}


def tier_breaks(pool: Sequence[Player], min_gap: float = 18.0) -> dict[str, int]:
    """Players left at each position before the next real cliff.

    A cliff is a projection gap of `min_gap` season points between
    consecutive players. This is the number that should drive a decision at
    the turn, and v34 had no equivalent anywhere.
    """
    out: dict[str, int] = {}
    by: dict[str, list[float]] = defaultdict(list)
    for p in pool:
        by[p.pos].append(p.proj_adj)
    for pos, vals in by.items():
        vals.sort(reverse=True)
        n = len(vals)
        for i in range(len(vals) - 1):
            if vals[i] - vals[i + 1] >= min_gap:
                n = i + 1
                break
        out[pos] = n
    return out


def plan_ev(roster: Sequence[Player], cand: Player, pool: Sequence[Player],
            pick_no: int, upcoming: Sequence[int], state: "DraftState",
            base: Baseline, room: "RoomModel") -> float:
    """Expected added value of taking `cand` now AND drafting on for real.

    The two-ply form - MV(c) + E[best single follow-on | c] - compares plans of
    two picks when the draft has many left, and that truncation has a
    direction: the candidate who fills your biggest hole is the only one whose
    option term collapses, because after taking him nothing else is worth much.
    Every other candidate keeps crediting itself with the hole-filler it did
    not take. The engine can therefore defer the same need forever, re-promising
    the same points at every pick, and only pay up when legality forces it.
    That is exactly what happened at picks 88 through 136 of draft
    1400876063868850176: e_next sat at ~72 for five consecutive picks while the
    RB2 slot stayed empty and the picks went QB2, WR6, WR7.

    Rolling the plan forward over several picks removes the asymmetry: both
    orderings acquire both players, so the comparison reduces to who is
    likelier to be gone - which is what survival is for.
    """
    shape = state.shape
    plan = list(roster) + [cand]
    base_val = season_value(roster, shape, base, sims=0)
    total = season_value(plan, shape, base, sims=0) - base_val
    remaining = [q for q in pool if q.pid != cand.pid]

    for n in list(upcoming)[:ROLLOUT_DEPTH]:
        if not remaining:
            break
        surv_n = survival_probs(remaining, pick_no, n, state, room=room)
        cur = season_value(plan, shape, base, sims=0)
        opts = sorted(((season_value(plan + [q], shape, base, sims=0) - cur, q)
                       for q in remaining), key=lambda x: -x[0])
        if not opts:
            break
        # EXPECTED MAX, not max-of-discounted-value. Booking raw * S and then
        # assuming possession is the same mistake in miniature: it credits the
        # best player to whichever plan leaves him on the board and never pays
        # for the branch where he is gone, so a plan is rewarded for deferring
        # the scarcest player. The closed form below prices the fallback.
        gain, none_yet = 0.0, 1.0
        for raw, q in opts:
            s = surv_n.get(q.pid, 0.0)
            gain += raw * s * none_yet
            none_yet *= (1.0 - s)
        gain += none_yet * opts[-1][0]
        total += gain
        best = opts[0][1]
        plan.append(best)
        remaining = [q for q in remaining if q.pid != best.pid]
    return total


# =============================================================================
# THE DECISION
# =============================================================================
def decide(state: DraftState, pick_no: int, base: Baseline,
           sims: int = SHORTLIST_SIMS, rank_sims: int = RANK_SIMS,
           title_model: "TitleModel | None" = None,
           room: "RoomModel | None" = None) -> dict[str, Any]:
    """Two-ply expected-value pick decision.

        EV(c) = MV(c) + E[ best marginal value available at your next pick | c ]

    The second term is exact, not simulated:

        E[max] = SUM_j  MV_j * S_j * PROD_{i<j} (1 - S_i)

    over follow-on candidates sorted by marginal value, where S_j is that
    player's survival probability. Under independent survival this IS the
    expectation of the max, so there is no Monte Carlo noise at all - and it
    prices tier cliffs and the snake turn by construction rather than by a
    hand-tuned `pair_pen` constant.

    v34 instead ran 220 full-draft rollouts in which its own future picks
    were chosen by a position-blind greedy, compared weekly PPG against
    season totals, and gave each candidate an independent RNG stream so the
    noise added instead of cancelling. That pass overrode a correct round-1
    recommendation in the slot-9 mock.
    """
    shape = state.shape
    players = state.players
    picks = state.my_picks or my_pick_numbers(state.my_slot, shape.teams, shape.rounds,
                                              shape.snake, shape.reversal_round)
    picks_left = len([n for n in picks if n >= pick_no])
    upcoming = [n for n in picks if n > pick_no]
    my_next = upcoming[0] if upcoming else pick_no
    has_next = bool(upcoming)
    rnd = (pick_no - 1) // shape.teams + 1

    roster = [players[pid] for pid in state.slot_rosters.get(state.my_slot, []) if pid in players]
    counts = counts_of([p.pid for p in roster], players)
    h = holes(counts, shape)
    forced = legality_forced(counts, shape, picks_left)

    avail = [p for p in players.values() if p.pid not in state.taken]

    # Candidate shortlist. The quota is ROSTER-AWARE on purpose: ranking the
    # whole board by frozen VORP is roster-blind, and because the QB curve is
    # flat the leftover QBs carry the best VORP late in a draft. A pure VORP
    # prefilter therefore fills the pool with backup QBs and the engine never
    # even evaluates the tight end it still needs. Positions with an open
    # starting slot always get represented; saturated single-slot positions
    # get a token few so a genuine bargain can still surface.
    quota: dict[str, int] = {}
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        have, need = counts.get(pos, 0), shape.slots.get(pos, 0)
        if pos in LATE_ONLY:
            eligible = forced or rnd >= shape.rounds - 2
            quota[pos] = 5 if (eligible and have < need) else 0
        elif single_slot(pos, shape):
            # one backup is real bye and injury cover; a second can only ever
            # start if BOTH players ahead of him are out in the same week, so
            # it is capped outright rather than argued with in the score
            if have >= need + 1:
                quota[pos] = 0
            elif have >= need:
                quota[pos] = 3
            else:
                quota[pos] = CAND_PER_POS
        else:
            quota[pos] = CAND_PER_POS
    if forced:
        quota = {pos: (CAND_PER_POS if pos in forced else 0) for pos in quota}
        avail = [p for p in avail if p.pos in forced]

    avail.sort(key=lambda p: -p.vorp)
    cands: list[Player] = []
    per_pos: Counter = Counter()
    for p in avail:
        if not forced and p.injury in INJURY_BLOCK and rnd < shape.rounds - 1:
            continue
        if per_pos[p.pos] >= quota.get(p.pos, 0):
            continue
        per_pos[p.pos] += 1
        cands.append(p)
    cands.sort(key=lambda p: -p.vorp)
    if len(cands) > CAND_POOL:
        keep = {pos: 0 for pos in quota}
        trimmed = []
        for p in cands:                       # keep breadth while trimming
            floor_n = 6 if quota.get(p.pos, 0) >= CAND_PER_POS else quota.get(p.pos, 0)
            if len(trimmed) < CAND_POOL or keep[p.pos] < floor_n:
                trimmed.append(p)
                keep[p.pos] += 1
        cands = trimmed

    if not cands:
        return {"ranked": [], "round": rnd, "my_next": my_next, "picks_left": picks_left,
                "counts": counts, "holes": h, "forced": forced, "tiers": {},
                "room": room or fit_room(state), "strategy": "NO LEGAL PICK", "note": ""}

    room = room or fit_room(state)
    surv = survival_probs(cands, pick_no, my_next, state, room=room)

    # Marginal value is measured WITH injuries sampled, because a bench player's
    # entire worth is the weeks he covers for someone else. A byes-only model
    # scores every late-round skill player at ~0 and then picks arbitrarily,
    # which is how you end up with a QB2 and a TE2 on the bench. Every candidate
    # is drawn against the SAME random stream (common random numbers), so the
    # sampling noise cancels in the comparison instead of adding to it.
    seed = 20260901 + pick_no
    roster_val = season_value(roster, shape, base, sims=0)
    roster_val_s = (season_value(roster, shape, base, sims=rank_sims, rng=random.Random(seed))
                    if rank_sims > 0 else roster_val)

    results: list[dict[str, Any]] = []
    for c in cands:
        roster_c = roster + [c]
        val_c = season_value(roster_c, shape, base, sims=0)
        if rank_sims > 0:
            mv_c = season_value(roster_c, shape, base, sims=rank_sims,
                                rng=random.Random(seed)) - roster_val_s
        else:
            mv_c = val_c - roster_val

        followers: list[tuple[float, Player]] = []
        for q in cands[:NEXT_POOL]:
            if q.pid == c.pid:
                continue
            followers.append((season_value(roster_c + [q], shape, base, sims=0) - val_c, q))
        followers.sort(key=lambda x: -x[0])

        # On the LAST pick there is no next pick, so there is no option value.
        # survival_probs() returns 1.0 for everyone when my_next <= pick_no + 1,
        # which made e_next reward leaving the biggest hole -- at pick 177 that
        # is why the worst defense on the board outranked the best one.
        if not has_next:
            e_next = 0.0
        else:
            e_next = 0.0
            none_yet = 1.0
            for mv_q, q in followers:
                s = surv.get(q.pid, 0.0)
                e_next += mv_q * s * none_yet
                none_yet *= (1.0 - s)
            e_next += none_yet * (followers[-1][0] if followers else 0.0)

        results.append({
            "player": c, "mv": mv_c, "e_next": e_next, "ev": mv_c + e_next,
            "survival": surv.get(c.pid, 1.0), "vorp": c.vorp,
            "fills": fills_label(c.pos, counts, shape),
        })

    results.sort(key=lambda r: -r["ev"])

    # Full-horizon re-rank. The shortlist is drawn by MARGINAL VALUE, not by the
    # two-ply ev, because the two-ply ev is the thing being corrected: at pick
    # 136 it ranked a 54.6-point running back below a 1.8-point wide receiver,
    # so any shortlist drawn from it would never have contained the right pick.
    if has_next and len(results) > 1:
        pool = [r["player"] for r in sorted(results, key=lambda r: -r["mv"])[:NEXT_POOL]]
        head_ids = {p.pid for p in pool[:ROLLOUT_K]}
        for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):     # keep one of each
            for r in sorted(results, key=lambda r: -r["mv"]):
                if r["player"].pos == pos:
                    head_ids.add(r["player"].pid)
                    break
        for r in results:
            if r["player"].pid in head_ids:
                r["plan_ev"] = plan_ev(roster, r["player"], pool, pick_no,
                                       upcoming, state, base, room)
        scored = [r for r in results if "plan_ev" in r]
        rest = [r for r in results if "plan_ev" not in r]
        scored.sort(key=lambda r: -r["plan_ev"])
        results = scored + rest

    # Injury-sampled re-score of the shortlist, with COMMON RANDOM NUMBERS so
    # the sampling noise cancels in the difference instead of adding to it.
    if sims > 0 and len(results) > 1:
        base_sampled = season_value(roster, shape, base, sims=sims, rng=random.Random(seed))
        for r in results[:SHORTLIST_N]:
            r["ev_sampled"] = (
                season_value(roster + [r["player"]], shape, base,
                             sims=sims, rng=random.Random(seed))
                - base_sampled + r["e_next"]
            )
        head = sorted(results[:SHORTLIST_N], key=lambda r: -r.get("ev_sampled", r["ev"]))
        results = head + results[SHORTLIST_N:]

    # Final arbiter: title probability, not points. Expected points is the
    # right primary signal and it does almost all the work, but the two come
    # apart at the margin - variance is worth buying when you trail the field
    # and worth shedding when you lead it, and a three-week single-elimination
    # bracket pays for a ceiling. The field is drawn once and shared by every
    # candidate so the comparison is paired, and a reordering only happens when
    # the gap clears twice its own standard error. Anything smaller is noise
    # and gets left alone - which is precisely the discipline v34's Monte Carlo
    # tie-break lacked when it overrode a correct round-one pick.
    if title_model is not None and len(results) > 1:
        for r in results[:SHORTLIST_N]:
            rc = roster + [r["player"]]
            reg_m, po_m = season_split(rc, shape, base, sims=rank_sims,
                                       rng=random.Random(seed))
            cv = team_cv(rc, shape, base)
            odds, made, se = title_model.title_odds(reg_m, reg_m * cv, po_m, po_m * cv)
            r["title_odds"], r["playoff_odds"], r["title_se"] = odds, made, se
            r["weekly_mean"], r["weekly_sd"] = reg_m, reg_m * cv
        head = results[:SHORTLIST_N]
        pick = head[0]
        for r in head[1:]:
            gap = r["title_odds"] - pick["title_odds"]
            if gap > 2.0 * math.sqrt(r["title_se"] ** 2 + pick["title_se"] ** 2):
                pick = r
        if pick is not results[0]:
            results = [pick] + [r for r in head if r is not pick] + results[SHORTLIST_N:]

    tiers = tier_breaks(cands)
    strat, note = describe_strategy(results, tiers, counts, h, shape, rnd,
                                    my_next - pick_no, forced)
    return {
        "ranked": results, "round": rnd, "my_next": my_next, "picks_left": picks_left,
        "counts": counts, "holes": h, "forced": forced, "tiers": tiers,
        "roster_value": roster_val, "strategy": strat, "note": note, "room": room,
    }


def describe_strategy(results: list[dict], tiers: dict[str, int], counts: Counter,
                      h: dict[str, int], shape: LeagueShape, rnd: int,
                      gap: int, forced: list[str]) -> tuple[str, str]:
    """Label derived FROM the decision, not alongside it.

    In the v34 mock the label said "RB SCARCITY" for three straight rounds
    while the engine recommended a TE, then a QB, then an RB. The label was
    cosmetic because it came from a separate weighting function that barely
    touched the score. Here it is read off the result that was actually
    chosen.
    """
    if forced:
        return "ROSTER LOCK", f"Take {'/'.join(forced)} now or the lineup cannot be filled."
    if not results:
        return "NO LEGAL PICK", ""
    top = results[0]
    pos = top["player"].pos
    left = tiers.get(pos, 99)

    if top["survival"] < 0.35 and left <= 3:
        return f"{pos} TIER CLIFF", f"Only {left} left at this tier and he likely will not last to your next pick."
    if top["survival"] < 0.30:
        return "LAST CALL", f"{int((1 - top['survival']) * 100)}% chance he is gone before your next pick."
    if gap and gap <= 3 and rnd < shape.rounds - 2:
        return "TURN LEVERAGE", f"Back-to-back picks in {gap}. Take the scarcer of the two now."
    if top["e_next"] > 0 and top["mv"] > 0 and top["mv"] > 2.2 * top["e_next"]:
        return "VALUE SPIKE", "This pick is worth far more than what the board is likely to return next round."
    if h["skill"] > 0:
        return "FILL STARTERS", f"Best marginal points that still fills a starting slot ({top['fills']})."
    return "BEST MARGINAL VALUE", "Starters set; taking the largest addition to expected lineup points."


# =============================================================================
# TURN CARD
# =============================================================================
def format_turn_card(out: dict, pick_no: int, slot: int, shape: LeagueShape) -> str:
    ranked = out["ranked"]
    rnd = out["round"]
    if not ranked:
        return f"<b>ON THE CLOCK</b>\nRound {rnd} Pick {pick_no}\nNo legal picks under current constraints."

    c = out["counts"]
    h = out["holes"]
    gap = out["my_next"] - pick_no
    lines = [
        "<b>ON THE CLOCK</b>",
        f"Round {rnd} Pick {pick_no} Slot {slot}",
        "",
        f"<b>{html.escape(out['strategy'])}</b>",
    ]
    if out["note"]:
        lines.append(f"<i>{html.escape(out['note'])}</i>")

    best = ranked[0]
    p = best["player"]
    lines += [
        "",
        "<b>PRIMARY</b>",
        f"<b>{html.escape(p.name)}</b>",
        f"{html.escape(p.pos)} - {html.escape(p.team)} | {html.escape(best['fills'])}",
        f"ADP {p.adp:.0f} | Proj {p.proj_adj:.0f} | VORP {p.vorp:+.0f}",
        f"<code>+{best['mv']:.1f} pts to your lineup | {int((1 - best['survival']) * 100)}% gone by next</code>",
        f"<code>Board returns ~{best['e_next']:.1f} next pick | EV {best['ev']:.1f}</code>",
    ]

    if "title_odds" in best:
        lines.append(f"<code>Playoffs {best['playoff_odds']:.0f}% | "
                     f"Title {best['title_odds']:.1f}% (+/-{best['title_se']:.1f})</code>")
        lines.append(f"<code>Weekly {best['weekly_mean']:.1f} +/- {best['weekly_sd']:.1f}</code>")

    for label, r in (("ALTERNATIVE", ranked[1] if len(ranked) > 1 else None),
                     ("THIRD", ranked[2] if len(ranked) > 2 else None)):
        if not r:
            continue
        q = r["player"]
        lines += [
            "",
            f"<b>{label}</b>",
            f"<b>{html.escape(q.name)}</b> {html.escape(q.pos)}-{html.escape(q.team)}",
            f"<code>+{r['mv']:.1f} pts | EV {r['ev']:.1f} | {int((1 - r['survival']) * 100)}% gone{' | Title ' + format(r['title_odds'], '.1f') + '%' if 'title_odds' in r else ''}</code>",
        ]

    room = out.get("room")
    tiers = out["tiers"]
    tier_str = " ".join(f"{k}{tiers[k]}" for k in ("QB", "RB", "WR", "TE") if k in tiers)
    open_slots = [k for k in ("QB", "RB", "WR", "TE", "FLEX", "K", "DEF") if h.get(k, 0)]
    lines += [
        "",
        f"<code>QB{c['QB']} RB{c['RB']} WR{c['WR']} TE{c['TE']} K{c['K']} DEF{c['DEF']}</code>",
        f"Tier left: <code>{tier_str}</code>",
        f"Open: {', '.join(open_slots) if open_slots else 'Complete'}",
        f"Next pick in {gap if gap > 0 else 'final pick'}",
    ]
    if room is not None:
        drift = [f"{k}{v:+.0f}" for k, v in sorted(room.pos_shift.items()) if abs(v) >= 3]
        lines.append(f"Room: {html.escape(room.descriptor)}"
                     + (f" | drift {' '.join(drift)}" if drift else ""))
    return "\n".join(lines)


# =============================================================================
# POST-DRAFT: SEASON SIMULATION
# =============================================================================
@dataclass
class TeamAnalysis:
    slot: int
    name: str
    is_me: bool
    reg_ppg: float
    reg_sd: float
    po_ppg: float
    po_sd: float
    season_points: float
    wins: float
    seed: float
    playoff_odds: float
    final_odds: float
    title_odds: float
    grade: float
    grade_letter: str
    core_strength: str
    risk_factor: str


def _bracket_winner(seeds: list[int], mean: list[float], sd: list[float],
                    rng: random.Random) -> tuple[int, int]:
    """Single-elimination bracket for `len(seeds)` teams, reseeded each round.

    The smallest power of two at or above the field size sets the number of
    first-round byes, which are given to the top seeds. For this league's
    6-team field that is exactly the real format: seeds 1-2 idle in week 15,
    3v6 and 4v5 play, semifinals in week 16, final in week 17.
    """
    n = len(seeds)
    if n <= 1:
        return (seeds[0], seeds[0]) if seeds else (0, 0)
    pow2 = 1
    while pow2 < n:
        pow2 *= 2
    byes = pow2 - n

    def game(a: int, b: int) -> int:
        return a if rng.gauss(mean[a], sd[a]) >= rng.gauss(mean[b], sd[b]) else b

    rank = {t: i for i, t in enumerate(seeds)}
    alive = list(seeds)
    first = True
    while len(alive) > 1:
        if first and byes:
            resting, playing = alive[:byes], alive[byes:]
            first = False
        else:
            resting, playing = [], alive
        winners = [game(playing[i], playing[len(playing) - 1 - i])
                   for i in range(len(playing) // 2)]
        alive = sorted(resting + winners, key=lambda t: rank[t])
        if len(alive) == 2:
            champ = game(alive[0], alive[1])
            runner = alive[1] if champ == alive[0] else alive[0]
            return champ, runner
    return alive[0], alive[0]


def simulate_season(state: DraftState, base: Baseline) -> list[TeamAnalysis]:
    shape = state.shape
    n_teams = shape.teams
    rows: list[dict] = []
    for slot in range(1, n_teams + 1):
        pids = state.slot_rosters.get(slot, [])
        roster = [state.players[pid] for pid in pids if pid in state.players]
        rm, rs, pm, ps = team_week_profile(roster, shape, base, seed=100 + slot)
        core, risk = diagnose_team(roster, shape)
        rows.append({
            "slot": slot, "is_me": slot == state.my_slot,
            "name": "You" if slot == state.my_slot else f"Team {slot}",
            "reg_ppg": rm, "reg_sd": rs, "po_ppg": pm, "po_sd": ps,
            "core": core, "risk": risk,
            "wins": 0.0, "seed": 0.0, "playoffs": 0, "final": 0, "titles": 0,
            "season_points": rm * len(shape.reg_weeks),
        })

    rng = random.Random(4242)
    weeks = len(shape.reg_weeks)
    idxs = list(range(n_teams))
    for _ in range(SEASON_SIMS):
        wins = [0] * n_teams
        pts = [0.0] * n_teams
        for _w in range(weeks):
            scores = [rng.gauss(r["reg_ppg"], r["reg_sd"]) for r in rows]
            for i in range(n_teams):
                pts[i] += scores[i]
            rng.shuffle(idxs)
            for m in range(0, n_teams - 1, 2):
                a, b = idxs[m], idxs[m + 1]
                if scores[a] >= scores[b]:
                    wins[a] += 1
                else:
                    wins[b] += 1
        for i in range(n_teams):
            rows[i]["wins"] += wins[i]
        standings = sorted(range(n_teams), key=lambda i: (-wins[i], -pts[i]))
        for rank, i in enumerate(standings, 1):
            rows[i]["seed"] += rank
        seeds = standings[: shape.playoff_teams]
        for i in seeds:
            rows[i]["playoffs"] += 1
        champ, runner = _bracket_winner(
            seeds, [r["po_ppg"] for r in rows], [r["po_sd"] for r in rows], rng
        )
        rows[champ]["titles"] += 1
        rows[champ]["final"] += 1
        rows[runner]["final"] += 1

    lo = min(r["reg_ppg"] for r in rows)
    hi = max(r["reg_ppg"] for r in rows)
    out: list[TeamAnalysis] = []
    for r in rows:
        g = 55.0 + 44.0 * ((r["reg_ppg"] - lo) / max(1.0, hi - lo))
        letter = next(
            (lab for cut, lab in ((94, "A+"), (89, "A"), (84, "B+"), (79, "B"),
                                  (74, "B-"), (69, "C+"), (64, "C")) if g >= cut), "D")
        out.append(TeamAnalysis(
            slot=r["slot"], name=r["name"], is_me=r["is_me"],
            reg_ppg=r["reg_ppg"], reg_sd=r["reg_sd"], po_ppg=r["po_ppg"], po_sd=r["po_sd"],
            season_points=r["season_points"],
            wins=r["wins"] / SEASON_SIMS, seed=r["seed"] / SEASON_SIMS,
            playoff_odds=100.0 * r["playoffs"] / SEASON_SIMS,
            final_odds=100.0 * r["final"] / SEASON_SIMS,
            title_odds=100.0 * r["titles"] / SEASON_SIMS,
            grade=g, grade_letter=letter, core_strength=r["core"], risk_factor=r["risk"],
        ))
    out.sort(key=lambda t: (-t.title_odds, -t.reg_ppg))
    return out


def diagnose_team(roster: Sequence[Player], shape: LeagueShape) -> tuple[str, str]:
    c = Counter(p.pos for p in roster)
    rbs = sorted([p for p in roster if p.pos == "RB"], key=lambda x: x.adp)
    wrs = sorted([p for p in roster if p.pos == "WR"], key=lambda x: x.adp)
    strengths = []
    if any(p.pos == "TE" and p.adp <= 36 for p in roster):
        strengths.append("Elite TE")
    if rbs and rbs[0].adp <= 14:
        strengths.append("Hero RB")
    elif len(rbs) >= 2 and rbs[0].adp <= 24 and rbs[1].adp <= 48:
        strengths.append("Two-back core")
    if len(wrs) >= 3 and wrs[2].adp <= 50:
        strengths.append("Deep WR room")
    elif len(wrs) >= 2 and wrs[1].adp <= 28:
        strengths.append("WR anchor pair")
    core = " + ".join(strengths[:2]) if strengths else "Balanced lineup"

    risks = []
    byes: Counter = Counter(p.bye for p in roster if p.pos in SKILL and p.bye)
    worst_bye, worst_n = (byes.most_common(1)[0] if byes else (0, 0))
    if c["RB"] <= 2:
        risks.append("Thin RB room")
    if c["WR"] <= 2:
        risks.append("Thin WR room")
    if worst_n >= 3:
        risks.append(f"{worst_n} skill players on the week {worst_bye} bye")
    if c["QB"] >= 2 and not shape.superflex:
        risks.append("Backup QB using a bench slot")
    if c["TE"] >= 2 and shape.slots.get("TE", 1) == 1:
        risks.append("Backup TE using a bench slot")
    if c["K"] == 0 or c["DEF"] == 0:
        risks.append("Incomplete lineup")
    return core, (risks[0] if risks else "Clean construction")


def format_power_rankings(rankings: list[TeamAnalysis], shape: LeagueShape) -> str:
    lines = [
        "<b>POST-DRAFT POWER RANKINGS</b>",
        f"<i>{SEASON_SIMS} seasons | {shape.teams} teams | {shape.playoff_teams} make playoffs | "
        f"weeks {shape.playoff_start}-{FULL_SEASON_WEEKS - 1}</i>",
        "",
    ]
    for i, t in enumerate(rankings, 1):
        me = " <b>(YOU)</b>" if t.is_me else ""
        title = f"{t.title_odds:.1f}%" if t.title_odds >= 0.5 else "<1%"
        lines.append(f"#{i}. <b>{html.escape(t.name)}</b> (Slot {t.slot}){me}")
        lines.append(f"  {t.reg_ppg:.1f} PPG (sd {t.reg_sd:.1f}) | {t.wins:.1f}-{len(shape.reg_weeks) - t.wins:.1f}")
        lines.append(f"  Playoffs {t.playoff_odds:.0f}% | Final {t.final_odds:.0f}% | Title {title} ({t.grade_letter})")
        lines.append(f"  {html.escape(t.core_strength)} | {html.escape(t.risk_factor)}")
    me = next((t for t in rankings if t.is_me), None)
    if me:
        lines += [
            "",
            f"You finished #{rankings.index(me) + 1} of {shape.teams}: "
            f"{me.playoff_odds:.0f}% playoffs, {me.title_odds:.1f}% title, avg seed {me.seed:.1f}.",
        ]
    return "\n".join(lines)


# =============================================================================
# AUTOPSY: HONEST, OUTCOME-FREE GRADING
# =============================================================================
GRADE_BANDS = ((10.0, "A+"), (3.0, "A"), (-3.0, "B+"), (-10.0, "B"),
               (-22.0, "B-"), (-40.0, "C"), (-70.0, "D"))


@dataclass
class PickAudit:
    pick_no: int
    round_num: int
    name: str
    pos: str
    team: str
    adp: float
    proj: float
    vorp: float
    marginal_value: float
    best_alternative: str
    best_alternative_mv: float
    points_left: float
    grade: str
    fills: str
    verdict: str


def audit_my_picks(state: DraftState, base: Baseline) -> list[PickAudit]:
    """Grade every pick in season points against the best alternative that was
    actually available AND gone by your next pick.

    v34 added +10 for obeying the engine and then deleted the opportunity cost
    and the better-players list entirely, which is why 14 of 15 picks in the
    slot-9 mock graded A or A+ including a round-11 wide receiver whose true
    VORP was -19. Nothing here knows or cares what the engine recommended.
    """
    shape = state.shape
    ordered = list(state.picks)
    my_slot = state.my_slot
    my_picks = state.my_picks or my_pick_numbers(my_slot, shape.teams, shape.rounds,
                                                 shape.snake, shape.reversal_round)
    audits: list[PickAudit] = []

    for i, pk in enumerate(ordered):
        if int(pk.get("draft_slot") or 0) != my_slot:
            continue
        p = state.players.get(str(pk.get("player_id") or ""))
        if not p:
            continue
        pick_no = i + 1
        rnd = (pick_no - 1) // shape.teams + 1

        taken_before = {str(r.get("player_id") or "") for r in ordered[: pick_no - 1]}
        roster_before = [
            state.players[str(r.get("player_id"))]
            for r in ordered[: pick_no - 1]
            if int(r.get("draft_slot") or 0) == my_slot and str(r.get("player_id")) in state.players
        ]
        counts = counts_of([x.pid for x in roster_before], state.players)

        upcoming = [n for n in my_picks if n > pick_no]
        next_pick = upcoming[0] if upcoming else len(ordered) + 1
        gone_by_next = {str(r.get("player_id") or "") for r in ordered[: next_pick - 1]}

        # only players you could have had, and could NOT have had one pick later
        alts = [
            x for x in state.players.values()
            if x.pid not in taken_before and x.pid != p.pid and x.pid in gone_by_next
        ]
        alts.sort(key=lambda x: -x.vorp)
        alts = alts[:40]

        seed = 777 + pick_no
        roster_val = season_value(roster_before, shape, base, sims=AUDIT_SIMS,
                                  rng=random.Random(seed))
        mv = marginal_value(p, roster_before, shape, base, roster_value=roster_val,
                            sims=AUDIT_SIMS, rng_seed=seed)
        best_mv, best_alt = -1e9, None
        for a in alts:
            m = marginal_value(a, roster_before, shape, base, roster_value=roster_val,
                               sims=AUDIT_SIMS, rng_seed=seed)
            if m > best_mv:
                best_mv, best_alt = m, a

        delta = (mv - best_mv) if best_alt else 0.0
        grade = next((lab for cut, lab in GRADE_BANDS if delta >= cut), "F")

        if not best_alt:
            verdict = "Nothing comparable was leaving the board."
        elif delta >= 3.0:
            verdict = "Best available use of this pick."
        elif delta >= -3.0:
            verdict = f"Even with {best_alt.name}."
        else:
            verdict = f"{best_alt.name} was worth {(-delta):.0f} more points to this roster."

        audits.append(PickAudit(
            pick_no=pick_no, round_num=rnd, name=p.name, pos=p.pos, team=p.team,
            adp=round(p.adp, 1), proj=round(p.proj_adj, 1), vorp=round(p.vorp, 1),
            marginal_value=round(mv, 1),
            best_alternative=best_alt.name if best_alt else "",
            best_alternative_mv=round(best_mv, 1) if best_alt else 0.0,
            points_left=round(-delta, 1) if delta < 0 else 0.0,
            grade=grade, fills=fills_label(p.pos, counts, shape), verdict=verdict,
        ))
    return audits


def construction_notes(state: DraftState, base: Baseline) -> list[str]:
    shape = state.shape
    roster = [state.players[i] for i in state.slot_rosters.get(state.my_slot, []) if i in state.players]
    c = Counter(p.pos for p in roster)
    notes: list[str] = []

    # every bench player, priced honestly against the waiver floor
    full = season_value(roster, shape, base, sims=AUDIT_SIMS, rng=random.Random(99))
    dead: list[str] = []
    for p in roster:
        without = [q for q in roster if q.pid != p.pid]
        if full - season_value(without, shape, base, sims=AUDIT_SIMS,
                               rng=random.Random(99)) < 5.0:
            dead.append(f"{p.name} ({p.pos})")
    if dead:
        notes.append(f"Near-zero marginal value: {', '.join(dead[:5])}. Those roster spots bought nothing.")

    byes: Counter = Counter(p.bye for p in roster if p.pos in SKILL and p.bye)
    for wk, n in byes.most_common(2):
        if n >= 3:
            notes.append(f"{n} skill players share the week {wk} bye.")

    if c["QB"] >= 2 and not shape.superflex:
        notes.append("Backup QB in a 1QB league. A waiver QB is worth roughly the same.")
    if c["TE"] >= 2 and shape.slots.get("TE", 1) == 1:
        notes.append("Backup TE in a 1TE league. Same story.")
    if c["RB"] <= 2:
        notes.append("Only 2 RBs. One injury puts a waiver back in your flex.")
    if c["WR"] <= 2:
        notes.append("Only 2 WRs.")
    h = holes(c, shape)
    if h["K"] or h["DEF"]:
        notes.append("Lineup is not legal: missing K and/or DST.")
    if not notes:
        notes.append("Construction is clean: no dead roster spots, no bye pileups.")
    return notes


def survival_scorecard(state: DraftState) -> dict[str, Any]:
    """Did the survival model actually predict this draft?

    Reads back every candidate the engine scored during the draft and checks
    whether that player was still there at the next pick. An engine that
    cannot be shown to be wrong is not measuring anything, and this is the one
    number that says whether the half of the decision nobody can eyeball was
    any good.
    """
    path = decision_log_path(state.draft_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}

    ordered = list(state.picks)
    mine = state.my_picks or my_pick_numbers(state.my_slot, state.shape.teams,
                                             state.shape.rounds, state.shape.snake,
                                             state.shape.reversal_round)
    rows: list[tuple[float, float]] = []
    for entry in data.get("picks", []):
        pn = int(entry.get("pick_no") or 0)
        nxt = next((n for n in mine if n > pn), None)
        if not nxt:
            continue
        gone = {str(r.get("player_id") or "") for r in ordered[: nxt - 1]}
        for c in entry.get("candidates", []):
            s = c.get("survival")
            if s is None:
                continue
            rows.append((float(s), 0.0 if str(c.get("id")) in gone else 1.0))
    if len(rows) < 8:
        return {}

    buckets: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for pred, actual in rows:
        lo = min(4, int(pred * 5))
        buckets[f"{lo*20}-{lo*20+20}%"].append((pred, actual))
    table = []
    for label in sorted(buckets, key=lambda x: int(x.split("-")[0])):
        vals = buckets[label]
        table.append({"band": label, "n": len(vals),
                      "predicted": round(100 * sum(v[0] for v in vals) / len(vals), 1),
                      "actual": round(100 * sum(v[1] for v in vals) / len(vals), 1)})
    brier = sum((p - a) ** 2 for p, a in rows) / len(rows)
    mae = sum(abs(p - a) for p, a in rows) / len(rows)
    return {"n": len(rows), "brier": round(brier, 4), "mae": round(mae, 3), "bands": table}


def format_autopsy(state: DraftState, audits: list[PickAudit], notes: list[str],
                   rankings: list[TeamAnalysis]) -> str:
    me = next((t for t in rankings if t.is_me), None)
    rank = rankings.index(me) + 1 if me else 0
    shape = state.shape
    lines = [
        "<b>DRAFT AUTOPSY v35</b>",
        f"Slot {state.my_slot} | {shape.teams}-team | {shape.ppr} PPR | "
        f"{shape.playoff_teams} playoff teams",
        f"Ranked #{rank} | {me.playoff_odds if me else 0:.0f}% playoffs | "
        f"{me.title_odds if me else 0:.1f}% title",
        "",
        "<i>Graded in season points against the best player who was on the board "
        "and gone by your next pick. Following the engine earns nothing.</i>",
        "",
        "<b>PICKS</b>",
    ]
    for a in audits:
        lines.append(f"R{a.round_num} #{a.pick_no} <b>{html.escape(a.name)}</b> {a.pos} <code>{a.grade}</code>")
        lines.append(f"  ADP {a.adp:.0f} | Proj {a.proj:.0f} | VORP {a.vorp:+.0f} | "
                     f"adds {a.marginal_value:+.1f} pts")
        lines.append(f"  {html.escape(a.verdict)}")
    total_left = sum(a.points_left for a in audits)
    lines += [
        "",
        f"<b>Points left on the board across the draft: {total_left:.0f}</b>",
        "",
        "<b>CONSTRUCTION</b>",
    ]
    for n in notes:
        lines.append(f"- {html.escape(n)}")
    card = survival_scorecard(state)
    if card:
        lines += ["", "<b>SURVIVAL MODEL SCORECARD</b>",
                  f"<i>{card['n']} predictions, Brier {card['brier']}, MAE {card['mae']}</i>"]
        for row in card["bands"]:
            lines.append(f"  said {row['predicted']:.0f}% -> actually {row['actual']:.0f}% "
                         f"(n={row['n']})")
    return "\n".join(lines)


def build_roster_json(state: DraftState, base: Baseline) -> dict:
    shape = state.shape
    teams_out = []
    for slot in range(1, shape.teams + 1):
        pids = state.slot_rosters.get(slot, [])
        roster = [state.players[pid] for pid in pids if pid in state.players]
        total = season_value(roster, shape, base, sims=AUDIT_SIMS, rng=random.Random(99))
        detail = []
        for p in roster:
            without = [q for q in roster if q.pid != p.pid]
            detail.append({
                "player_id": p.pid, "name": p.name, "pos": p.pos, "team": p.team,
                "adp": round(p.adp, 1), "proj": round(p.proj_adj, 1),
                "proj_raw": round(p.proj_raw, 1), "proj_source": p.proj_source,
                "vorp": round(p.vorp, 1),
                "marginal_value": round(total - season_value(
                    without, shape, base, sims=AUDIT_SIMS, rng=random.Random(99)), 1),
                "bye": p.bye, "injury": p.injury or "", "years_exp": p.years,
            })
        detail.sort(key=lambda x: -x["marginal_value"])
        teams_out.append({
            "slot": slot, "is_me": slot == state.my_slot,
            "name": "You" if slot == state.my_slot else f"Team {slot}",
            "roster_counts": dict(Counter(p.pos for p in roster)),
            "lineup_season_points": round(total, 1),
            "players": detail,
        })
    return {
        "engine": ENGINE_VERSION, "draft_id": state.draft_id, "season": SEASON,
        "teams": shape.teams, "rounds": shape.rounds,
        "scoring": f"{shape.ppr} PPR" + (" + Superflex" if shape.superflex else ""),
        "playoff_teams": shape.playoff_teams, "playoff_start": shape.playoff_start,
        "my_slot": state.my_slot, "generated_at": int(time.time()),
        "replacement_levels": {k: round(v, 1) for k, v in base.repl.items()},
        "waiver_floor_ppg": {k: round(v, 2) for k, v in base.stream_ppg.items()},
        "league_rosters": teams_out,
    }


def analyze_and_report(state: DraftState, base: Baseline) -> None:
    print("=" * 68, flush=True)
    print("FULL-LEAGUE ROSTERS", flush=True)
    print("=" * 68, flush=True)
    payload = build_roster_json(state, base)
    out_path = f"draft_{state.draft_id}_full_rosters_v35.json"
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"[saved] {out_path}", flush=True)
    except OSError as exc:
        print(f"[warn] could not save rosters: {exc}", flush=True)

    print(f"[sim] {SEASON_SIMS} seasons, {POST_DRAFT_TEAM_SIMS} weekly sims per team...", flush=True)
    rankings = simulate_season(state, base)
    report = format_power_rankings(rankings, state.shape)
    print(re.sub(r"<[^>]+>", "", report))
    send_telegram(report)

    print("=" * 68, flush=True)
    print("AUTOPSY", flush=True)
    print("=" * 68, flush=True)
    audits = audit_my_picks(state, base)
    notes = construction_notes(state, base)
    card = format_autopsy(state, audits, notes, rankings)
    print(re.sub(r"<[^>]+>", "", card))
    send_telegram(card)

    autopsy_path = f"draft_{state.draft_id}_autopsy_v35.json"
    try:
        with open(autopsy_path, "w", encoding="utf-8") as f:
            json.dump({
                "engine": ENGINE_VERSION, "draft_id": state.draft_id, "slot": state.my_slot,
                "points_left_on_board": round(sum(a.points_left for a in audits), 1),
                "picks": [a.__dict__ for a in audits],
                "construction_notes": notes,
                "rankings": [t.__dict__ for t in rankings],
            }, f, indent=2, ensure_ascii=False)
        print(f"[saved] {autopsy_path}", flush=True)
    except OSError as exc:
        print(f"[warn] could not save autopsy: {exc}", flush=True)


# =============================================================================
# PRE-DRAFT SLOT PLAN
# =============================================================================
def slot_plan(players: dict[str, Player], shape: LeagueShape, slot: int,
              base: Baseline, confidence: float = 0.55) -> list[dict]:
    """What the board is likely to offer at each of your picks from this seat.

    Run it for every slot before the draft: it shows which rounds are your
    leverage rounds and which positions will already be gone. v34 had no
    planning layer at all.
    """
    picks = my_pick_numbers(slot, shape.teams, shape.rounds, shape.snake)
    pool = sorted((p for p in players.values() if p.pos in SKILL and 0 < p.adp < 300),
                  key=lambda p: p.adp)
    sigma = 9.0
    plan = []
    for n in picks:
        expected = {}
        for pos in ("QB", "RB", "WR", "TE"):
            for p in pool:
                if p.pos != pos:
                    continue
                z = (p.adp - n) / sigma
                if 1.0 / (1.0 + math.exp(-1.702 * z)) >= confidence:
                    expected[pos] = {"name": p.name, "adp": round(p.adp, 1),
                                     "vorp": round(p.vorp, 1)}
                    break
        plan.append({"pick": n, "round": (n - 1) // shape.teams + 1, "expected": expected})
    return plan


def print_seat_report(players: dict[str, Player], shape: LeagueShape,
                      base: Baseline, slot: int) -> None:
    """Everything the seat itself dictates, before a single pick is made."""
    picks = my_pick_numbers(slot, shape.teams, shape.rounds, shape.snake)
    gaps = [picks[i + 1] - picks[i] for i in range(len(picks) - 1)]
    print(f"\n=== SEAT REPORT: slot {slot} of {shape.teams} ===")
    print(f"Your picks: {', '.join(str(n) for n in picks)}")
    print(f"Gaps between picks: {gaps}")
    long_waits = [(picks[i], gaps[i]) for i in range(len(gaps)) if gaps[i] >= shape.teams]
    if long_waits:
        print("Longest waits (these are the picks where survival risk bites):")
        for n, g in long_waits[:6]:
            print(f"  after pick {n:<4} you wait {g} picks")
    print()
    tiers = tier_breaks([p for p in players.values() if p.pos in SKILL])
    print(f"Pre-draft tier depth (players before the next real cliff): {tiers}")
    print(f"Replacement level: {{ {', '.join(f'{k}:{v:.0f}' for k, v in base.repl.items())} }}")
    print(f"Waiver floor ppg:  {{ {', '.join(f'{k}:{v:.1f}' for k, v in base.stream_ppg.items())} }}")
    print()
    for row in slot_plan(players, shape, slot, base):
        bits = " | ".join(f"{pos} {d['name']} ({d['vorp']:+.0f})"
                          for pos, d in row["expected"].items())
        print(f"  R{row['round']:<3} pick {row['pick']:<4} {bits}")


def print_slot_plans(players: dict[str, Player], shape: LeagueShape, base: Baseline) -> None:
    print(f"\nReplacement levels (frozen): "
          f"{ {k: round(v, 1) for k, v in base.repl.items()} }")
    print(f"Waiver floor PPG:            "
          f"{ {k: round(v, 2) for k, v in base.stream_ppg.items()} }\n")
    for slot in range(1, shape.teams + 1):
        print(f"--- SLOT {slot} " + "-" * 52)
        for row in slot_plan(players, shape, slot, base)[:8]:
            bits = " | ".join(
                f"{pos} {d['name']} ({d['vorp']:+.0f})"
                for pos, d in row["expected"].items() if pos in ("RB", "WR", "TE")
            )
            print(f"  R{row['round']:<3} pick {row['pick']:<4} {bits}")
        print()


def print_value_board(players: dict[str, Player], base: Baseline, n: int = 60) -> None:
    pool = sorted((p for p in players.values() if p.pos in SKILL), key=lambda x: -x.vorp)
    print(f"{'#':<4}{'POS':<5}{'NAME':<24}{'TM':<5}{'ADP':>7}{'PROJ':>8}{'VORP':>8}{'SRC':>15}")
    print("-" * 76)
    for i, p in enumerate(pool[:n], 1):
        print(f"{i:<4}{p.pos:<5}{p.name[:23]:<24}{p.team:<5}{p.adp:>7.1f}"
              f"{p.proj_adj:>8.0f}{p.vorp:>+8.0f}{p.proj_source:>15}")


# =============================================================================
# LIVE DRAFT LOOP
# =============================================================================
def ingest_picks(state: DraftState, picks: list[dict]) -> None:
    state.picks = picks
    state.taken = set()
    state.slot_rosters = defaultdict(list)
    for pk in picks:
        pid = pk.get("player_id")
        if not pid:
            continue
        state.taken.add(str(pid))
        slot = int(pk.get("draft_slot") or 0)
        if slot:
            state.slot_rosters[slot].append(str(pid))


def decision_log_path(draft_id: str) -> str:
    return f"draft_{draft_id}_decisions_v35.json"


def log_decision(state: DraftState, pick_no: int, out: dict) -> None:
    path = decision_log_path(state.draft_id)
    data = {"draft_id": state.draft_id, "slot": state.my_slot, "engine": ENGINE_VERSION, "picks": []}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            pass
    ranked = out["ranked"]
    row = {
        "pick_no": pick_no, "round": out["round"],
        "strategy": out["strategy"], "note": out["note"],
        "tiers_left": out["tiers"],
        # The available board at this pick. Without it a post-hoc analysis has to
        # reconstruct who was on the board from the final rosters, and that
        # reconstruction is not faithful enough to re-adjudicate a decision:
        # it cannot see undrafted players at all, and it has to guess the order
        # opponents picked in. Replaying a pick exactly is the only way to test
        # a proposed change to the ranking against a real draft.
        "board": [
            {"id": p.pid, "name": p.name, "pos": p.pos, "adp": round(p.adp, 1),
             "proj": round(p.proj_adj, 1)}
            for p in sorted((x for x in state.players.values()
                             if x.pid not in state.taken),
                            key=lambda x: -x.vorp)[:BOARD_LOG_N]
        ],
        "candidates": [
            {"id": r["player"].pid, "name": r["player"].name, "pos": r["player"].pos,
             "marginal_value": round(r["mv"], 2), "expected_next": round(r["e_next"], 2),
             "ev": round(r["ev"], 2), "survival": round(r["survival"], 3),
             "title_odds": round(r["title_odds"], 2) if "title_odds" in r else None,
             "title_se": round(r["title_se"], 2) if "title_se" in r else None}
            for r in ranked[:8]
        ],
        "logged_at": int(time.time()),
    }
    rows = [p for p in data.get("picks", []) if int(p.get("pick_no") or 0) != pick_no]
    rows.append(row)
    rows.sort(key=lambda x: int(x.get("pick_no") or 0))
    data["picks"] = rows
    data["slot"] = state.my_slot
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        print(f"[warn] could not write decision log: {exc}", flush=True)


def fallback_card(state: DraftState, pick_no: int, base: Baseline, why: str) -> str:
    """Last-resort board if the full decision blows up mid-draft.

    A silent exception on the clock is the worst thing this program can do, so
    every layer that can fail is optional: title odds, then the simulation,
    then finally everything, degrading to a frozen-VORP board that needs
    nothing but the pre-draft baseline.
    """
    shape = state.shape
    counts = counts_of(state.slot_rosters.get(state.my_slot, []), state.players)
    h = holes(counts, shape)
    want = [p for p in ("QB", "RB", "WR", "TE", "K", "DEF") if h.get(p, 0) > 0] or list(SKILL)
    pool = sorted((p for p in state.players.values()
                   if p.pid not in state.taken and p.pos in want), key=lambda p: -p.vorp)[:5]
    rnd = (pick_no - 1) // shape.teams + 1
    lines = ["<b>ON THE CLOCK (DEGRADED)</b>",
             f"Round {rnd} Pick {pick_no} Slot {state.my_slot}",
             f"<i>Full model unavailable: {html.escape(why)}. "
             f"Falling back to the frozen value board.</i>", ""]
    for i, p in enumerate(pool, 1):
        lines.append(f"{i}. <b>{html.escape(p.name)}</b> {p.pos}-{p.team} "
                     f"| ADP {p.adp:.0f} | VORP {p.vorp:+.0f}")
    lines += ["", f"<code>QB{counts['QB']} RB{counts['RB']} WR{counts['WR']} "
                  f"TE{counts['TE']} K{counts['K']} DEF{counts['DEF']}</code>",
              f"Open: {', '.join(want)}"]
    return "\n".join(lines)


def emit_turn(state: DraftState, pick_no: int, base: Baseline) -> None:
    t0 = time.time()
    out = None
    try:
        try:
            tm = TitleModel(state, base)
        except Exception as exc:                      # title layer is optional
            print(f"[warn] title model unavailable ({exc}); ranking on points only.",
                  flush=True)
            tm = None
        out = decide(state, pick_no, base, title_model=tm)
        card = format_turn_card(out, pick_no, state.my_slot, state.shape)
    except Exception as exc:
        traceback_msg = f"{type(exc).__name__}: {exc}"
        print(f"[error] decision failed: {traceback_msg}", flush=True)
        try:
            card = fallback_card(state, pick_no, base, traceback_msg)
        except Exception as exc2:
            card = (f"<b>ON THE CLOCK</b>\nPick {pick_no}, slot {state.my_slot}\n"
                    f"Engine error: {type(exc2).__name__}. Draft manually.")
    if out is not None:
        try:
            log_decision(state, pick_no, out)
        except Exception as exc:
            print(f"[warn] decision log not written: {exc}", flush=True)
    print(f"\n{'=' * 68}")
    print(re.sub(r"<[^>]+>", "", card))
    print(f"[decided in {time.time() - t0:.1f}s]")
    print("=" * 68, flush=True)
    send_telegram(card)


# =============================================================================
# CLI / MAIN
# =============================================================================
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Draft Commander v35")
    p.add_argument("draft_id", nargs="?", default=None, help="Sleeper draft ID or URL")
    p.add_argument("--slot", type=int, default=None, help="Your draft slot (1-N)")
    p.add_argument("--league", default=None, help="Override LEAGUE_ID")
    p.add_argument("--preview", action="store_true", help="Show your next pick and exit")
    p.add_argument("--plan", action="store_true", help="Pre-draft plan for every seat")
    p.add_argument("--board", action="store_true", help="Top-60 value board and exit")
    p.add_argument("--report", "--autopsy", dest="report", action="store_true",
                   help="Post-draft rankings + autopsy")
    return p.parse_args()


def resolve_draft(draft_id: str, league_id: str) -> tuple[dict, dict, dict | None]:
    user = try_json(f"{BASE}/user/{USER_ID}") or {"user_id": USER_ID}
    chosen_id = extract_draft_id(draft_id)
    league = try_json(f"{BASE}/league/{league_id}") if league_id else None

    if not chosen_id and league_id:
        drafts = try_json(f"{BASE}/league/{league_id}/drafts") or []
        if isinstance(drafts, list) and drafts:
            live = next((d for d in drafts if d.get("status") in ("drafting", "paused")), None)
            pre = next((d for d in drafts if d.get("status") == "pre_draft"), None)
            chosen = live or pre or drafts[0]
            chosen_id = str(chosen.get("draft_id") or "")
            print(f"[init] Discovered draft {chosen_id} from league {league_id}", flush=True)

    if not chosen_id:
        raise SystemExit("Set DRAFT_ID or pass a draft ID/URL.")

    draft = try_json(f"{BASE}/draft/{chosen_id}")
    if not draft or not draft.get("draft_id"):
        raise SystemExit(f"Sleeper returned no draft for ID {chosen_id}.")
    if draft.get("league_id") and not league:
        league = try_json(f"{BASE}/league/{draft['league_id']}") or league

    kind = "Mock" if not draft.get("league_id") else "League"
    meta = draft.get("metadata") or {}
    print(f"[init] {kind} draft {chosen_id} | status {draft.get('status')} | "
          f"type {draft.get('type')} | {meta.get('name') or 'unnamed'}", flush=True)
    return user, draft, league


def resolve_slot(draft: dict, uid: str, picks: list[dict],
                 override: int | None) -> tuple[int | None, str]:
    order = draft.get("draft_order") or {}
    if isinstance(order, dict):
        for k, v in order.items():
            if str(k) == str(uid) and v not in (None, "", 0, "0"):
                try:
                    return int(v), "draft_order"
                except (TypeError, ValueError):
                    pass
    for pk in picks or []:
        if str(pk.get("picked_by") or "") == str(uid) and pk.get("draft_slot"):
            try:
                return int(pk["draft_slot"]), "picks"
            except (TypeError, ValueError):
                pass
    if override and int(override) > 0:
        return int(override), "config"
    return None, "unresolved"


def main() -> None:
    print("=" * 68, flush=True)
    print("DRAFT COMMANDER v35", flush=True)
    print("Single objective: expected starting-lineup points, playoff-weighted.", flush=True)
    print("=" * 68, flush=True)
    print(f"[env] telegram={'ok' if TELEGRAM_BOT_TOKEN else 'MISSING'} user={USER_ID or 'none'}",
          flush=True)

    args = parse_args()
    draft_id = extract_draft_id(args.draft_id or DRAFT_ID)
    league_id = (args.league or LEAGUE_ID or "").strip()
    override = int(args.slot or MY_SLOT or 0)

    user, draft, league = resolve_draft(draft_id, league_id)
    shape = shape_from_draft(draft, league)
    players = load_players(shape)

    repairs = repair_projections(players)
    if repairs:
        print(f"[repair] {len(repairs)} projections disagreed with the market and were shrunk:",
              flush=True)
        for name, was, now in repairs[:8]:
            print(f"          {name:<24} {was:>7.1f} -> {now:>7.1f}", flush=True)
    base = freeze_baseline(players, shape)
    print(f"[base] replacement {{ {', '.join(f'{k}:{v:.0f}' for k, v in base.repl.items())} }}",
          flush=True)

    dq = data_quality(players, shape)
    print(f"[data] projection sources: {dq['sources']}", flush=True)
    for w in dq["warnings"]:
        print(f"[data] WARNING: {w}", flush=True)

    traded = try_json(f"{BASE}/league/{draft.get('league_id')}/traded_picks") \
        if draft.get("league_id") else None

    uid = str(USER_ID or user.get("user_id") or "")
    state = DraftState(
        draft_id=str(draft["draft_id"]), user_id=uid,
        my_slot=max(1, override) if override else 1,
        shape=shape, players=players,
        status=str(draft.get("status") or ""),
        is_mock=not bool(draft.get("league_id")),
    )
    print(f"[ready] {shape.name or ('Mock' if state.is_mock else 'League')} | {shape.teams} teams | "
          f"{shape.ppr} PPR | {shape.rounds} rounds | {shape.playoff_teams} playoff teams | "
          f"playoffs wk {shape.playoff_start}-{FULL_SEASON_WEEKS - 1}", flush=True)

    if args.board:
        print_value_board(players, base)
        return
    if args.plan:
        seat = override or resolve_slot(draft, uid, [], override)[0]
        if seat:
            print_seat_report(players, shape, base, int(seat))
        print_slot_plans(players, shape, base)
        return

    if args.report:
        picks = try_json(f"{BASE}/draft/{state.draft_id}/picks") or []
        ingest_picks(state, picks)
        slot, _src = resolve_slot(draft, uid, picks, override)
        if slot:
            state.my_slot = slot
            state.my_picks = resolve_owned_picks(draft, traded, shape, slot)
        analyze_and_report(state, base)
        return

    print("[ready] Monitoring live board...", flush=True)
    last_notified = -1
    consecutive_errors = 0
    while True:
      try:
        draft_data = try_json(f"{BASE}/draft/{state.draft_id}") or {}
        picks = try_json(f"{BASE}/draft/{state.draft_id}/picks") or []
        state.status = str(draft_data.get("status") or "")
        ingest_picks(state, picks)

        slot, source = resolve_slot(draft_data, uid, picks, override)
        if slot:
            if state.slot_source == "unresolved":
                print(f"[locked] Seat: slot {slot} ({source})", flush=True)
            state.my_slot = slot
            state.slot_source = source
            state.my_picks = resolve_owned_picks(draft_data or draft, traded, shape, slot)
            if state.my_picks != my_pick_numbers(slot, shape.teams, shape.rounds,
                                                 shape.snake, shape.reversal_round):
                print(f"[picks] traded or reversed picks detected: {state.my_picks}",
                      flush=True)

        current = len(picks) + 1
        total = shape.teams * shape.rounds

        if args.preview:
            if state.slot_source == "unresolved":
                raise SystemExit("Pass --slot N to preview.")
            mine = state.my_picks or my_pick_numbers(
                state.my_slot, shape.teams, shape.rounds, shape.snake, shape.reversal_round)
            target = next((n for n in mine if n >= current), current)
            print(f"[preview] pick {target} | slot {state.my_slot}", flush=True)
            emit_turn(state, target, base)
            return

        if state.status == "complete" or current > total:
            print("[draft complete]", flush=True)
            analyze_and_report(state, base)
            break

        if state.slot_source == "unresolved":
            print("[waiting] draft order not posted...", end="\r", flush=True)
            time.sleep(POLL_SECONDS)
            continue

        mine = state.my_picks or my_pick_numbers(
            state.my_slot, shape.teams, shape.rounds, shape.snake, shape.reversal_round)
        if current in mine and last_notified != current:
            emit_turn(state, current, base)
            last_notified = current
        else:
            rnd = (current - 1) // shape.teams + 1
            on = pick_to_slot(current, shape.teams, shape.snake, shape.reversal_round)
            print(f"[drafting] R{rnd} pick {current} | on clock: slot {on} "
                  f"(you: slot {state.my_slot})", end="\r", flush=True)
        consecutive_errors = 0
        time.sleep(POLL_SECONDS)
      except KeyboardInterrupt:
        raise
      except Exception as exc:
        consecutive_errors += 1
        print(f"\n[warn] poll failed ({type(exc).__name__}: {exc}); "
              f"retrying ({consecutive_errors})", flush=True)
        if consecutive_errors >= 20:
            print("[error] 20 consecutive failures; stopping.", flush=True)
            raise
        time.sleep(min(20.0, POLL_SECONDS * consecutive_errors))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[stopped] Draft Commander v35 terminated.")
        sys.exit(0)
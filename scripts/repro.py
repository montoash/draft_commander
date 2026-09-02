import json, random, statistics, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "docs"))
import draft_commander_v35_original as E

ROST = json.load(open(ROOT / "data" / "draft_1400876063868850176_full_rosters_v35.json"))

shape = E.LeagueShape(
    teams=12, rounds=15, snake=True, ppr=1.0, te_bonus=0.0,
    slots={"QB":1,"RB":2,"WR":2,"TE":1,"FLEX":2,"K":1,"DEF":1},
    scoring=E.DEFAULT_SCORING, superflex=False,
    playoff_teams=6, playoff_start=15,
)

repl = ROST['replacement_levels']
stream_ppg = ROST['waiver_floor_ppg']
base = E.Baseline(repl=repl, stream_ppg=stream_ppg,
                  idx=E.replacement_index(shape), shape=shape)

def mk(d):
    p = E.Player(pid=d['player_id'], name=d['name'], team=d['team'], pos=d['pos'],
                 adp=d['adp'], proj=d['proj'], injury=d.get('injury') or '',
                 bye=int(d.get('bye') or 0))
    p.proj_adj = d['proj']; p.ppg = d['proj']/E.FULL_SEASON_WEEKS
    p.vorp = base.vorp(p)
    return p

allp = {}
for t in ROST['league_rosters']:
    for d in t['players']:
        allp[d['name']] = mk(d)

# my roster in draft order
order = ["Jaxon Smith-Njigba","Amon-Ra St. Brown","Chris Olave","DeVonta Smith",
         "Jalen Hurts","Jadarian Price","Sam LaPorta","Jayden Reed","Travis Kelce",
         "Brock Purdy","Khalil Shakir","Makai Lemon","Jonathon Brooks",
         "Ka'imi Fairbairn","ARI Defense"]
mine = [allp[n] for n in order]

# roster BEFORE pick 112 (first 9 picks)
before112 = mine[:9]
print("roster before pick 112:", [(p.pos,p.name) for p in before112])

purdy = allp["Brock Purdy"]
mason = allp["Jordan Mason"]

def mv(p, roster, sims, seed):
    rv = E.season_value(roster, shape, base, sims=sims, rng=random.Random(seed))
    return E.season_value(list(roster)+[p], shape, base, sims=sims, rng=random.Random(seed)) - rv

print()
print("=== marginal value at pick 112, roster of 9 ===")
for label, sims, seed in [("deterministic (sims=0)",0,0),
                          ("decide path (sims=60, seed 20260901+112)",60,20260901+112),
                          ("audit path  (sims=150, seed 777+112)",150,777+112)]:
    print(f"{label:46} Purdy={mv(purdy,before112,sims,seed):7.2f}  Mason={mv(mason,before112,sims,seed):7.2f}")

print()
print("=== noise: 12 different seeds, sims=60 ===")
pv=[];mvals=[]
for s in range(12):
    pv.append(mv(purdy,before112,60,1000+s)); mvals.append(mv(mason,before112,60,1000+s))
print("Purdy mv: min %.1f max %.1f mean %.1f sd %.1f" % (min(pv),max(pv),statistics.mean(pv),statistics.stdev(pv)))
print("Mason mv: min %.1f max %.1f mean %.1f sd %.1f" % (min(mvals),max(mvals),statistics.mean(mvals),statistics.stdev(mvals)))

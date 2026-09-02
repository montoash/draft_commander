import json, random, sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "docs"))
import draft_commander_v35_original as E
exec(open(_HERE / "repro.py").read().split('print()')[0])   # reuse setup

NEXT = {112:129, 129:136, 136:153}

def followers_ev(roster, cand, pool, surv):
    """Engine's own two-ply EV, with e_next deterministic (as in decide())."""
    rc = list(roster)+[cand]
    val_c = E.season_value(rc, shape, base, sims=0)
    fol = []
    for q in pool:
        if q.pid == cand.pid: continue
        fol.append((E.season_value(rc+[q], shape, base, sims=0) - val_c, q))
    fol.sort(key=lambda x:-x[0])
    e_next, none_yet = 0.0, 1.0
    for mv_q, q in fol:
        s = surv.get(q.name, 0.6)
        e_next += mv_q*s*none_yet
        none_yet *= (1-s)
    e_next += none_yet*(fol[-1][0] if fol else 0.0)
    return e_next, fol[:3]

# available pool approximations at each pick (players we KNOW were undrafted then)
POOL = {
 112: ["Jordan Mason","Jonathon Brooks","Kenny Gainwell","Rachaad White","Aaron Jones",
       "Blake Corum","Chris Rodriguez","Brock Purdy","Khalil Shakir","Makai Lemon",
       "George Kittle","Brenton Strange","Josh Downs","Jalen Coker","Patrick Mahomes"],
 129: ["Jonathon Brooks","Rachaad White","Aaron Jones","Chris Rodriguez","Blake Corum",
       "Khalil Shakir","Makai Lemon","Jalen Coker","Xavier Worthy","Isaiah Likely"],
 136: ["Jonathon Brooks","Rachaad White","Chris Rodriguez","Makai Lemon","Jalen Coker",
       "Xavier Worthy","T.J. Hockenson"],
}
SURV = {"Jordan Mason":0.60,"Jonathon Brooks":0.72,"Kenny Gainwell":0.99,"Rachaad White":0.85,
        "Aaron Jones":0.9,"Blake Corum":0.9,"Chris Rodriguez":0.95,"Brock Purdy":0.66,
        "Khalil Shakir":0.52,"Makai Lemon":0.68,"George Kittle":0.63,"Brenton Strange":0.97,
        "Josh Downs":0.29,"Jalen Coker":0.61,"Patrick Mahomes":0.68,"Xavier Worthy":0.75,
        "Isaiah Likely":0.87,"T.J. Hockenson":0.97}

for pick, taken_idx in ((112,9),(129,10),(136,11)):
    roster = mine[:taken_idx]
    pool = [allp[n] for n in POOL[pick] if n in allp]
    print(f"\n===== PICK {pick}  (roster: {E.Counter(p.pos for p in roster)}) =====")
    rows=[]
    for c in pool:
        seed = 20260901+pick
        rv = E.season_value(roster, shape, base, sims=60, rng=random.Random(seed))
        mv = E.season_value(roster+[c], shape, base, sims=60, rng=random.Random(seed)) - rv
        en,_ = followers_ev(roster, c, pool, SURV)
        rows.append((mv+en, mv, en, c))
    rows.sort(key=lambda r:-r[0])
    for ev,mv,en,c in rows[:8]:
        print(f"  {c.pos:3} {c.name:22} mv={mv:6.1f}  e_next={en:6.1f}  EV={ev:7.2f}")

import json, random, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import draft_commander as F

ROST=json.load(open(ROOT / "data" / "draft_1400876063868850176_full_rosters_v35.json"))
shape=F.LeagueShape(teams=12,rounds=15,snake=True,ppr=1.0,te_bonus=0.0,
    slots={"QB":1,"RB":2,"WR":2,"TE":1,"FLEX":2,"K":1,"DEF":1},
    scoring=F.DEFAULT_SCORING,superflex=False,playoff_teams=6,playoff_start=15)
base=F.Baseline(repl=ROST['replacement_levels'],stream_ppg=ROST['waiver_floor_ppg'],
                idx=F.replacement_index(shape),shape=shape)
def mkp(pid,name,team,pos,adp,proj,inj="",bye=0):
    p=F.Player(pid=pid,name=name,team=team,pos=pos,adp=adp,proj=proj,injury=inj,bye=bye)
    p.proj_adj=proj; p.ppg=proj/F.FULL_SEASON_WEEKS; p.vorp=base.vorp(p); return p

allp,byslot={},defaultdict(list)
for t in ROST['league_rosters']:
    for d in t['players']:
        p=mkp(d['player_id'],d['name'],d['team'],d['pos'],d['adp'],d['proj'],
              d.get('injury') or '',int(d.get('bye') or 0))
        allp[d['name']]=p; byslot[t['slot']].append(p)

# pad the universe with the undrafted pool the live engine would see
rng=random.Random(7)
for pos,n,top in (("RB",70,150.0),("WR",90,168.0),("TE",45,155.0),("QB",25,285.0),
                  ("K",25,100.0),("DEF",20,95.0)):
    for i in range(n):
        nm=f"UD {pos}{i}"
        allp[nm]=mkp(f"ud{pos}{i}",nm,"FA",pos,190.0+i*1.6,top*(1.0-0.012*i),
                     "",rng.choice([0,5,6,7,8,9,10,11,12,13,14]))
players={p.pid:p for p in allp.values()}
assign={}
for slot,ps in byslot.items():
    for n,p in zip(F.my_pick_numbers(slot,12,15,True), sorted(ps,key=lambda x:x.adp)):
        assign[n]=(slot,p)

order=["Jaxon Smith-Njigba","Amon-Ra St. Brown","Chris Olave","DeVonta Smith","Jalen Hurts",
       "Jadarian Price","Sam LaPorta","Jayden Reed","Travis Kelce","Brock Purdy",
       "Khalil Shakir","Makai Lemon","Jonathon Brooks","Ka'imi Fairbairn","ARI Defense"]
mine=[allp[n] for n in order]

def state_at(pick_no,nheld):
    st=F.DraftState(draft_id="x",user_id="u",my_slot=9,shape=shape,players=players)
    st.slot_rosters=defaultdict(list); st.picks=[]
    for n in sorted(assign):
        if n>=pick_no: break
        slot,p=assign[n]
        if slot==9: continue
        st.taken.add(p.pid); st.slot_rosters[slot].append(p.pid)
        st.picks.append({"draft_slot":slot,"player_id":p.pid})
    for p in mine[:nheld]:
        st.taken.add(p.pid); st.slot_rosters[9].append(p.pid)
    st.my_picks=F.my_pick_numbers(9,12,15,True)
    return st

room=F.RoomModel(tau=3.0)
for pick,nheld in ((112,9),(129,10),(136,11)):
    roster=mine[:nheld]; st=state_at(pick,nheld)
    avail=[p for p in players.values() if p.pid not in st.taken and p.pos not in ("K","DEF")]
    avail.sort(key=lambda p:-p.vorp); pool=avail[:F.NEXT_POOL]
    upcoming=[n for n in st.my_picks if n>pick]
    surv=F.survival_probs(pool,pick,upcoming[0],st,room=room)
    rows=[]
    for c in pool:
        seed=20260901+pick
        rv=F.season_value(roster,shape,base,sims=60,rng=random.Random(seed))
        mv=F.season_value(roster+[c],shape,base,sims=60,rng=random.Random(seed))-rv
        # OLD two-ply ev
        rc=roster+[c]; vc=F.season_value(rc,shape,base,sims=0)
        fol=sorted(((F.season_value(rc+[q],shape,base,sims=0)-vc,q) for q in pool if q.pid!=c.pid),key=lambda x:-x[0])
        e,ny=0.0,1.0
        for m,q in fol:
            s=surv.get(q.pid,0.0); e+=m*s*ny; ny*=(1-s)
        e+=ny*(fol[-1][0] if fol else 0)
        rows.append((mv+e, F.plan_ev(roster,c,pool,pick,upcoming,st,base,room), mv, c))
    print(f"\n===== PICK {pick} =====")
    print(f"  {'':4}{'player':24}{'mv':>7}{'OLD ev':>9}{'NEW plan_ev':>13}")
    print("  -- ranked by OLD two-ply ev (what v35 did) --")
    for ev,pe,mv,c in sorted(rows,key=lambda r:-r[0])[:4]:
        print(f"  {c.pos:4}{c.name:24}{mv:7.1f}{ev:9.1f}{pe:13.1f}")
    print("  -- ranked by NEW full-horizon plan_ev --")
    for ev,pe,mv,c in sorted(rows,key=lambda r:-r[1])[:4]:
        print(f"  {c.pos:4}{c.name:24}{mv:7.1f}{ev:9.1f}{pe:13.1f}")

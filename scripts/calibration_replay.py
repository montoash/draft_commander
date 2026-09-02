import json, sys
from collections import defaultdict
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import draft_commander as NEW
OLD=None
U=str(ROOT/"data")+"/"
DEC=U+"draft_1400911267358629888_decisions_v36.json"
ROS=U+"draft_1400911267358629888_full_rosters_v36.json"

dec=json.load(open(DEC)); ros=json.load(open(ROS))
owner={}                                   # player_id -> final team slot
meta={}
for t in ros['league_rosters']:
    for p in t['players']:
        owner[p['player_id']]=t['slot']; meta[p['player_id']]=p
boards={p['pick_no']:p.get('board',[]) for p in dec['picks'] if p.get('board')}
cands={p['pick_no']:p['candidates'] for p in dec['picks']}
order=sorted(boards)

def run(M):
    shape=M.LeagueShape(teams=12,rounds=15,snake=True,ppr=1.0,te_bonus=0.0,
        slots={"QB":1,"RB":2,"WR":2,"TE":1,"FLEX":2,"K":1,"DEF":1},
        scoring=M.DEFAULT_SCORING,superflex=False,playoff_teams=6,playoff_start=15)
    players={}
    for b in boards.values():
        for x in b:
            if x['id'] not in players:
                p=M.Player(pid=x['id'],name=x['name'],team="X",pos=x['pos'],
                           adp=x['adp'],proj=x['proj'])
                p.proj_adj=x['proj']; p.ppg=x['proj']/18; players[x['id']]=p
    for pid,m in meta.items():
        if pid not in players:
            p=M.Player(pid=pid,name=m['name'],team=m['team'],pos=m['pos'],
                       adp=m['adp'],proj=m['proj'])
            p.proj_adj=m['proj']; p.ppg=m['proj']/18; players[pid]=p
    rows=[]
    for i,pn in enumerate(order[:-1]):
        nxt=order[i+1]
        cur={x['id'] for x in boards[pn]}; nx={x['id'] for x in boards[nxt]}
        st=M.DraftState(draft_id="C",user_id="u",my_slot=9,shape=shape,players=players)
        # taken = anyone with a known owner who is NOT on the current board
        st.taken={pid for pid in players if pid not in cur and pid in owner}
        st.slot_rosters=defaultdict(list); st.picks=[]
        for pid in st.taken:                       # REAL owning team
            st.slot_rosters[owner[pid]].append(pid)
            st.picks.append({"draft_slot":owner[pid],"player_id":pid})
        st.my_picks=M.my_pick_numbers(9,12,15,True)
        pool=[players[x['id']] for x in boards[pn]]
        surv=M.survival_probs(pool,pn,nxt,st,room=M.RoomModel(tau=3.0))
        for c in cands[pn]:
            if c['id'] in players:
                rows.append((pn,(pn-1)//12+1,c['pos'],c['name'],
                             surv.get(c['id'],1.0),1 if c['id'] in nx else 0,c['survival']))
    return rows

def bias(rs): return (sum(r[4] for r in rs)-sum(r[5] for r in rs))/max(1,len(rs))
def brier(rs): return sum((r[4]-r[5])**2 for r in rs)/max(1,len(rs))
print("Survival calibration replay: draft 1400911267358629888, real opponent rosters\n")
for label,M in (("NEW (positional rank)",NEW),):
    rows=run(M)
    late=[r for r in rows if r[1]>=13 and r[2] in ("K","DEF")]
    dl=[r for r in late if r[2]=="DEF"]; kl=[r for r in late if r[2]=="K"]
    print(f"{label}")
    print(f"   all cands    bias {bias(rows):+.3f}  Brier {brier(rows):.3f}  n={len(rows)}")
    print(f"   endgame DEF  bias {bias(dl):+.3f}  Brier {brier(dl):.3f}  n={len(dl)}")
    print(f"   endgame K    bias {bias(kl):+.3f}  Brier {brier(kl):.3f}  n={len(kl)}")
    if label.startswith("NEW"):
        print("   pick 160 defenses:")
        for r in sorted(dl,key=lambda x:x[0]):
            if r[0]==160:
                print(f"      {r[3]:16} logged {r[6]:.2f} -> now {r[4]:.2f}  "
                      f"({'SURVIVED' if r[5] else 'GONE'})")
    print()

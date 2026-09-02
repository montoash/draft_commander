import random, sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
exec(open(_HERE / "validate2.py").read().split('room=F.RoomModel')[0])
import draft_commander as F
room=F.RoomModel(tau=3.0)

# ---- 1. last pick (177): DEF forced, no next pick
pick,nheld=177,14
roster=mine[:nheld]; st=state_at(pick,nheld)
defs=[p for p in players.values() if p.pos=="DEF" and p.pid not in st.taken]
defs.sort(key=lambda p:-p.proj); defs=defs[:8]
cur=F.season_value(roster,shape,base,sims=0)
print("=== PICK 177 (last pick, DEF forced) ===")
rows=[]
for d in defs:
    mv=F.season_value(roster+[d],shape,base,sims=0)-cur
    e=max(F.season_value(roster+[d,q],shape,base,sims=0)-F.season_value(roster+[d],shape,base,sims=0)
          for q in defs if q.pid!=d.pid)
    rows.append((mv,e,d))
print(f"  {'DEF':16}{'proj':>6}{'mv':>8}{'OLD ev=mv+e_next':>19}{'NEW ev (e_next=0)':>19}")
old=sorted(rows,key=lambda r:-(r[0]+r[1]))[0][2].name
new=sorted(rows,key=lambda r:-r[0])[0][2].name
for mv,e,d in sorted(rows,key=lambda r:-(r[0]+r[1])):
    print(f"  {d.name:16}{d.proj:6.0f}{mv:8.2f}{mv+e:19.2f}{mv:19.2f}")
print(f"  OLD would take: {old}   NEW takes: {new}")

# ---- 2. waiver floor
sys.path.insert(0, str(_HERE.parent / "docs"))
import draft_commander_v35_original as E
print("\n=== waiver floor (season pts) ===")
print(f"  {'pos':5}{'repl':>8}{'v35 (0.55 clamp)':>19}{'fixed (pool model)':>21}")
class FakeP:
    def __init__(s,pos,proj,adp): s.pos,s.proj_adj,s.adp,s.vorp=pos,proj,adp,0.0
import collections
pool=collections.defaultdict(list)
for p in players.values(): pool[p.pos].append(p)
for pos in ("RB","WR","TE","QB"):
    vals=sorted((p.proj_adj for p in pool[pos]),reverse=True)
    idx=F.replacement_index(shape); i=min(len(vals)-1,max(0,idx[pos]-1)); repl=vals[i]
    dc=sum(1 for p in pool[pos] if 0<p.adp<=180)
    j=min(len(vals)-1,max(0,dc))
    old_s=max(vals[j]*E.STREAM_HAIRCUT[pos], E.STREAM_FLOOR_FRAC*repl)
    sub=vals[j:j+F.STREAM_POOL[pos]]
    k=max(1,int(round(len(sub)*F.STREAM_CLAIM_ODDS)))
    new_s=min(sum(sub[:k])/k*F.STREAM_HAIRCUT[pos], repl)
    print(f"  {pos:5}{repl:8.1f}{old_s:19.1f}{new_s:21.1f}")

# ---- 3. early rounds must not regress
print("\n=== round 1 and 2 sanity (pick 9 / 16) ===")
for pick,nheld in ((9,0),(16,1)):
    st=state_at(pick,nheld)
    avail=[p for p in players.values() if p.pid not in st.taken and p.pos not in ("K","DEF")]
    avail.sort(key=lambda p:-p.vorp); pool=avail[:F.NEXT_POOL]
    up=[n for n in st.my_picks if n>pick]
    rows=[(F.plan_ev(mine[:nheld],c,pool,pick,up,st,base,room),c) for c in pool[:10]]
    rows.sort(key=lambda r:-r[0])
    print(f"  pick {pick}: NEW top-3 -> " + ", ".join(f"{c.name}" for _,c in rows[:3]))

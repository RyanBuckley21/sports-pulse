"""Re-measure CLV from OUR FIRST CAPTURE instead of the book's open.

READ-ONLY. Writes nothing, rewrites no ledger row. Run it from the repo root
with a deep clone (`git fetch --unshallow origin main`); it walks the committed
games store's own history, so it needs no network and no new data.

    python3 -m tools.clv_first_seen_backfill

The stored `*_ml_open` is ESPN's own opening number, which can predate our
first sighting of a game by days. On 2026_05_PHI_JAX the book opened JAX at
+110 and our first capture already read -285: the whole move happened before
the board had an opinion, and crediting it to the model is the confound the
docs name.

The committed games store is the fix and needs no new data: it is written
several times a day, so the first commit in which a game carried BOTH a price
AND a standout is what we actually saw when the pick existed. Anchor there.
"""
import json, subprocess, collections, statistics, random

def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout

def be(american):
    try: n=float(american)
    except (TypeError, ValueError): return None
    if n==0: return None
    return 1.0/(1.0+(n/100.0 if n>0 else 100.0/abs(n)))

def side_price(odds, side, home, away):
    if not odds or not side: return None
    lead=str(side).split()[0]
    if lead==home: return odds.get("home_ml")
    if lead==away: return odds.get("away_ml")
    return None

# the picks we need, from the ledger (latest run per pick)
latest={}
for line in open("data/signal_report_history.jsonl"):
    r=json.loads(line)
    if r.get("status")!="recorded" or r.get("clv_delta") is None: continue
    k=(r.get("sport"), r.get("gamePk"), r.get("bet_type"))
    if k not in latest or (r.get("run_id") or "")>=(latest[k].get("run_id") or ""): latest[k]=r
want={(k[0],k[1]) for k in latest}
print("picks to re-measure:", len(want))

commits=[l.split(" ",1) for l in
         sh("git","log","--reverse","--format=%H %ad","--date=format:%Y-%m-%d %H:%M",
            "--since=2026-09-20","--","data/insights.games.json").strip().splitlines()]
print("store commits scanned:", len(commits))

first={}   # (sport,pk) -> (price_at_first_pick, iso)
for sha, when in commits:
    blob=sh("git","show","{}:data/insights.games.json".format(sha))
    if not blob: continue
    try: store=json.loads(blob)
    except Exception: continue
    for sport, part in store.items():
        if not isinstance(part, dict): continue
        for pk, e in part.items():
            key=(sport,pk)
            if key not in want or key in first: continue
            so=e.get("standout") or {}; o=e.get("odds") or {}
            if not so.get("score"): continue          # no pick yet -- not our anchor
            p=side_price(o, so.get("side"), (e.get("home") or {}).get("abbr"),
                         (e.get("away") or {}).get("abbr"))
            if p is None: continue                    # unpriced at that moment
            first[key]=(p, when)
print("anchored from history:", len(first))

def ci(v, boot=4000, seed=17):
    rng=random.Random(seed); n=len(v); o=[]
    for _ in range(boot): o.append(statistics.mean(v[rng.randrange(n)] for _ in range(n)))
    o.sort(); return o[int(.025*boot)], o[int(.975*boot)]

book=collections.defaultdict(list); ours=collections.defaultdict(list)
for (sport,pk,bt), r in latest.items():
    close=r.get("price")
    anchor=first.get((sport,pk))
    if close is None or anchor is None: continue
    b_open=(r.get("clv") or {}).get("open")
    if b_open is None: continue
    bo, co, ao = be(b_open), be(close), be(anchor[0])
    if None in (bo,co,ao): continue
    book[sport].append(co-bo); ours[sport].append(co-ao)
for d in (book,ours): d["ALL"]=[x for s,v in d.items() if s!="ALL" for x in v]

print("\n{:<5} {:>4}  {:>22}   {:>22}".format("sport","n","from the BOOK's open","from OUR first capture"))
for sp in ("nfl","cfb","mlb","ALL"):
    b,a=book.get(sp) or [], ours.get(sp) or []
    if len(a)<3: continue
    bl,bh=ci(b); al,ah=ci(a)
    verdict="REAL" if al>0 else ("negative" if ah<0 else "spans zero")
    print("{:<5} {:>4}  {:+6.2f}pp [{:+.2f},{:+.2f}]   {:+6.2f}pp [{:+.2f},{:+.2f}]  {}".format(
        sp, len(a), 100*statistics.mean(b),100*bl,100*bh,
        100*statistics.mean(a),100*al,100*ah, verdict))

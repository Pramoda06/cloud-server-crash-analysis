"""
"Why did this metric change?" — walks the contribution PARTS backwards.

For a node N:  delta = value_now − value_ref.
Each part of N is (dependency, contribution). The part whose contribution changed most
in the same direction as delta is the dominant cause; the trace continues at that
dependency until it reaches an operator INPUT, the process/lock table, or a constant.

ref = the baseline captured after the last reset (or the values a few seconds ago).
"""
from .graph import NODES


def _fmt(v, unit):
    if v is None:
        return "—"
    s = f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.1f}" if abs(v) >= 1 else f"{v:.2f}"
    return s + ("%" if unit in ("%",) else f" {unit}" if unit else "")


def trace(node, V, P, refV, refP, max_depth=8):
    steps, seen = [], set()
    cur = node
    while cur and cur not in seen and len(steps) < max_depth:
        seen.add(cur)
        meta = NODES.get(cur, {"label": cur, "unit": "", "type": "DERIVED"})
        now, ref = V.get(cur), (refV or {}).get(cur)
        step = {"node": cur, "label": meta["label"], "unit": meta["unit"], "type": meta["type"],
                "now": now, "ref": ref, "now_txt": _fmt(now, meta["unit"]), "ref_txt": _fmt(ref, meta["unit"])}
        if meta["type"] in ("INPUT",) or cur in ("procs_table", "locks"):
            step["because"] = "operator control" if meta["type"] == "INPUT" else "simulated table (Process / Deadlock Lab)"
            steps.append(step)
            break
        parts, rparts = P.get(cur) or {}, (refP or {}).get(cur) or {}
        delta = (now or 0) - (ref or 0)
        step["delta"] = delta
        if not parts:
            steps.append(step)
            break
        rows = []
        for name, (dep, val) in parts.items():
            rv = rparts.get(name, (dep, 0.0))[1]
            rows.append({"part": name, "dep": dep, "now": val, "ref": rv, "delta": val - rv})
        step["parts"] = [dict(r, now_txt=_fmt(r["now"], meta["unit"]), delta_txt=_fmt(r["delta"], meta["unit"]))
                         for r in rows]
        sign = 1 if delta >= 0 else -1
        best = max(rows, key=lambda r: sign * r["delta"])
        if abs(delta) < 1e-6 or sign * best["delta"] <= 0:
            step["because"] = "unchanged"
            steps.append(step)
            break
        tot = sum(r["delta"] for r in rows if sign * r["delta"] > 0) or 1.0
        step["because"] = best["part"]
        step["because_share"] = best["delta"] / tot
        step["next"] = best["dep"]
        steps.append(step)
        cur = best["dep"]
    return {"node": node, "steps": steps, "sentences": sentences(steps)}


def sentences(steps):
    out = []
    for i, s in enumerate(steps):
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        if s.get("ref") is None or s.get("now") is None:
            continue
        if abs((s["now"] or 0) - (s["ref"] or 0)) < 1e-6:
            out.append(f"{s['label']} has not changed ({s['now_txt']}).")
            break
        verb = "increased" if s["now"] > s["ref"] else "decreased"
        if s["type"] == "INPUT":
            out.append(f"{s['label']} was changed by the operator from {s['ref_txt']} to {s['now_txt']}.")
            break
        if s["node"] in ("procs_table", "locks"):
            out.append(f"The {s['label'].lower()} changed (see the Process & Resource Lab).")
            break
        if nxt is None:
            out.append(f"{s['label']} {verb} from {s['ref_txt']} to {s['now_txt']}"
                       + (f" — mainly {s['because']}." if s.get("because") and s["because"] != "unchanged" else "."))
            break
        share = s.get("because_share", 1.0)
        src = f"{share:.0%} of the change comes from {s['because']}" if share < 0.995 else f"driven by {s['because']}"
        out.append(f"{s['label']} {verb} from {s['ref_txt']} to {s['now_txt']} — {src} ({nxt['label']}).")
    return out

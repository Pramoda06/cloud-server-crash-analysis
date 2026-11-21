"""
Resource-allocation algorithms for the Deadlock Lab (pure functions, no state).

The lab supports single-instance AND multiple-instance resources, so it uses the
algorithm that is correct for each case:

* wait-for graph (WFG)  — process -> process edge when P waits for a resource that Q holds.
  A cycle in the WFG is the same thing as a cycle in the resource-allocation graph (RAG).
  With ONLY single-instance resources a cycle  <=>  deadlock.
  With multiple instances a cycle is necessary but NOT sufficient for deadlock.

* detection algorithm  (Coffman / Shoshani; Silberschatz "Deadlock detection, several
  instances of a resource type") — Work = Available; repeatedly find an unfinished process
  whose outstanding Request <= Work, assume it runs to completion and returns its Allocation.
  Processes that can never be finished are deadlocked. Correct for both resource models.

* Banker's safety algorithm (avoidance) — same reduction, but with Need = Max - Allocation
  instead of the current Request. A state is SAFE if some order lets every process obtain
  its maximum claim and finish.

All functions take plain dicts so they are easy to test and to show step by step in the UI:
    avail  {rid: free instances}
    alloc  {pid: {rid: n}}
    req    {pid: {rid: n}}          (outstanding request of a blocked process)
    need   {pid: {rid: n}}          (Max - Allocation, Banker's only)
"""


def _le(vec, work):
    return all(n <= work.get(r, 0) for r, n in vec.items())


def reduce_state(pids, avail, alloc, demand):
    """Generic reduction used by both detection (demand = request) and safety (demand = need).

    Returns (finished_order, unfinished, steps) where steps is a human-readable trace."""
    work = dict(avail)
    finish = {p: False for p in pids}
    order, steps = [], []
    progress = True
    while progress:
        progress = False
        for p in pids:
            if finish[p]:
                continue
            d = {r: n for r, n in (demand.get(p) or {}).items() if n > 0}
            if _le(d, work):
                before = dict(work)
                for r, n in (alloc.get(p) or {}).items():
                    work[r] = work.get(r, 0) + n
                finish[p] = True
                order.append(p)
                steps.append({"pid": p, "demand": d, "work_before": before, "work_after": dict(work)})
                progress = True
    return order, [p for p in pids if not finish[p]], steps


def detect(pids, avail, alloc, req):
    """Deadlock detection for multiple-instance resources.

    A process holding nothing cannot be part of a deadlock (textbook: Finish[i] = true when
    Allocation_i = 0), even if it is blocked behind one — it is reported separately."""
    holding = [p for p in pids if any(n > 0 for n in (alloc.get(p) or {}).values())]
    order, dead, steps = reduce_state(holding, avail, alloc, req)
    free_ok = [p for p in pids if p not in holding]
    return {"deadlocked": dead, "order": free_ok + order, "steps": steps}


def is_safe(pids, avail, alloc, need):
    order, unfinished, steps = reduce_state(pids, avail, alloc, need)
    return {"safe": not unfinished, "sequence": order, "stuck": unfinished, "steps": steps}


def wait_for_edges(waiting, alloc):
    """waiting {pid: rid}; alloc {pid: {rid: n}} -> [(p, q, rid)] for every holder q of rid."""
    edges = []
    for p, rid in waiting.items():
        for q, held in alloc.items():
            if q != p and held.get(rid, 0) > 0:
                edges.append((p, q, rid))
    return edges


def find_cycle(edges, restrict=None):
    """First cycle (list of pids) in a directed graph given as [(a, b, label)]."""
    adj = {}
    for a, b, _ in edges:
        if restrict is not None and (a not in restrict or b not in restrict):
            continue
        adj.setdefault(a, []).append(b)
    color, stack = {}, []

    def dfs(u):
        color[u] = 1
        stack.append(u)
        for v in adj.get(u, []):
            if color.get(v, 0) == 1:
                return stack[stack.index(v):]
            if color.get(v, 0) == 0:
                c = dfs(v)
                if c:
                    return c
        stack.pop()
        color[u] = 2
        return None

    for node in list(adj):
        if color.get(node, 0) == 0:
            c = dfs(node)
            if c:
                return c
    return []

"""Exact count-pattern DP and global E/T convolution for Q1 only.

Interchangeable boxes are grouped by (mass, volume); identity is restored on
export. Q1 has neither deadlines nor fleet scheduling. Every nonempty count
pattern and EVERY aircraft type is retained. No beam size or label cap is used.
Floating comparisons use 1e-9 kWh and 1e-6 s, not a discretized budget grid.
"""
from dataclasses import dataclass
from functools import lru_cache
from itertools import product
from math import ceil

from q1 import direct_energy

ENERGY_TOL = 1e-9
TIME_TOL = 1e-6


@dataclass(frozen=True)
class Pattern:
    id: int
    counts: tuple
    model: str
    kg: float
    volume: float
    energy: float
    duration: float
    critical_reserve: float


@dataclass(frozen=True)
class Label:
    energy: float
    seconds: float
    choices: tuple


def pareto(labels):
    """Keep one certificate per nondominated objective pair (within tolerance)."""
    ordered = sorted(labels, key=lambda v: (v.energy, v.seconds, v.choices))
    result, fastest = [], float("inf")
    for v in ordered:
        if v.seconds < fastest - TIME_TOL:
            # Equal energies to numerical precision: retain the faster label.
            while result and abs(result[-1].energy - v.energy) <= ENERGY_TOL:
                result.pop()
            result.append(v)
            fastest = v.seconds
    return tuple(result)


class ZonePatterns:
    def __init__(self, s, zone):
        self.s, self.zone = s, zone
        groups = {}
        for b in sorted(s.zone_boxes[zone], key=lambda b: b.id):
            groups.setdefault((b.kg, b.volume), []).append(b.id)
        self.keys = tuple(sorted(groups))
        self.ids = tuple(tuple(groups[k]) for k in self.keys)
        self.demand = tuple(map(len, self.ids))
        patterns = []
        out, back = s.node_leg("O01", zone), s.node_leg(zone, "O01")
        for counts in product(*(range(n + 1) for n in self.demand)):
            n = sum(counts)
            if not n:
                continue
            mass = sum(c * k[0] for c, k in zip(counts, self.keys))
            volume = sum(c * k[1] for c, k in zip(counts, self.keys))
            for model, t in sorted(s.transport.items()):
                if mass > t.max_kg + 1e-9 or volume > t.max_volume + 1e-9:
                    continue
                energy = direct_energy(s, zone, model, mass)
                rho = 1 - energy / t.battery_kwh
                if rho < -1e-12:
                    continue
                seconds = (t.prepare + n * t.load_each + s.flight_time(model, out) +
                           s.flight_time(model, back) + t.handoff + n * t.handoff_each)
                patterns.append(Pattern(len(patterns), counts, model, mass, volume, energy, seconds, rho))
        self.patterns = tuple(patterns)

    def feasible(self, reserve=None):
        return tuple(p for p in self.patterns if p.critical_reserve + 1e-12 >=
                     (self.s.transport[p.model].reserve if reserve is None else reserve))

    @lru_cache(maxsize=None)
    def transitions(self, state):
        if not any(state):
            return ()
        anchor = next(i for i, n in enumerate(state) if n)
        return tuple((p.id, tuple(a - b for a, b in zip(state, p.counts)))
                     for p in self.patterns if p.counts[anchor] and
                     all(a >= b for a, b in zip(state, p.counts)))

    def scalar(self, reserve=None, objective="energy"):
        """Independent state space from the existing individual-box bitmask DP."""
        feasible = {p.id for p in self.feasible(reserve)}
        @lru_cache(maxsize=None)
        def dp(state):
            if not any(state):
                return (0, 0.0, 0.0, ())
            best = None
            for pid, rem in self.transitions(state):
                if pid not in feasible:
                    continue
                prev = dp(rem)
                if prev is None:
                    continue
                p = self.patterns[pid]
                v = (prev[0] + 1, prev[1] + p.energy, prev[2] + p.duration, (pid,) + prev[3])
                key = lambda x: ((x[0], round(x[1], 9), round(x[2], 6), x[3]) if objective == "energy"
                                 else (x[0], round(x[2], 6), round(x[1], 9), x[3]))
                if best is None or key(v) < key(best):
                    best = v
            return best
        result = dp(self.demand)
        dp.cache_clear()
        return result

    def frontiers(self, maximum, reserve=None):
        feasible = {p.id for p in self.feasible(reserve)}
        @lru_cache(maxsize=None)
        def dp(state, count):
            if not any(state):
                return (Label(0.0, 0.0, ()),) if count == 0 else ()
            if count <= 0 or sum(state) < count:
                return ()
            mass = sum(n * k[0] for n, k in zip(state, self.keys))
            if mass > count * max(t.max_kg for t in self.s.transport.values()) + 1e-9:
                return ()
            candidates = []
            for pid, rem in self.transitions(state):
                if pid not in feasible:
                    continue
                p = self.patterns[pid]
                for old in dp(rem, count - 1):
                    candidates.append(Label(old.energy + p.energy, old.seconds + p.duration, (pid,) + old.choices))
            return pareto(candidates)
        result = {n: dp(self.demand, n) for n in range(1, min(maximum, sum(self.demand)) + 1)}
        dp.cache_clear()
        return {n: rows for n, rows in result.items() if rows}

    def plan(self, choice):
        used = [0] * len(self.ids)
        batches = []
        for pid in choice:
            p = self.patterns[pid]
            ids = []
            for i, n in enumerate(p.counts):
                ids.extend(self.ids[i][used[i]:used[i] + n])
                used[i] += n
            batches.append(dict(model=p.model, box_ids=sorted(ids), kg=p.kg, volume=p.volume,
                                energy=p.energy, duration=p.duration, return_soc=p.critical_reserve))
        if tuple(used) != self.demand:
            raise AssertionError("Pattern solution does not cover demand")
        return dict(zone=self.zone, batches=batches, sorties=len(batches),
                    energy_kwh=sum(b["energy"] for b in batches), work_seconds=sum(b["duration"] for b in batches))


def global_frontiers(zones, extra_sorties=0, reserve=None, progress=None):
    if extra_sorties < 0:
        raise ValueError("extra_sorties must be nonnegative")
    minima = {z: p.scalar(reserve) for z, p in zones.items()}
    if any(v is None for v in minima.values()):
        raise ValueError("At least one zone is infeasible")
    nmin = sum(v[0] for v in minima.values())
    combined = {0: (Label(0.0, 0.0, ()),)}
    local = {}
    remaining = nmin
    for z, patterns in zones.items():
        local[z] = patterns.frontiers(minima[z][0] + extra_sorties, reserve)
        remaining -= minima[z][0]
        gathered = {}
        for old_n, old_rows in combined.items():
            for add_n, rows in local[z].items():
                count = old_n + add_n
                if count + remaining > nmin + extra_sorties:
                    continue
                bucket = gathered.setdefault(count, [])
                for a in old_rows:
                    bucket.extend(Label(a.energy + b.energy, a.seconds + b.seconds,
                                        a.choices + ((z, b.choices),)) for b in rows)
        combined = {n: pareto(rows) for n, rows in gathered.items()}
        if progress:
            progress(f"Global frontier {z}: " + ", ".join(f"N={n}:{len(v)}" for n, v in combined.items()))
    return nmin, combined, local


def recover(zones, label):
    return {z: zones[z].plan(ids) for z, ids in label.choices}


def budget_choice(frontier, budget):
    feasible = [p for p in frontier if p.energy <= budget + ENERGY_TOL]
    return min(feasible, key=lambda p: (p.seconds, p.energy)) if feasible else None


def count_proof(s, plans):
    maximum_mass = max(t.max_kg for t in s.transport.values())
    maximum_volume = max(t.max_volume for t in s.transport.values())
    rows = []
    for zone, boxes in sorted(s.zone_boxes.items()):
        kg, volume = sum(b.kg for b in boxes), sum(b.volume for b in boxes)
        mass_lb, volume_lb = ceil(kg / maximum_mass - 1e-12), ceil(volume / maximum_volume - 1e-12)
        lb = max(mass_lb, volume_lb)
        rows.append(dict(zone=zone, demand_kg=kg, demand_m3=volume, mass_lower_bound=mass_lb,
                         volume_lower_bound=volume_lb, lower_bound=lb, feasible_sorties=plans[zone]["sorties"]))
    lower, upper = sum(r["lower_bound"] for r in rows), sum(r["feasible_sorties"] for r in rows)
    return dict(scope="Q1: each sortie serves one zone; indivisible boxes; no fleet/time-window constraints",
                maximum_aircraft_payload_kg=maximum_mass, maximum_aircraft_volume_m3=maximum_volume,
                lower_bound=lower, feasible_upper_bound=upper, gap=upper - lower,
                globally_optimal_count=upper == lower, zones=rows)


def critical_analysis(zones, low=0.1, high=0.4, progress=None):
    """Enumerate ALL candidate SOC thresholds, not an arbitrary reserve grid.

    A pattern is feasible at rho == rho_crit and infeasible strictly above it.
    Interval left endpoints are open and right endpoints closed; the lower
    bound itself is exported separately. Adjacent identical optima are merged.
    """
    if not 0 <= low < high <= 1:
        raise ValueError("Expected 0 <= reserve_min < reserve_max <= 1")
    zone_results, events = {}, []
    for z, patterns in zones.items():
        values = sorted({p.critical_reserve for p in patterns.patterns if low < p.critical_reserve < high})
        for p in patterns.patterns:
            events.append(dict(zone=z, pattern_id=p.id, model=p.model, counts=p.counts,
                               kg=p.kg, volume=p.volume, energy_kwh=p.energy, critical_reserve=p.critical_reserve))
        intervals = []
        for a, b in zip([low] + values, values + [high]):
            result = patterns.scalar((a + b) / 2)
            signature = None if result is None else (result[0], round(result[1], 9), round(result[2], 6))
            if intervals and intervals[-1]["signature"] == signature:
                intervals[-1]["right"] = b
                continue
            intervals.append(dict(left=a, right=b, signature=signature, result=result))
        zone_results[z] = dict(at_lower_bound=patterns.scalar(low), intervals=intervals,
                               candidate_breakpoints=len(values))
        if progress:
            progress(f"Critical reserve {z}: {len(values)} events, {len(intervals)} solution regimes")
    boundaries = sorted({low, high, *(r["left"] for v in zone_results.values() for r in v["intervals"]),
                         *(r["right"] for v in zone_results.values() for r in v["intervals"])})
    global_rows = []
    for a, b in zip(boundaries, boundaries[1:]):
        mid = (a + b) / 2
        values = {z: next(r["result"] for r in v["intervals"] if r["left"] < mid <= r["right"])
                  for z, v in zone_results.items()}
        bad = [z for z, v in values.items() if v is None]
        global_rows.append(dict(left_open=a, right_closed=b, feasible=not bad, infeasible_zones=bad,
                                sorties=None if bad else sum(v[0] for v in values.values()),
                                energy_kwh=None if bad else sum(v[1] for v in values.values()),
                                work_seconds=None if bad else sum(v[2] for v in values.values()),
                                choices={z: v[3] for z, v in values.items() if v is not None}))
    at_low = {z: d["at_lower_bound"] for z, d in zone_results.items()}
    low_bad = [z for z, v in at_low.items() if v is None]
    lower_endpoint = dict(reserve=low, feasible=not low_bad, infeasible_zones=low_bad,
                          sorties=None if low_bad else sum(v[0] for v in at_low.values()),
                          energy_kwh=None if low_bad else sum(v[1] for v in at_low.values()),
                          work_seconds=None if low_bad else sum(v[2] for v in at_low.values()),
                          choices={z:v[3] for z,v in at_low.items() if v is not None})
    return dict(reserve_min=low, reserve_max=high, equality_feasible=True, lower_endpoint=lower_endpoint,
                interval_convention="(left_open, right_closed]; lower endpoint is reported separately",
                zone_results=zone_results, global_intervals=global_rows, pattern_events=events)

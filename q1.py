"""Question 1: exact single-zone box partition and safe payloads."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from model import Box, Scenario


@dataclass(frozen=True)
class Batch:
    model: str
    box_ids: tuple[str, ...]
    kg: float
    volume: float
    duration: float
    energy: float
    return_soc: float


def direct_energy(s: Scenario, zone: str, model: str, payload: float) -> float:
    outbound = s.node_leg("O01", zone)
    inbound = s.node_leg(zone, "O01")
    return s.flight_energy(model, outbound, payload) + s.flight_energy(model, inbound, 0.0)


def safe_payload(s: Scenario, zone: str, model: str, reserve: float | None = None) -> float:
    t = s.transport[model]
    rho = t.reserve if reserve is None else reserve
    if not isfinite(rho) or not 0 <= rho <= 1:
        raise ValueError("Reserve must be finite and in [0, 1]")
    limit = (1 - rho) * t.battery_kwh
    if direct_energy(s, zone, model, 0.0) > limit + 1e-10:
        return 0.0
    if direct_energy(s, zone, model, t.max_kg) <= limit + 1e-10:
        return t.max_kg
    lo, hi = 0.0, t.max_kg
    for _ in range(46):
        mid = (lo + hi) / 2
        if direct_energy(s, zone, model, mid) <= limit:
            lo = mid
        else:
            hi = mid
    return lo


def safe_payload_status(s: Scenario, zone: str, model: str, reserve: float | None = None) -> dict:
    """Distinguish an infeasible empty round trip from a zero payload limit."""
    t = s.transport[model]
    rho = t.reserve if reserve is None else reserve
    payload = safe_payload(s, zone, model, rho)
    empty = direct_energy(s, zone, model, 0.0)
    feasible = empty <= (1 - rho) * t.battery_kwh + 1e-10
    return dict(empty_round_trip_feasible=feasible, max_payload_kg=payload if feasible else None,
                empty_energy_kwh=empty, reserve=rho,
                status="infeasible_empty_round_trip" if not feasible else
                       "nameplate_limited" if payload == t.max_kg else "energy_limited")


def enumerate_batches(s: Scenario, zone: str, reserve: float | None = None,
                      box_ids: set[str] | None = None, objective: str = "energy") -> tuple[list[Box], list[Batch | None]]:
    if objective not in ("energy", "time"):
        raise ValueError("objective must be energy or time")
    if reserve is not None and (not isfinite(reserve) or not 0 <= reserve <= 1):
        raise ValueError("Reserve must be finite and in [0, 1]")
    if box_ids is not None and not box_ids <= {b.id for b in s.zone_boxes[zone]}:
        raise ValueError("Unknown or wrong-zone cargo subset")
    boxes = sorted((b for b in s.zone_boxes[zone] if box_ids is None or b.id in box_ids), key=lambda b: b.id)
    n = len(boxes)
    total = 1 << n
    mass = [0.0] * total
    volume = [0.0] * total
    count = [0] * total
    for mask in range(1, total):
        bit = mask & -mask
        i = bit.bit_length() - 1
        rest = mask ^ bit
        mass[mask] = mass[rest] + boxes[i].kg
        volume[mask] = volume[rest] + boxes[i].volume
        count[mask] = count[rest] + 1
    out = s.node_leg("O01", zone)
    back = s.node_leg(zone, "O01")
    candidates: list[Batch | None] = [None] * total
    for mask in range(1, total):
        best: Batch | None = None
        for model, t in s.transport.items():
            if mass[mask] > t.max_kg + 1e-9 or volume[mask] > t.max_volume + 1e-9:
                continue
            energy = s.flight_energy(model, out, mass[mask]) + s.flight_energy(model, back, 0.0)
            rho = t.reserve if reserve is None else reserve
            if energy > (1 - rho) * t.battery_kwh + 1e-9:
                continue
            duration = (t.prepare + count[mask] * t.load_each +
                        s.flight_time(model, out) + s.flight_time(model, back) +
                        t.handoff + count[mask] * t.handoff_each)
            batch = Batch(model, tuple(boxes[i].id for i in range(n) if mask & (1 << i)),
                          mass[mask], volume[mask], duration, energy, 1 - energy / t.battery_kwh)
            key = lambda b: ((b.energy, b.duration, b.model) if objective == "energy"
                             else (b.duration, b.energy, b.model))
            if best is None or key(batch) < key(best):
                best = batch
        candidates[mask] = best
    return boxes, candidates


def solve_zone(s: Scenario, zone: str, reserve: float | None = None,
               box_ids: set[str] | None = None, objective: str = "energy") -> dict:
    boxes, candidates = enumerate_batches(s, zone, reserve, box_ids, objective)
    total = 1 << len(boxes)
    # Each chosen subset includes the least significant unsatisfied box. This
    # removes duplicate ordering of the same partition and remains exact.
    dp: list[tuple[int, float, float] | None] = [None] * total
    choice = [0] * total
    dp[0] = (0, 0.0, 0.0)
    for mask in range(1, total):
        anchor = mask & -mask
        sub = mask
        best = None
        best_sub = 0
        while sub:
            candidate = candidates[sub]
            if sub & anchor and candidate is not None:
                prior = dp[mask ^ sub]
                if prior is not None:
                    increments = ((candidate.energy, candidate.duration) if objective == "energy"
                                  else (candidate.duration, candidate.energy))
                    value = (prior[0] + 1, prior[1] + increments[0], prior[2] + increments[1])
                    if best is None or value < best:
                        best, best_sub = value, sub
            sub = (sub - 1) & mask
        dp[mask] = best
        choice[mask] = best_sub
    full = total - 1
    if dp[full] is None:
        raise ValueError(f"No all-box direct-service solution for {zone} at reserve {reserve}")
    result = []
    mask = full
    while mask:
        sub = choice[mask]
        batch = candidates[sub]
        assert batch is not None
        result.append(batch)
        mask ^= sub
    return dict(zone=zone, batches=[vars(b) for b in result],
                sorties=dp[full][0], energy_kwh=sum(b.energy for b in result),
                work_seconds=sum(b.duration for b in result))


def solve_all(s: Scenario, sensitivity: bool = True) -> dict:
    zones = sorted(s.zone_boxes)
    payloads = {z: {g: safe_payload(s, z, g) for g in s.transport} for z in zones}
    plans = {z: solve_zone(s, z) for z in zones}
    output = dict(max_safe_payload_kg=payloads, zone_plans=plans,
                  totals=dict(sorties=sum(x["sorties"] for x in plans.values()),
                              energy_kwh=sum(x["energy_kwh"] for x in plans.values()),
                              work_seconds=sum(x["work_seconds"] for x in plans.values())))
    if sensitivity:
        rows = {}
        for rho in (0.10, 0.15, 0.20, 0.25, 0.30):
            counts = {}
            for z in zones:
                counts[z] = solve_zone(s, z, rho)["sorties"]
            rows[f"{rho:.2f}"] = dict(sorties=sum(counts.values()), by_zone=counts,
                                     payload_kg={z: {g: safe_payload(s, z, g, rho) for g in s.transport}
                                                 for z in zones})
        output["reserve_sensitivity"] = rows
    return output

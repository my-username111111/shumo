"""Independent individual-box Pareto oracle (no count-pattern DP imports).

It uses independently recomputed physics, expands every actual box subset and
minimizes E/T without a sortie bound. Small-subset brute-force tests additionally
check this recurrence. It intentionally does not share the production pruner.
"""
from functools import lru_cache


def reference_prune(rows):
    # Sweep from fastest to slowest; retain strictly improving energy.
    best_by_time = {}
    for energy, seconds in rows:
        key = round(seconds, 6)
        old = best_by_time.get(key)
        if old is None or energy < old[0]:
            best_by_time[key] = (energy, seconds)
    best = float("inf")
    kept = []
    for key in sorted(best_by_time):
        energy, seconds = best_by_time[key]
        if energy < best - 1e-9:
            kept.append((energy, seconds))
            best = energy
    return tuple(sorted(kept))


def zone_frontier(s, replay, zone, reserve=None):
    boxes = sorted(s.zone_boxes[zone], key=lambda b: b.id)
    total = 1 << len(boxes)
    mass, volume, counts = [0.0] * total, [0.0] * total, [0] * total
    for mask in range(1, total):
        bit = mask & -mask
        b = boxes[bit.bit_length() - 1]
        mass[mask] = mass[mask ^ bit] + b.kg
        volume[mask] = volume[mask ^ bit] + b.volume
        counts[mask] = counts[mask ^ bit] + 1
    outward, inward = replay.leg("O01", zone), replay.leg(zone, "O01")
    @lru_cache(maxsize=None)
    def candidates(kg, m3, number):
        rows = []
        for t in s.transport.values():
            if kg > t.max_kg + 1e-9 or m3 > t.max_volume + 1e-9:
                continue
            rho = t.reserve if reserve is None else reserve
            loaded_range = t.empty_range + (t.full_range - t.empty_range) * (kg / t.max_kg) ** 1.5
            e = (t.battery_kwh * (outward["distance"] / loaded_range + inward["distance"] / t.empty_range) +
                 9.81 * ((t.empty_kg + kg) * outward["climb"] + t.empty_kg * inward["climb"]) /
                 (3600000 * t.climb_efficiency))
            if e > t.battery_kwh * (1 - rho) + 1e-9:
                continue
            seconds = (t.prepare + t.handoff + number * (t.load_each + t.handoff_each) +
                       (outward["climb"] + inward["climb"]) / t.climb_speed +
                       (outward["descend"] + inward["descend"]) / t.descend_speed +
                       (outward["distance"] + inward["distance"]) / t.speed)
            rows.append((e, seconds))
        return reference_prune(rows)
    options = [candidates(mass[m], round(volume[m], 12), counts[m]) if m else () for m in range(total)]
    values = [()] * total
    values[0] = ((0.0, 0.0),)
    for mask in range(1, total):
        anchor = mask & -mask
        rows = []
        subset = mask
        while subset:
            if subset & anchor and options[subset]:
                rows.extend((ae + be, at + bt) for ae, at in options[subset] for be, bt in values[mask ^ subset])
            subset = (subset - 1) & mask
        values[mask] = reference_prune(rows)
    return values[-1]


def global_reference(s, replay, progress=None):
    combined = ((0.0, 0.0),)
    local = {}
    for zone in sorted(s.zone_boxes):
        local[zone] = zone_frontier(s, replay, zone)
        combined = reference_prune([(ae + be, at + bt) for ae, at in combined for be, bt in local[zone]])
        if progress:
            progress(f"Independent subset oracle {zone}: {len(local[zone])} local points")
    return combined, local


def reserve_thresholds(s, replay, zone):
    """Widest-path subset DP: largest common reserve for EXACTLY k sorties.

    Recurrence is max over subsets of min(single-sortie SOC, previous SOC),
    independently verifying every sortie-count transition without a rho grid.
    """
    boxes = sorted(s.zone_boxes[zone], key=lambda b: b.id)
    size = 1 << len(boxes)
    mass, volume, number = [0.0] * size, [0.0] * size, [0] * size
    out, back = replay.leg("O01", zone), replay.leg(zone, "O01")
    @lru_cache(maxsize=None)
    def best_soc(kg, m3):
        values = [-1.0]
        for t in s.transport.values():
            if kg <= t.max_kg + 1e-9 and m3 <= t.max_volume + 1e-9:
                loaded = t.empty_range + (t.full_range - t.empty_range) * (kg / t.max_kg) ** 1.5
                fraction = (out["distance"] / loaded + back["distance"] / t.empty_range +
                            9.81 * ((t.empty_kg + kg) * out["climb"] + t.empty_kg * back["climb"]) /
                            (3600000 * t.climb_efficiency * t.battery_kwh))
                values.append(1 - fraction)
        return max(values)
    soc = [-1.0] * size
    for mask in range(1, size):
        bit = mask & -mask
        b = boxes[bit.bit_length() - 1]
        mass[mask] = mass[mask ^ bit] + b.kg
        volume[mask] = volume[mask ^ bit] + b.volume
        number[mask] = number[mask ^ bit] + 1
        soc[mask] = best_soc(mass[mask], round(volume[mask], 12))
    dp = [[] for _ in range(size)]
    dp[0] = [1.0]
    for mask in range(1, size):
        values = [-1.0] * (number[mask] + 1)
        anchor, sub = mask & -mask, mask
        while sub:
            if sub & anchor and soc[sub] >= 0:
                for n, previous in enumerate(dp[mask ^ sub]):
                    if previous >= 0:
                        candidate = min(previous, soc[sub])
                        if candidate > values[n + 1]:
                            values[n + 1] = candidate
            sub = (sub - 1) & mask
        dp[mask] = values
    return {n: v for n, v in enumerate(dp[-1]) if n > 0 and v >= 0}


def global_reserve_thresholds(s, replay, progress=None):
    current, local = {0: 1.0}, {}
    for z in sorted(s.zone_boxes):
        local[z] = reserve_thresholds(s, replay, z)
        values = {}
        for n, a in current.items():
            for k, b in local[z].items():
                values[n + k] = max(values.get(n + k, -1), min(a, b))
        current = values
        if progress:
            progress(f"Independent reserve oracle {z}: {len(local[z])} count thresholds")
    return current, local

"""Independent transport replay; intentionally does not call model evaluators.

Only input records and raster pixels are shared. Geometry uses segment/rectangle
clipping rather than the production grid-crossing traversal. No Q2/Q3/Q4 search
logic is imported or changed here.
"""
from collections import Counter
from functools import lru_cache
from math import atan2, cos, floor, isclose, isfinite, radians, sin, sqrt

import numpy as np


def close(actual, expected, label, atol=1e-6):
    if not (isfinite(actual) and isfinite(expected) and
            isclose(actual, expected, rel_tol=1e-10, abs_tol=atol)):
        raise AssertionError(f"{label}: {actual} != {expected}")


class TransportReplay:
    def __init__(self, scenario):
        self.s = scenario

    def cells(self, a, b):
        d = self.s.dem
        x0, y0 = (a[0] - d.x0) / d.dx, (d.y0 - a[1]) / d.dy
        x1, y1 = (b[0] - d.x0) / d.dx, (d.y0 - b[1]) / d.dy
        if not all(0 <= x < d.width and 0 <= y < d.nrows for x, y in ((x0, y0), (x1, y1))):
            raise ValueError("Independent replay: segment outside DEM")
        eps = 1e-9
        xx, yy = np.meshgrid(
            np.arange(max(0, floor(min(x0, x1) - eps)), min(d.width - 1, floor(max(x0, x1) + eps)) + 1),
            np.arange(max(0, floor(min(y0, y1) - eps)), min(d.nrows - 1, floor(max(y0, y1) + eps)) + 1))
        enter, leave = np.zeros(xx.shape), np.ones(xx.shape)
        valid = np.ones(xx.shape, dtype=bool)
        for start, delta, low in ((x0, x1 - x0, xx), (y0, y1 - y0, yy)):
            if delta == 0:
                valid &= (start >= low - eps) & (start <= low + 1 + eps)
            else:
                u, v = (low - eps - start) / delta, (low + 1 + eps - start) / delta
                enter = np.maximum(enter, np.minimum(u, v))
                leave = np.minimum(leave, np.maximum(u, v))
        valid &= enter <= leave
        return sorted(zip(xx[valid].tolist(), yy[valid].tolist()))

    @lru_cache(maxsize=None)
    def leg(self, origin, destination):
        a, b = self.s.nodes[origin], self.s.nodes[destination]
        cells = self.cells((a.lon, a.lat), (b.lon, b.lat))
        heights = [float(self.s.dem.values[y, x]) for x, y in cells]
        if not heights or any(not isfinite(z) or z == self.s.dem.nodata for z in heights):
            raise ValueError("Independent replay: missing terrain")
        h = sin(radians(b.lat - a.lat) / 2) ** 2 + cos(radians(a.lat)) * cos(radians(b.lat)) * sin(radians(b.lon - a.lon) / 2) ** 2
        distance = 12742000 * atan2(sqrt(max(0, h)), sqrt(max(0, 1 - h)))
        aa = a.ground + (0 if origin == "O01" else 30)
        bb = b.ground + (0 if destination == "O01" else 30)
        cruise = max(max(heights) + 50, aa, bb)
        return dict(distance=distance, climb=cruise - aa, descend=cruise - bb,
                    peak_ground=max(heights), cruise_alt=cruise, cells=cells)

    def evaluate(self, model, visits, reserve=None):
        s, t = self.s, self.s.transport[model]
        rho = t.reserve if reserve is None else reserve
        if not isfinite(rho) or not 0 <= rho <= 1:
            raise ValueError("Invalid reserve")
        ids = [bid for z, bids in visits for bid in bids]
        if not visits or not ids or len(ids) != len(set(ids)):
            raise AssertionError("Empty route or duplicate cargo")
        for z, bids in visits:
            if z == "O01" or not bids or any(b not in s.boxes or s.boxes[b].zone != z for b in bids):
                raise AssertionError("Cargo/zone mismatch")
        mass = sum(s.boxes[b].kg for b in ids)
        volume = sum(s.boxes[b].volume for b in ids)
        if mass > t.max_kg + 1e-9 or volume > t.max_volume + 1e-9:
            raise AssertionError("Independent replay: overloaded")
        clock = t.prepare + len(ids) * t.load_each
        remaining, energy, current = mass, 0.0, "O01"
        delivery, legs = {}, []
        for z, bids in [*visits, ("O01", [])]:
            leg = self.leg(current, z)
            effective_range = t.empty_range + (t.full_range - t.empty_range) * (max(0, remaining) / t.max_kg) ** 1.5
            horizontal = t.battery_kwh * leg["distance"] / effective_range
            climb = (t.empty_kg + remaining) * 9.81 * leg["climb"] / t.climb_efficiency / 3600000
            energy += horizontal + climb
            clock += leg["climb"] / t.climb_speed + leg["distance"] / t.speed + leg["descend"] / t.descend_speed
            legs.append(dict(origin=current, destination=z, payload_kg=remaining,
                             horizontal_kwh=horizontal, climb_kwh=climb,
                             **{k: v for k, v in leg.items() if k != "cells"}))
            if bids:
                clock += t.handoff + t.handoff_each * len(bids)
                delivery.update({b: clock for b in bids})
                remaining -= sum(s.boxes[b].kg for b in bids)
            current = z
        if energy > (1 - rho) * t.battery_kwh + 1e-8:
            raise AssertionError("Independent replay: reserve violation")
        return dict(energy_kwh=energy, duration=clock, return_soc=1 - energy / t.battery_kwh,
                    load_kg=mass, load_m3=volume, delivery=delivery, box_ids=ids, legs=legs)

    def check_q1(self, plans, reserve=None):
        if set(plans) != set(self.s.zone_boxes):
            raise AssertionError("Wrong service-zone coverage")
        seen, energy, seconds, count = [], 0.0, 0.0, 0
        for zone, plan in plans.items():
            pe, pt = 0.0, 0.0
            for batch in plan["batches"]:
                r = self.evaluate(batch["model"], [(zone, batch["box_ids"])], reserve)
                for actual, key in (("energy_kwh", "energy"), ("duration", "duration"),
                                    ("return_soc", "return_soc"), ("load_kg", "kg"), ("load_m3", "volume")):
                    close(r[actual], batch[key], f"Q1 {zone} {key}")
                seen.extend(r["box_ids"])
                pe += r["energy_kwh"]
                pt += r["duration"]
            close(pe, plan["energy_kwh"], "zone energy")
            close(pt, plan["work_seconds"], "zone work")
            if plan["sorties"] != len(plan["batches"]):
                raise AssertionError("Incorrect sortie count")
            energy += pe
            seconds += pt
            count += len(plan["batches"])
        if Counter(seen) != Counter({b: 1 for b in self.s.boxes}):
            raise AssertionError("Every cargo must be delivered exactly once")
        return dict(boxes=len(seen), sorties=count, energy_kwh=energy, work_seconds=seconds)

    def geometry_audit(self):
        rows = []
        for zone in sorted(self.s.zone_boxes):
            for a, b in (("O01", zone), (zone, "O01")):
                leg = self.leg(a, b)
                production = self.s.node_leg(a, b)
                p, q = self.s.nodes[a], self.s.nodes[b]
                if leg["cells"] != sorted(self.s.dem.crossed_pixels((p.lon, p.lat), (q.lon, q.lat))):
                    raise AssertionError(f"Different terrain cell sets: {a}->{b}")
                for key in ("distance", "climb", "descend", "peak_ground", "cruise_alt"):
                    close(leg[key], getattr(production, key), key)
                rows.append(dict(origin=a, destination=b, cells=len(leg["cells"]),
                                 **{k: v for k, v in leg.items() if k != "cells"}))
        return rows

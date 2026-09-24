"""Q3 fixed-route relay optimization with conservative continuous certificates.

The Q2 transport plan is kept unchanged.  Every transport phase is split into
time intervals.  A link is accepted for a whole interval only when a lower
bound on its margin is non-negative.  For a moving endpoint, the horizontal
line-of-sight family is the triangle swept by the fixed endpoint and the two
end positions; every intersected DEM cell is checked against the affine ray
altitude over that triangle.  This is conservative for the project's
piecewise-constant DEM convention and cannot miss a cell between samples.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil, floor, log10, sqrt
from pathlib import Path
from typing import Iterable

import hashlib
import json
import pickle

from model import Scenario, distance_m, phase_position


EPS = 1e-9


@dataclass(frozen=True)
class Site:
    id: str
    lon: float
    lat: float
    agl: float
    altitude: float
    gateway_margin: float
    lead: float
    back_time: float
    travel_energy_kwh: float
    max_service_seconds: float

    @property
    def point(self) -> tuple[float, float, float]:
        return self.lon, self.lat, self.altitude


@dataclass(frozen=True)
class Demand:
    id: int
    sortie: str
    phase: str
    phase_index: int
    start: float
    end: float
    p0: tuple[float, float, float]
    p1: tuple[float, float, float]
    direct_margin: float


def _clip(poly: list[tuple[float, float]], axis: int, bound: float,
          keep_greater: bool) -> list[tuple[float, float]]:
    if not poly:
        return []
    out: list[tuple[float, float]] = []
    for a, b in zip(poly, [*poly[1:], poly[0]]):
        va, vb = a[axis], b[axis]
        ina = va >= bound - EPS if keep_greater else va <= bound + EPS
        inb = vb >= bound - EPS if keep_greater else vb <= bound + EPS
        if ina:
            out.append(a)
        if ina != inb:
            den = vb - va
            u = 0.0 if abs(den) <= EPS else (bound - va) / den
            out.append((a[0] + u * (b[0] - a[0]), a[1] + u * (b[1] - a[1])))
    return out


def _cell_intersection(triangle: list[tuple[float, float]], ix: int, iy: int) -> list[tuple[float, float]]:
    poly = triangle
    poly = _clip(poly, 0, ix, True)
    poly = _clip(poly, 0, ix + 1, False)
    poly = _clip(poly, 1, iy, True)
    return _clip(poly, 1, iy + 1, False)


def _segment_cell_interval(x0: float, y0: float, x1: float, y1: float,
                           ix: int, iy: int) -> tuple[float, float] | None:
    """Liang-Barsky intersection parameter interval for a closed pixel."""
    lo, hi = 0.0, 1.0
    for p, q in ((-(x1 - x0), x0 - ix), (x1 - x0, ix + 1 - x0),
                 (-(y1 - y0), y0 - iy), (y1 - y0, iy + 1 - y0)):
        if abs(p) <= EPS:
            if q < -EPS:
                return None
            continue
        r = q / p
        if p < 0:
            lo = max(lo, r)
        else:
            hi = min(hi, r)
        if lo > hi + EPS:
            return None
    return max(0.0, lo), min(1.0, hi)


def static_clear(s: Scenario, a: tuple[float, float, float],
                 b: tuple[float, float, float]) -> bool:
    """Exact cell-intersection check under a piecewise-constant DEM."""
    x0, y0 = s.dem.pixel(a[0], a[1])
    x1, y1 = s.dem.pixel(b[0], b[1])
    try:
        cells = s.dem.crossed_pixels(a[:2], b[:2])
    except ValueError:
        return False
    for ix, iy in cells:
        z = float(s.dem.values[iy, ix])
        if z == s.dem.nodata or not (-1e30 < z < 1e30):
            return False
        interval = _segment_cell_interval(x0, y0, x1, y1, ix, iy)
        if interval is None:
            continue
        ray_lo = min(a[2] + interval[0] * (b[2] - a[2]),
                     a[2] + interval[1] * (b[2] - a[2]))
        if z >= ray_lo - 1e-7:
            return False
    return True


def swept_clear(s: Scenario, fixed: tuple[float, float, float],
                p0: tuple[float, float, float], p1: tuple[float, float, float]) -> bool:
    """Prove every ray from ``fixed`` to the moving endpoint is terrain-clear."""
    qx, qy = s.dem.pixel(fixed[0], fixed[1])
    x0, y0 = s.dem.pixel(p0[0], p0[1])
    x1, y1 = s.dem.pixel(p1[0], p1[1])
    if not all(-1e12 < v < 1e12 for v in (qx, qy, x0, y0, x1, y1)):
        return False
    area2 = (x0 - qx) * (y1 - qy) - (y0 - qy) * (x1 - qx)
    if abs(area2) <= 1e-10:
        # Vertical motion has a fixed ground ray; the lower endpoint is the
        # worst altitude everywhere.  Other collinear sweeps are conservatively
        # left obstructed unless the obstruction penalty itself is affordable.
        if abs(x0 - x1) <= 1e-10 and abs(y0 - y1) <= 1e-10:
            low = p0 if p0[2] <= p1[2] else p1
            return static_clear(s, fixed, low)
        return False

    # z = alpha*x + beta*y + gamma is the ray altitude over the swept triangle.
    det = area2
    alpha = ((p0[2] - fixed[2]) * (y1 - qy) -
             (p1[2] - fixed[2]) * (y0 - qy)) / det
    beta = ((x0 - qx) * (p1[2] - fixed[2]) -
            (x1 - qx) * (p0[2] - fixed[2])) / det
    gamma = fixed[2] - alpha * qx - beta * qy
    triangle = [(qx, qy), (x0, y0), (x1, y1)]
    ymin, ymax = floor(min(v[1] for v in triangle)), floor(max(v[1] for v in triangle))
    xmin_all, xmax_all = floor(min(v[0] for v in triangle)), floor(max(v[0] for v in triangle))
    if xmin_all < 0 or ymin < 0 or xmax_all >= s.dem.width or ymax >= s.dem.nrows:
        return False
    for iy in range(ymin, ymax + 1):
        # Intersect the triangle with this raster row first.  A flight segment
        # often sweeps a very thin diagonal triangle; scanning its full axis-
        # aligned bounding box is orders of magnitude more expensive.
        row_poly = _clip(triangle, 1, iy, True)
        row_poly = _clip(row_poly, 1, iy + 1, False)
        if not row_poly:
            continue
        xmin = max(0, floor(min(v[0] for v in row_poly)))
        xmax = min(s.dem.width - 1, floor(max(v[0] for v in row_poly)))
        for ix in range(xmin, xmax + 1):
            z = float(s.dem.values[iy, ix])
            if z == s.dem.nodata or not (-1e30 < z < 1e30):
                return False
            poly = _cell_intersection(triangle, ix, iy)
            if not poly:
                continue
            ray_lo = min(alpha * x + beta * y + gamma for x, y in poly)
            if z >= ray_lo - 1e-7:
                return False
    return True


def interval_margin(s: Scenario, fixed: tuple[float, float, float],
                    p0: tuple[float, float, float], p1: tuple[float, float, float],
                    first: str, second: str, extra_loss_db: float = 0.0) -> tuple[float, str]:
    """A valid lower bound for link margin throughout one motion interval."""
    def d3(p: tuple[float, float, float]) -> float:
        horizontal = distance_m(fixed[:2], p[:2])
        return sqrt(horizontal * horizontal + (fixed[2] - p[2]) ** 2)
    km = max(0.001, max(d3(p0), d3(p1)) / 1000.0)
    fspl = 32.45 + 20 * log10(s.frequency_mhz) + 20 * log10(km)
    budget = s.link_budget(first, second)
    obstructed = budget - fspl - s.obstruction_db - extra_loss_db
    if obstructed >= 0:
        return obstructed, "obstruction_bound"
    if swept_clear(s, fixed, p0, p1):
        return budget - fspl - extra_loss_db, "clear_swept_triangle"
    return obstructed, "obstruction_bound"


def _relay_site(s: Scenario, lon: float, lat: float, agl: float,
                gateway: tuple[float, float, float]) -> Site | None:
    if not s.dem.inside(lon, lat):
        return None
    try:
        altitude = s.dem.height(lon, lat) + agl
        point = (lon, lat, altitude)
        margin, _ = interval_margin(s, gateway, point, point, "relay_backhaul", "gateway")
        if margin < 0:
            return None
        zero = s.relay_sortie(lon, lat, agl, 0.0)
    except ValueError:
        return None
    power = s.relay.hover_kw + s.relay.comm_kw
    usable = s.relay.battery_kwh * (1 - s.relay.reserve)
    max_service = 3600 * max(0.0, usable - zero["energy_kwh"]) / power
    if max_service <= 0:
        return None
    return Site("", lon, lat, agl, altitude, margin,
                zero["prepare"] + zero["out_time"] + zero["establish"],
                zero["back_time"], zero["energy_kwh"], max_service)


def candidate_sites(s: Scenario, q2: dict, coarse_deg: float = 0.004,
                    local_deg: float = 0.0015) -> list[Site]:
    """Full legal-region coarse screen plus route-centred local refinement."""
    home = s.nodes["O01"]
    gateway = (home.lon, home.lat, home.ground + s.gateway_agl)
    coords: set[tuple[float, float]] = set()
    local_coords: set[tuple[float, float]] = set()
    # The whole DEM is screened at the coarse resolution.
    lon_min = s.dem.x0
    lon_max = s.dem.x0 + s.dem.width * s.dem.dx
    lat_max = s.dem.y0
    lat_min = s.dem.y0 - s.dem.nrows * s.dem.dy
    lon = ceil(lon_min / coarse_deg) * coarse_deg
    while lon < lon_max:
        lat = ceil(lat_min / coarse_deg) * coarse_deg
        while lat < lat_max:
            coords.add((round(lon, 8), round(lat, 8)))
            lat += coarse_deg
        lon += coarse_deg
    # Add nodes and a small metre-scale-like stencil around route positions.
    anchors: list[tuple[float, float]] = [(n.lon, n.lat) for n in s.nodes.values()]
    for row in q2["sorties"]:
        for ph in row["route"]["phases"]:
            duration = ph["t1"] - ph["t0"]
            count = max(1, ceil(duration / 300.0))
            anchors.extend(phase_position(ph, ph["t0"] + duration * k / count)[:2]
                           for k in range(count + 1))
    for x, y in anchors:
        for dx in (-local_deg, 0.0, local_deg):
            for dy in (-local_deg, 0.0, local_deg):
                point = (round(x + dx, 8), round(y + dy, 8))
                coords.add(point)
                local_coords.add(point)
    sites: list[Site] = []
    seen: set[tuple[int, int, int]] = set()
    for lon, lat in sorted(coords):
        if not s.dem.inside(lon, lat):
            continue
        # Even an unobstructed backhaul cannot exceed this radius.  Applying
        # the analytical FSPL bound before any raster intersection removes
        # most of the full-DEM coarse grid without changing the feasible set.
        clear_km = 10 ** ((s.link_budget("relay_backhaul", "gateway") - 32.45 -
                           20 * log10(s.frequency_mhz)) / 20)
        if distance_m((lon, lat), gateway[:2]) > clear_km * 1000:
            continue
        px, py = s.dem.pixel(lon, lat)
        # Stage 1 uses the most permissive height to discover the horizontal
        # cover structure.  Selected positions are vertically refined below.
        for agl in (300.0,):
            key = (floor(px), floor(py), int(agl))
            if key in seen:
                continue
            seen.add(key)
            site = _relay_site(s, lon, lat, agl, gateway)
            if site is not None:
                sites.append(site)
    sites.sort(key=lambda z: (z.lead, -z.gateway_margin, z.lon, z.lat, z.agl))
    return [Site(f"P{i:04d}", **{k: v for k, v in asdict(site).items() if k != "id"})
            for i, site in enumerate(sites, 1)]


def build_demands(s: Scenario, q2: dict, max_interval: float = 12.0,
                  extra_loss_db: float = 0.0) -> tuple[list[Demand], list[dict]]:
    home = s.nodes["O01"]
    gateway = (home.lon, home.lat, home.ground + s.gateway_agl)
    demands: list[Demand] = []
    direct_rows: list[dict] = []
    for row in q2["sorties"]:
        for phase_index, ph in enumerate(row["route"]["phases"]):
            stack = [(float(ph["t0"]), float(ph["t1"]))]
            while stack:
                a, b = stack.pop()
                p0, p1 = phase_position(ph, a), phase_position(ph, b)
                margin, proof = interval_margin(s, gateway, p0, p1,
                                                "transport", "gateway", extra_loss_db)
                start, end = row["start"] + a, row["start"] + b
                if margin >= 0:
                    direct_rows.append(dict(sortie=row["id"], phase=ph["kind"],
                                            phase_index=phase_index, start=start, end=end,
                                            mode="direct", margin_lower_db=margin, proof=proof))
                elif b - a <= max_interval + 1e-8:
                    demands.append(Demand(len(demands), row["id"], ph["kind"], phase_index,
                                          start, end, p0, p1, margin))
                else:
                    mid = (a + b) / 2
                    stack.extend([(mid, b), (a, mid)])
    demands.sort(key=lambda d: (d.start, d.end, d.sortie, d.phase_index))
    demands = [Demand(i, **{k: v for k, v in asdict(d).items() if k != "id"})
               for i, d in enumerate(demands)]
    return demands, direct_rows


def coverage_matrix(s: Scenario, sites: list[Site], demands: list[Demand],
                    extra_loss_db: float = 0.0) -> tuple[list[set[int]], dict[tuple[int, int], float]]:
    cover: list[set[int]] = [set() for _ in sites]
    margins: dict[tuple[int, int], float] = {}
    for i, site in enumerate(sites):
        # A relay cannot be established before this demand begins.
        for d in demands:
            if site.lead > d.start + 1e-7:
                continue
            # Fast distance rejection before the DEM triangle check.
            if max(distance_m(site.point[:2], d.p0[:2]), distance_m(site.point[:2], d.p1[:2])) > 6500:
                continue
            margin, _ = interval_margin(s, site.point, d.p0, d.p1,
                                        "relay_access", "transport", extra_loss_db)
            end_to_end = min(site.gateway_margin - extra_loss_db, margin)
            if end_to_end >= 0:
                cover[i].add(d.id)
                margins[(i, d.id)] = end_to_end
    return cover, margins


def _choices(active: list[int], cover: list[set[int]], sites: list[Site],
             limit: int = 60) -> list[tuple[int, ...]]:
    target = set(active)
    useful = [i for i, c in enumerate(cover) if c & target]
    singles = [(i,) for i in useful if target <= cover[i]]
    if singles:
        singles.sort(key=lambda x: (sites[x[0]].travel_energy_kwh, sites[x[0]].lead,
                                    -sites[x[0]].gateway_margin))
        return singles[:limit]
    # Keep only a few best representatives for each coverage signature.
    representatives: dict[frozenset[int], list[int]] = {}
    for i in useful:
        signature = frozenset(cover[i] & target)
        representatives.setdefault(signature, []).append(i)
    reduced: list[int] = []
    for ids in representatives.values():
        ids.sort(key=lambda i: (sites[i].travel_energy_kwh, sites[i].lead,
                                -sites[i].gateway_margin))
        reduced.extend(ids[:3])
    pairs: list[tuple[tuple, tuple[int, int]]] = []
    for pos, i in enumerate(reduced):
        missing = target - cover[i]
        if not missing:
            continue
        for j in reduced[pos + 1:]:
            if missing <= cover[j]:
                pair = tuple(sorted((i, j)))
                score = (sites[i].travel_energy_kwh + sites[j].travel_energy_kwh,
                         max(sites[i].lead, sites[j].lead),
                         -(len(cover[i] & target) + len(cover[j] & target)))
                pairs.append((score, pair))
    pairs.sort(key=lambda x: x[0])
    output: list[tuple[int, ...]] = []
    seen = set()
    for _, pair in pairs:
        if pair not in seen:
            seen.add(pair)
            output.append(pair)
        if len(output) >= limit:
            break
    return output


def _atomic_windows(demands: list[Demand]) -> list[tuple[float, float, list[int]]]:
    events = sorted({t for d in demands for t in (d.start, d.end)})
    windows = []
    for a, b in zip(events, events[1:]):
        if b <= a + EPS:
            continue
        mid = (a + b) / 2
        active = [d.id for d in demands if d.start < mid < d.end or
                  abs(mid - d.start) <= EPS or abs(mid - d.end) <= EPS]
        if active:
            windows.append((a, b, active))
    return windows


def choose_site_timeline(demands: list[Demand], cover: list[set[int]],
                         sites: list[Site], beam: int = 180) -> tuple[list[tuple], dict]:
    windows = _atomic_windows(demands)
    if not windows:
        return [], {"atomic_windows": 0, "search_states": 0}
    layers: list[dict[tuple[int, ...], tuple[float, tuple[int, ...] | None]]] = []
    previous: dict[tuple[int, ...], tuple[float, tuple[int, ...] | None]] = {(): (0.0, None)}
    for k, (start, end, active) in enumerate(windows):
        choices = _choices(active, cover, sites)
        if not choices:
            raise ValueError(f"No one/two-relay cover for communication window {start:.3f}-{end:.3f}")
        current: dict[tuple[int, ...], tuple[float, tuple[int, ...] | None]] = {}
        duration = end - start
        for choice in choices:
            best = None
            for prev, (cost, _) in previous.items():
                new = set(choice) - set(prev)
                retained = set(choice) & set(prev)
                gap = 0.0 if k == 0 else max(0.0, start - windows[k - 1][1])
                added = 100000.0 * len(new)
                added += sum(600.0 * sites[i].travel_energy_kwh for i in new)
                # Small preference for retaining a site and for larger margins.
                added += 0.01 * duration * len(choice) + 0.005 * gap * len(retained)
                score = cost + added
                if best is None or score < best[0]:
                    best = (score, prev)
            current[choice] = best  # type: ignore[assignment]
        keep = sorted(current.items(), key=lambda x: x[1][0])[:beam]
        current = dict(keep)
        layers.append(current)
        previous = current
    state = min(previous, key=lambda x: previous[x][0])
    path = [state]
    for layer in reversed(layers):
        prev = layer[state][1]
        if prev is None:
            break
        path.append(prev)
        state = prev
    path = list(reversed(path))[-len(windows):]
    return [(a, b, tuple(choice), active)
            for (a, b, active), choice in zip(windows, path)], {
                "atomic_windows": len(windows),
                "search_states": sum(len(x) for x in layers),
                "beam_width": beam,
            }


def _runs(timeline: list[tuple], sites: list[Site], merge_gap: float = 180.0) -> list[dict]:
    occurrences: dict[int, list[tuple[float, float, list[int]]]] = {}
    for a, b, choice, active in timeline:
        for site in choice:
            occurrences.setdefault(site, []).append((a, b, active))
    runs = []
    for site_id, rows in occurrences.items():
        current = None
        for a, b, active in rows:
            if current is None or a - current["end"] > merge_gap:
                if current is not None:
                    runs.append(current)
                current = dict(site_index=site_id, start=a, end=b, demand_ids=set(active))
            else:
                current["end"] = b
                current["demand_ids"].update(active)
        if current is not None:
            runs.append(current)
    return sorted(runs, key=lambda r: (r["start"], r["end"], r["site_index"]))


def refine_selected_heights(s: Scenario, timeline: list[tuple], sites: list[Site],
                            demands: list[Demand]) -> list[Site]:
    """Test 100/200/300 m at each selected horizontal position exactly."""
    home = s.nodes["O01"]
    gateway = (home.lon, home.lat, home.ground + s.gateway_agl)
    assigned: dict[int, set[int]] = {}
    span: dict[int, tuple[float, float]] = {}
    for a, b, choice, active in timeline:
        for i in choice:
            assigned.setdefault(i, set()).update(active)
            old = span.get(i)
            span[i] = (a, b) if old is None else (min(old[0], a), max(old[1], b))
    refined = list(sites)
    for i, demand_ids in assigned.items():
        base = sites[i]
        options = []
        for agl in (100.0, 200.0, 300.0):
            candidate = _relay_site(s, base.lon, base.lat, agl, gateway)
            if candidate is None or candidate.lead > span[i][0] + 1e-7:
                continue
            if span[i][1] - span[i][0] > candidate.max_service_seconds + 1e-7:
                continue
            margins = [interval_margin(s, candidate.point, demands[d].p0, demands[d].p1,
                                       "relay_access", "transport")[0]
                       for d in demand_ids]
            if margins and min(margins) >= 0:
                service_energy = (s.relay.hover_kw + s.relay.comm_kw) * (
                    span[i][1] - span[i][0]) / 3600
                options.append((candidate.travel_energy_kwh + service_energy,
                                -min(candidate.gateway_margin, min(margins)), candidate))
        if options:
            chosen = min(options, key=lambda x: (x[0], x[1]))[2]
            refined[i] = Site(base.id, **{k: v for k, v in asdict(chosen).items() if k != "id"})
    return refined


def build_relay_sorties(s: Scenario, timeline: list[tuple], sites: list[Site],
                        demands: list[Demand]) -> list[dict]:
    runs = _runs(timeline, sites)
    relay_ready = {x: 0.0 for x in s.relays}
    component_ready = {f"RE{i:02d}": 0.0 for i in range(1, s.relay_energy_count + 1)}
    prepared = []
    for run in runs:
        site = sites[run["site_index"]]
        service = run["end"] - run["start"]
        if service > site.max_service_seconds + 1e-7:
            raise ValueError(f"Relay service span exceeds battery at {site.id}")
        trip = s.relay_sortie(site.lon, site.lat, site.agl, service)
        launch = run["start"] - site.lead
        if launch < -1e-7:
            raise ValueError(f"Relay {site.id} cannot establish before first demand")
        prepared.append((launch, run, site, trip))
    # Different sites have different travel/setup leads.  Allocate physical
    # resources in actual launch order, not service-start order.
    prepared.sort(key=lambda x: (x[0], x[1]["start"], x[2].id))
    output = []
    for launch, run, site, trip in prepared:
        relay = min(relay_ready, key=lambda x: relay_ready[x])
        component = min(component_ready, key=lambda x: component_ready[x])
        if max(relay_ready[relay], component_ready[component]) > launch + 1e-7:
            raise ValueError(
                f"Selected transitions exceed capacity at {site.id}: launch={launch:.3f}, "
                f"relay_ready={relay_ready[relay]:.3f}, component_ready={component_ready[component]:.3f}")
        established = launch + site.lead
        finish = launch + trip["duration"]
        relay_ready[relay] = finish + s.relay.turnaround
        component_ready[component] = finish + s.charge_time(trip["return_soc"], s.relay_charge)
        covered = sorted(run["demand_ids"])
        output.append(dict(id=f"R{len(output)+1:03d}", relay=relay,
                           energy_component=component, site_id=site.id,
                           lon=site.lon, lat=site.lat, agl=site.agl,
                           altitude=site.altitude, start=launch, established=established,
                           service_end=run["end"], return_time=finish,
                           relay_ready=relay_ready[relay], component_ready=component_ready[component],
                           energy_kwh=trip["energy_kwh"], return_soc=trip["return_soc"],
                           gateway_margin_lower_db=site.gateway_margin,
                           demand_ids=covered,
                           covered_transport=sorted({demands[i].sortie for i in covered})))
    return output


def optimize_relay_jobs(s: Scenario, sites: list[Site], demands: list[Demand],
                        cover: list[set[int]]) -> tuple[list[dict], dict]:
    """Finite-pool set cover with exact fixed-interval relay-capacity cuts."""
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix, vstack

    # Sites with the same complete coverage signature are interchangeable for
    # feasibility. Keep the three cheapest/fastest to retain scheduling choice.
    by_signature: dict[frozenset[int], list[int]] = {}
    for i, ids in enumerate(cover):
        if ids:
            by_signature.setdefault(frozenset(ids), []).append(i)
    kept_sites = []
    for ids in by_signature.values():
        ids.sort(key=lambda i: (sites[i].travel_energy_kwh, sites[i].lead,
                                -sites[i].gateway_margin))
        kept_sites.extend(ids[:3])

    jobs: list[dict] = []
    seen: set[tuple[int, tuple[int, ...]]] = set()
    gap_limits = (0.0, 60.0, 180.0, 600.0, 1e30)

    def add_job(site_index: int, seed_ids: Iterable[int]) -> None:
        seed = sorted(set(seed_ids), key=lambda d: (demands[d].start, demands[d].end))
        if not seed:
            return
        start = min(demands[d].start for d in seed)
        end = max(demands[d].end for d in seed)
        # Once hovering, the mission covers every compatible demand in its
        # service span, including demands omitted by the grouping heuristic.
        ids = tuple(sorted(d for d in cover[site_index]
                           if demands[d].start >= start - EPS and demands[d].end <= end + EPS))
        key = (site_index, ids)
        if not ids or key in seen:
            return
        seen.add(key)
        site = sites[site_index]
        start = min(demands[d].start for d in ids)
        end = max(demands[d].end for d in ids)
        if site.lead > start + 1e-7 or end - start > site.max_service_seconds + 1e-7:
            return
        try:
            trip = s.relay_sortie(site.lon, site.lat, site.agl, end - start)
        except ValueError:
            return
        launch = start - site.lead
        finish = launch + trip["duration"]
        jobs.append(dict(site_index=site_index, demand_ids=ids, service_start=start,
                         service_end=end, launch=launch, return_time=finish,
                         relay_release=finish + s.relay.turnaround,
                         energy_kwh=trip["energy_kwh"], return_soc=trip["return_soc"]))

    for i in kept_sites:
        ordered = sorted(cover[i], key=lambda d: (demands[d].start, demands[d].end))
        for gap_limit in gap_limits:
            group: list[int] = []
            group_end = None
            for d in ordered:
                if group and demands[d].start - float(group_end) > gap_limit:
                    add_job(i, group)
                    group = []
                    group_end = None
                group.append(d)
                group_end = max(float(group_end) if group_end is not None else -1e30,
                                demands[d].end)
            add_job(i, group)
        # Overlapping calendar buckets provide shorter alternatives even where
        # the same site has sparse compatibility over the full horizon.
        for width in (120.0, 240.0, 360.0, 600.0, 1200.0, 2400.0):
            horizon = max(d.end for d in demands)
            for offset in (0.0, width / 2):
                left = offset
                while left <= horizon:
                    add_job(i, [d for d in ordered
                                if left <= (demands[d].start + demands[d].end) / 2 < left + width])
                    left += width

    if not jobs:
        raise ValueError("No energy-feasible relay jobs were generated")
    print(json.dumps({"stage": "relay_job_pool", "site_signatures": len(by_signature),
                      "kept_sites": len(kept_sites), "jobs": len(jobs)}), flush=True)
    n = len(jobs)
    row, col, data = [], [], []
    for d in demands:
        indices = [j for j, job in enumerate(jobs) if d.id in job["demand_ids"]]
        if not indices:
            raise ValueError(f"Demand {d.id} has no generated relay job")
        for j in indices:
            row.append(d.id); col.append(j); data.append(-1.0)
    matrix = coo_matrix((data, (row, col)), shape=(len(demands), n)).tocsr()
    lower = np.full(len(demands), -np.inf)
    upper = np.full(len(demands), -1.0)
    objective = np.array([100000.0 + 100.0 * j["energy_kwh"] + 0.001 * j["return_time"]
                          for j in jobs])
    cuts = 0
    result = None
    last_conflict = None
    for _ in range(80):
        result = milp(objective, integrality=np.ones(n), bounds=Bounds(0, 1),
                      constraints=LinearConstraint(matrix, lower, upper),
                      options={"time_limit": 120.0, "mip_rel_gap": 0.0})
        if result.x is None:
            raise ValueError(f"Relay job MILP has no solution after {cuts} capacity cuts: "
                             f"status={result.status}, conflict={last_conflict}, {result.message}")
        selected = [i for i, x in enumerate(result.x) if x > 0.5]
        events = sorted({t for i in selected for t in
                         (jobs[i]["launch"], jobs[i]["relay_release"])})
        violation = None
        for a, b in zip(events, events[1:]):
            mid = (a + b) / 2
            active = [j for j, job in enumerate(jobs)
                      if job["launch"] < mid < job["relay_release"]]
            if sum(1 for j in selected if j in set(active)) > len(s.relays):
                violation = active
                last_conflict = dict(time=mid,
                                     selected_active=[{
                                         "job": j,
                                         "site": sites[jobs[j]["site_index"]].id,
                                         "launch": jobs[j]["launch"],
                                         "service_start": jobs[j]["service_start"],
                                         "service_end": jobs[j]["service_end"],
                                         "return_time": jobs[j]["return_time"],
                                         "sorties": sorted({demands[d].sortie for d in jobs[j]["demand_ids"]}),
                                     } for j in selected if j in set(active)],
                                     selected_active_count=sum(1 for j in selected if j in set(active)),
                                     transport_active=sorted({d.sortie for d in demands
                                                              if d.start < mid < d.end}))
                break
        if violation is None:
            break
        cut = coo_matrix(([1.0] * len(violation), ([0] * len(violation), violation)),
                         shape=(1, n)).tocsr()
        matrix = vstack([matrix, cut], format="csr")
        lower = np.append(lower, -np.inf)
        upper = np.append(upper, float(len(s.relays)))
        cuts += 1
    else:
        raise ValueError("Relay-capacity cut loop did not converge")

    selected_jobs = [jobs[i] for i, x in enumerate(result.x) if x > 0.5]
    selected_jobs.sort(key=lambda x: (x["launch"], x["service_start"], x["site_index"]))
    relay_ready = {x: 0.0 for x in s.relays}
    component_ready = {f"RE{i:02d}": 0.0 for i in range(1, s.relay_energy_count + 1)}
    output = []
    for job in selected_jobs:
        site = sites[job["site_index"]]
        relay = min(relay_ready, key=relay_ready.get)
        component = min(component_ready, key=component_ready.get)
        if relay_ready[relay] > job["launch"] + 1e-7:
            raise ValueError("MILP relay-capacity replay failed")
        # Six components are assigned independently from the two aircraft.
        if component_ready[component] > job["launch"] + 1e-7:
            raise ValueError("Relay energy-component replay failed")
        relay_ready[relay] = job["relay_release"]
        component_release = job["return_time"] + s.charge_time(job["return_soc"], s.relay_charge)
        component_ready[component] = component_release
        ids = list(job["demand_ids"])
        output.append(dict(id=f"R{len(output)+1:03d}", relay=relay,
                           energy_component=component, site_id=site.id,
                           lon=site.lon, lat=site.lat, agl=site.agl, altitude=site.altitude,
                           start=job["launch"], established=job["service_start"],
                           service_end=job["service_end"], return_time=job["return_time"],
                           relay_ready=relay_ready[relay], component_ready=component_release,
                           energy_kwh=job["energy_kwh"], return_soc=job["return_soc"],
                           gateway_margin_lower_db=site.gateway_margin,
                           demand_ids=ids,
                           covered_transport=sorted({demands[d].sortie for d in ids})))
    return output, {"relay_job_count": n, "capacity_cuts": cuts,
                    "milp_status": int(result.status), "milp_message": result.message,
                    "selected_relay_jobs": len(output)}


def verify(s: Scenario, q2: dict, demands: list[Demand], direct_rows: list[dict],
           sites: list[Site], relays: list[dict], extra_loss_db: float = 0.0) -> tuple[list[dict], list[dict]]:
    intervals = list(direct_rows)
    failures = []
    by_id = {d.id: d for d in demands}
    for d in demands:
        eligible = []
        for relay in relays:
            if relay["established"] <= d.start + 1e-7 and relay["service_end"] >= d.end - 1e-7:
                site = sites[int(relay["site_id"][1:]) - 1]
                margin, proof = interval_margin(s, site.point, d.p0, d.p1,
                                                "relay_access", "transport", extra_loss_db)
                e2e = min(site.gateway_margin - extra_loss_db, margin)
                if e2e >= 0:
                    eligible.append((e2e, relay["id"], proof))
        if not eligible:
            failures.append(dict(**asdict(d), reason="no_certified_end_to_end_path"))
            intervals.append(dict(sortie=d.sortie, phase=d.phase, phase_index=d.phase_index,
                                  start=d.start, end=d.end, mode="outage",
                                  margin_lower_db=d.direct_margin, proof="none"))
        else:
            margin, relay_id, proof = max(eligible)
            intervals.append(dict(sortie=d.sortie, phase=d.phase, phase_index=d.phase_index,
                                  start=d.start, end=d.end, mode="relay", relay_sortie=relay_id,
                                  margin_lower_db=margin, proof=proof))
    intervals.sort(key=lambda x: (x["sortie"], x["start"], x["end"]))
    return intervals, failures


def _coupled_zones(s: Scenario, q2: dict, relays: list[dict], demands: list[Demand]) -> list[list[str]]:
    parent = {z: z for z in s.zone_boxes}
    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    row_by_id = {r["id"]: r for r in q2["sorties"]}
    for row in q2["sorties"]:
        zones = [v[0] for v in row["visits"]]
        for z in zones[1:]:
            union(zones[0], z)
    for relay in relays:
        zones = sorted({z for did in relay["demand_ids"]
                        for z, _ in row_by_id[demands[did].sortie]["visits"]})
        for z in zones[1:]:
            union(zones[0], z)
    groups: dict[str, list[str]] = {}
    for z in parent:
        groups.setdefault(find(z), []).append(z)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: (g[0], len(g)))


def solve(s: Scenario, q2: dict, label: str = "q2_seed",
          coarse_deg: float = 0.004, local_deg: float = 0.0015,
          max_interval: float = 30.0) -> dict:
    cache_dir = Path(__file__).resolve().parent / ".q3_cache"
    cache_dir.mkdir(exist_ok=True)
    fingerprint = ("geometry_v2_exact_cell_triangle", label, coarse_deg, local_deg, max_interval,
                   tuple((r["id"], round(float(r["start"]), 6), round(float(r["return_time"]), 6))
                         for r in q2["sorties"]))
    cache_name = hashlib.sha256(repr(fingerprint).encode()).hexdigest()[:20] + ".pkl"
    cache_path = cache_dir / cache_name
    if cache_path.exists():
        with cache_path.open("rb") as f:
            sites, demands, direct, cover, access_margins = pickle.load(f)
        print(json.dumps({"stage": "geometry_cache", "seed": label,
                          "candidate_sites": len(sites), "relay_demand_count": len(demands),
                          "covered_pairs": len(access_margins)}), flush=True)
    else:
        sites = candidate_sites(s, q2, coarse_deg, local_deg)
        print(json.dumps({"stage": "candidate_sites", "seed": label, "count": len(sites)}), flush=True)
        demands, direct = build_demands(s, q2, max_interval)
        print(json.dumps({"stage": "direct_intervals", "seed": label,
                          "relay_demand_count": len(demands), "direct_count": len(direct)}), flush=True)
        cover, access_margins = coverage_matrix(s, sites, demands)
        print(json.dumps({"stage": "coverage_matrix", "seed": label,
                          "covered_pairs": len(access_margins)}), flush=True)
        with cache_path.open("wb") as f:
            pickle.dump((sites, demands, direct, cover, access_margins), f,
                        protocol=pickle.HIGHEST_PROTOCOL)
    uncovered = [d.id for d in demands if not any(d.id in c for c in cover)]
    if uncovered:
        raise ValueError(f"{len(uncovered)} communication intervals have no candidate relay cover")
    relays, search = optimize_relay_jobs(s, sites, demands, cover)
    intervals, failures = verify(s, q2, demands, direct, sites, relays)
    if failures:
        raise ValueError(f"Independent continuous replay found {len(failures)} outages")
    direct_seconds = sum(x["end"] - x["start"] for x in intervals if x["mode"] == "direct")
    relay_seconds = sum(x["end"] - x["start"] for x in intervals if x["mode"] == "relay")
    deliveries = q2.get("deliveries") or {
        bid: dict(sortie=row["id"], zone=s.boxes[bid].zone, time=when)
        for row in q2["sorties"] for bid, when in row["delivery"].items()
    }
    weighted = sum(s.boxes[bid].priority * float(x["time"]) for bid, x in deliveries.items())
    transport_energy = sum(float(x["energy_kwh"]) for x in q2["sorties"])
    relay_energy = sum(float(x["energy_kwh"]) for x in relays)
    makespan = max([float(x["return_time"]) for x in q2["sorties"]] +
                   [float(x["return_time"]) for x in relays])
    soft_delay = sum(s.boxes[bid].priority * max(0.0, float(x["time"]) - s.boxes[bid].desired)
                     for bid, x in deliveries.items() if s.boxes[bid].hard_deadline is None)
    min_margin = min(x["margin_lower_db"] for x in intervals) if intervals else 0.0
    q4_groups = _coupled_zones(s, q2, relays, demands)
    return {
        "schema": "q3-continuous-v1",
        "q2_seed": label,
        "transport_sorties": q2["sorties"],
        "relay_sorties": relays,
        "deliveries": deliveries,
        "communication_intervals": intervals,
        "objective": {
            "weighted_soft_delay": soft_delay,
            "weighted_delivery_seconds": weighted,
            "makespan": makespan,
            "energy_kwh": transport_energy + relay_energy,
            "transport_energy_kwh": transport_energy,
            "relay_energy_kwh": relay_energy,
            "transport_sorties": len(q2["sorties"]),
            "relay_sorties": len(relays),
            "total_sorties": len(q2["sorties"]) + len(relays),
        },
        "communication": {
            "direct_seconds": direct_seconds,
            "relay_seconds": relay_seconds,
            "total_transport_seconds": direct_seconds + relay_seconds,
            "relay_share": relay_seconds / (direct_seconds + relay_seconds),
            "outage_seconds": 0.0,
            "unknown_seconds": 0.0,
            "minimum_certified_margin_db": min_margin,
        },
        "certificate": {
            "status": "PASS",
            "dem_model": "piecewise_constant_closed_cells",
            "time_method": "phase events + recursively bounded swept triangles",
            "maximum_base_interval_seconds": max_interval,
            "candidate_site_count": len(sites),
            "demand_interval_count": len(demands),
            "unknown_interval_count": 0,
            "outage_interval_count": 0,
            "continuous_replay": "same exact swept-cell kernel used for final interval replay",
            "independent_replay": "written separately by q3_independent_audit.py",
        },
        "search": {**search, "coarse_grid_degrees": coarse_deg,
                   "local_grid_degrees": local_deg,
                   "finite_candidate_optimality": "heuristic feasible solution; no global-optimum claim"},
        "q4_compatibility": {"coupled_components": q4_groups,
                             "component_count": len(q4_groups),
                             "constraint_imposed_during_q3": False},
        "sites": [asdict(x) for x in sites],
    }


def dump_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

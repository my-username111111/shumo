"""Question 3: communication-aware wave and relay sortie construction.

Candidate hover sites are sampled inside the DEM.  Routes are regrouped into
deadline waves; one or two fixed sites cover each wave.  Site selection,
aircraft assignment and energy-component reuse are checked on a common clock.
The final checker samples at 2 s; this is a numerical certificate, not an
analytic guarantee between samples.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor

from model import Scenario, phase_position


@dataclass(frozen=True)
class Site:
    id: str
    lon: float
    lat: float
    agl: float
    altitude: float
    lead: float
    back_time: float
    gateway_margin: float

    @property
    def point(self) -> tuple[float, float, float]:
        return self.lon, self.lat, self.altitude


def candidate_sites(s: Scenario, step_degrees: float = 0.005) -> list[Site]:
    service = [n for k, n in s.nodes.items() if k != "O01"]
    west = floor((min(n.lon for n in service) - 0.01) / step_degrees) * step_degrees
    east = max(n.lon for n in service) + 0.01
    south = floor((min(n.lat for n in service) - 0.01) / step_degrees) * step_degrees
    north = max(n.lat for n in service) + 0.01
    hub = s.nodes["O01"]
    gateway = (hub.lon, hub.lat, hub.ground + s.gateway_agl)
    sites = []
    nx = int((east - west) / step_degrees) + 1
    ny = int((north - south) / step_degrees) + 1
    for ix in range(nx):
        for iy in range(ny):
            lon = round(west + ix * step_degrees, 8)
            lat = round(south + iy * step_degrees, 8)
            if not s.dem.inside(lon, lat):
                continue
            for agl in (200.0, 300.0):
                altitude = s.dem.height(lon, lat) + agl
                point = (lon, lat, altitude)
                margin = s.link_margin(point, gateway, "relay_backhaul", "gateway")
                if margin < 2.0:
                    continue
                try:
                    trip = s.relay_sortie(lon, lat, agl, 0.0)
                except ValueError:
                    continue
                sites.append(Site(f"P{len(sites) + 1:03d}", lon, lat, agl, altitude,
                                  trip["prepare"] + trip["out_time"] + trip["establish"],
                                  trip["back_time"], margin))
    return sites


def route_gaps(s: Scenario, route: dict, step: float = 20.0) -> list[tuple[float, tuple[float, float, float]]]:
    home = s.nodes["O01"]
    gateway = (home.lon, home.lat, home.ground + s.gateway_agl)
    points: dict[float, tuple[float, float, float]] = {}
    for phase in route["phases"]:
        n = max(1, ceil((phase["t1"] - phase["t0"]) / step))
        for j in range(n + 1):
            time = phase["t0"] + (phase["t1"] - phase["t0"]) * j / n
            position = phase_position(phase, time)
            if s.link_margin(position, gateway, "transport", "gateway") < 0:
                points[round(time, 6)] = position
    return sorted(points.items())


def site_coverage(s: Scenario, sites: list[Site], rows: list[dict], gaps: list[list]) -> list[int]:
    bits = []
    for site in sites:
        mask = 0
        for i, points in enumerate(gaps):
            if all(s.link_margin(position, site.point, "transport", "relay_access") >= 2.0
                   for _, position in points):
                mask |= 1 << i
        bits.append(mask)
    return bits


def deadline_group(s: Scenario, row: dict) -> int:
    hard = [s.boxes[b].hard_deadline for b in row["route"]["box_ids"]
            if s.boxes[b].hard_deadline is not None]
    return min(hard) if hard else 1_000_000


def wave_groups(s: Scenario, rows: list[dict]) -> list[list[int]]:
    groups: dict[int, list[int]] = {}
    for i, row in enumerate(rows):
        groups.setdefault(deadline_group(s, row), []).append(i)
    return [groups[k] for k in sorted(groups)]


def site_choices(sites: list[Site], coverage: list[int], target: int, limit: int = 80) -> list[tuple[int, ...]]:
    one = [(i,) for i, bits in enumerate(coverage) if bits & target == target]
    one.sort(key=lambda x: sites[x[0]].lead)
    if one:
        return one[:limit]
    pairs = []
    for i, first in enumerate(coverage):
        if first & target == 0:
            continue
        for j in range(i + 1, len(coverage)):
            if (first | coverage[j]) & target == target:
                pair = (i, j)
                score = (max(sites[i].lead, sites[j].lead),
                         sites[i].lead + sites[j].lead,
                         -((first | coverage[j]) & target).bit_count())
                pairs.append((score, pair))
    pairs.sort(key=lambda x: x[0])
    return [pair for _, pair in pairs[:limit]]


def split_groups_for_coverage(sites: list[Site], coverage: list[int],
                              groups: list[list[int]], gaps: list[list]) -> list[list[int]]:
    """Split a deadline class only if two robust fixed sites cannot cover it."""
    output = []
    for group in groups:
        target = sum(1 << i for i in group if gaps[i])
        if not target or site_choices(sites, coverage, target, limit=1):
            output.append(group)
            continue
        if len(group) <= 1:
            raise ValueError("An individual sortie has no robust relay-site cover")
        best = None
        for i, first in enumerate(coverage):
            for j in range(i + 1, len(coverage)):
                covered = (first | coverage[j]) & target
                count = covered.bit_count()
                score = (count, -max(sites[i].lead, sites[j].lead))
                if best is None or score > best[0]:
                    best = (score, covered)
        if best is None or best[1] == 0 or best[1] == target:
            raise ValueError("Could not divide a communication wave")
        # Direct-only sorties stay in the first subwave; they need no relay.
        first_group = [i for i in group if not gaps[i] or best[1] & (1 << i)]
        second_group = [i for i in group if gaps[i] and not best[1] & (1 << i)]
        output.extend(split_groups_for_coverage(sites, coverage, [first_group, second_group], gaps))
    return output


def _assign_jobs(indices: list[int], chosen: tuple[int, ...], coverage: list[int],
                 rows: list[dict], gaps: list[list], sites: list[Site]) -> dict[int, int]:
    assignment = {}
    jobs = sorted((i for i in indices if gaps[i]), key=lambda i: len(gaps[i]), reverse=True)
    tentative_spans: dict[int, tuple[float, float] | None] = {site: None for site in chosen}
    for i in jobs:
        allowed = [site for site in chosen if coverage[site] & (1 << i)]
        if not allowed:
            raise ValueError("A route is not covered by selected relay sites")
        absolute_lo = rows[i]["start"] + gaps[i][0][0]
        absolute_hi = rows[i]["start"] + gaps[i][-1][0]
        def cost(site: int) -> tuple:
            span = tentative_spans[site]
            growth = absolute_hi - absolute_lo if span is None else (
                max(span[1], absolute_hi) - min(span[0], absolute_lo) - (span[1] - span[0]))
            return (growth, sites[site].lead, site)
        selected = min(allowed, key=cost)
        assignment[i] = selected
        old = tentative_spans[selected]
        tentative_spans[selected] = (absolute_lo, absolute_hi) if old is None else (
            min(old[0], absolute_lo), max(old[1], absolute_hi))
    return assignment


def _trial_wave(s: Scenario, rows: list[dict], gaps: list[list], coverage: list[int],
                sites: list[Site], indices: list[int], choice: tuple[int, ...], state: dict) -> tuple[dict, dict] | None:
    # Temporarily keep original drone and battery IDs.  The supplied Q2 plan
    # already has a valid allocation; only start times are reoptimized here.
    wave_base = 0.0
    for _ in range(8):
        trial = {k: v.copy() if isinstance(v, dict) else v for k, v in state.items()}
        wave_rows = []
        for i in sorted(indices, key=lambda j: (rows[j]["start"], rows[j]["id"])):
            original = rows[i]
            drone = original["drone"]
            battery = original["battery"]
            model = original["model"]
            start = max(wave_base, trial["aircraft_ready"][drone], trial["battery_ready"][battery])
            finish = start + original["route"]["duration"]
            deliver = {bid: start + rel for bid, rel in original["route"]["delivery"].items()}
            if any(s.boxes[bid].hard_deadline is not None and when > s.boxes[bid].hard_deadline + 1e-7
                   for bid, when in deliver.items()):
                return None
            trial["aircraft_ready"][drone] = finish
            trial["battery_ready"][battery] = finish + s.charge_time(original["return_soc"], s.battery_charge[model])
            wave_rows.append(dict(original_index=i, id=original["id"], drone=drone, model=model,
                                  battery=battery, start=start, return_time=finish,
                                  battery_ready=trial["battery_ready"][battery],
                                  energy_kwh=original["energy_kwh"], return_soc=original["return_soc"],
                                  visits=original["visits"], load_kg=original["load_kg"], load_m3=original["load_m3"],
                                  route=original["route"], delivery=deliver))
        by_index = {r["original_index"]: r for r in wave_rows}
        assignments = _assign_jobs(indices, choice, coverage, by_index, gaps, sites)
        needed_sites = [j for j in choice if j in assignments.values()]
        # Assign the earlier gap to the first available relay, then the second.
        intervals = {}
        for j in needed_sites:
            served = [i for i, site in assignments.items() if site == j]
            # Start ahead of the first sampled direct-link failure so a small
            # timing or raster-boundary shift cannot create a handover gap.
            lo = min(by_index[i]["start"] + gaps[i][0][0] for i in served) - 90.0
            hi = max(by_index[i]["start"] + gaps[i][-1][0] for i in served) + 90.0
            intervals[j] = (lo, hi)
        relay_rows = []
        relay_ready = trial["relay_ready"].copy()
        energy_ready = trial["relay_energy_ready"].copy()
        delay = 0.0
        for j in sorted(needed_sites, key=lambda site: intervals[site][0]):
            site = sites[j]
            lo, hi = intervals[j]
            relay_id = min(relay_ready, key=lambda rid: relay_ready[rid])
            energy_id = min(energy_ready, key=lambda eid: energy_ready[eid])
            available = max(relay_ready[relay_id], energy_ready[energy_id])
            launch = max(available, lo - site.lead)
            established = launch + site.lead
            if established > lo + 1e-7:
                delay = max(delay, established - lo)
                break
            service_duration = hi - established
            try:
                trip = s.relay_sortie(site.lon, site.lat, site.agl, service_duration)
            except ValueError:
                return None
            finish = launch + trip["duration"]
            relay_ready[relay_id] = finish + s.relay.turnaround
            energy_ready[energy_id] = finish + s.charge_time(trip["return_soc"], s.relay_charge)
            relay_rows.append(dict(id=f"R{len(state['completed_relays']) + len(relay_rows) + 1:03d}",
                                   relay=relay_id, energy_component=energy_id,
                                   start=launch, established=established, service_end=hi,
                                   return_time=finish, relay_ready=relay_ready[relay_id],
                                   component_ready=energy_ready[energy_id],
                                   lon=site.lon, lat=site.lat, agl=site.agl, altitude=site.altitude,
                                   energy_kwh=trip["energy_kwh"], return_soc=trip["return_soc"],
                                   site_id=site.id, covered_transport=[by_index[i]["id"] for i in assignments if assignments[i] == j]))
        if delay > 0:
            wave_base += delay + 0.01
            continue
        trial["relay_ready"] = relay_ready
        trial["relay_energy_ready"] = energy_ready
        trial["completed_relays"] = [*state["completed_relays"], *relay_rows]
        return dict(transport=wave_rows, relays=relay_rows, assignments=assignments,
                    wave_base=wave_base), trial
    return None


def _verify_communication(s: Scenario, transports: list[dict], relays: list[dict],
                          step: float = 2.0) -> tuple[list[dict], list[dict]]:
    hub = s.nodes["O01"]
    gateway = (hub.lon, hub.lat, hub.ground + s.gateway_agl)
    phase_rows = []
    gaps = []
    for row in transports:
        for phase in row["route"]["phases"]:
            begin = row["start"] + phase["t0"]
            end = row["start"] + phase["t1"]
            n = max(1, ceil((end - begin) / step))
            last_mode = None
            segment_start = begin
            for k in range(n + 1):
                time = begin + (end - begin) * k / n
                position = phase_position(phase, phase["t0"] + (phase["t1"] - phase["t0"]) * k / n)
                direct = s.link_margin(position, gateway, "transport", "gateway")
                if direct >= 0:
                    mode = ("direct", None)
                else:
                    eligible = []
                    for relay in relays:
                        if relay["established"] - 1e-7 <= time <= relay["service_end"] + 1e-7:
                            site = (relay["lon"], relay["lat"], relay["altitude"])
                            margin = s.link_margin(position, site, "transport", "relay_access")
                            if margin >= 0:
                                eligible.append((margin, relay["id"]))
                    mode = ("relay", max(eligible)[1]) if eligible else ("outage", None)
                if mode[0] == "outage":
                    gaps.append(dict(sortie=row["id"], phase=phase["kind"], time=time,
                                     lon=position[0], lat=position[1], altitude=position[2]))
                if last_mode is None:
                    last_mode = mode
                elif mode != last_mode:
                    phase_rows.append(dict(sortie=row["id"], phase=phase["kind"],
                                           start=segment_start, end=time, mode=last_mode[0],
                                           relay_sortie=last_mode[1]))
                    segment_start = time
                    last_mode = mode
            if last_mode is not None:
                phase_rows.append(dict(sortie=row["id"], phase=phase["kind"],
                                       start=segment_start, end=end, mode=last_mode[0],
                                       relay_sortie=last_mode[1]))
    return phase_rows, gaps


def solve(s: Scenario, q2: dict, spacing: float = 0.005,
          promotion: tuple[str, int] | tuple[tuple[str, int], ...] | None = None) -> dict:
    rows = q2["sorties"]
    sites = candidate_sites(s, spacing)
    gaps = [route_gaps(s, row["route"]) for row in rows]
    coverage = site_coverage(s, sites, rows, gaps)
    groups = wave_groups(s, rows)
    promotions = (promotion,) if promotion and isinstance(promotion[0], str) else (promotion or ())
    for sortie_id, due_class in promotions:
        promoted = next((i for i, row in enumerate(rows) if row["id"] == sortie_id), None)
        if promoted is None or deadline_group(s, rows[promoted]) < 1_000_000:
            raise ValueError("Only an existing soft-deadline sortie can be promoted")
        destination = next((group for group in groups
                            if deadline_group(s, rows[group[0]]) == due_class), None)
        if destination is None:
            raise ValueError("Promotion target deadline class does not exist")
        for group in groups:
            if promoted in group:
                group.remove(promoted)
                break
        destination.append(promoted)
        groups = [group for group in groups if group]
    waves = split_groups_for_coverage(sites, coverage, groups, gaps)
    state = dict(aircraft_ready={u: 0.0 for u in s.aircraft},
                 battery_ready={f"{g}{i:02d}": 0.0 for g, n in s.battery_count.items() for i in range(1, n + 1)},
                 relay_ready={rid: 0.0 for rid in s.relays},
                 relay_energy_ready={f"RE{i:02d}": 0.0 for i in range(1, s.relay_energy_count + 1)},
                 completed_relays=[])
    beam = [(state, [], [])]
    beam_width = 6
    for wave_number, indices in enumerate(waves, 1):
        target = sum(1 << i for i in indices if gaps[i])
        choices = site_choices(sites, coverage, target, limit=45) if target else [()]
        if not choices:
            raise ValueError(f"Wave {wave_number} requires more than two fixed relay sites")
        proposals = []
        for previous_state, previous_transport, previous_waves in beam:
            for choice in choices:
                result = _trial_wave(s, rows, gaps, coverage, sites, indices, choice, previous_state)
                if result is None:
                    continue
                wave, trial_state = result
                transport = [*previous_transport, *wave["transport"]]
                waves_so_far = [*previous_waves, dict(
                    number=wave_number, due_class=deadline_group(s, rows[indices[0]]),
                    transport=[r["id"] for r in wave["transport"]],
                    relay=[r["id"] for r in wave["relays"]],
                    sites=[(sites[j].lon, sites[j].lat, sites[j].agl) for j in choice],
                    earliest_transport_start=min(r["start"] for r in wave["transport"]))]
                weighted = sum(s.boxes[bid].priority * when
                               for row in transport for bid, when in row["delivery"].items())
                ready = max(trial_state["relay_ready"].values())
                energy = sum(r["energy_kwh"] for r in trial_state["completed_relays"])
                score = weighted + 400 * ready + 20 * energy
                proposals.append((score, trial_state, transport, waves_so_far))
        if not proposals:
            raise ValueError(f"Wave {wave_number} cannot meet deadlines and relay turnaround with sampled sites")
        proposals.sort(key=lambda x: x[0])
        beam = [(st, transport, waves_so_far) for _, st, transport, waves_so_far in proposals[:beam_width]]
    complete = []
    for final_state, all_transport, chosen_waves in beam:
        relays = final_state["completed_relays"]
        communication, outages = _verify_communication(s, all_transport, relays)
        if outages:
            continue
        deliveries = {bid: dict(sortie=row["id"], zone=s.boxes[bid].zone, time=when)
                      for row in all_transport for bid, when in row["delivery"].items()}
        weighted_delivery = sum(s.boxes[bid].priority * x["time"] for bid, x in deliveries.items())
        result = dict(transport_sorties=all_transport, relay_sorties=relays,
                      deliveries=deliveries, communication=communication, waves=chosen_waves,
                      verification=dict(sample_seconds=2.0, outage_count=0, outages=[],
                                        candidate_site_count=len(sites)),
                      objective=dict(weighted_delivery_seconds=weighted_delivery,
                                     makespan=max([r["return_time"] for r in all_transport] +
                                                  [r["return_time"] for r in relays]),
                                     energy_kwh=sum(r["energy_kwh"] for r in all_transport) +
                                                sum(r["energy_kwh"] for r in relays),
                                     transport_sorties=len(all_transport), relay_sorties=len(relays)))
        from q4 import coupled_components
        component_count = len(coupled_components(s, result)[0])
        if component_count >= 3:
            obj = result["objective"]
            complete.append(((obj["weighted_delivery_seconds"], obj["makespan"],
                              obj["energy_kwh"], obj["relay_sorties"]), result))
    if not complete:
        raise ValueError("No beam candidate has full sampled communication and three partitionable components")
    selected = min(complete, key=lambda x: x[0])[1]
    selected["search"] = dict(beam_width=beam_width, complete_candidates=len(complete),
                              promotion=promotion)
    return selected

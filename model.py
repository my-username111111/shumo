"""Shared, auditable physical model for all four questions.

The source statement specifies the payload dependent equivalent range and the
sum of horizontal and climb energy, but not the two component formulae.  This
implementation states the missing convention explicitly in ``flight_energy``.
All coordinates are WGS84; horizontal distances are in metres and time in s.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from math import asin, cos, floor, log10, radians, sin, sqrt
from pathlib import Path
from typing import Iterable

import numpy as np
from openpyxl import load_workbook
from PIL import Image


BASE = Path(__file__).resolve().parent.parent / "数据"
TABLES = BASE / "无人机应急物资运输基础数据"
TERRAIN = BASE / "镇龙乡地理空间数据"
G = 9.81


@dataclass(frozen=True)
class Node:
    id: str
    lon: float
    lat: float
    ground: float

    @property
    def work_alt(self) -> float:
        return self.ground if self.id == "O01" else self.ground + 30.0


@dataclass(frozen=True)
class Box:
    id: str
    zone: str
    kind: str
    kg: float
    volume: float
    first: bool
    first_deadline: int | None
    desired: int
    priority: int

    @property
    def hard_deadline(self) -> int | None:
        deadlines = []
        if self.first and self.first_deadline is not None:
            deadlines.append(self.first_deadline)
        if self.kind == "医疗物资":
            deadlines.append(self.desired)
        return min(deadlines) if deadlines else None


@dataclass(frozen=True)
class TransportType:
    id: str
    empty_kg: float
    max_kg: float
    max_volume: float
    speed: float
    empty_range: float
    full_range: float
    battery_kwh: float
    reserve: float
    prepare: int
    load_each: int
    handoff: int
    handoff_each: int
    climb_speed: float
    descend_speed: float
    climb_efficiency: float


@dataclass(frozen=True)
class RelayType:
    mass: float
    speed: float
    cruise_kw: float
    battery_kwh: float
    reserve: float
    prepare: int
    establish: int
    turnaround: int
    climb_speed: float
    descend_speed: float
    climb_efficiency: float
    hover_kw: float
    comm_kw: float
    max_agl: float


@dataclass(frozen=True)
class Leg:
    distance: float
    cruise_alt: float
    climb: float
    descend: float
    peak_ground: float


class DEM:
    def __init__(self, path: Path):
        image = Image.open(path)
        self.values = np.asarray(image, dtype=np.float32)
        self.width, self.nrows = image.size
        self.dx, self.dy = map(float, image.tag_v2[33550][:2])
        self.x0, self.y0 = map(float, image.tag_v2[33922][3:5])
        self.nodata = -32767.0

    def pixel(self, lon: float, lat: float) -> tuple[float, float]:
        return (lon - self.x0) / self.dx, (self.y0 - lat) / self.dy

    def inside(self, lon: float, lat: float) -> bool:
        x, y = self.pixel(lon, lat)
        return 0 <= x < self.width and 0 <= y < self.nrows

    def height(self, lon: float, lat: float) -> float:
        x, y = self.pixel(lon, lat)
        ix, iy = floor(x), floor(y)
        if not (0 <= ix < self.width and 0 <= iy < self.nrows):
            raise ValueError(f"Point outside DEM: {lon}, {lat}")
        z = float(self.values[iy, ix])
        if z == self.nodata or not np.isfinite(z):
            raise ValueError(f"NoData DEM pixel at {lon}, {lat}")
        return z

    def crossed_pixels(self, a: tuple[float, float], b: tuple[float, float]) -> list[tuple[int, int]]:
        """Raster supercover for a straight lon/lat segment (including corners)."""
        x0, y0 = self.pixel(*a)
        x1, y1 = self.pixel(*b)
        dx, dy = x1 - x0, y1 - y0
        ix, iy = floor(x0), floor(y0)
        ex, ey = floor(x1), floor(y1)
        sx = 1 if dx > 0 else -1 if dx < 0 else 0
        sy = 1 if dy > 0 else -1 if dy < 0 else 0
        tx = ((ix + 1 - x0) / dx) if dx > 0 else ((ix - x0) / dx) if dx < 0 else float("inf")
        ty = ((iy + 1 - y0) / dy) if dy > 0 else ((iy - y0) / dy) if dy < 0 else float("inf")
        stepx = abs(1 / dx) if dx else float("inf")
        stepy = abs(1 / dy) if dy else float("inf")
        result: list[tuple[int, int]] = []
        seen = set()

        def add(x: int, y: int) -> None:
            if not (0 <= x < self.width and 0 <= y < self.nrows):
                raise ValueError("Flight segment exits the DEM coverage")
            if (x, y) not in seen:
                seen.add((x, y))
                result.append((x, y))

        add(ix, iy)
        while (ix, iy) != (ex, ey):
            if abs(tx - ty) < 1e-12:
                add(ix + sx, iy)
                add(ix, iy + sy)
                ix, iy = ix + sx, iy + sy
                tx += stepx
                ty += stepy
            elif tx < ty:
                ix += sx
                tx += stepx
            else:
                iy += sy
                ty += stepy
            add(ix, iy)
        return result

    def peak(self, a: tuple[float, float], b: tuple[float, float]) -> float:
        vals = [float(self.values[y, x]) for x, y in self.crossed_pixels(a, b)]
        vals = [v for v in vals if v != self.nodata and np.isfinite(v)]
        if not vals:
            raise ValueError("Flight segment has no DEM data")
        return max(vals)


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = a
    lon2, lat2 = b
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    h = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6371000.0 * asin(sqrt(h))


class Scenario:
    def __init__(self, base: Path = BASE):
        self.base = base
        data_dir = base / "无人机应急物资运输基础数据"
        geo_dir = base / "镇龙乡地理空间数据"
        dem_files = list(geo_dir.rglob("*.tif"))
        if len(dem_files) != 1:
            raise FileNotFoundError(f"Expected one DEM GeoTIFF, found {len(dem_files)}")
        self.dem = DEM(dem_files[0])

        node_rows = list(load_workbook(data_dir / "调度中心与服务区.xlsx", read_only=True, data_only=True).active.values)
        self.nodes = {r[0]: Node(r[0], float(r[2]), float(r[3]), float(r[4]))
                      for r in [node_rows[2], *node_rows[6:21]]}
        demand_rows = list(load_workbook(data_dir / "物资需求与配送时限.xlsx", read_only=True, data_only=True)["逐箱货箱清单"].values)[1:]
        self.boxes = {r[0]: Box(r[0], r[1], r[2], float(r[3]), float(r[4]), r[5] == "是",
                                int(r[6]) if r[6] is not None else None, int(r[7]), int(r[8]))
                      for r in demand_rows}
        self.zone_boxes = {z: [b for b in self.boxes.values() if b.zone == z]
                           for z in self.nodes if z != "O01"}
        transport_rows = list(load_workbook(data_dir / "运输无人机数据.xlsx", read_only=True, data_only=True).active.values)
        self.transport = {
            r[0]: TransportType(r[0], float(r[2]), float(r[3]), float(r[4]), float(r[5]),
                                float(r[6]), float(r[7]), float(r[8]), float(r[9]) / 100,
                                int(r[10]), int(r[11]), int(r[12]), int(r[13]),
                                float(r[14]), float(r[15]), float(r[16]))
            for r in transport_rows[2:5]
        }
        self.aircraft = {r[0]: r[1] for r in transport_rows[8:16]}
        self.battery_count = {r[0]: int(r[1]) for r in transport_rows[19:22]}
        self.battery_charge = {r[0]: int(r[2]) for r in transport_rows[19:22]}

        relay_rows = list(load_workbook(data_dir / "中继无人机数据.xlsx", read_only=True, data_only=True).active.values)
        r = relay_rows[2]
        self.relay = RelayType(float(r[4]), float(r[5]), float(r[6]), float(r[7]), float(r[8]) / 100,
                               int(r[9]), int(r[10]), int(r[11]), float(r[12]), float(r[13]),
                               float(r[14]), float(r[16]), float(r[17]), float(r[18]))
        self.relays = [r[0] for r in relay_rows[6:8]]
        self.relay_energy_count = int(relay_rows[11][1])
        self.relay_charge = int(relay_rows[11][2])
        radio_rows = list(load_workbook(data_dir / "通信链路参数.xlsx", read_only=True, data_only=True).active.values)
        self.frequency_mhz = float(radio_rows[2][4])
        self.system_loss_db = float(radio_rows[3][4])
        self.obstruction_db = float(radio_rows[4][4])
        self.sensitivity_dbm = float(radio_rows[5][4])
        self.fade_db = float(radio_rows[6][4])
        self.gateway_agl = float(radio_rows[15][4])
        self.radio = {
            "transport": (float(radio_rows[7][4]), float(radio_rows[8][4])),
            "relay_access": (float(radio_rows[9][4]), float(radio_rows[10][4])),
            "relay_backhaul": (float(radio_rows[11][4]), float(radio_rows[12][4])),
            "gateway": (float(radio_rows[13][4]), float(radio_rows[14][4])),
        }

    @lru_cache(maxsize=None)
    def node_leg(self, origin: str, destination: str) -> Leg:
        a, b = self.nodes[origin], self.nodes[destination]
        return self.leg((a.lon, a.lat, a.work_alt), (b.lon, b.lat, b.work_alt))

    def leg(self, a: tuple[float, float, float], b: tuple[float, float, float]) -> Leg:
        peak = self.dem.peak(a[:2], b[:2])
        # A relay may hover higher than the 50 m terrain-clearance cruise
        # level.  The level flight cannot be below either endpoint altitude.
        cruise = max(peak + 50.0, a[2], b[2])
        return Leg(distance_m(a[:2], b[:2]), cruise, max(0.0, cruise - a[2]),
                   max(0.0, cruise - b[2]), peak)

    def flight_energy(self, model: str, leg: Leg, payload: float) -> float:
        """Assumption: horizontal kWh = E_use*d/L(q); climb kWh = mgh/(eta*3.6e6)."""
        t = self.transport[model]
        if payload < -1e-9 or payload > t.max_kg + 1e-9:
            raise ValueError("Payload exceeds nameplate capacity")
        effective_range = t.empty_range - (t.empty_range - t.full_range) * (max(0.0, payload) / t.max_kg) ** 1.5
        horizontal = t.battery_kwh * leg.distance / effective_range
        climb = (t.empty_kg + payload) * G * leg.climb / (t.climb_efficiency * 3_600_000)
        return horizontal + climb

    def flight_time(self, model: str, leg: Leg) -> float:
        t = self.transport[model]
        return leg.climb / t.climb_speed + leg.distance / t.speed + leg.descend / t.descend_speed

    def charge_time(self, soc: float, full_time: float) -> float:
        if not 0 <= soc <= 1 + 1e-9:
            raise ValueError(f"Invalid state of charge: {soc}")
        soc = min(1.0, soc)
        if soc < 0.9:
            return full_time * (0.65 * (0.9 - soc) / 0.9 + 0.35)
        return full_time * 0.35 * (1 - soc) / 0.1

    def route(self, model: str, visits: list[tuple[str, list[str]]]) -> dict:
        """Evaluate a fully loaded multi-stop sortie beginning at preparation time 0."""
        if not visits:
            raise ValueError("A route must visit at least one service area")
        t = self.transport[model]
        ids = [bid for _, boxes in visits for bid in boxes]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate box in sortie")
        if any(not boxes or any(self.boxes[b].zone != zone for b in boxes) for zone, boxes in visits):
            raise ValueError("Box/zone mismatch")
        kg = sum(self.boxes[b].kg for b in ids)
        volume = sum(self.boxes[b].volume for b in ids)
        if kg > t.max_kg + 1e-8 or volume > t.max_volume + 1e-8:
            raise ValueError("Payload mass or volume exceeded")
        clock = float(t.prepare + t.load_each * len(ids))
        current = "O01"
        energy = 0.0
        remaining = kg
        phases = []
        delivery = {}
        for zone, box_ids in [*visits, ("O01", [])]:
            leg = self.node_leg(current, zone)
            e = self.flight_energy(model, leg, remaining)
            energy += e
            src, dst = self.nodes[current], self.nodes[zone]
            up = leg.climb / t.climb_speed
            cruise = leg.distance / t.speed
            down = leg.descend / t.descend_speed
            phases.extend([
                dict(kind="climb", t0=clock, t1=clock + up,
                     a=(src.lon, src.lat, src.work_alt), b=(src.lon, src.lat, leg.cruise_alt)),
                dict(kind="cruise", t0=clock + up, t1=clock + up + cruise,
                     a=(src.lon, src.lat, leg.cruise_alt), b=(dst.lon, dst.lat, leg.cruise_alt)),
                dict(kind="descent", t0=clock + up + cruise, t1=clock + up + cruise + down,
                     a=(dst.lon, dst.lat, leg.cruise_alt), b=(dst.lon, dst.lat, dst.work_alt)),
            ])
            clock += up + cruise + down
            if zone != "O01":
                service = t.handoff + t.handoff_each * len(box_ids)
                phases.append(dict(kind="handoff", t0=clock, t1=clock + service,
                                   a=(dst.lon, dst.lat, dst.work_alt), b=(dst.lon, dst.lat, dst.work_alt)))
                clock += service
                for bid in box_ids:
                    delivery[bid] = clock
                remaining -= sum(self.boxes[bid].kg for bid in box_ids)
            current = zone
        limit = t.battery_kwh * (1 - t.reserve)
        if energy > limit + 1e-8:
            raise ValueError(f"Return energy {energy:.4f} exceeds {limit:.4f} kWh")
        return dict(model=model, visits=[(z, list(bs)) for z, bs in visits], box_ids=ids,
                    load_kg=kg, load_m3=volume, duration=clock, energy_kwh=energy,
                    return_soc=1 - energy / t.battery_kwh, delivery=delivery, phases=phases)

    def relay_sortie(self, lon: float, lat: float, agl: float, service_seconds: float) -> dict:
        r = self.relay
        if not self.dem.inside(lon, lat) or not 0 <= agl <= r.max_agl:
            raise ValueError("Invalid relay hover location or height")
        home = self.nodes["O01"]
        hover_alt = self.dem.height(lon, lat) + agl
        a = (home.lon, home.lat, home.work_alt)
        b = (lon, lat, hover_alt)
        out = self.leg(a, b)
        back = self.leg(b, a)
        out_time = out.climb / r.climb_speed + out.distance / r.speed + out.descend / r.descend_speed
        back_time = back.climb / r.climb_speed + back.distance / r.speed + back.descend / r.descend_speed
        energy = r.cruise_kw * (out.distance / r.speed + back.distance / r.speed) / 3600
        energy += r.mass * G * (out.climb + back.climb) / (r.climb_efficiency * 3_600_000)
        energy += (r.hover_kw + r.comm_kw) * (r.establish + service_seconds) / 3600
        limit = r.battery_kwh * (1 - r.reserve)
        if energy > limit + 1e-8:
            raise ValueError("Relay return reserve exceeded")
        return dict(lon=lon, lat=lat, agl=agl, altitude=hover_alt,
                    out_time=out_time, back_time=back_time,
                    establish=r.establish, prepare=r.prepare, turnaround=r.turnaround,
                    service_seconds=service_seconds, duration=r.prepare + out_time + r.establish + service_seconds + back_time,
                    energy_kwh=energy, return_soc=1 - energy / r.battery_kwh)

    def link_budget(self, first: str, second: str) -> float:
        p1, g1 = self.radio[first]
        p2, g2 = self.radio[second]
        threshold = self.sensitivity_dbm + self.fade_db
        return min(p1 + g1 + g2 - self.system_loss_db - threshold,
                   p2 + g2 + g1 - self.system_loss_db - threshold)

    def blocked(self, a: tuple[float, float, float], b: tuple[float, float, float], spacing: float = 15.0) -> bool:
        """Conservative 3-D DSM ray sample; endpoint pixels are omitted."""
        dist = distance_m(a[:2], b[:2])
        n = max(2, int(dist / spacing) + 1)
        f = np.arange(1, n, dtype=np.float64) / n
        lon = a[0] + f * (b[0] - a[0])
        lat = a[1] + f * (b[1] - a[1])
        ix = np.floor((lon - self.dem.x0) / self.dem.dx).astype(int)
        iy = np.floor((self.dem.y0 - lat) / self.dem.dy).astype(int)
        if np.any((ix < 0) | (ix >= self.dem.width) | (iy < 0) | (iy >= self.dem.nrows)):
            return True
        z = self.dem.values[iy, ix]
        if np.any((z == self.dem.nodata) | ~np.isfinite(z)):
            return True
        ray = a[2] + f * (b[2] - a[2])
        return bool(np.any(z >= ray))

    def link_margin(self, a: tuple[float, float, float], b: tuple[float, float, float],
                    first: str, second: str) -> float:
        horizontal = distance_m(a[:2], b[:2])
        km = max(0.001, sqrt(horizontal * horizontal + (a[2] - b[2]) ** 2) / 1000)
        loss = 32.45 + 20 * log10(self.frequency_mhz) + 20 * log10(km)
        if self.blocked(a, b):
            loss += self.obstruction_db
        return self.link_budget(first, second) - loss


def phase_position(phase: dict, t: float) -> tuple[float, float, float]:
    if phase["t1"] <= phase["t0"]:
        return tuple(phase["b"])
    f = min(1.0, max(0.0, (t - phase["t0"]) / (phase["t1"] - phase["t0"])))
    return tuple(a + f * (b - a) for a, b in zip(phase["a"], phase["b"]))


def route_position(route: dict, absolute_start: float, absolute_time: float) -> tuple[float, float, float] | None:
    local = absolute_time - absolute_start
    for phase in route["phases"]:
        if phase["t0"] - 1e-8 <= local <= phase["t1"] + 1e-8:
            return phase_position(phase, local)
    return None

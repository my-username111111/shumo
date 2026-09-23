"""Static route, resource-time and partition figures made from verified results."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from model import Scenario


def _font(size: int):
    for path in (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\msyh.ttc"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _dem_background(s: Scenario, width: int, height: int) -> tuple[Image.Image, callable]:
    nodes = list(s.nodes.values())
    lon0 = min(n.lon for n in nodes) - 0.025
    lon1 = max(n.lon for n in nodes) + 0.025
    lat0 = min(n.lat for n in nodes) - 0.025
    lat1 = max(n.lat for n in nodes) + 0.025
    px0, py1 = s.dem.pixel(lon0, lat0)
    px1, py0 = s.dem.pixel(lon1, lat1)
    left = max(0, int(px0))
    right = min(s.dem.width, int(px1) + 1)
    top = max(0, int(py0))
    bottom = min(s.dem.nrows, int(py1) + 1)
    dem = np.asarray(s.dem.values[top:bottom, left:right], dtype=np.float32)
    dem = np.where(dem == s.dem.nodata, np.nan, dem)
    valid = dem[np.isfinite(dem)]
    low, high = np.quantile(valid, [0.05, 0.98])
    norm = np.clip((np.nan_to_num(dem, nan=low) - low) / max(1.0, high - low), 0, 1)
    stops = np.array([[83, 145, 110], [171, 185, 116], [174, 129, 99], [238, 230, 211]], dtype=np.float32)
    pos = norm * 3
    idx = np.minimum(2, np.floor(pos).astype(int))
    frac = (pos - idx)[..., None]
    rgb = stops[idx] * (1 - frac) + stops[idx + 1] * frac
    gy, gx = np.gradient(np.nan_to_num(dem, nan=low))
    shade = np.clip(0.93 + 0.018 * (gx - gy), 0.75, 1.12)
    rgb = np.clip(rgb * shade[..., None], 0, 255).astype(np.uint8)
    base = Image.fromarray(rgb, "RGB").resize((width, height), Image.Resampling.BILINEAR)

    def xy(lon: float, lat: float) -> tuple[int, int]:
        return (int((lon - lon0) / (lon1 - lon0) * width),
                int((lat1 - lat) / (lat1 - lat0) * height))

    return base, xy


def route_map(s: Scenario, q3: dict, path: Path) -> None:
    canvas, xy = _dem_background(s, 1300, 1020)
    draw = ImageDraw.Draw(canvas, "RGBA")
    model_color = {"A": (15, 92, 194, 150), "B": (238, 117, 28, 160), "C": (190, 45, 46, 160)}
    for row in q3["transport_sorties"]:
        route = ["O01", *[z for z, _ in row["visits"]], "O01"]
        pts = [xy(s.nodes[z].lon, s.nodes[z].lat) for z in route]
        draw.line(pts, fill=model_color[row["model"]], width=3)
    for node in s.nodes.values():
        x, y = xy(node.lon, node.lat)
        size = 9 if node.id == "O01" else 6
        fill = (255, 220, 46, 255) if node.id == "O01" else (26, 33, 48, 255)
        draw.ellipse((x - size, y - size, x + size, y + size), fill=fill,
                     outline=(255, 255, 255, 255), width=2)
        draw.text((x + 9, y - 17), node.id, font=_font(19), fill=(255, 255, 255, 255),
                  stroke_width=2, stroke_fill=(35, 45, 50, 255))
    unique_relay = {}
    for row in q3["relay_sorties"]:
        key = (row["lon"], row["lat"], row["agl"])
        unique_relay.setdefault(key, []).append(row["id"])
    for (lon, lat, agl), ids in unique_relay.items():
        x, y = xy(lon, lat)
        draw.polygon([(x, y - 11), (x - 11, y + 9), (x + 11, y + 9)],
                     fill=(26, 34, 180, 255), outline=(255, 255, 255, 255), width=2)
        draw.text((x + 12, y + 5), "/".join(ids), font=_font(14),
                  fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(25, 35, 70, 255))
    draw.rounded_rectangle((20, 20, 500, 96), radius=10, fill=(255, 255, 255, 230))
    draw.text((34, 30), "Q3 transport routes and relay hover sites", font=_font(26), fill=(26, 43, 58))
    draw.text((34, 64), "A blue   B orange   C red   relay triangle", font=_font(17), fill=(46, 58, 66))
    canvas.save(path)


def timeline(s: Scenario, q3: dict, path: Path) -> None:
    trips = sorted(q3["transport_sorties"], key=lambda r: (r["start"], r["id"]))
    relays = sorted(q3["relay_sorties"], key=lambda r: (r["start"], r["id"]))
    width = 1700
    row_height = 31
    top = 90
    left = 190
    right = 55
    height = top + row_height * (len(trips) + len(relays)) + 80
    image = Image.new("RGB", (width, height), "#fafafa")
    draw = ImageDraw.Draw(image)
    max_time = max([r["return_time"] for r in trips + relays]) * 1.03

    def tx(seconds: float) -> int:
        return left + int(seconds / max_time * (width - left - right))

    draw.text((22, 22), "Q3 aircraft occupations and communication", font=_font(28), fill="#253448")
    for hour in range(int(max_time // 3600) + 2):
        x = tx(hour * 3600)
        draw.line((x, top - 15, x, height - 40), fill="#d6dce1", width=1)
        draw.text((x - 18, top - 40), f"{hour} h", font=_font(15), fill="#607081")
    comm_by_trip = {}
    for interval in q3["communication"]:
        comm_by_trip.setdefault(interval["sortie"], []).append(interval)
    color = {"A": "#2e7cb8", "B": "#ec8c32", "C": "#c94d4d"}
    for i, row in enumerate(trips):
        y = top + i * row_height
        draw.text((18, y + 2), f"{row['id']}  {row['drone']}", font=_font(16), fill="#344155")
        draw.rounded_rectangle((tx(row["start"]), y + 1, tx(row["return_time"]), y + 17),
                               radius=4, fill=color[row["model"]])
        for c in comm_by_trip.get(row["id"], []):
            if c["end"] > c["start"]:
                draw.line((tx(c["start"]), y + 22, tx(c["end"]), y + 22),
                          fill="#284bc7" if c["mode"] == "relay" else "#79b893", width=5)
        draw.line((left, y + row_height - 2, width - right, y + row_height - 2), fill="#eceff2")
    base = top + len(trips) * row_height
    for i, row in enumerate(relays):
        y = base + i * row_height
        draw.text((18, y + 2), f"{row['id']}  {row['relay']}", font=_font(16), fill="#344155")
        draw.rounded_rectangle((tx(row["start"]), y + 1, tx(row["return_time"]), y + 18),
                               radius=4, fill="#233f88")
        draw.line((tx(row["established"]), y + 22, tx(row["service_end"]), y + 22),
                  fill="#3b6ee6", width=5)
        draw.line((left, y + row_height - 2, width - right, y + row_height - 2), fill="#eceff2")
    draw.line((left, base - 5, width - right, base - 5), fill="#7b899a", width=3)
    draw.text((left, height - 35), "Green: direct  Blue: relay  Dark blue: relay sortie", font=_font(18), fill="#506070")
    image.save(path)


def partition_map(s: Scenario, q4: dict, path: Path) -> None:
    colors = [(31, 96, 182, 255), (212, 76, 45, 255), (121, 74, 173, 255)]
    panels = []
    for k in ("2", "3"):
        plan = q4["partitions"][k]
        background, xy = _dem_background(s, 880, 750)
        draw = ImageDraw.Draw(background, "RGBA")
        for group_number, group in enumerate(plan["groups"]):
            for zone in group["zones"]:
                node = s.nodes[zone]
                x, y = xy(node.lon, node.lat)
                draw.ellipse((x - 10, y - 10, x + 10, y + 10), fill=colors[group_number],
                             outline=(255, 255, 255, 255), width=2)
                draw.text((x + 12, y - 10), zone, font=_font(17), fill=(255, 255, 255, 255),
                          stroke_width=2, stroke_fill=(32, 39, 51, 255))
        panels.append((k, plan, background))
    image = Image.new("RGB", (1800, 850), "#ffffff")
    draw = ImageDraw.Draw(image)
    for i, (k, plan, panel) in enumerate(panels):
        x = 15 + i * 900
        draw.text((x + 8, 10), f"Q4  K={k}  minimum stock shortage", font=_font(26), fill="#26374b")
        total = sum(plan["shortage_strict"].values())
        draw.text((x + 8, 44), f"Extra units: {total}    Workload CV: {plan['workload_cv']:.2f}",
                  font=_font(18), fill="#526277")
        image.paste(panel, (x, 85))
    image.save(path)


def create_all(s: Scenario, q3: dict, q4: dict, output: Path) -> None:
    route_map(s, q3, output / "q3_route_map.png")
    timeline(s, q3, output / "q3_resource_timeline.png")
    partition_map(s, q4, output / "q4_partition_map.png")

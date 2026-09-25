"""Publication-ready raster charts for the fixed-input Q4 result."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from math import floor, ceil

from PIL import Image, ImageDraw, ImageFont

from q4 import RESOURCE_KEYS


W, H = 1500, 900
INK = "#19283C"
MUTED = "#617084"
GRID = "#DCE4ED"
PALETTE = ["#3E78AA", "#E2A13D", "#57A793", "#895BB7"]
BG = "#F7FAFD"


def _font(size: int, bold: bool = False):
    candidates = (["C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc"] if bold else
                  ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"])
    for file in candidates:
        if Path(file).is_file():
            return ImageFont.truetype(file, size)
    return ImageFont.load_default()


F18, F22, F26, F34, F46 = [_font(n) for n in (18, 22, 26, 34, 46)]
B22, B30 = _font(22, True), _font(30, True)


def _canvas(title: str, subtitle: str, height: int = H):
    im = Image.new("RGB", (W, height), BG)
    d = ImageDraw.Draw(im)
    d.text((70, 48), title, fill=INK, font=F46)
    d.text((72, 110), subtitle, fill=MUTED, font=F22)
    d.line((70, 152, W - 70, 152), fill=GRID, width=2)
    return im, d


def _legend(d, x, y, color, label):
    d.rounded_rectangle((x, y + 3, x + 20, y + 23), radius=4, fill=color)
    d.text((x + 28, y), label, fill=INK, font=F22)


def component_chart(result, output):
    comps = result["components"]["components"]
    im, d = _canvas("第四问任务绑定结构", f"运输架次与实际中继保障关系形成{len(comps)}个不可拆任务块")
    work = result["components"]["component_work_seconds"]
    counts = result["components"]["component_box_counts"]
    total = sum(work)
    widths = [280, 700, 280] if len(comps)==3 else [(1360-20*(len(comps)-1))/len(comps)]*len(comps)
    x = 70
    for i, (zones, sec, boxes, width) in enumerate(zip(comps, work, counts, widths)):
        col = PALETTE[i%len(PALETTE)]
        d.rounded_rectangle((x, 210, x + width, 670), radius=20, fill="white", outline=GRID, width=2)
        d.rounded_rectangle((x + 18, 230, x + width - 18, 305), radius=12, fill=col)
        d.text((x + 36, 246), f"C{i + 1}  ·  {len(zones)}区", fill="white", font=B30)
        d.text((x + 32, 342), f"{boxes}箱", fill=INK, font=F34)
        d.text((x + 32, 398), f"{sec / 3600:.2f} 小时", fill=INK, font=F34)
        d.text((x + 32, 452), f"工作量占比 {sec / total:.1%}", fill=MUTED, font=F22)
        label = ", ".join(zones)
        if i == 1:
            mid = len(zones) // 2
            d.text((x + 32, 525), ", ".join(zones[:mid]), fill=MUTED, font=F18)
            d.text((x + 32, 554), ", ".join(zones[mid:]), fill=MUTED, font=F18)
        else:
            d.text((x + 32, 525), label, fill=MUTED, font=F22)
        x += width + 20
    counts_by_k={k:sum(p['group_count']==k for p in result['partitions']) for k in (2,3)}
    dominant=max(range(len(work)),key=work.__getitem__)
    d.text((72, 720), f"合法分区：两组 {counts_by_k[2]} 种；三组 {counts_by_k[3]} 种。C{dominant+1}工作量占比 {work[dominant]/total:.2%}。", fill=INK, font=F26)
    d.text((72, 776), "分组时保持原Q3的货箱组批、访问顺序、中继保障关系和任务时刻。", fill=MUTED, font=F22)
    im.save(output)


def tradeoff_chart(result, output):
    im, d = _canvas("资源需求与工作量均衡", "固定Q3后枚举全部合法的两组和三组方案")
    left, right, top, bottom = 180, 1330, 210, 710
    parts=result['partitions']
    ymin=min(p['resource_total'] for p in parts)-1;ymax=max(p['resource_total'] for p in parts)+1
    xmin=floor(min(p['workload_cv'] for p in parts)*20)/20-.05
    xmax=ceil(max(p['workload_cv'] for p in parts)*20)/20+.05
    for v in range(ymin,ymax+1,2):
        y = bottom - (v-ymin)/(ymax-ymin) * (bottom - top)
        d.line((left, y, right, y), fill=GRID, width=2)
        d.text((112, y - 15), str(v), fill=MUTED, font=F22)
    for v in [xmin+i*(xmax-xmin)/5 for i in range(6)]:
        x = left + (v-xmin)/(xmax-xmin) * (right - left)
        d.line((x, top, x, bottom), fill=GRID, width=2)
        d.text((x - 27, bottom + 18), f"{v:.2f}", fill=MUTED, font=F18)
    d.text((590, 785), "工作量变异系数 CV →", fill=INK, font=F26)
    d.text((40, 175), "资源件数", fill=INK, font=F22)
    offsets = {"P1": (-98, -48), "P2": (-100, 16), "P3": (20, -42), "P4": (-100, -50)}
    for i, p in enumerate(result["partitions"]):
        x = left + (p["workload_cv"]-xmin)/(xmax-xmin) * (right - left)
        y = bottom - (p["resource_total"]-ymin)/(ymax-ymin) * (bottom - top)
        d.ellipse((x - 15, y - 15, x + 15, y + 15), fill=PALETTE[i%len(PALETTE)], outline="white", width=3)
        dx, dy = offsets.get(p['id'],(20,-35))
        d.text((x + dx, y + dy), f"{p['id']}  {p['resource_total']}件", fill=INK, font=B22)
    two=[p for p in parts if p['group_count']==2]
    d.text((72, 830), f"两组：{min(two,key=lambda p:p['resource_total'])['id']}资源最少；{min(two,key=lambda p:p['workload_cv'])['id']}最均衡。库存缺口允许保留并解释。", fill=MUTED, font=F22)
    im.save(output)


def battery_chart(result, plan, output):
    pid=result['recommended_partition']
    p3 = next(p for p in result["partitions"] if p["id"] == pid)
    gid=max(p3['groups'],key=lambda g:g['work_seconds'])['id']
    im, d = _canvas(f"{pid}/{gid}电池接续优化", "实线段为任务开始至充满；同一行对应同一组可复用电池", 1200)
    tasks = {t["id"]: t for t in plan["transport_sorties"]}
    assignment = [r for r in result["assignments"] if r["partition"] == pid and r["group"] == gid]
    start_x, end_x = 350, 1370
    all_time = max(t["battery_ready"] for t in tasks.values())
    scale = (end_x - start_x) / (all_time + 300)
    for section, model in enumerate(("B", "C")):
        y0 = 205 + section * 430
        d.text((75, y0 - 32), f"{model}型电池", fill=INK, font=B30)
        d.text((85, y0 + 8), "原编号", fill=MUTED, font=F18)
        original = defaultdict(list)
        selected_ids = {r["task"] for r in assignment if r["resource_type"] == f"{model}_batteries"}
        for tid in selected_ids:
            original[tasks[tid]["battery"]].append(tasks[tid])
        for j, (rid, jobs) in enumerate(sorted(original.items())):
            y = y0 + 37 + j * 27
            d.text((80, y - 7), rid, fill=MUTED, font=F18)
            for task in jobs:
                x0 = start_x + task["start"] * scale
                x1 = start_x + task["battery_ready"] * scale
                d.rounded_rectangle((x0, y - 4, x1, y + 13), radius=4, fill="#B6C4D2")
                d.text((x0 + 3, y - 9), task["id"], fill=INK, font=F18)
        yy = y0 + 160
        d.text((85, yy), "优化编号", fill=MUTED, font=F18)
        optimal = defaultdict(list)
        for row in assignment:
            if row["resource_type"] == f"{model}_batteries":
                optimal[row["resource_id"]].append(row)
        for j, (rid, jobs) in enumerate(sorted(optimal.items())):
            y = yy + 34 + j * 27
            d.text((80, y - 7), rid.replace("G1-", ""), fill=MUTED, font=F18)
            for row in jobs:
                x0 = start_x + row["start"] * scale
                x1 = start_x + row["release"] * scale
                d.rounded_rectangle((x0, y - 4, x1, y + 13), radius=4,
                                    fill=PALETTE[0] if model == "B" else PALETTE[2])
                d.text((x0 + 3, y - 9), row["task"], fill=INK, font=F18)
        d.line((70, y0 + 340, W - 70, y0 + 340), fill=GRID, width=2)
    d.line((start_x, 1003, end_x, 1003), fill=MUTED, width=2)
    for t in range(0, int(all_time) + 1, 1000):
        x = start_x + t * scale
        d.line((x, 997, x, 1009), fill=MUTED, width=2)
        d.text((x - 25, 1015), str(t), fill=MUTED, font=F18)
    d.text((1300, 970), "时间/s", fill=MUTED, font=F18)
    def slack(model):
        c=p3['certificates'][gid][model+'_batteries']
        values=[c['original_id_min_handoff_slack_seconds'],c['optimal_min_handoff_slack_seconds']]
        return '→'.join('无接续' if v is None else f'{v:.3f}' for v in values)
    d.text((75, 1090), f"B型最小接续余量 {slack('B')} s；C型 {slack('C')} s。", fill=INK, font=F22)
    im.save(output)


def stock_chart(result, output):
    im, d = _canvas("八类资源库存与独立分组需求", "设备数量按类型核算；库存总数不能抵消某一类型的缺口")
    left, right, top, bottom = 125, 1410, 245, 710
    labels = ["A机", "B机", "C机", "A电", "B电", "C电", "中继机", "中继组件"]
    colors = ["#7E91A9", *PALETTE]
    series = [("库存", result["stock"])] + [(p["id"], p["resource_need"]) for p in result["partitions"]]
    for n in range(9):
        y = bottom - n / 8 * (bottom - top)
        d.line((left, y, right, y), fill=GRID, width=1)
        d.text((87, y - 12), str(n), fill=MUTED, font=F18)
    band = (right - left) / 8
    bar = 24
    for i, (key, label) in enumerate(zip(RESOURCE_KEYS, labels)):
        x0 = left + i * band + 12
        for j, (_, values) in enumerate(series):
            v = values[key]
            x = x0 + j * 28
            y = bottom - v / 8 * (bottom - top)
            d.rounded_rectangle((x, y, x + bar, bottom), radius=3, fill=colors[j])
        d.text((x0 + 28, bottom + 18), label, fill=INK, font=F22)
    for i, (label, _) in enumerate(series):
        _legend(d, 200 + i * 225, 185, colors[i], label)
    selected=next(p for p in result['partitions'] if p['id']==result['recommended_partition'])
    d.text((75, 808), f"{selected['id']}需{selected['resource_total']}件，逐类型库存缺口合计{selected['shortage_total']}件；完整类型见交付表。", fill=INK, font=F26)
    im.save(output)


def create_all(result, plan, output: Path):
    component_chart(result, output / "q4_task_blocks.png")
    tradeoff_chart(result, output / "q4_resource_balance.png")
    battery_chart(result, plan, output / "q4_battery_handoffs.png")
    stock_chart(result, output / "q4_stock_vs_demand.png")

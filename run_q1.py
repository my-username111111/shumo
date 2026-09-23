"""Q1-only reproducible run: certificates, global frontier and independent audit."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import platform
from time import perf_counter

from model import BASE, Scenario
from q1 import safe_payload_status, solve_all, solve_zone
from q1_frontier import (ZonePatterns, budget_choice, count_proof, critical_analysis,
                         global_frontiers, pareto, recover)
from q1_reference import global_reference, global_reserve_thresholds
from transport_check import TransportReplay, close


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def table(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def fingerprint(path):
    h = sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def label_record(p):
    return dict(sorties=sum(len(ids) for z, ids in p.choices), energy_kwh=p.energy,
                work_seconds=p.seconds, choices=dict(p.choices))


def compare_frontier(actual, expected, label):
    if len(actual) != len(expected):
        raise AssertionError(f"{label}: different numbers of nondominated points: {len(actual)} vs {len(expected)}")
    for a, b in zip(actual, expected):
        close(a[0], b[0], f"{label} energy", 1e-8)
        close(a[1], b[1], f"{label} time", 1e-5)


def build_report(output, q1, proof, frontier, critical_counts, audit, limits, s):
    nominal = q1["totals"]
    text = ["# 第一问升级验收报告", "",
            "本次范围：公共运输计算与核验、第一问。第二至四问的搜索与调度算法未修改。", "",
            "## 结论与最优性范围", "",
            f"原默认方案仍成立：{nominal['sorties']} 架次，{nominal['energy_kwh']:.9f} kWh，累计作业时间 {nominal['work_seconds']:.6f} 秒。",
            "最优性限定于题目第一问的单服务区往返、整箱不可拆、确定性参数及当前能耗口径；时间是准备、装箱、往返飞行与交接时长之和，不是机队并行完工时间。",
            "水平能耗沿用 E_use·d/L(q)，爬升为 (机重+载荷)·g·爬升高度/(效率·3.6×10^6)，下降不单独加能耗。算法核验不等于对这一物理约定的实验验证。", "",
            "## 架次下界与可行证书", "",
            "每架次只服务一个区，因此 N ≥ Σ_i max{ceil(W_i/Q_max), ceil(V_i/V_max)}。每个区至少需要该数量的架次；不依赖任何搜索算法。",
            f"本数据 Q_max={proof['maximum_aircraft_payload_kg']:g} kg，下界 {proof['lower_bound']}；逐箱独立复算的可行上界 {proof['feasible_upper_bound']}；最优间隙 {proof['gap']}。", "",
            "| 服务区 | 总重 kg | 体积 m³ | 架次下界 | 达到的架次 |", "|---|---:|---:|---:|---:|"]
    text += [f"| {r['zone']} | {r['demand_kg']:g} | {r['demand_m3']:.3f} | {r['lower_bound']} | {r['feasible_sorties']} |" for r in proof["zones"]]
    text += ["", "## 全局时间—能耗权衡", "",
             f"穷尽可行架次数 {limits[0]}—{limits[1]}；固定每个架次数的前沿另存 q1_frontiers.json。跨架次数的全局非支配前沿有 {len(frontier)} 个目标点：", "",
             "| 架次 | 总能耗 kWh | 累计作业秒 | 累计作业小时 |", "|---:|---:|---:|---:|"]
    text += [f"| {p['sorties']} | {p['energy_kwh']:.9f} | {p['work_seconds']:.6f} | {p['work_seconds']/3600:.6f} |" for p in frontier]
    if len(frontier) == 2:
        a, b = frontier
        text += ["", f"选择较快方案增加 {b['energy_kwh']-a['energy_kwh']:.9f} kWh（相对最省能耗方案 {(b['energy_kwh']/a['energy_kwh']-1)*100:.6f}%），"
                 f"减少 {a['work_seconds']-b['work_seconds']:.6f} 秒（{(a['work_seconds']-b['work_seconds'])/60:.6f} 分钟）。",
                 "固定最少架次时若前沿只有一个点，应明确报告时间与能耗同向最优，不能画出虚构的连续折中曲线。"]
    text += ["", "能耗预算约束施加于全局总能耗：min ΣT，s.t. ΣE≤B；先保留每区全部非支配候选，再作全局卷积。没有按区域分摊固定百分比预算，也没有用加权和漏掉非凸前沿。",
             "各预算档的完整逐箱方案见 q1_budget_scenarios.json；目标对相同的多种箱号排列只保留一个证书。", "",
             "## 临界返航余量", "",
             "单个装载模式的临界值为 ρ*=1−E/E_use；ρ=ρ* 时仍可行，严格超过后失效。枚举所有模式事件并重优化，不以 10%、15%、20% 等稀疏采样代替临界分析。",
             "下表给出不同最小架次数的可行余量区间。每行左端开、右端闭；ρ=0另存证书。", "",
             "| 余量左端 %（开） | 余量右端 %（闭） | 最少架次 |", "|---:|---:|---:|"]
    text += [f"| {r['left_open']*100:.9f} | {r['right_closed']*100:.9f} | {r['sorties'] if r['sorties'] is not None else '不可完成全部货箱'} |" for r in critical_counts]
    text += ["", "本次原始数据的首个瓶颈是 S004：其 C 型59 kg单架次返航SOC为22.788615720%；当要求高于该值，18架次不再可能。绝对可行性瓶颈为 S008 的14 kg饮用水箱：所有可载机型中，最佳单箱往返SOC为35.104703262%，超过后该箱无法单点往返交付。具体事件和边界证书见 q1_critical_boundaries.json；更换数据后应以该机器可读文件为准。", "",
             "每个区的箱组/机型切换、所有候选临界事件、不可行区列表均保留在 q1_critical_reserve.json。默认方案逐架次的质量、体积、能量与 SOC 余量见 q1_transport_audit.csv。", "",
             "## 独立交叉核对", "",
             "1. 运输几何：主核采用格线交点分段遍历；核验器采用线段与栅格矩形相交计算。往返30条航段逐栅格集合、最高地形、距离和爬降高度一致。",
             "2. 能耗与时间：核验器单独实现剩余载荷、水平/爬升能耗、装载/交接时间，不调用主核的 route、flight_energy、flight_time、node_leg 或 DEM.peak。",
             "3. 优化：主算法按同重同体积箱计数建状态；参考算法按实际箱号位掩码建状态，用不同方向的支配扫描，核对全部全局 E/T 非支配点。",
             "4. 临界架次数：参考算法用 max–min（最大化最差架次 SOC）位掩码递推，独立算出每个精确架次数可达到的最大统一余量，再全局合并；核对所有区间及端点。",
             f"5. 已复算 {audit['frontier_certificates']} 份固定架次前沿证书、{audit['pattern_checks']} 个装载模式、{audit['critical_interval_checks']} 个全局余量区间；默认两种目标的逐区位掩码核对 {audit['scalar_crosschecks']} 次。", "",
             "浮点核验：目标能耗容差 10^-9 kWh、时间容差 10^-6 s；交叉实现汇总允许 10^-8 kWh/10^-5 s。几何格线容差 10^-9 像素。架次下界是整数证明；非线性能耗数值及临界小数值为上述精度下的穷举结果，不是符号精确算术。", "",
             "主核现在对穿越任意 NoData/非有限高程报错，覆盖沿格线飞行的两侧栅格，并检查重复编号与运输参数。共享运输核验还检查实体无人机、电池编号和型号以及充电时长。通信与中继逻辑不在本次验收范围。", "",
             "## 复现与产物", "",
             "运行：`python run_q1.py --data-dir <题目数据目录> --output <结果目录>`。仅运行第一问，无新增第三方依赖。",
             "测试：`python -m unittest discover -s tests -v`。原始数据、代码 SHA-256 和运行版本见 manifest.json。",
             "关键文件：q1.json（原目标方案），q1_optimality.json（上下界），q1_patterns.json（模式与箱号映射），q1_frontiers.json（全架次前沿证书），q1_global_frontier.csv，q1_budget_scenarios.json，q1_critical_reserve.json，q1_critical_count.csv，q1_validation.json。", ""]
    (output / "第一问升级报告.md").write_text("\n".join(text), encoding="utf-8")


def run(data_dir, output, reserve_min=0.0, reserve_max=1.0, q2_baseline=None):
    started = perf_counter()
    output.mkdir(parents=True, exist_ok=True)
    log = lambda text: print(text, flush=True)
    s = Scenario(data_dir)
    replay = TransportReplay(s)
    log("Audit terrain and original Q1 objective")
    geometry = replay.geometry_audit()
    q1 = solve_all(s, sensitivity=False)
    verified = replay.check_q1(q1["zone_plans"])
    for k in ("sorties", "energy_kwh", "work_seconds"):
        close(verified[k], q1["totals"][k], k)
    proof = count_proof(s, q1["zone_plans"])
    if not proof["globally_optimal_count"]:
        raise AssertionError("Simple lower bound not attained; cannot issue optimality certificate")
    zones = {z: ZonePatterns(s, z) for z in sorted(s.zone_boxes)}
    log("Enumerate all feasible sortie counts and global frontiers")
    nmin, frontiers, local = global_frontiers(zones, len(s.boxes))
    global_rows = pareto([p for rows in frontiers.values() for p in rows])
    frontier_checks = 0
    for n, rows in frontiers.items():
        for p in rows:
            actual = replay.check_q1(recover(zones, p))
            close(actual["energy_kwh"], p.energy, "frontier energy")
            close(actual["work_seconds"], p.seconds, "frontier time")
            if actual["sorties"] != n:
                raise AssertionError("Frontier count mismatch")
            frontier_checks += 1
    pattern_checks = 0
    for z, patterns in zones.items():
        for p in patterns.patterns:
            ids = [b for group, n in zip(patterns.ids, p.counts) for b in group[:n]]
            result = replay.evaluate(p.model, [(z, ids)], reserve=0)
            close(result["energy_kwh"], p.energy, "pattern energy")
            close(result["duration"], p.duration, "pattern time")
            pattern_checks += 1
    scalar_checks = []
    for z, patterns in zones.items():
        for objective in ("energy", "time"):
            box_result = solve_zone(s, z, objective=objective)
            grouped = patterns.scalar(objective=objective)
            if box_result["sorties"] != grouped[0]:
                raise AssertionError("Independent state-space count mismatch")
            close(box_result["energy_kwh"], grouped[1], "scalar energy")
            close(box_result["work_seconds"], grouped[2], "scalar time")
            scalar_checks.append(dict(zone=z, objective=objective, passed=True))
    log("Independent individual-box E/T frontier crosscheck")
    reference, reference_local = global_reference(s, replay, progress=log)
    compare_frontier([(p.energy, p.seconds) for p in global_rows], reference, "global")
    for z in zones:
        expected = reference_local[z]
        actual = pareto([p for rows in local[z].values() for p in rows])
        compare_frontier([(p.energy, p.seconds) for p in actual], expected, z)
    log("All critical-reserve events and independent max-min oracle")
    critical = critical_analysis(zones, reserve_min, reserve_max, progress=log)
    thresholds, zone_thresholds = global_reserve_thresholds(s, replay, progress=log)
    lower = critical["lower_endpoint"]
    attainable = [n for n,r in thresholds.items() if reserve_min <= r + 1e-10]
    if lower["sorties"] != (min(attainable) if attainable else None):
        raise AssertionError("Lower endpoint reserve count mismatch")
    if lower["feasible"]:
        replay.check_q1({z:zones[z].plan(ids) for z,ids in lower["choices"].items()}, reserve_min)
    for row in critical["global_intervals"]:
        for rho in ((row["left_open"] + row["right_closed"]) / 2, row["right_closed"]):
            attainable = [n for n, r in thresholds.items() if rho <= r + 1e-10]
            expected = min(attainable) if attainable else None
            if row["sorties"] != expected:
                raise AssertionError(f"Independent critical count mismatch at {rho}")
        if row["feasible"]:
            plans = {z: zones[z].plan(ids) for z, ids in row["choices"].items()}
            actual = replay.check_q1(plans, reserve=row["right_closed"])
            close(actual["energy_kwh"], row["energy_kwh"], "critical energy")
            close(actual["work_seconds"], row["work_seconds"], "critical time")
    # Also verify local intervals after the whole instance has become infeasible.
    for z, data in critical["zone_results"].items():
        for row in data["intervals"]:
            for rho in ((row["left"] + row["right"]) / 2, row["right"]):
                attainable = [n for n, r in zone_thresholds[z].items() if rho <= r + 1e-10]
                expected = min(attainable) if attainable else None
                count = None if row["result"] is None else row["result"][0]
                if expected != count:
                    raise AssertionError(f"Local critical count mismatch: {z}, {rho}")
            if row["result"] is not None:
                for batch in zones[z].plan(row["result"][3])["batches"]:
                    replay.evaluate(batch["model"], [(z, batch["box_ids"])], row["right"])
    count_rows = []
    for row in critical["global_intervals"]:
        if count_rows and count_rows[-1]["sorties"] == row["sorties"]:
            count_rows[-1]["right_closed"] = row["right_closed"]
        else:
            count_rows.append({k: row[k] for k in ("left_open", "right_closed", "sorties")})
    boundary_rows = []
    for current, after in zip(count_rows, count_rows[1:]):
        rho = current["right_closed"]
        next_events = [p.critical_reserve for v in zones.values() for p in v.patterns
                       if p.critical_reserve > rho + 1e-10]
        above = rho + min(1e-7, (min(next_events, default=1.0)-rho)/4)
        before_results = {z:v.scalar(rho) for z,v in zones.items()}
        after_results = {z:v.scalar(above) for z,v in zones.items()}
        changed = [z for z in zones if (None if before_results[z] is None else before_results[z][0]) !=
                   (None if after_results[z] is None else after_results[z][0])]
        witnesses = {z:zones[z].plan(v[3]) for z,v in before_results.items() if v is not None}
        replay.check_q1(witnesses,rho)
        boundary_rows.append(dict(reserve=rho, just_above=above, at_boundary_sorties=current["sorties"],
                                  above_boundary_sorties=after["sorties"], affected_zones=changed,
                                  at_boundary_plan=witnesses,
                                  limiting_patterns=[e for e in critical["pattern_events"] if
                                                     e["zone"] in changed and abs(e["critical_reserve"]-rho)<=1e-10]))
    # Budget is global. Separate count-first policy from unrestricted E/T policy.
    budgets = {}
    for policy, rows in (("minimum_sorties", frontiers[nmin]), ("unrestricted_sorties", global_rows)):
        minimum_energy = rows[0].energy
        scenarios = []
        for epsilon in (0.0, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05):
            budget = minimum_energy * (1 + epsilon)
            p = budget_choice(rows, budget)
            scenarios.append(dict(epsilon=epsilon, energy_budget_kwh=budget,
                                  **label_record(p), zone_plans=recover(zones, p)))
        budgets[policy] = scenarios
    batch_audits = []
    for z, plan in q1["zone_plans"].items():
        for i, b in enumerate(plan["batches"], 1):
            t = s.transport[b["model"]]
            evaluated = replay.evaluate(b["model"], [(z, b["box_ids"])])
            batch_audits.append(dict(zone=z, batch=i, model=b["model"], boxes=";".join(b["box_ids"]),
                                     load_kg=b["kg"], mass_slack_kg=t.max_kg-b["kg"], load_m3=b["volume"],
                                     volume_slack_m3=t.max_volume-b["volume"], energy_kwh=b["energy"],
                                     horizontal_kwh=sum(l["horizontal_kwh"] for l in evaluated["legs"]),
                                     climb_kwh=sum(l["climb_kwh"] for l in evaluated["legs"]),
                                     energy_slack_kwh=(1-t.reserve)*t.battery_kwh-b["energy"],
                                     return_soc=b["return_soc"], soc_slack=b["return_soc"]-t.reserve,
                                     duration_s=b["duration"]))
    payloads = [dict(zone=z, model=g, **safe_payload_status(s,z,g)) for z in zones for g in s.transport]
    audit = dict(passed=True, geometry_legs=len(geometry), baseline=verified,
                 frontier_certificates=frontier_checks, pattern_checks=pattern_checks,
                 scalar_crosschecks=len(scalar_checks), scalar_results=scalar_checks,
                 global_frontier_reference=reference, critical_interval_checks=len(critical["global_intervals"]),
                 independent_reserve_thresholds=thresholds, independent_zone_thresholds=zone_thresholds)
    if q2_baseline:
        from verify import verify_q2
        audit["existing_q2_replay"] = verify_q2(s, json.loads(q2_baseline.read_text(encoding="utf-8")))
    save(output / "q1.json", q1)
    save(output / "q1_optimality.json", proof)
    save(output / "q1_patterns.json", {z: dict(group_keys=p.keys, box_ids=p.ids, demand=p.demand,
                                               patterns=[asdict(v) for v in p.patterns]) for z, p in zones.items()})
    save(output / "q1_frontiers.json", dict(minimum_sorties=nmin, maximum_sorties=max(frontiers),
                                           by_count={n: [label_record(p) for p in rows] for n, rows in frontiers.items()},
                                           global_frontier=[dict(**label_record(p), zone_plans=recover(zones,p)) for p in global_rows]))
    save(output / "q1_budget_scenarios.json", budgets)
    save(output / "q1_critical_reserve.json", critical)
    save(output / "q1_critical_boundaries.json", boundary_rows)
    save(output / "q1_validation.json", audit)
    table(output / "q1_global_frontier.csv", [{k: v for k,v in label_record(p).items() if k != "choices"} for p in global_rows])
    table(output / "q1_transport_audit.csv", batch_audits)
    table(output / "q1_critical_count.csv", count_rows)
    table(output / "q1_safe_payload.csv", payloads)
    table(output / "q1_geometry_audit.csv", geometry)
    root = Path(__file__).resolve().parent
    manifest = dict(python=platform.python_version(), elapsed_seconds=perf_counter()-started,
                    data_directory_label=data_dir.name, reserve_range=[reserve_min,reserve_max],
                    input_sha256={str(p.relative_to(data_dir)):fingerprint(p) for p in sorted(data_dir.rglob("*"))
                                  if p.is_file() and p.suffix.lower() in (".xlsx", ".tif")},
                    code_sha256={str(p.relative_to(root)):fingerprint(p) for p in
                                 sorted([*root.glob("*.py"), *(root/"tests").glob("*.py")])})
    save(output / "manifest.json", manifest)
    build_report(output,q1,proof,[label_record(p) for p in global_rows],count_rows,audit,(min(frontiers),max(frontiers)),s)
    log(json.dumps(dict(baseline=q1["totals"], global_frontier=[label_record(p) for p in global_rows],
                        frontier_certificates=frontier_checks, reserve_count_regimes=count_rows), ensure_ascii=False, indent=2))
    log(f"Saved Q1-only artifacts to {output}")
    return audit


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "results_q1")
    parser.add_argument("--reserve-min", type=float, default=0.0)
    parser.add_argument("--reserve-max", type=float, default=1.0)
    parser.add_argument("--q2-baseline", type=Path, help="Optional read-only replay of an existing Q2 JSON")
    args = parser.parse_args()
    run(args.data_dir,args.output,args.reserve_min,args.reserve_max,args.q2_baseline)

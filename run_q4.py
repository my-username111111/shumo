"""Execute the Q4 v2 design on the saved, continuously certified Q3 plan."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from model import Scenario
from q4 import RESOURCE_KEYS
from q4_audit import audit
from q4_exact import load_and_solve
from q4_figures import create_all


ROOT = Path(__file__).resolve().parent
DEFAULT_PLAN = ROOT / "results_q3_complete" / "q3_plan.json"
DEFAULT_OUTPUT = ROOT / "results_q4"
EXPECTED_Q3_SHA256 = "DF5DBF182CEA219A3EB173D86CE411ACD339AD7B9ED93BB1B4D0F5BC23F0F181"


def write_json(path: Path, item) -> None:
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def partition_rows(result: dict) -> list[dict]:
    rows = []
    for p in result["partitions"]:
        row = dict(partition=p["id"], group_count=p["group_count"],
                   group_components=" / ".join("+".join(f"C{i}" for i in g["component_indices"])
                                                for g in p["groups"]),
                   zones_per_group=" / ".join(str(len(g["zones"])) for g in p["groups"]),
                   boxes_per_group=" / ".join(str(g["boxes"]) for g in p["groups"]),
                   work_seconds_per_group=" / ".join(f"{g['work_seconds']:.6f}" for g in p["groups"]),
                   workload_cv=p["workload_cv"], resource_total=p["resource_total"],
                   shortage_total=p["shortage_total"],
                   structural_legal=p["structural_legal"],
                   assignment_replay_passed=p["assignment_replay_passed"],
                   stock_sufficient=p["stock_sufficient"],
                   executable_after_procurement=p["executable_after_procurement"])
        for key in RESOURCE_KEYS:
            row[f"need_{key}"] = p["resource_need"][key]
            row[f"shortage_{key}"] = p["shortage"][key]
            row[f"sharing_loss_{key}"] = p["sharing_loss"][key]
        rows.append(row)
    return rows


def group_rows(result: dict) -> list[dict]:
    rows = []
    for p in result["partitions"]:
        for g in p["groups"]:
            row = dict(partition=p["id"], group=g["id"],
                       components="+".join(f"C{i}" for i in g["component_indices"]),
                       zones="|".join(g["zones"]), boxes=g["boxes"],
                       cargo_kg=g["cargo_kg"], transport_sorties=g["transport_sorties"],
                       relay_sorties=g["relay_sorties"], work_seconds=g["work_seconds"],
                       energy_kwh=g["energy_kwh"], horizon_seconds=g["horizon_seconds"])
            for key in RESOURCE_KEYS:
                row[f"need_{key}"] = g["resource_need"][key]
                row[f"occupancy_{key}"] = g["resource_occupancy_ratio"][key]
            rows.append(row)
    return rows


def candidate_rows(scenario: Scenario, directory: Path) -> list[dict]:
    rows = []
    for path in sorted(directory.glob("*.json")):
        plan = json.loads(path.read_text(encoding="utf-8"))
        solved = load_and_solve(scenario, path)
        checked = audit(scenario, plan, solved)
        if checked["status"] != "PASS":
            raise AssertionError(f"Q4 candidate replay failed: {path.name}")
        two = [p for p in solved["partitions"] if p["group_count"] == 2]
        three = [p for p in solved["partitions"] if p["group_count"] == 3]
        best_two = min(two, key=lambda p: (p["shortage_total"], p["resource_total"], p["id"]))
        best_three = min(three, key=lambda p: (p["shortage_total"], p["resource_total"], p["id"]))
        objective = plan["objective"]
        rows.append(dict(candidate=path.stem, sha256=solved["components"]["input_sha256"],
                         component_count=len(solved["components"]["components"]),
                         weighted_soft_delay=objective["weighted_soft_delay"],
                         makespan_seconds=objective["makespan"],
                         weighted_delivery_seconds=objective["weighted_delivery_seconds"],
                         energy_kwh=objective["energy_kwh"],
                         total_sorties=objective["total_sorties"],
                         two_group_best_partition=best_two["id"],
                         two_group_shortage=best_two["shortage_total"],
                         three_group_best_partition=best_three["id"],
                         three_group_shortage=best_three["shortage_total"],
                         q4_replay_status=checked["status"]))
    return rows


def _vector(values: dict) -> str:
    return "(" + ",".join(str(values[key]) for key in RESOURCE_KEYS) + ")"


def _fmt_slack(value) -> str:
    return "不适用" if value is None else f"{value:.6f}"


def report(result: dict, validation: dict, source: Path, plan: dict, candidates: list[dict]) -> str:
    partitions = {p["id"]: p for p in result["partitions"]}
    p3 = partitions["P3"]
    components = result["components"]["components"]
    component_work = result["components"]["component_work_seconds"]
    lines = [
        "# 第四问交付说明：固定第三问后的精确分区与资源接续优化",
        "",
        f"输入：`{source.as_posix()}`；SHA256：`{result['components']['input_sha256']}`。",
        f"输入是已连续通信认证的第三问主方案；保持{result['components']['box_count']}箱、"
        f"{result['components']['transport_task_count']}架运输任务、"
        f"{result['components']['relay_task_count']}架中继任务、任务时刻和实际通信关系不变。",
        "本次仅决定服务区分组，以及各组内部同类型机身、电池和能源组件的具体编号与接续。",
        "资源可互换、初始满电、再次投入前充满，充电可并行；中继机占用计至300秒周转完成。",
        "",
        "## 任务块与全部合法分区",
        "",
        "运输架次连接其访问服务区；中继架次连接它实际保障的运输架次所访问服务区。",
        "对这些超边取传递闭包，得到：",
        "",
        "| 块 | 服务区 | 箱数 | 工作量/s |",
        "|---|---|---:|---:|",
    ]
    for i, (zones, work) in enumerate(zip(components, component_work), 1):
        boxes = result["components"]["component_box_counts"][i - 1]
        lines.append(f"| C{i} | {', '.join(zones)} | {boxes} | {work:.6f} |")
    lines += [
        "",
        "工作量为运输和中继任务从准备开始至返航的时长之和，同一中继任务只计一次，不含充电时间。",
        "组标签互换视为相同方案；3个任务块恰有3个两组方案和1个三组方案。",
        "",
        "八维向量顺序：A/B/C运输机，A/B/C电池，中继机，中继能源组件。",
        f"库存为 {_vector(result['stock'])}，统一执行最低需求为 {_vector(result['pooled_resource_need'])}。",
        "",
        "| 方案 | 分组 | 最低需求 | 合计 | 缺口 | 缺口合计 | 工作量CV | 库存足够 |",
        "|---|---|---|---:|---|---:|---:|---|",
    ]
    for p in result["partitions"]:
        grouping = " / ".join("+".join(f"C{i}" for i in g["component_indices"])
                              for g in p["groups"])
        lines.append(f"| {p['id']} | {grouping} | {_vector(p['resource_need'])} | "
                     f"{p['resource_total']} | {_vector(p['shortage'])} | "
                     f"{p['shortage_total']} | {p['workload_cv']:.9f} | "
                     f"{'是' if p['stock_sufficient'] else '否'} |")
    lines += [
        "",
        "在两组方案中，以新增资源总件数最少作为明确政策，推荐P3。",
        "P3缺1架B型运输机和1组B型电池；即使总需求29件小于库存总数30件，现有库存仍无法直接执行。",
        "如B型资源难以采购，P1没有B型缺口，但总缺口增至6件；P2是两组中最均衡的方案。",
        "P4是唯一合法三组方案，不能通过拆开C2来人为改善均衡。",
        "",
        "P3各组具体工作量与配置：",
        "",
        "| 组 | 任务块 | 服务区数 | 箱数 | 质量/kg | 运输/中继架次 | 工作量/s | 资源向量 |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for g in p3["groups"]:
        lines.append(f"| {g['id']} | {'+'.join(f'C{i}' for i in g['component_indices'])} | "
                     f"{len(g['zones'])} | {g['boxes']} | {g['cargo_kg']:.3f} | "
                     f"{g['transport_sorties']}/{g['relay_sorties']} | {g['work_seconds']:.6f} | "
                     f"{_vector(g['resource_need'])} |")
    lines += [
        "",
        "## 最低配置及接续最优性",
        "",
        "每组每类资源的最低数等于半开占用区间的最大同时占用数。",
        "独立建立任务接续二分图：前任务释放不晚于后任务开始时才可连边；",
        "最少接续链数为任务数减最大匹配数。全部组与八类资源的两种算法一致。",
        "峰值时刻、同时占用任务、最大匹配大小和完整接续链见 `q4_optimality_certificate.json`；",
        "具体实体编号、任务起止和接续余量见 `q4_resource_assignments.csv`。",
        "各组箱数、质量、架次、能耗和八类占用率见 `q4_group_detail.csv`；"
        "占用率分母为该组首任务开始至全部资源最后释放的时长乘该类最低资源数。",
        "",
        "固定最低件数后，对接续边的最小余量做阈值二分，每个阈值重新求最大匹配。",
        "P3大组G1的关键电池结果：",
        "",
        "| 电池 | 数量 | 原编号最小余量/s | 优化后最小余量/s | 改善/s | 下一阈值匹配数 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key in ("B_batteries", "C_batteries"):
        c = p3["certificates"]["G1"][key]
        before = c["original_id_min_handoff_slack_seconds"]
        after = c["optimal_min_handoff_slack_seconds"]
        lines.append(f"| {key} | {c['minimum_resources']} | {_fmt_slack(before)} | "
                     f"{_fmt_slack(after)} | {after-before:.6f} | "
                     f"{c['matching_size_at_next_threshold']} |")
    lines += ["", "对应的优化接续链：", "", "| 资源 | 任务链 | 接续余量/s |", "|---|---|---|"]
    for key in ("B_batteries", "C_batteries"):
        c = p3["certificates"]["G1"][key]
        by_edge = {(x["predecessor"], x["successor"]): x["slack_seconds"]
                   for x in c["matching_edges"]}
        for number, chain in enumerate(c["chains"], 1):
            gaps = [by_edge[a, b] for a, b in zip(chain, chain[1:])]
            lines.append(f"| G1-{key}-{number:02d} | {' → '.join(chain)} | "
                         f"{', '.join(f'{v:.6f}' for v in gaps) if gaps else '不适用'} |")
    a_slack = p3["certificates"]["G1"]["A_aircraft"]["optimal_min_handoff_slack_seconds"]
    lines += [
        "",
        f"电池接续余量的改善不代表全系统对延误已有同等缓冲：P3大组A型机的最小接续余量仍只有{a_slack:.6f}秒。",
        "",
        "## 工作量下界与资源—均衡权衡",
        "",
        f"总工作量为{result['total_work_seconds']:.6f}秒；最大任务块占比"
        f"{result['dominant_component_fraction']:.9f}。",
        "以总体标准差定义工作量CV，K组的放松下界为"
        "(Kα−1)/√(K−1)。",
        f"两组下界{result['workload_cv_lower_bound']['2']:.9f}，由P2达到；"
        f"三组下界{result['workload_cv_lower_bound']['3']:.9f}，唯一合法P4实际为"
        f"{partitions['P4']['workload_cv']:.9f}。",
        "",
        "若限制两组工作量CV不超过给定阈值，同时使资源件数最少，则：",
        "",
        "| CV上限范围 | 方案 | 最低资源件数 |",
        "|---|---|---:|",
    ]
    steps = result["balance_threshold_policy"]
    lines.append(f"| ε < {steps[0]['maximum_cv']:.9f} | 无合法方案 | — |")
    for i, step in enumerate(steps):
        upper = f"ε < {steps[i+1]['maximum_cv']:.9f}" if i + 1 < len(steps) else "无上限"
        pid = step["least_resource_partition"]
        lines.append(f"| {step['maximum_cv']:.9f} ≤ ε，{upper} | {pid} | "
                     f"{partitions[pid]['resource_total']} |")
    lines += [
        "",
        "资源件数是等权统计，不代表采购金额。若有实际价格，应使用八类缺口分别核算。",
        "P3的新增成本为`B型机单价+B型电池单价`；P1为"
        "`A型机单价+2×C型机单价+A型电池单价+2×C型电池单价`。"
        "这给出两方案随采购价格切换的条件。",
        "分组独立运行损失了不同时间段之间的设备复用机会；相对统一执行的27件，",
        "P1/P2/P3/P4分别增加6/8/2/8件。",
        "",
        "## 已保存Q3候选的后验比较",
        "",
        f"逐一按同样的第四问口径复算并回放已保存的{len(candidates)}份Q3候选，"
        "详细目标与缺口见 `q4_q3_candidate_comparison.csv`。",
    ]
    if candidates:
        compatible = next((c for c in candidates if c["candidate"] == "q4_compatible"), None)
        if compatible is not None:
            baseline = plan["objective"]
            lines += [
                f"所有候选均产生{candidates[0]['component_count']}个任务块；其中单独保存的"
                f"`q4_compatible` 将两组最低缺口从P3的2件降至"
                f"{compatible['two_group_shortage']}件，三组缺口从P4的8件降至"
                f"{compatible['three_group_shortage']}件。",
                f"但其Q3最晚返航比当前主方案增加"
                f"{compatible['makespan_seconds']-baseline['makespan']:.3f}秒，"
                f"总架次增加{compatible['total_sorties']-baseline['total_sorties']}，"
                f"总能耗增加{compatible['energy_kwh']-baseline['energy_kwh']:.6f} kWh，"
                f"加权交付时刻增加"
                f"{compatible['weighted_delivery_seconds']-baseline['weighted_delivery_seconds']:.3f}。",
                "这些是更换Q3输入的后验实验结果，不能当作固定本次Q3后的第四问改进。",
            ]
    lines += [
        "",
        "## 当前问题与后续改进",
        "",
        "1. **库存不足。** P1/P2/P3/P4的逐类型缺口分别为6/8/2/8件，现有库存下四方案均不能直接执行。"
        "P3至少需补1架B型运输机和1组B型电池；补齐前不得把`executable_after_procurement=True`解释为库存可执行。",
        "2. **任务块失衡。** C2占总工作量约90.34%；固定Q3下两组CV不可能低于0.806817，"
        "P3为0.958162。改变这一结构必须另行修改Q3任务和中继关系，并重新验证通信、能耗和时限。",
        "3. **关键机身接续脆弱。** P3大组A型机的最小余量仅0.050585秒。"
        "电池余量改善只是局部改进；应对准备、飞行、返航和充电延误做扰动回放，"
        "评估增配或调整Q3任务时刻的代价。",
        "4. **最优余量证书尚未独立复核。** 当前回放独立重算区间峰值并检查全部任务链，"
        "但未用第二套算法重算证书中的最大最小接续余量及下一阈值匹配数。"
        "改变这些证书字段而保持任务链不变，回放仍可返回PASS；"
        "因此`q4_validation.json`的PASS不能单独作为这些最优值的独立验收。",
        "5. **实体资源映射未落地。** `G1-B_batteries-01`等是组内规划编号，"
        "尚未逐一对应现有U系列机身、电池和新增资源的实物编号。"
        "正式执行前应形成采购、配组和实物编号映射表并再次回放。",
        "6. **推荐取决于采购政策。** P3按资源总件数最少推荐，尚无实际价格或供应约束。"
        "若B型资源昂贵或缺货，应依据上文价格条件比较P1等备选，不能把P3写成唯一经济最优。",
        "7. **程序与输入边界。** `run_q4.py`锁定本文Q3输入SHA256，Q3更新后需重新生成四方案。"
        "完整结果经独立`run_q4.py`生成，原`run.py`仍输出旧Q4预览；"
        "当前执行入口还依赖`results_q3_complete/plans`目录提供11份候选对照。",
        "",
        "## 回放与适用范围",
        "",
        f"独立回放状态：**{validation['status']}**；四方案资源分配记录共"
        f"{validation['assignment_rows']}行。回放逐任务检查一次分配、型号、区间不重叠、",
        "电池或能源组件充满后复用、组间不共享、箱与任务守恒，以及实际通信关系。",
        "`q4_validation.json` 保存逐方案结论。结构合法、回放通过、库存满足和补齐缺口后可执行分别记录。",
        "结论只针对上述固定Q3输入与同类资源可互换的政策；没有改变第三问路线或声称Q3/Q4联合全局最优。",
        "",
        "## 图表",
        "",
        "![任务块](q4_task_blocks.png)",
        "",
        "![资源与均衡](q4_resource_balance.png)",
        "",
        "![P3电池接续](q4_battery_handoffs.png)",
        "",
        "![库存与需求](q4_stock_vs_demand.png)",
        "",
        "运行：`python run_q4.py --plan results_q3_complete/q3_plan.json --output results_q4`。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    scenario = Scenario()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    result = load_and_solve(scenario, args.plan)
    if result["components"]["input_sha256"] != EXPECTED_Q3_SHA256:
        raise ValueError("Q4 v2 delivery is locked to the documented Q3 input SHA256")
    validation = audit(scenario, plan, result)
    for partition in result["partitions"]:
        partition["assignment_replay_passed"] = validation["partitions"][partition["id"]]["status"] == "PASS"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    candidates = candidate_rows(scenario, ROOT / "results_q3_complete" / "plans")
    write_json(output / "q4_components.json", result["components"])
    rows = partition_rows(result)
    write_csv(output / "q4_all_partitions.csv", rows, list(rows[0]))
    details = group_rows(result)
    write_csv(output / "q4_group_detail.csv", details, list(details[0]))
    write_csv(output / "q4_q3_candidate_comparison.csv", candidates, list(candidates[0]))
    assignment_fields = ["partition", "group", "resource_type", "resource_id", "task",
                         "start", "release", "next_task", "handoff_slack_seconds"]
    write_csv(output / "q4_resource_assignments.csv", result["assignments"], assignment_fields)
    write_json(output / "q4_optimality_certificate.json", {
        p["id"]: p["certificates"] for p in result["partitions"]})
    write_json(output / "q4_validation.json", validation)
    write_json(output / "q4_results.json", {k: v for k, v in result.items() if k != "assignments"})
    create_all(result, plan, output)
    (output / "Q4_交付说明.md").write_text(report(result, validation, args.plan, plan, candidates), encoding="utf-8")
    print(json.dumps(dict(status=validation["status"], input_sha256=result["components"]["input_sha256"],
                          recommended=result["recommended_partition"],
                          partitions=[dict(id=p["id"], needs=p["resource_need"],
                                           shortage=p["shortage"], cv=p["workload_cv"])
                                      for p in result["partitions"]], output=str(output)),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

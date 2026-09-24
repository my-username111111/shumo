"""Summarize an already completed Q3 experiment without rerunning the search."""
from pathlib import Path
import argparse
import json
import csv
from statistics import median
from collections import Counter

from model import Scenario
from q3_certificate import accept, legacy_view, certify
from run_q3_upgrade import write_csv, sha256
from q3_upgrade import dump_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path(__file__).parent/'results_q3_complete')
    args=parser.parse_args();out=args.output.resolve();root=Path(__file__).resolve().parent
    plan=json.loads((out/'q3_plan.json').read_text(encoding='utf-8'))
    history=json.loads((out/'q3_search_history.json').read_text(encoding='utf-8'))
    manifest=json.loads((out/'manifest.json').read_text(encoding='utf-8'))
    s=Scenario();accept(s,plan,plan.get('extra_loss_db',0.))
    robust_path=out/'q3_robustness.csv'
    if robust_path.exists():
        with robust_path.open(encoding='utf-8-sig',newline='') as file: robust=list(csv.DictReader(file))
        witnesses=[]
        for row in robust:
            _,comm,cert=certify(s,plan,float(row['extra_loss_db']),stop_on_failure=True)
            row['fixed_plan_status']=cert['status'];row['fixed_unknown_seconds']=comm.get('unknown_seconds')
            witnesses.append(cert)
        write_csv(robust_path,robust)
        dump_json(out/'q3_fixed_plan_robustness.json',witnesses)
    from q3_independent_audit import audit
    diagnostic=audit(s,plan)
    if diagnostic['status']!='PASS':raise AssertionError('Dense diagnostic replay failed')
    dump_json(out/'q3_sample_diagnostics.json',diagnostic)
    from figures import route_map, timeline
    route_map(s,legacy_view(plan),out/'q3_route_map.png')
    timeline(s,legacy_view(plan),out/'q3_resource_timeline.png')
    old=json.loads((root/'results_q3_upgrade/q3_plan.json').read_text(encoding='utf-8'))
    allplans={p.stem:json.loads(p.read_text(encoding='utf-8')) for p in (out/'plans').glob('*.json')}
    from run_q3_joint import key, KEYS
    compatibility=json.loads((out/'q3_q4_compatibility.json').read_text(encoding='utf-8'))
    eligible=[p for p in allplans.values() if p.get('extra_loss_db',0.)==0. and p['q4_compatibility']['component_count']>=3]
    compatible=min(eligible,key=key) if eligible else None
    compatibility['selected_already_supports_three_groups']=plan['q4_compatibility']['component_count']>=3
    compatibility['best_observed_compatible']=(dict(label=compatible['q2_seed'],objective=compatible['objective'],
        delta_from_selected={k:compatible['objective'][k]-plan['objective'][k] for k in KEYS}) if compatible else None)
    compatibility['comparison_note']='The separately constrained branch is a controlled experiment, not the minimum necessary compatibility cost.'
    dump_json(out/'q3_q4_compatibility.json',compatibility)
    from q4 import solve as solve_q4
    from verify import verify_q4
    q4=solve_q4(s,legacy_view(plan));verify_q4(s,legacy_view(plan),q4)
    dump_json(out/'q3_q4_partition_preview.json',q4)
    q4_table='\n'.join(f"| {k} | {sum(v['shortage_minimum_relabelled'][g+'_aircraft'] for g in ('A','B','C'))} | {sum(v['shortage_minimum_relabelled'][g+'_batteries'] for g in ('A','B','C'))} | {v['workload_cv']:.6f} |"
                        for k,v in q4['partitions'].items() if v['feasible'])
    summary=[]
    for name in ('baseline_routes','balanced_24','delivery_23','fastest_25'):
        attempts=[r for r in history['runs'] if r['label'].startswith(name+'_')]
        passed=[allplans[r['label']] for r in attempts if r['label'] in allplans]
        summary.append(dict(source=name,attempts=len(attempts),feasible=len(passed),
            feasible_rate=len(passed)/max(1,len(attempts)),
            best_weighted_delivery=min((p['objective']['weighted_delivery_seconds'] for p in passed),default=None),
            median_weighted_delivery=median([p['objective']['weighted_delivery_seconds'] for p in passed]) if passed else None,
            median_elapsed_seconds=median([r.get('elapsed_seconds',0) for r in attempts]) if attempts else None))
    write_csv(out/'q3_multiseed_summary.csv',summary)
    resources=[]
    for g in s.transport:
        rows=[r for r in plan['transport_sorties'] if r['model']==g]
        resources.append(dict(kind=g,sorties=len(rows),aircraft_used=len({r['drone'] for r in rows}),
            aircraft_inventory=sum(v==g for v in s.aircraft.values()),
            energy_units_used=len({r['battery'] for r in rows}),energy_units_inventory=s.battery_count[g],
            energy_kwh=sum(r['energy_kwh'] for r in rows)))
    rr=plan['relay_sorties']
    resources.append(dict(kind='relay',sorties=len(rr),aircraft_used=len({r['relay'] for r in rr}),
        aircraft_inventory=len(s.relays),energy_units_used=len({r['energy_component'] for r in rr}),
        energy_units_inventory=s.relay_energy_count,energy_kwh=sum(r['energy_kwh'] for r in rr)))
    write_csv(out/'q3_resource_summary.csv',resources)
    deadlines=[dict(box_id=b,zone=s.boxes[b].zone,delivery=d['time'],
        desired=s.boxes[b].desired,hard_deadline=s.boxes[b].hard_deadline,
        hard_slack=s.boxes[b].hard_deadline-d['time'] if s.boxes[b].hard_deadline is not None else None,
        soft_delay=max(0,d['time']-s.boxes[b].desired) if s.boxes[b].hard_deadline is None else 0)
        for b,d in plan['deliveries'].items()]
    write_csv(out/'q3_deadline_audit.csv',deadlines)
    hard_slack=min(x['hard_slack'] for x in deadlines if x['hard_slack'] is not None)
    o=plan['objective'];b=old['objective'];comm=plan['communication']
    comparison=[]
    for k,label in [('transport_sorties','运输架次'),('relay_sorties','中继架次'),('total_sorties','总架次'),
        ('weighted_soft_delay','加权软延误'),('weighted_delivery_seconds','加权交付时刻'),('makespan','联合最晚返航 / s'),
        ('transport_energy_kwh','运输能耗 / kWh'),('relay_energy_kwh','中继能耗 / kWh'),('energy_kwh','总能耗 / kWh')]:
        comparison.append(f'| {label} | {b[k]:,.3f} | {o[k]:,.3f} |')
    resource_table='\n'.join(f"| {r['kind']} | {r['sorties']} | {r['aircraft_used']}/{r['aircraft_inventory']} | {r['energy_units_used']}/{r['energy_units_inventory']} | {r['energy_kwh']:.6f} |" for r in resources)
    multiseed='\n'.join(f"| {r['source']} | {r['feasible']}/{r['attempts']} | {r['best_weighted_delivery'] if r['best_weighted_delivery'] is not None else '—'} | {r['median_weighted_delivery'] if r['median_weighted_delivery'] is not None else '—'} |" for r in summary)
    operator_table='\n'.join(f"| {name} | {v['proposed']} | {v['formed']} | {v['feasible']} | {v['accepted']} |" for name,v in history['operators'].items())
    text=f'''# 第三问联合运输—通信调度交付说明

本轮按 `Q3_方法调研.md` 的 A—F 顺序补充实现。最新结果在 `results_q3_complete`，旧的 `results_q3_upgrade` 作为对照保留。主方案为 `{manifest['selected']}`，已通过独立运输/中继物理核验、四类资源核验与连续通信区间认证。所有数值来自保存的完整计划，未把求解器超时当作不可行证明。

## 结果与原基线比较

| 指标 | 原连续通信基线 | 本轮主方案 |
| --- | ---: | ---: |
{chr(10).join(comparison)}

全部 80 箱交付，31 箱硬截止通过；最小硬截止余量 {hard_slack:.3f} s。累计运输飞行与交接时间 {comm['total_transport_seconds']:.3f} s，其中按认证路径分配的直连时间 {comm['direct_seconds']:.3f} s、中继保障时间 {comm['relay_seconds']:.3f} s，中继占比 {comm['relay_share']:.3%}。连续中断 0 s，UNKNOWN 0 s，最小认证余量 {comm['minimum_certified_margin_db']:.6f} dB。这里的直连/中继时间按保守证书划分，不是精确测定的最大直连时长；未证明直连可用的区间可能被保守分配给中继。

目标依次为加权软延误、加权实际交付、运输与中继联合最晚返航、总能耗、总架次。阶段尚未证明最优时，仅锁定已找到值的上限；后续阶段若顺带改善前项则收紧上限。不能把后段单项 `OPTIMAL` 写成整个问题全局最优。另有节能分支先优化软延误再优化总能耗；两类结果共同进入六维非支配筛选，运输与中继架次数分别保留。

## 资源使用

| 类型 | 架次 | 实体机使用/库存 | 电池或组件使用/库存 | 能耗 / kWh |
| --- | ---: | ---: | ---: | ---: |
{resource_table}

上述使用量表示至少执行一次任务的实体数量，不代表全程满负荷。运输机只占准备至返航区间，电池另占返航后充电；中继机还计 300 s 周转，能源组件单独计两阶段充电。同型电池共享，不同型不可混用。中继同时服务多架运输机时仍只计一段真实悬停能耗，空闲等待不扣除。

## 按方案补充的实现

| 环节 | 本轮实现 | 验收产物 |
| --- | --- | --- |
| A 输入及对照 | 对旧基线重新认证，锁定 23/24/25 架次 Q2 输入及显式热启动计划 | `manifest.json`、逐箱表和完整计划 |
| B 连续通信 | 按运输阶段、建链、离场事件切分；对整个扫掠区域及球面距离给保守界，未证明区间递归细化至 0.25 s 后报 UNKNOWN | `q3_continuity_certificate.json`、通信区间 CSV |
| C 四类资源联合排程 | CP-SAT 同时分配运输机、电池、中继机、组件；中继服务起止可变，覆盖子区间可接力 | `q3_joint.py`、资源时间表 |
| D 多初值与路线反馈 | 多 Q2 初值、两个随机种子；八类箱级邻域、收益权重更新及退火接受；每个可行候选重新联合排程和独立核验 | 搜索历史、多种子统计、非支配档案 |
| D 点位和高度细化 | 入选点附近约 50/100 m 水平邻域与 100/200/300 m 高度；按实际服务区间、能耗和余量筛选 | 搜索历史中的 refinement |
| E 求解界 | 每一有限模型阶段记录状态、可行值、下界和条件间隙；另给原问题容量及单箱飞行松弛下界 | `q3_bound_report.json` |
| F 独立核验和衔接 | 独立三角形—像元顶点枚举，独立中继飞行/能量重算；0/1/2/3 dB 固定计划与重优化分开；单列 Q4 兼容支路 | 独立回放、鲁棒性表、Q4 兼容报告 |

原代码仅取两端球面距离最大值作为全区间上界，没有充分的凸性依据；本轮改用球面经纬路径长度的保守度量上界。闭像元最小坐标恰落格线时，补查另一侧接触像元。独立核验器不调用优化器的 `blocked`、`link_margin`、`interval_margin` 或扫掠裁剪函数；采用另一套交点顶点枚举核验连续区域，重新计算中继飞行、建链、悬停、返航、SOC、周转及充电。未取得认证不是确定失联；负保守下界只标 UNKNOWN。固定方案鲁棒性检查另计算精确时点反例，只有明确找到所有路径均不可用的时点才标 FAIL，并在 `q3_fixed_plan_robustness.json` 保存架次、时刻与余量；不据一个反例估算整段中断时长。

模型采用 1 s 整数刻度，飞行和充电时长向上取整，覆盖开始向下取整、结束向上取整。中继两阶段充电曲线通过分段仿射上界及整数除法保守转换。最终导出连续原始时长，核验全部占用、逐箱时间和目标。每个候选悬停位置最多生成两次服务任务；候选池及该次数限制都属于有限搜索范围。

## 多初值与算子实际表现

| 初值 | 通过/尝试 | 最好加权交付 | 中位加权交付 |
| --- | ---: | ---: | ---: |
{multiseed}

| 算子 | 提议次数 | 成功构造箱组 | 联合可行并通过核验 | 被搜索链接受 |
| --- | ---: | ---: | ---: | ---: |
{operator_table}

算子接受包含退火接受，不等于必然优于当前全局主方案；最佳结果由全部已认证档案按目标政策另行选择。未找到解的种子和邻域也保留状态，不能只展示成功次数。有限预算下的箱级搜索不保证每种算子都有收益。

## 鲁棒性、Q4与外部对照

`q3_robustness.csv` 分别记录固定原计划加损耗后的认证状态，以及允许重排机型、时刻、中继窗口和候选点后的结果；后者不改变货箱组批。如返回 `NO_CERTIFIED_SOLUTION_WITHIN_BUDGET`，只表示该候选域与预算内未找到认证解。未将论文训练网络、NOMA/RIS或新的干扰参数加入题目。

Q4 耦合依据最终证书实际选用的中继关系，而不是候选覆盖能力。本轮主方案实际得到 **{plan['q4_compatibility']['component_count']} 个任务块**，其中最大块含 {max(map(len,plan['q4_compatibility']['coupled_components']))} 个服务区。{'它本身已支持两组和三组划分，因此本轮观察到的三组兼容额外代价为 0；不必改用数值更差的受约束支路。' if compatibility['selected_already_supports_three_groups'] else '本方案不足三个任务块，三组划分应采用另存的兼容方案。'} `q3_q4_compatibility.json` 另存沿用旧基线三个服务区块、禁止跨块中继服务的受约束支路和最好已知兼容计划；该支路的差值只是受控实验结果，不是分区必须付出的最小代价。已用原 Q4 求解及核验器生成 `q3_q4_partition_preview.json`，但大任务块仍可能导致严重工作量不均衡，不能只凭“可以分三组”宣称分区质量好。

上述零代价指不必改动 Q3 路线和时序，不代表分组独立运行时没有设备缺口。按保持原时序、允许组内资源重新编号的口径，库存缺口如下；详细机型和严格编号口径见预览 JSON。

| 分组数 | 新增运输机 | 新增运输电池 | 工作量变异系数 |
| --- | ---: | ---: | ---: |
{q4_table}

外部“21+3、7779.2 s、73.10 kWh”只有汇总值，未经过本项目统一核验。表中数值可以逐项比较，但不能据此宣称全面超过外部方案，也不能因本轮使用更严格的证书就认定外部方案不可行。

## 复现与文件

```powershell
python -m pip install -r requirements.txt -r requirements-q3.txt
python run_q3_joint.py --seconds 20 --iterations 8 --seeds 2026 2027 --warm-start q3_joint_feasible_seed.json
python q3_deliver.py
python -m unittest discover -s tests -v
```

`q3_joint_feasible_seed.json` 是本轮先行联合排程所得、独立复核通过的明确输入，来源和有限模型求解状态保存在其 `search` 中。省略 `--warm-start` 可从原 Q3 和 Q2 方案重新搜索。8 个求解线程和墙钟预算会使重新求解得到不同数值；已交付 JSON 和哈希保存本次实际运行结果，不能承诺逐位复现搜索过程。

完整四问可用 `python run.py --q3-plan results_q3_complete/q3_plan.json` 接入本次方案；程序先重做独立认证，再按最终中继关系求第四问。若需要三组兼容计划，可将路径改为 `results_q3_complete/plans/q4_compatible.json`（以本轮确实生成该文件为前提）。旧的 `python run.py` 未指定此参数时继续使用历史采样搜索，不能把它的输出误当成本轮连续联合方案。

`q3_plan.json` 保存主方案，`plans/` 保存所有通过认证的实验计划；`q3_pareto.csv` 仅含本轮已找到的非支配解，不是完整前沿；`q3_all_candidates.csv` 包含全部认证记录；`q3_search_history.json` 包含失败状态及算子统计；`q3_multiseed_summary.csv` 汇总可行率、最好值、中位值和耗时；逐箱、资源、通信和边界报告可直接追溯计划。`q3_deliver.py` 只重新认证及汇总，不重新优化。

当前实现是有约束验收的有限联合搜索。候选位置沿已知可行点局部细化，没有穷尽连续平面与高度；没有逻辑型 Benders 或完整路线池集合划分的全局证明。原问题下界与有限候选模型下界分开报告。第三问主体已形成可运行、可核验、可继续扩展的交付闭环，上述限制应保留在论文中。
'''
    (root/'Q3_交付说明.md').write_text(text,encoding='utf-8')
    # Refresh provenance after writing the additional reports.
    manifest['code'].update({p.name:sha256(p) for p in (root/'q3_deliver.py',root/'q3_certificate.py',root/'q3_joint.py',root/'run_q3_joint.py')})
    manifest['warm_start_sha256']=sha256(root/'q3_joint_feasible_seed.json')
    manifest['sources']['results_q3_upgrade/q3_plan.json']=sha256(root/'results_q3_upgrade/q3_plan.json')
    manifest['input_files']={str(p.relative_to(s.base)):sha256(p) for p in sorted(s.base.rglob('*'))
                             if p.is_file() and p.suffix.lower() in {'.xlsx','.tif','.tiff'}}
    manifest['code'].update({p.name:sha256(p) for p in (root/'model.py',root/'verify.py',root/'transport_check.py',root/'q3_upgrade.py',root/'run_q3_upgrade.py')})
    manifest['files']={str(p.relative_to(out)):sha256(p) for p in sorted(out.rglob('*')) if p.is_file() and p.name!='manifest.json'}
    dump_json(out/'manifest.json',manifest)
    print(json.dumps(dict(selected=manifest['selected'],objective=o,resources=resources),ensure_ascii=False))


if __name__=='__main__':main()

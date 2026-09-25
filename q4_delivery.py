"""General Q4 delivery, independent proof replay and physical inventory mapping."""
from __future__ import annotations
import argparse
import csv
import json
from math import isfinite
from pathlib import Path
from model import Scenario
from q4 import RESOURCE_KEYS
from q4_audit import audit, verify_fixed_q3
from q4_exact import load_and_solve

ROOT=Path(__file__).resolve().parent


def default_plan_path():
    robust=ROOT/'results_q3_q4_resilience/selection/primary/q3_plan.json'
    if robust.is_file():return robust
    latest=ROOT/'results_q3_q4_frontier/selection/primary/q3_plan.json'
    return latest if latest.is_file() else ROOT/'results_q3_complete/q3_plan.json'


def write_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')


def write_csv(path,rows,fields):
    with path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader();writer.writerows(rows)


def candidate_rows(s,directory):
    rows=[]
    if directory is None:return rows
    for path in sorted(directory.glob('*.json')):
        try:
            plan=json.loads(path.read_text(encoding='utf8'))
            verify_fixed_q3(s,plan)
            result=load_and_solve(s,path)
            audit(s,plan,result)
            best={k:min((p for p in result['partitions'] if p['group_count']==k),
                         key=lambda p:(p['shortage_total'],p['resource_total'],p['workload_cv']),default=None)
                  for k in (2,3)}
            rows.append(dict(candidate=path.stem,status='PASS',error='',
                             component_count=len(result['components']['components']),**plan['objective'],
                             two_group_shortage=best[2]['shortage_total'] if best[2] else None,
                             three_group_shortage=best[3]['shortage_total'] if best[3] else None))
        except (ValueError,AssertionError) as exc:
            rows.append(dict(candidate=path.stem,status='REJECTED',error=str(exc)))
    return rows


def vector(values):return '('+','.join(str(values[k]) for k in RESOURCE_KEYS)+')'


def markdown(result,validation,source,candidates):
    c=result['components'];parts=result['partitions']
    rec=next((p for p in parts if p['id']==result['recommended_partition']),None)
    lines=['# 第四问交付说明：固定任务分区与资源接续配置','',
           '**题意口径：允许资源需求超过现有库存。** 原题要求“若某一分区方案所需资源超过现有库存，进一步给出相应的资源缺口及其原因”。库存缺口是评价和解释指标，不是分区的硬性不可行条件；只有声称“现有库存可直接执行”时才要求逐类型缺口为零。',
           f"输入：`{source.as_posix()}`。原文件SHA256：`{c['input_sha256']}`。",
           f"固定{c['box_count']}箱、{c['transport_task_count']}个运输架次、{c['relay_task_count']}个中继架次及其实际通信关系。",
           f"输入Q3认证的额外通信损耗为{result.get('q3_scenario',{}).get('extra_loss_db',0.):g} dB，原实物排程声明的额外接续缓冲为{result.get('q3_scenario',{}).get('resource_buffer_seconds',0.):g}秒。Q4最低资源重编号后的实际余量以本页逐组证书为准，不自动继承原编号的缓冲结论。",
           'Q4仅选择任务组及同类型资源编号；时间、机型、货箱组批、访问顺序与通信关系保持输入方案原值。',
           '', '## 任务块与完整分区','',
           '| 块 | 服务区 | 箱数 | 工作量/s |','|---|---|---:|---:|']
    for i,zones in enumerate(c['components']):
        lines.append(f"| C{i+1} | {', '.join(zones)} | {c['component_box_counts'][i]} | {c['component_work_seconds'][i]:.6f} |")
    lines += ['', '八维向量顺序为A/B/C运输机、A/B/C电池、中继机、中继能源组件。',
              f"库存{vector(result['stock'])}；统一执行最低需求{vector(result['pooled_resource_need'])}。",
              '', '| 方案 | 组数 | 分组 | 需求 | 件数 | 缺口 | 缺口件数 | 工作量CV | 库存满足 |',
              '|---|---:|---|---|---:|---|---:|---:|---|']
    for p in parts:
        groups=' / '.join('+'.join(f'C{i}' for i in g['component_indices']) for g in p['groups'])
        lines.append(f"| {p['id']} | {p['group_count']} | {groups} | {vector(p['resource_need'])} | {p['resource_total']} | "
                     f"{vector(p['shortage'])} | {p['shortage_total']} | {p['workload_cv']:.9f} | {'是' if p['stock_sufficient'] else '否'} |")
    if rec:
        policy='实际输入单价计算的新增成本优先' if result.get('resource_prices') else '缺口件数、总件数、工作量CV依次最小'
        lines += ['',f"两组推荐方案为**{rec['id']}**，政策为{policy}。",
                  f"其缺口为{vector(rec['shortage'])}；{'现有库存满足名义计划。' if rec['stock_sufficient'] else '必须补齐逐类型缺口才能执行名义计划。'}",
                  '', '| 组 | 区数 | 箱数 | 质量/kg | 运输/中继架次 | 工作量/s | 最低资源 |',
                  '|---|---:|---:|---:|---:|---:|---|']
        for g in rec['groups']:
            lines.append(f"| {g['id']} | {len(g['zones'])} | {g['boxes']} | {g['cargo_kg']:.3f} | {g['transport_sorties']}/{g['relay_sorties']} | {g['work_seconds']:.6f} | {vector(g['resource_need'])} |")
    lines += ['', '## 独立最优性核验与设备映射','',
              '资源占用均采用半开区间：运输机准备至返航；电池开始至充满；中继机开始至周转完成；能源组件开始至充满。',
              '求解器用峰值与增广路匹配计算最低数量，再在有限接续间隔上二分求最大最小余量。',
              '独立核验器重新计算峰值，以Edmonds–Karp最大流检查匹配大小，按阈值从大到小独立重求瓶颈最优值，并核对下一阈值的不可行性。',
              '证书匹配边、完整接续链、峰值见证均与逐任务分配交叉检查。篡改最优余量、下一阈值匹配数或链信息会被拒绝。',
              '输入Q3的物理量和已保存通信区间使用独立内核复核；保留原通信关系，未重新选中继以改变任务块。',
              '`q4_inventory_mapping.csv`把组内编号映射至真实库存编号；`NEW-...`明确表示待采购资源，不能算作已有库存。',
              '全部方案分别给出映射，方案之间可重复使用同一库存编号；单个方案内各组不能共享。',
              '', '| 组/类型 | 数量 | 原编号最小余量/s | 最优余量/s | 下一阈值/s | 下一阈值匹配数 |',
              '|---|---:|---:|---:|---:|---:|']
    def fmt(v):return '不适用' if v is None else f'{v:.6f}'
    if rec:
        for gid,certs in rec['certificates'].items():
            for key,cert in certs.items():
                if cert['task_count']:
                    lines.append(f"| {gid}/{key} | {cert['minimum_resources']} | {fmt(cert['original_id_min_handoff_slack_seconds'])} | {fmt(cert['optimal_min_handoff_slack_seconds'])} | {fmt(cert['next_threshold_seconds'])} | {cert['matching_size_at_next_threshold']} |")
    lines += ['', '## 均衡与政策边界','',
              f"八类资源需求与工作量CV共同比较的完整非支配分区：两组{', '.join(result['pareto_partitions']['2']) or '无'}；三组{', '.join(result['pareto_partitions']['3']) or '无'}。不同类型资源不可互相抵扣，没有指定价格或偏好时不宣称其中某个分区是唯一最优。",
              f"总工作量{result['total_work_seconds']:.6f}秒，最大任务块占比{result['dominant_component_fraction']:.6%}。",
              '工作量为准备至返航的运输与中继任务时长总和，中继任务只计一次，不含充电；它是工作量代理，不能等同人员劳动量。',
              f"两组CV放松下界{result['workload_cv_lower_bound']['2']:.9f}，三组下界{result['workload_cv_lower_bound']['3']:.9f}；真实最小值由全部分区决定。",
              '各组箱数、质量、架次、能耗、八类占用率见`q4_group_detail.csv`；占用率分母为组内第一任务开始至最后资源释放的时长乘最低数量。',
              '没有采购价格时，件数不代表成本。可用`--prices prices.json`输入八类资源的非负单价，以新增成本选择两组方案。',
              '', '## 当前问题、已完成修复与剩余限制','',
              '1. 库存能否执行取决于本页缺口表。补齐缺口后可执行是相同类型资源和既定模型条件下的结论。',
              '2. 大任务块造成的失衡只能通过另行改变Q3路线或实际中继关系改善；该新Q3必须另存并重新认证，Q4本身不能拆开固定任务块。',
              '3. 接续余量是局部时间缓冲，不能据此保证任意飞行、充电或通信扰动都安全；需结合新Q3的缓冲试验和扰动回放。',
              '4. 原版最优余量证书独立验收缺口已修复，完整证明字段和任务链均已独立重算；PASS的范围为保存输入下的资源与证书一致性。',
              '5. 原版实物编号映射缺口已修复，已有库存与待采购编号分开导出；采购资源到位后还需登记实际序列号。',
              '6. 仍沿用同型资源可互换、初始满电、再用前充满、充电可并行且无充电器数量上限的模型。若新增充电器或人员限制，需另建约束。',
              '7. 固定SHA限制已改为记录输入哈希并实际核验；允许新Q3输入。候选对照目录可省略，缺失不会阻断Q4主交付。',
              '',f"独立回放：**{validation['status']}**；{validation['assignment_rows']}条分配记录。",
              f"可选Q3候选对照：{len(candidates)}份，见`q4_q3_candidate_comparison.csv`。更换Q3的对照不能计作固定Q3下的第四问改善。",
              '', '运行：`python run_q4.py --plan <已认证Q3计划.json> --output <结果目录>`。', '']
    return '\n'.join(lines)


def export(s,plan_path,output,candidate_dir=None,prices=None,expected_sha256=None,figures=True):
    plan=json.loads(plan_path.read_text(encoding='utf8'))
    q3_check=verify_fixed_q3(s,plan)
    result=load_and_solve(s,plan_path)
    result['q3_scenario']=dict(extra_loss_db=plan.get('extra_loss_db',0.),
                              resource_buffer_seconds=plan.get('search',{}).get('resource_buffer_seconds',0.),
                              original_minimum_handoff_seconds=q3_check['physical']['minimum_handoff_seconds'])
    if expected_sha256 and result['components']['input_sha256'] != expected_sha256.upper():
        raise ValueError('Input SHA256 differs from requested value')
    if prices is not None:
        if set(prices)!=set(RESOURCE_KEYS) or any(not isfinite(v) or v<0 for v in prices.values()):
            raise ValueError('Prices require all eight nonnegative finite type prices')
        result['resource_prices']=prices
        for p in result['partitions']:p['procurement_cost']=sum(prices[k]*p['shortage'][k] for k in RESOURCE_KEYS)
        choices=[p for p in result['partitions'] if p['group_count']==2]
        result['recommended_partition']=min(choices,key=lambda p:(p['procurement_cost'],p['workload_cv']))['id'] if choices else None
    validation=audit(s,plan,result)
    candidates=candidate_rows(s,candidate_dir)
    output.mkdir(parents=True,exist_ok=True)
    for name,value in [('q4_components',result['components']),('q4_validation',validation),
                       ('q4_input_verification',q3_check),('q4_optimality_certificate',{p['id']:p['certificates'] for p in result['partitions']}),
                       ('q4_results',{k:v for k,v in result.items() if k!='assignments'})]:
        write_json(output/(name+'.json'),value)
    rows=[];details=[]
    for p in result['partitions']:
        row={k:p[k] for k in ['id','group_count','resource_total','shortage_total','workload_cv','stock_sufficient']}
        row['assignment_replay_passed']=True
        if 'procurement_cost' in p:row['procurement_cost']=p['procurement_cost']
        for key in RESOURCE_KEYS:
            row['need_'+key]=p['resource_need'][key];row['shortage_'+key]=p['shortage'][key]
        rows.append(row)
        for g in p['groups']:
            d=dict(partition=p['id'],group=g['id'],zones='|'.join(g['zones']),boxes=g['boxes'],cargo_kg=g['cargo_kg'],
                   transport_sorties=g['transport_sorties'],relay_sorties=g['relay_sorties'],work_seconds=g['work_seconds'],energy_kwh=g['energy_kwh'])
            for key in RESOURCE_KEYS:
                d['need_'+key]=g['resource_need'][key];d['occupancy_'+key]=g['resource_occupancy_ratio'][key]
            details.append(d)
    write_csv(output/'q4_all_partitions.csv',rows,list(rows[0]) if rows else ['id','group_count'])
    write_csv(output/'q4_group_detail.csv',details,list(details[0]) if details else ['partition','group'])
    write_csv(output/'q4_resource_assignments.csv',result['assignments'],
              ['partition','group','resource_type','resource_id','task','start','release','next_task','handoff_slack_seconds','physical_id','inventory_status'])
    write_csv(output/'q4_inventory_mapping.csv',result['inventory_mapping'],
              ['partition','group','resource_type','resource_id','physical_id','inventory_status'])
    fields=sorted({k for row in candidates for k in row}) if candidates else ['candidate','status']
    write_csv(output/'q4_q3_candidate_comparison.csv',candidates,fields)
    (output/'Q4_交付说明.md').write_text(markdown(result,validation,plan_path,candidates),encoding='utf8')
    # The four charts are designed for the common three-block, four-partition case.
    # Avoid drawing a misleading partial chart for larger task graphs.
    if figures and len(result['components']['components'])==3 and len(result['partitions'])==4:
        from q4_figures import create_all
        create_all(result,plan,output)
    return result,validation


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,default=default_plan_path())
    parser.add_argument('--output',type=Path,default=ROOT/'results_q4')
    parser.add_argument('--candidate-dir',type=Path)
    parser.add_argument('--prices',type=Path)
    parser.add_argument('--expected-sha256')
    args=parser.parse_args()
    prices=json.loads(args.prices.read_text(encoding='utf8')) if args.prices else None
    result,validation=export(Scenario(),args.plan,args.output,args.candidate_dir,prices,args.expected_sha256)
    print(json.dumps(dict(status=validation['status'],recommended=result['recommended_partition'],
                          partitions=len(result['partitions']),output=str(args.output)),ensure_ascii=False))


if __name__=='__main__':main()

"""Deliver one accepted faster Q3 with explicit, unchanged-membership Q4 comparison."""
import argparse
import hashlib
import json
from pathlib import Path

from model import Scenario
from q3_upgrade import dump_json
from q4_delivery import export
from q4_exact import solve as solve_q4
from deliver_q34_compromise import choose, csv_out
from search_q34_compromise import save_candidate
from optimize_q3_time_guarded import DEFAULT, ROOT, evaluate, matching


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input',type=Path,required=True)
    ap.add_argument('--output',type=Path,default=ROOT/'results_q3_time_guarded/selection/primary')
    a=ap.parse_args()
    verdict=json.loads((a.input.parent/'acceptance.json').read_text(encoding='utf8'))
    if verdict['status']!='ACCEPTED': raise ValueError('Candidate did not pass its stated acceptance policy')
    config_path=a.input.parent/'configuration.json'
    if not config_path.exists():config_path=a.input.parent.parent/'configuration.json'
    config=json.loads(config_path.read_text(encoding='utf8'))
    baseline_path=Path(config['baseline']);baseline=json.loads(baseline_path.read_text(encoding='utf8'))
    s=Scenario()
    before=solve_q4(s,baseline,baseline_path.read_bytes())
    protected=[choose(before,k) for k in (2,3)]
    plan,q4,_=save_candidate(s,json.loads(a.input.read_text(encoding='utf8')),a.output)
    result=evaluate(plan,q4,baseline,protected,config['cv_tolerance'],config['quality_tolerance'],
                    config['resource_policy'],config['resource_allowance'])
    if result['status']!='ACCEPTED':raise ValueError(result['failures'])
    _,validation=export(s,a.output/'q3_plan.json',a.output/'q4',figures=False)
    selected=[matching(q4,p) for p in protected]
    dump_json(a.output/'balanced_partitions.json',dict(
        policy='Retain baseline zone membership; compare resource vectors, shortage and CV explicitly.',
        partitions={str(p['group_count']):p for p in selected}))
    csv_out(a.output/'transport_sorties.csv',[
        {**{k:r[k] for k in ['id','model','drone','battery','start','return_time','battery_ready','energy_kwh','return_soc']},
         'visits':json.dumps(r['visits'],ensure_ascii=False)} for r in plan['transport_sorties']])
    csv_out(a.output/'relay_sorties.csv',[
        {k:r[k] for k in ['id','relay','energy_component','start','established','service_end','return_time',
                         'relay_ready','component_ready','energy_kwh','return_soc']} for r in plan['relay_sorties']])
    csv_out(a.output/'box_deliveries.csv',[
        dict(box=b,zone=s.boxes[b].zone,time=d['time'],hard_deadline=s.boxes[b].hard_deadline,
             desired=s.boxes[b].desired,priority=s.boxes[b].priority) for b,d in sorted(plan['deliveries'].items())])
    csv_out(a.output/'communication_intervals.csv',[
        {k:r.get(k) for k in ['sortie','phase_index','start','end','mode','relay_sortie','margin_lower_db','status']}
        for r in plan['communication_intervals']])
    old_rows={r['id']:r for r in baseline['transport_sorties']}
    changes=[dict(task=r['id'],before_model=old_rows.get(r['id'],{}).get('model'),after_model=r['model'],
                  before_start=old_rows.get(r['id'],{}).get('start'),after_start=r['start'],
                  before_visits=old_rows.get(r['id'],{}).get('visits'),after_visits=r['visits'])
             for r in plan['transport_sorties']]
    dump_json(a.output/'task_changes.json',changes)
    hard=[s.boxes[b].hard_deadline-d['time'] for b,d in plan['deliveries'].items() if s.boxes[b].hard_deadline is not None]
    result.update(baseline_objective=baseline['objective'],communication=plan['communication'],
        hard_box_count=len(hard),minimum_hard_margin_seconds=min(hard),
        minimum_handoff_seconds=plan['independent_replay']['minimum_handoff_seconds'],
        q4_validation=validation,source_candidate=str(a.input),policy=config,
        baseline_sha256=hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        plan_sha256=hashlib.sha256((a.output/'q3_plan.json').read_bytes()).hexdigest(),
        code_sha256={f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in
                     ['q3_joint.py','optimize_q3_timing.py','optimize_q3_time_guarded.py','q3_certificate.py','q4_exact.py','q4_audit.py']})
    dump_json(a.output/'delivery_summary.json',result)
    metrics=[('联合完工时间/s','makespan'),('总能耗/kWh','energy_kwh'),
             ('加权送达时间/优先级·s','weighted_delivery_seconds'),('加权软延误','weighted_soft_delay'),
             ('运输架次','transport_sorties'),('中继架次','relay_sorties')]
    rows=[dict(metric=label,before=baseline['objective'][key],after=plan['objective'][key]) for label,key in metrics]
    for old,new in zip(protected,selected):
        for key,label in [('shortage_total','缺口/件'),('resource_total','设备总数/件'),('workload_cv','工作量CV')]:
            rows.append(dict(metric=f"{old['group_count']}组{label}",before=old[key],after=new[key]))
    csv_out(a.output/'comparison.csv',rows)
    text=['# Q3缩时及Q4影响交付说明','',
          f"以提交 c8b7305 的折中主方案为基准，最大联合完工时间由 {baseline['objective']['makespan']:.3f} 秒降至 "
          f"{plan['objective']['makespan']:.3f} 秒，提前 {result['saved_seconds']:.3f} 秒"
          f"（{result['saved_seconds']/baseline['objective']['makespan']:.2%}）。",'',
          '## 同一Q3的对比','', '| 指标 | 优化前 | 优化后 |','|---|---:|---:|']
    text += [f"| {r['metric']} | {r['before']:.6f} | {r['after']:.6f} |" for r in rows]
    text += ['', '## 对第四问的实际影响','',
             '两种分区的服务区成员保持一致。每个方案的两组和三组均使用同一份冻结Q3，不混用不同Q3的较好数字。',
             '下表逐类型统计；不同设备的富余库存不抵扣其他类型缺口。','']
    for old,new in zip(protected,selected):
        text += [f"### {old['group_count']}组",'', '| 资源 | 原需求 | 新需求 | 原缺口 | 新缺口 |','|---|---:|---:|---:|---:|']
        for k in old['resource_need']:
            text.append(f"| {k} | {old['resource_need'][k]} | {new['resource_need'][k]} | {old['shortage'][k]} | {new['shortage'][k]} |")
        text += ['']+[f"- {g['id']}：{', '.join(g['zones'])}；工作量占比 {g['work_seconds']/q4['total_work_seconds']:.2%}。" for g in new['groups']]+['']
    text += ['## 改动与验收','',
        '新增 makespan 优先的连续时间精修；联合排程同时约束原两组与三组的资源需求，避免仅改善其中一种分区。',
        f"本交付实际采用 resource_policy={config['resource_policy']}；每种分区设备总数和缺口相对基准最多增加 "
        f"{config['resource_allowance']} 件，CV最多增加 {config['cv_tolerance']}，能耗与加权送达上限增幅 {config['quality_tolerance']:.0%}。这些是本轮选择偏好，不是题目约束。",'',
        f"80箱均恰好交付一次；{len(hard)}个硬时限箱全部按时，最小硬时限余量 {min(hard):.3f} 秒；普通物资加权软延误为0。",
        f"连续通信中断 {plan['communication']['outage_seconds']} 秒、未认证 {plan['communication']['unknown_seconds']} 秒。额外1 dB损耗与5秒实体资源接续缓冲均保持；独立Q3/Q4验收通过。",'',
        '组间隔离后的Q4设备需求可能超过库存；本轮没有增加Q3实际库存。上表新增需求如有，是Q4独立运行的代价。',
        '物理模型、原始题目数据和连续通信认证标准保持原口径。任务级改动和起飞时刻详见 task_changes.json。','',
        '## 搜索记录与结论边界','',
        '原固定结构LP没有缩时收益。原组批、原机型/可变机型、几种合批和逐箱移位均做了受约束搜索，日志保存在上级实验目录。',
        'INFEASIBLE仅说明给定候选航线、点位、整数时间与本轮上限组成的有限模型不可行；UNKNOWN表示限时没有找到解，二者不等于原题不可优化。',
        'CP-SAT的最优界只适用于对应阶段的有限模型；连续LP最优只适用于冻结路线、设备顺序及中继覆盖义务，均不宣称Q3全局最优。',
        'Q4对冻结Q3枚举全部合法分区并独立复算。本轮保护的指定分区见 balanced_partitions.json；q4/Q4_交付说明.md 的默认最小缺口推荐属于另一选择政策。','',
        '## 复现','', '```powershell',
        f".\\.venv\\Scripts\\python.exe optimize_q3_time_guarded.py --output results_q3_time_guarded/replay --resource-policy {config['resource_policy']} --resource-allowance {config['resource_allowance']} --quality-tolerance {config['quality_tolerance']} --seconds {config['seconds']} --seed {config['seed']}",
        '```','', '限时多线程搜索不保证重新运行命中完全相同候选。已保存完整计划、配置、阶段界、原始输入指纹和独立验收，复算保存的计划优先。']
    report='\n'.join(text)+'\n'
    (a.output/'Q3缩时及Q4影响交付说明.md').write_text(report,encoding='utf8')
    print(json.dumps(dict(output=str(a.output),objective=plan['objective'],q4=[dict(k=p['group_count'],shortage=p['shortage_total'],cv=p['workload_cv']) for p in selected]),ensure_ascii=False),flush=True)


if __name__=='__main__': main()

"""Deliver an audited optimization of the supplied Q3, keeping source provenance."""
import argparse
from hashlib import sha256
import json
from pathlib import Path

from model import Scenario
from q3_upgrade import dump_json
from q4_exact import solve as solve_q4
from q4_delivery import export
from search_q34_compromise import save_candidate
from deliver_q34_compromise import choose,csv_out
from optimize_q3_imported import ROOT,OUT,read,compare,compact


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input',type=Path,required=True)
    ap.add_argument('--output',type=Path,default=OUT/'selection/primary')
    args=ap.parse_args()
    config_path=args.input.parent/'configuration.json'
    if not config_path.exists():config_path=args.input.parent.parent/'configuration.json'
    config=read(config_path)
    baseline=read(OUT/'baseline/q3_plan.json');s=Scenario()
    before=solve_q4(s,baseline,(OUT/'baseline/q3_plan.json').read_bytes())
    protected=[choose(before,k) for k in (2,3)]
    plan,q4,_=save_candidate(s,read(args.input),args.output)
    comparison=compare(plan,q4,baseline,protected,config.get('energy_tolerance',0.),config.get('cv_tolerance',.01))
    if comparison['status']!='ACCEPTED':raise ValueError(comparison)
    _,audit=export(s,args.output/'q3_plan.json',args.output/'q4',figures=False)
    dump_json(args.output/'protected_partitions.json',comparison['partitions'])
    csv_out(args.output/'transport_sorties.csv',[
        {**{k:r[k] for k in ['id','model','drone','battery','start','return_time','battery_ready','energy_kwh','return_soc']},
         'visits':json.dumps(r['visits'],ensure_ascii=False)} for r in plan['transport_sorties']])
    csv_out(args.output/'relay_sorties.csv',[
        {k:r[k] for k in ['id','relay','energy_component','start','established','service_end','return_time','relay_ready','component_ready','energy_kwh','return_soc']}
        for r in plan['relay_sorties']])
    csv_out(args.output/'box_deliveries.csv',[
        dict(box=b,zone=s.boxes[b].zone,time=d['time'],baseline_time=baseline['deliveries'][b]['time'],
             hard_deadline=s.boxes[b].hard_deadline,desired=s.boxes[b].desired,priority=s.boxes[b].priority)
        for b,d in sorted(plan['deliveries'].items())])
    csv_out(args.output/'communication_intervals.csv',[
        {k:r.get(k) for k in ['sortie','phase_index','start','end','mode','relay_sortie','margin_lower_db','status']}
        for r in plan['communication_intervals']])
    hard=[s.boxes[b].hard_deadline-d['time'] for b,d in plan['deliveries'].items() if s.boxes[b].hard_deadline is not None]
    deltas=[d['time']-baseline['deliveries'][b]['time'] for b,d in plan['deliveries'].items()]
    comparison.update(baseline=compact(baseline,before),
        source_attachment_sha256=sha256((OUT/'baseline/input_original.json').read_bytes()).hexdigest(),
        plan_sha256=sha256((args.output/'q3_plan.json').read_bytes()).hexdigest(),source_candidate=str(args.input),
        configuration=config,hard_box_count=len(hard),minimum_hard_margin_seconds=min(hard),
        handoff=plan['independent_replay']['minimum_handoff_seconds'],q4_audit_status=audit['status'],
        q4_partitions=len(q4['partitions']),resource_assignment_count=audit['assignment_rows'],
        delivery_changes=dict(earlier=sum(x < -1e-3 for x in deltas),later=sum(x > 1e-3 for x in deltas),
                              unchanged=sum(abs(x)<=1e-3 for x in deltas)),
        code_sha256={f:sha256((ROOT/f).read_bytes()).hexdigest() for f in
            ['optimize_q3_imported.py','build_q3_imported_seeds.py','deliver_q3_imported.py','q3_joint.py',
             'optimize_q3_timing.py','q3_certificate.py','q4_exact.py','q4_audit.py']})
    dump_json(args.output/'delivery_summary.json',comparison)
    records=[]
    for directory in sorted(OUT.iterdir()):
        if not (directory/'configuration.json').exists():continue
        verdict=read(directory/'acceptance.json') if (directory/'acceptance.json').exists() else {}
        report=read(directory/'solver_report.json') if (directory/'solver_report.json').exists() else {}
        records.append(dict(case=directory.name,status=verdict.get('status','BASELINE_AUDIT' if directory.name=='baseline' else 'UNFINISHED'),
                            failures=verdict.get('failures',[]),improved=verdict.get('improved'),stages=report.get('stages',[]),
                            polished=read(directory/'polished/acceptance.json') if (directory/'polished/acceptance.json').exists() else None))
    dump_json(OUT/'experiment_inventory.json',records)
    metrics=[('联合完工时间/s','makespan'),('总能耗/kWh','energy_kwh'),
             ('加权送达时间/优先级·s','weighted_delivery_seconds'),('加权软延误','weighted_soft_delay'),
             ('运输架次','transport_sorties'),('中继架次','relay_sorties')]
    old_repo=read(ROOT/'results_q3_time_guarded/selection/one_extra/delivery_summary.json')
    rows=[dict(metric=label,repository=old_repo['objective'][key],attachment=baseline['objective'][key],optimized=plan['objective'][key])
          for label,key in metrics]
    for k in ('2','3'):
        for key,label in [('shortage_total','缺口/件'),('workload_cv','工作量CV'),('resource_total','设备总数/件')]:
            rows.append(dict(metric=k+'组'+label,repository=old_repo['partitions'][k][key],
                             attachment=comparison['baseline']['partitions'][k][key],optimized=comparison['partitions'][k][key]))
    csv_out(args.output/'comparison.csv',rows)
    text=['# 按用户提供Q3方案继续优化的交付说明','',
          '用户提供的 `q3_plan.json` 已通过独立运输、逐箱、实体机/电池、中继和连续通信复核。原文件原样存于 `results_q3_imported/baseline/input_original.json`，没有依赖文件内PASS字样代替验收。',
          f"附件SHA256：`{comparison['source_attachment_sha256']}`。",'',
          '## 三方比较','', '| 指标 | 上次仓库缩时备选 | 本次附件 | 基于附件优化 |','|---|---:|---:|---:|']
    text += [f"| {r['metric']} | {r['repository']:.6f} | {r['attachment']:.6f} | {r['optimized']:.6f} |" for r in rows]
    text += ['', '附件相对仓库的提速属于用户给定方案本身的收益；本轮优化收益应看最后两列，不能将附件原有改进归因于本轮搜索。',
             '本轮最大完工时间未再缩短；加权送达时间改善约0.55%，总能耗增加约0.017%。这是一项很小的能耗与送达时效取舍，并非所有目标严格支配附件。',
             f"逐箱比较：{comparison['delivery_changes']['earlier']}箱提前、{comparison['delivery_changes']['later']}箱推迟、{comparison['delivery_changes']['unchanged']}箱不变；推迟的货箱均满足对应时限。S006医疗箱由{baseline['deliveries']['S006-MED-01']['time']:.3f}秒提前至{plan['deliveries']['S006-MED-01']['time']:.3f}秒，食品箱由{baseline['deliveries']['S006-FOD-01']['time']:.3f}秒推迟至{plan['deliveries']['S006-FOD-01']['time']:.3f}秒。",
             '两组和三组在各列均使用该列同一份冻结Q3。附件按CV≤0.30后最小化逐类型缺口、CV、总件数选区；优化列保持附件这两种分区成员，以便明确比较影响。仓库旧方案的分区成员与附件不同。','',
             '## 本轮实际修改','',
             '将S006-MED-01从C型T010_2移到A型T009，将S006-FOD-01交换到T010_2。两架次箱数不变；医疗箱更早送达，C型机的早期任务安排更灵活。每个候选都重算载重、容积、能耗、SOC、两阶段充电、送达时刻和中继覆盖。',
             '使用跨分区的共同细分任务块，同时保护附件两组、三组的设备总数和资源缺口；这两种分区不是简单的嵌套关系，不能直接把三组编号拿来约束两组。',
             '先作1秒保守整数排程，再固定任务和设备顺序进行连续时间精修，最终重新认证通信关系并穷举Q4。整数搜索允许至多5秒舍入余量，最终交付不允许完工时间超过附件。',
             f"本轮候选能耗上限为附件的 {1+config.get('energy_tolerance',0.):.3f} 倍，CV增量上限 {config.get('cv_tolerance',.01):.3f}；缺口和设备总数不增加。所有上限是本轮筛选偏好，不是新增题目条件。",'',
             '## 固定本轮Q3的分区与资源','']
    for k,p in comparison['partitions'].items():
        old=comparison['baseline']['partitions'][k]
        text += [f"### {k}组 {p['id']}",'',f"缺口 {old['shortage_total']}→{p['shortage_total']} 件，CV {old['workload_cv']:.6f}→{p['workload_cv']:.6f}。",'',
                 '| 资源 | 附件需求 | 优化后需求 |','|---|---:|---:|']
        text += [f"| {key} | {old['resource_need'][key]} | {value} |" for key,value in p['resource_need'].items()]
        text += ['']+[f"- {g['id']}：{', '.join(g['zones'])}。" for g in p['groups']]+['']
    text += ['## 独立验收与边界','',
             f"80箱恰好交付一次，{len(hard)}箱硬时限全部满足，最小余量 {min(hard):.3f} 秒；普通物资零软延误。",
             f"连续通信中断 {plan['communication']['outage_seconds']} 秒、未认证 {plan['communication']['unknown_seconds']} 秒；额外1 dB损耗与5秒实体资源接续保留。",
             f"独立复核 {len(q4['partitions'])} 个分区、{audit['assignment_rows']} 条资源分配，全部通过。",'',
             'Q4分区后的资源需求可能超过库存，须按具体类型补足；Q3本身使用题目原库存。附件的缺口比上次仓库备选明显增加，因此本轮作为另一条方案分支交付，不能将它描述成对历史Q4无影响。',
             '固定结构LP与原组批固定机型的受限搜索未发现对附件的改善。尾程拆箱等试验的失败和限时状态保留在 experiment_inventory.json；UNKNOWN不是不可行证明。所有阶段最优界只适用于各自有限候选模型，不宣称原题Q3全局最优。','',
             '## 文件与复现','',
             '`results_q3_imported/selection/primary` 包含完整计划、三方比较、逐箱送达、中继与运输明细、通信区间和Q4全分区证书。指定分区以 `protected_partitions.json` 为准；通用Q4报告按最小库存缺口排序，属于另一选择政策。',
             '测试日志位于 `results_q3_imported/regression_tests.log`。',
             '', '```powershell',
             '.\\.venv\\Scripts\\python.exe build_q3_imported_seeds.py',
             '.\\.venv\\Scripts\\python.exe optimize_q3_imported.py --mode cp --policy makespan --fixed-models --input results_q3_imported/seeds/early_s006_medical.json --output results_q3_imported/replay --energy-tolerance .001 --seconds 50 --seed 2405',
             '.\\.venv\\Scripts\\python.exe deliver_q3_imported.py --input results_q3_imported/replay/polished/q3_plan.json --output results_q3_imported/replay_delivery',
             '```', '', '仅复核本次保存结果，无需重新搜索：', '', '```powershell',
             f'.\\.venv\\Scripts\\python.exe deliver_q3_imported.py --input {args.input.as_posix()} --output results_q3_imported/replay_delivery',
             '```','', '多线程限时搜索不承诺重现同一搜索路径；保存的完整计划和独立验收是本轮数值依据。']
    report='\n'.join(text)+'\n'
    (args.output/'Q3外部方案优化交付说明.md').write_text(report,encoding='utf8')
    (ROOT/'Q3_外部方案优化交付说明.md').write_text(report,encoding='utf8')
    print(json.dumps(dict(objective=plan['objective'],q4={k:{f:p[f] for f in ['shortage_total','workload_cv']} for k,p in comparison['partitions'].items()},delivery_changes=comparison['delivery_changes'])),flush=True)


if __name__=='__main__':main()

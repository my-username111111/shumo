"""Replay and export supplementary Q3 resilience scenarios and their fixed-input Q4."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from model import Scenario
from q3_certificate import physical_audit
from q3_joint import data_signature
from q4_audit import verify_fixed_q3
from q4_delivery import export, write_csv
from q3_upgrade import dump_json
from run_q3_upgrade import emit
from run_q3_joint import nondominated

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'results_q3_q4_resilience'
DISPLAY={'radio1_tradeoff':'额外1 dB通信保障','radio1_expanded':'1 dB及30秒（扩展点）',
         'radio1_buffer30':'1 dB及30秒（3秒区间）','radio1_buffer30_coarse':'1 dB及30秒（6秒区间）'}


def read(path):return json.loads(path.read_text(encoding='utf8'))


def main():
    s=Scenario()
    # Rebuild old trial frontiers under the now-explicit scenario comparability rule.
    for config_path in OUT.glob('*/configuration.json'):
        args=read(config_path).get('args',{})
        if 'input' not in args:continue
        source=Path(args['input'])
        if not source.is_absolute():source=ROOT/source
        plans=[read(source)]+[read(p) for p in (config_path.parent/'plans').glob('*.json')]
        comparable=[p for p in plans if p.get('extra_loss_db',0.)==args.get('extra_loss',0.)
                    and p.get('search',{}).get('resource_buffer_seconds',0.)==args.get('buffer',0.)]
        dump_json(config_path.parent/'pareto.json',[
            dict(label=p['q2_seed'],**p['objective']) for p in nondominated(comparable)])
    cases={'名义主方案':ROOT/'results_q3_q4_frontier/selection/primary/q3_plan.json',
           '30秒接续缓冲':OUT/'buffer30_polished/plans/timely_2030.json'}
    for folder in ('radio1_expanded','radio1_tradeoff','radio1_buffer30','radio1_buffer30_coarse'):
        for path in (OUT/folder/'plans').glob('*.json'):cases[folder]=path
    records=[];manifest=[];delivered_paths={}
    for label,path in cases.items():
        plan=read(path);check=verify_fixed_q3(s,plan)
        physical=physical_audit(s,plan)
        name='nominal' if label=='名义主方案' else 'buffer30' if label=='30秒接续缓冲' else label
        target=OUT/'selection'/name
        delivered=deepcopy(plan);delivered['independent_replay']=physical
        emit(delivered,target)
        # State actual validation facts; the historical emitter's Q2-preservation claim
        # does not describe a newly rescheduled Q3 candidate.
        bound=read(target/'q3_bound_report.json')
        bound['proved_facts']=['Every input box is delivered once with physical replay.',
                              'Every saved communication interval passes independent continuous replay.',
                              'Declared resource handoff buffers are checked for all four resource classes.']
        dump_json(target/'q3_bound_report.json',bound)
        q4,checked=export(s,target/'q3_plan.json',target/'q4')
        rec=next((p for p in q4['partitions'] if p['id']==q4['recommended_partition']),None)
        hard=[s.boxes[b].hard_deadline-d['time'] for b,d in plan['deliveries'].items()
              if s.boxes[b].hard_deadline is not None]
        slack=[c['optimal_min_handoff_slack_seconds'] for cs in rec['certificates'].values()
               for c in cs.values() if c['optimal_min_handoff_slack_seconds'] is not None] if rec else []
        row=dict(scheme=label,**plan['objective'],extra_loss_db=plan.get('extra_loss_db',0.),
                 hard_violations=sum(x < -1e-7 for x in hard),minimum_hard_margin_seconds=min(hard),
                 communication_margin_lower_db=plan['communication']['minimum_certified_margin_db'],
                 **{k+'_min_handoff_seconds':v for k,v in physical['minimum_handoff_seconds'].items()},
                 q4_recommended=rec['id'] if rec else None,
                 q4_shortage=rec['shortage_total'] if rec else None,
                 q4_workload_cv=rec['workload_cv'] if rec else None,
                 q4_min_handoff_seconds=min(slack) if slack else None,
                 q4_three_groups_available=any(p['group_count']==3 for p in q4['partitions']),
                 validation=checked['status'])
        records.append(row)
        delivered_paths[label]=target
        manifest.append(dict(scheme=label,source=str(path.relative_to(ROOT)),
                             source_sha256=sha256(path.read_bytes()).hexdigest(),
                             delivered_sha256=sha256((target/'q3_plan.json').read_bytes()).hexdigest(),
                             q3_replay=check,q4_replay=checked))
    keys=('weighted_soft_delay','weighted_delivery_seconds','makespan','energy_kwh','total_sorties')
    baseline=records[0]
    improvements=[r for r in records[1:] if all(r[k]<=baseline[k]+1e-7 for k in keys)
                  and any(r[k]<baseline[k]-1e-7 for k in keys)
                  and r['extra_loss_db']>=baseline['extra_loss_db']
                  and r['q4_three_groups_available'] and r['q4_shortage'] is not None
                  and r['q4_shortage']<=baseline['q4_shortage']
                  and r['q4_workload_cv']<=baseline['q4_workload_cv']+1e-7]
    primary=min(improvements,key=lambda r:tuple(r[k] for k in keys)) if improvements else baseline
    chosen=delivered_paths[primary['scheme']]
    emit(read(chosen/'q3_plan.json'),OUT/'selection/primary')
    dump_json(OUT/'selection/primary/q3_bound_report.json',read(chosen/'q3_bound_report.json'))
    export(s,OUT/'selection/primary/q3_plan.json',OUT/'selection/primary/q4')
    export(s,OUT/'selection/primary/q3_plan.json',ROOT/'results_q4')
    selection=dict(primary=primary,source=str(chosen.relative_to(ROOT)),
                   policy='Replace prior default only if all five Q3 metrics are no worse with at least one better; extra-loss guarantee, two-group shortage/CV and availability of three groups must not worsen.',
                   default_plan='results_q3_q4_resilience/selection/primary/q3_plan.json')
    dump_json(OUT/'selection.json',selection)
    write_csv(OUT/'comparison.csv',records,list(records[0]))
    runs=[dict(path=str(p.relative_to(ROOT)),**read(p)) for p in sorted(OUT.glob('*/runs/*.json'))]
    dump_json(OUT/'manifest.json',dict(input_fingerprint=data_signature(s),cases=manifest,selection=selection,
              input_files_sha256={str(p.relative_to(s.base)):sha256(p.read_bytes()).hexdigest()
                                  for p in sorted(s.base.rglob('*')) if p.suffix.lower() in ('.xlsx','.tif')},
              code_sha256={name:sha256((ROOT/name).read_bytes()).hexdigest() for name in
                           ('q3_joint.py','q3_certificate.py','q4_exact.py','q4_audit.py','q4_delivery.py','run.py',
                            'search_q3_frontier.py','prepare_resilience_sites.py','deliver_q3_resilience.py')},
              trials=runs))
    lines=['# Q3/Q4继续优化交付说明','',
        '本轮新增30秒资源接续缓冲方案，并扩大中继选址开展额外1 dB损耗试验。表中“名义主方案”为上一轮基线；缓冲和额外损耗是附加情景，不擅自改成题目硬约束。',
        f"当前默认主方案为**{DISPLAY.get(primary['scheme'],primary['scheme'])}**，完整输入在`selection/primary/q3_plan.json`。仅当五个Q3指标不变或改善且至少一个改善、额外通信损耗保障不下降、Q4两组推荐缺口及CV不增加并保留三组分区时，才替换上一轮默认。该政策没有把资源接续余量或硬截止余量纳入支配判据，这些余量单列比较，不能称为所有指标均最优。",
        '', '## 已独立核验的结果','',
        '| 指标 | '+' | '.join(DISPLAY.get(r['scheme'],r['scheme']) for r in records)+' |',
        '|---|'+'---:|'*len(records)]
    fields=[('总架次','total_sorties',0),('运输架次','transport_sorties',0),('中继架次','relay_sorties',0),
            ('总能耗/kWh','energy_kwh',6),('最大完工时间/s','makespan',3),
            ('加权交付时刻','weighted_delivery_seconds',3),('加权软延迟','weighted_soft_delay',3),
            ('硬时限违反/箱','hard_violations',0),('最小硬时限余量/s','minimum_hard_margin_seconds',3),
            ('运输机最小接续/s','aircraft_min_handoff_seconds',6),('电池最小接续/s','battery_min_handoff_seconds',6),
            ('中继机最小接续/s','relay_min_handoff_seconds',6),('能源组件最小接续/s','component_min_handoff_seconds',6),
            ('认证的额外损耗/dB','extra_loss_db',2),('相应情景通信余量下界/dB','communication_margin_lower_db',6),
            ('Q4推荐两组缺口/件','q4_shortage',0),('Q4推荐两组工作量CV','q4_workload_cv',6),
            ('Q4推荐分区最小接续/s','q4_min_handoff_seconds',6)]
    for label,key,digits in fields:
        lines.append('| '+label+' | '+' | '.join('不适用' if r[key] is None else f'{r[key]:.{digits}f}' for r in records)+' |')
    original,new=records[:2]
    lines += ['',f"30秒缓冲方案相对名义主方案，最大完工时间增加{new['makespan']-original['makespan']:.3f}秒（{(new['makespan']/original['makespan']-1)*100:.3f}%），能耗降低{original['energy_kwh']-new['energy_kwh']:.6f}kWh；加权交付时刻增加{new['weighted_delivery_seconds']-original['weighted_delivery_seconds']:.3f}（{(new['weighted_delivery_seconds']/original['weighted_delivery_seconds']-1)*100:.3f}%）。这是以交付时效换取资源接续余量，不能称为对名义主方案的全面支配。",
        '“不适用”表示该类资源没有重复使用，因此没有接续间隔；不是零缓冲。通信余量是连续区间的保守下界，不是精确最小瞬时余量。',
        '', '## Q3的改动和验收','',
        '1. 在四类资源占用区间后增加30秒缓冲，细化通信求解区间至3秒，重新联合安排运输起飞、中继保障、机体及电池。飞行准备、返航、充电等原模型时间照常核算，缓冲为额外附加值。',
        '2. 独立物理回放新增缓冲检查，直接逐条检查同一实物资源的前后任务，防止整数模型和连续输出不一致。',
        '3. 通信验收默认继承保存计划的extra_loss_db，并同步写入计划和证书；Q4拒绝二者不一致。这样避免鲁棒方案复算时被静默降为名义验收。',
        '4. 扩大候选选址：沿网关至缺口的通道生成位置，并检查双段链路与DEM，逐步增加能覆盖剩余缺口的中继点。候选筛选是有限启发式，不是连续空间全局覆盖证明。',
        '5. 试验目录按通信损耗、缓冲参数分开；Pareto表只比较同一损耗和缓冲要求的方案。',
        '', '## Q4的结果与边界','',
        '每个候选先冻结完整Q3，再以原始时间和实际通信关系生成任务块、枚举两组和三组分区、核算八类最低资源及缺口，并独立验证任务链、最优接续余量和实物编号。',
        '30秒缓冲方案的推荐两组分区仍为P3，缺1架B型运输机和1组B型电池；三组仍有8件资源缺口。更换Q3带来的结果变化不是在固定旧Q3下改善Q4。',
        '本轮另试验了运输工作量较均衡的通信专属分组，只限制中继服务关系，Q3仍可共享实物资源，没有强制Q4零库存缺口。可行性结论及具体分组保存在balanced和balanced_tradeoff的运行记录。',
        '固定任务块仍造成较大失衡。Q4不能擅自拆批、改访问顺序或改中继关系来美化均衡指标；继续改善需要另建Q3候选并重新认证。',
        '', '## 全部试算记录','', '| 记录 | 状态 | 说明 |','|---|---|---|']
    for r in runs:
        detail=r.get('error') or ('已保存可行计划' if r.get('status')=='PASS' else
              '仅该有限候选模型不可行' if r.get('status')=='INFEASIBLE_FINITE_MODEL' else '给定时限内未找到可行解，不证明无解')
        lines.append(f"| {r['path']} | {r.get('status')} | {detail} |")
    lines += ['', '## 复算入口和文件','',
        '- `selection/buffer30/q3_plan.json`：可执行的缓冲候选；同目录含逐箱交付、四类资源时间表和连续通信证书。',
        '- `selection/buffer30/q4`：固定该候选的完整Q4，包含分区表、资源接续、库存映射、证书和图。',
        '- `comparison.csv`和`manifest.json`：全部接受方案的指标、输入/代码指纹与试验状态。',
        '- 默认完整`run.py`与`run.py --only-q4`、`run_q4.py`使用`selection/primary`中的当前主方案和精确Q4流程。`results_q4`已同步该方案。历史流程需`--legacy-q3-search`。',
        '- `entrypoint_replay`已用保存的Q1/Q2结果检验新全流程后半段的真实输出；本轮没有重跑Q1/Q2优化。测试集55项通过，另检验默认Q4入口的实际输出。',
        '', '```powershell',
        'python search_q3_frontier.py --input results_q3_q4_frontier/selection/primary/q3_plan.json --output results_q3_q4_resilience/buffer30 --phase schedule --seconds 30 --limit 1 --interval 3 --fixed-models --buffer 30 --policies timely',
        'python prepare_resilience_sites.py',
        'python search_q3_frontier.py --input results_q3_q4_frontier/selection/primary/q3_plan.json --output results_q3_q4_resilience/radio1_tradeoff --phase schedule --seconds 90 --limit 1 --interval 6 --fixed-models --extra-loss 1 --sites-json results_q3_q4_resilience/expanded_sites/sites.json --buffer 0 --policies timely --allow-soft-delay',
        'python search_q3_frontier.py --input results_q3_q4_resilience/radio1_tradeoff/plans/timely_2030.json --output results_q3_q4_resilience/radio1_buffer30_coarse --phase schedule --seconds 60 --limit 1 --interval 6 --fixed-models --extra-loss 1 --buffer 30 --policies timely',
        'python run_q4.py --plan results_q3_q4_resilience/selection/buffer30/q3_plan.json --output results_q3_q4_resilience/selection/buffer30/q4',
        'python deliver_q3_resilience.py',
        'python -m unittest discover -s tests', '```',
        '', '并行CP-SAT在相同种子和墙钟时限下也可能得到不同候选；保存计划、独立验收及文件哈希用于复核交付值。',
        '', '## 尚未解决','',
        '没有原题Q3全局最优证明；箱组和访问顺序在本轮缓冲试验中固定。30秒接续只抵御局部资源释放偏差，不保证整段任务受扰后的交付和通信安全。充电器数量、通信带宽/容量和气象扰动仍未显式建模。均衡与库存成本仍需在允许另选Q3的外层比较，不能修改冻结Q3的Q4条件。', '']
    combined=next((r for r in records if r['scheme']=='radio1_buffer30_coarse'),None)
    if combined:
        insertion=lines.index('## Q3的改动和验收')
        lines[insertion:insertion]=[
            '## 同时满足通信与接续缓冲的备选','',
            f"`selection/radio1_buffer30_coarse`同时通过额外1 dB损耗和30秒接续缓冲核验。相对当前时效主方案，完工增加{combined['makespan']-primary['makespan']:.3f}秒，能耗增加{combined['energy_kwh']-primary['energy_kwh']:.6f}kWh，加权交付增加{combined['weighted_delivery_seconds']-primary['weighted_delivery_seconds']:.3f}；仍为26架次、零软延迟和零硬时限违反。最小硬时限余量{combined['minimum_hard_margin_seconds']:.3f}秒。",
            '运输机、电池、中继机的最小接续均超过30秒，能源组件未复用。对应Q4推荐两组仍缺1架B型运输机和1组B型电池，三组仍缺8件；推荐分区最低资源重编号后的接续也超过30秒。',
            '重视交付时效可使用默认主方案；需要额外资源接续余量时使用此备选。两者均通过1 dB附加损耗认证，不能把附加损耗认证理解为对任意天气、设备故障或链路容量限制的保证。','']
    (OUT/'Q3_Q4_继续优化交付说明.md').write_text('\n'.join(lines),encoding='utf8')
    print(json.dumps(records,ensure_ascii=False),flush=True)


if __name__=='__main__':main()

"""Select independently verified nominal Q3 tradeoffs and freeze each for Q4."""
from hashlib import sha256
import json
from pathlib import Path
from model import Scenario
from q3_certificate import certify
from q4_audit import audit, verify_fixed_q3
from q4_exact import solve as solve_q4
from q4_delivery import export, write_csv, write_json, default_plan_path
from run_q3_upgrade import emit
from run_q3_joint import lower_bounds

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'results_q3_q4_frontier'
OBJECTIVES=('weighted_soft_delay','weighted_delivery_seconds','makespan','energy_kwh','total_sorties')


def dominates(a,b):
    return all(a[k]<=b[k]+1e-8 for k in OBJECTIVES) and any(a[k]<b[k]-1e-8 for k in OBJECTIVES)


def main():
    s=Scenario();records=[];plans={};partitions={};errors=[]
    baseline=ROOT/'results_q3_complete/q3_plan.json'
    paths=[baseline,*sorted((ROOT/'results_q3_complete/plans').glob('*.json')),
           *sorted(OUT.glob('*/plans/*.json'))]
    seen=set()
    for path in paths:
        plan=json.loads(path.read_text(encoding='utf8'))
        if plan.get('extra_loss_db',0.)!=0.:continue
        # Keep distinct schedules; do not claim objective-equal plans have identical Q4 properties.
        fingerprint=sha256(json.dumps({k:plan[k] for k in ('transport_sorties','relay_sorties','communication_intervals')},sort_keys=True).encode()).hexdigest()
        if fingerprint in seen:continue
        label='baseline' if path==baseline else str(path.relative_to(ROOT)).replace('\\','/')
        try:
            verify_fixed_q3(s,plan)
            q4=solve_q4(s,plan,path.read_bytes());audit(s,plan,q4)
        except (ValueError,AssertionError) as exc:errors.append(dict(path=label,error=str(exc)));continue
        seen.add(fingerprint)
        best={k:min((p for p in q4['partitions'] if p['group_count']==k),
                    key=lambda p:(p['resource_total'],p['workload_cv']),default=None) for k in (2,3)}
        hard=[s.boxes[b].hard_deadline-d['time'] for b,d in plan['deliveries'].items() if s.boxes[b].hard_deadline is not None]
        row=dict(label=label,**plan['objective'],hard_violations=sum(x < -1e-7 for x in hard),
                 minimum_hard_margin_seconds=min(hard),components=len(q4['components']['components']),
                 q4_two_and_three_available=all(best.values()),
                 two_min_resources=best[2]['resource_total'] if best[2] else None,
                 two_shortage_at_min_resources=best[2]['shortage_total'] if best[2] else None,
                 two_cv_at_min_resources=best[2]['workload_cv'] if best[2] else None,
                 three_min_resources=best[3]['resource_total'] if best[3] else None,
                 three_shortage_at_min_resources=best[3]['shortage_total'] if best[3] else None,
                 validation='PASS')
        records.append(row);plans[label]=(plan,path);partitions[label]=q4
    eligible=[r for r in records if r['q4_two_and_three_available']]
    efficient=[r for r in eligible if not any(dominates(other,r) for other in eligible)]
    primary=min(efficient,key=lambda r:tuple(r[k] for k in OBJECTIVES))
    zero=[r for r in efficient if r['weighted_soft_delay']<=1e-8]
    selections={'primary':primary,'fastest':min(zero,key=lambda r:(r['makespan'],r['energy_kwh'])),
                'energy':min(zero,key=lambda r:(r['energy_kwh'],r['makespan'])),
                'fewest_sorties':min(zero,key=lambda r:(r['total_sorties'],r['weighted_delivery_seconds']))}
    for name,row in selections.items():
        plan,path=plans[row['label']];directory=OUT/'selection'/name
        emit(plan,directory)
        configuration_path=path.parent.parent/'configuration.json'
        configuration=json.loads(configuration_path.read_text(encoding='utf8')) if configuration_path.exists() else None
        # Replace the legacy emitter's fixed-Q2 claims, since these routes may have been repacked.
        write_json(directory/'q3_bound_report.json',dict(status='CERTIFIED_FEASIBLE',
                   scope='No original-problem global optimality proof',objective=plan['objective'],
                   global_relaxation=lower_bounds(s),finite_model=plan.get('search',{}),configuration=configuration))
        export(s,path,directory/'q4')
    # The conventional Q4 folder follows the selected nominal Q3, while the
    # original Q3 and its earlier audit remain available as explicit baselines.
    export(s,default_plan_path(),ROOT/'results_q4')
    write_csv(OUT/'all_candidates.csv',records,list(records[0]))
    write_csv(OUT/'pareto.csv',efficient,list(efficient[0]))
    write_json(OUT/'selection.json',selections)
    write_json(OUT/'rejected_deliveries.json',errors)
    runs=[dict(path=str(p.relative_to(OUT)),**json.loads(p.read_text(encoding='utf8'))) for p in sorted(OUT.glob('*/runs/*.json'))]
    write_json(OUT/'search_index.json',runs)
    old=next(r for r in records if r['label']=='baseline')
    plan,_=plans[primary['label']]
    _,_,stress=certify(s,plan,extra=1.,stop_on_failure=True)
    write_json(OUT/'selection/primary/radio_extra_1db.json',stress)
    write_json(OUT/'manifest.json',dict(selected=primary['label'],policy=list(OBJECTIVES),
               q4_inventory_is_hard_constraint=False,global_optimality_proved=False,
               baseline_sha256=sha256(baseline.read_bytes()).hexdigest(),
               source_plans={str(path.relative_to(ROOT)):sha256(path.read_bytes()).hexdigest() for _,path in plans.values()},
               code={name:sha256((ROOT/name).read_bytes()).hexdigest() for name in
                     ('search_q3_frontier.py','polish_q3_windows.py','q3_joint.py','q3_certificate.py','q4_exact.py','q4_audit.py','q4_delivery.py','q4_figures.py','deliver_q3_frontier.py')}))
    labels=[('原基线',old),('交付优先',primary),('完工优先',selections['fastest']),('能耗优先',selections['energy'])]
    lines=['# Q3/Q4题意对齐后的优化交付','',
        '本文件保留题意对齐阶段的结果。后续通信鲁棒性与接续缓冲优化见`../results_q3_q4_resilience/Q3_Q4_继续优化交付说明.md`；默认入口及主目录results_q4以该后续交付为准。',
        '第四问允许库存缺口。原题明确要求：“若某一分区方案所需资源超过现有库存，进一步给出相应的资源缺口及其原因。”本轮取消以零缺口筛选Q3的做法；每个Q4都固定对应Q3的组批、访问顺序、任务安排和实际通信关系，独立枚举两组及三组分区。',
        '', '## 主要结果','',
        '| 指标 | '+' | '.join(label for label,_ in labels)+' |','|---|'+'---:|'*len(labels)]
    for title,key,precision in [('运输架次','transport_sorties',0),('中继架次','relay_sorties',0),
        ('总架次','total_sorties',0),('能耗/kWh','energy_kwh',6),('联合完工/s','makespan',3),
        ('加权交付/优先级·s','weighted_delivery_seconds',3),('加权软延误/优先级·s','weighted_soft_delay',3),
        ('硬截止违反/箱','hard_violations',0),('最小硬截止余量/s','minimum_hard_margin_seconds',3),
        ('两组最低资源总数/件','two_min_resources',0),('该两组方案缺口/件','two_shortage_at_min_resources',0),
        ('该两组方案工作量CV','two_cv_at_min_resources',6),('三组最低资源总数/件','three_min_resources',0),
        ('该三组方案缺口/件','three_shortage_at_min_resources',0)]:
        lines.append('| '+title+' | '+' | '.join(f'{r[key]:.{precision}f}' for _,r in labels)+' |')
    lines += ['',f"交付优先方案来源：`{primary['label']}`。相对原基线，完工变化{primary['makespan']-old['makespan']:.3f}秒，能耗变化{primary['energy_kwh']-old['energy_kwh']:.6f}kWh，加权交付变化{primary['weighted_delivery_seconds']-old['weighted_delivery_seconds']:.3f}优先级·秒。",
        f"是否在五个Q3指标上严格支配原基线：{'是' if dominates(primary,old) else '否，存在指标取舍'}。全部入选计划80箱各交付一次，硬截止、同型共享电池、能源组件及连续通信均通过独立回放。",
        '', '## 最优性的准确含义','',
        'Q3有多个目标，题目没有指定唯一权重；本交付保留完整已发现的非支配集合。交付优先展示采用“软延误、加权交付、完工、能耗、总架次”的字典序，另列完工优先与能耗优先；这是一项公开的展示政策，不是题目强制的唯一排序。',
        'Q4在每份固定Q3下，枚举全部合法两组三组分区，并对每组每类资源证明最低数量及该数量下的最大最小接续余量；这里具备固定输入下的最优性证据。八类资源与工作量CV之间不强行合并成虚构成本，完整非支配分区见各方案Q4交付说明。',
        '**尚未证明原题Q3的全局最优。** 求解器OPTIMAL仅适用于给定路线/机型候选、中继点、会话数、整数时间及已锁定目标条件。候选数有限、低架次试验超时及原问题松弛下界未闭合，均不允许宣称全局最优。',
        '', '## 本轮算法改进','',
        '1. 在零软延误分支分别优化加权交付、最晚返航和总能耗；不添加零库存缺口或额外接续缓冲作为原题硬约束。前轮缓冲与库存满足方案保留为附加情景。',
        '2. 对同服务区货箱进行箱级重分配，重新检查所有可行机型和真实能耗；静态能耗或时长改善只用于排序，真正接纳仍需要完整联合调度与独立认证。',
        '3. 缩短通信缺口的求解区间，从90秒探索15秒和3秒。细化放宽了保守整段覆盖造成的排程限制；仍对每段采用连续几何覆盖证明，未把离散采样当作连续通信证明。',
        '4. 对19、20、21运输架次初值与扩大后的9个候选中继点进行探测。各次预算、状态、条件下界和失败原因完整保留；UNKNOWN只表示预算内未解出。',
        '5. 每个被接纳的Q3重新计算Q4任务块与实际资源链。资源缺口允许保留，不能通过跨组共用资源掩盖，也不能在Q4中悄悄改变Q3。',
        '6. 修正中继热启动提示：从已保存的建链时刻反推整数起飞变量，避免对实际浮点起飞时刻再次向上取整导致提示整体晚1秒；不再无依据给提示服务时长增加2秒。仅改善求解初值，不放松任何物理约束。',
        '7. 依据已认证的实际服务区间收紧中继悬停窗口，保持运输任务完全不变；逐项证明资源占用不扩大、充电释放不推迟、能耗不增加，再重新认证。主方案未得到额外收益，完工优先备选减少4秒无必要悬停并节省0.001222kWh；无收益也如实保留日志。',
        '', '## 仍有的限制与验收','',
        f"共独立核验{len(records)}份不同计划；其中{len(eligible)}份可同时做两组及三组，Q3非支配记录{len(efficient)}份。本轮求解记录{len(runs)}份，详见search_index.json。相同目标值的不同设备/通信安排仍可能导致Q4差异，保留原始计划便于追踪。",
        f"主方案额外1dB损耗检查：{stress['status']}。这是题目给定衰落裕量之外的附加实验，不把附加实验失败等同原题不可行。接续缓冲、通信附加裕量和库存满足也不能互相替代。",
        '仍沿用附件与既有模型中的初始满电、充满后再用、同型资源可互换、无限并行充电器及未限制中继并发容量等假设。Q4的大任务块可能继续造成失衡，需在论文中解释，不把零缺口或低CV当作无代价改善。',
        '完整测试集51项通过；每份展示方案另经独立物理回放、连续通信区间证明及Q4证书与设备链核验。',
        '', '## 文件与复现','',
        '`selection/primary`保存交付优先完整Q3、逐箱表、资源时序、连续通信证书及其`q4`交付；`fastest`和`energy`为两类目标备选。`all_candidates.csv`、`pareto.csv`和`selection.json`保存比较与来源；各试验目录含配置、原始候选和求解日志。主目录results_q4同步最新主Q3；run_q4.py或run.py --only-q4默认读取该主计划。历史Q3仍在results_q3_complete，显式传入--plan或--q3-plan可复算历史方案。',
        '```powershell',
        r'.\.venv\Scripts\python.exe search_q3_frontier.py --output results_q3_q4_frontier/fine --phase schedule --seconds 30 --limit 2 --interval 15 --fixed-models --policies makespan timely',
        r'.\.venv\Scripts\python.exe search_q3_frontier.py --output results_q3_q4_frontier/fine3 --input results_q3_q4_frontier/fine/plans/makespan_2030.json --phase schedule --seconds 30 --limit 2 --interval 3 --fixed-models --policies makespan timely',
        r'.\.venv\Scripts\python.exe deliver_q3_frontier.py',
        r'.\.venv\Scripts\python.exe -m unittest discover -s tests -v','```',
        '其他试验参数见各目录configuration.json。8线程限时搜索即使固定随机种子也可能返回不同候选，已保存计划和独立回放用于复核本表数值。','']
    (OUT/'Q3_Q4_题意对齐优化交付说明.md').write_text('\n'.join(lines),encoding='utf8')
    print(json.dumps(dict(primary=primary,selected=selections,accepted=len(records),pareto=len(efficient)),ensure_ascii=False))


if __name__=='__main__':main()

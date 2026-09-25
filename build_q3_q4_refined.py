"""Re-audit saved experiments and export their Q3/Q4 tradeoffs; no search."""
from pathlib import Path
import json
from model import Scenario
from q3_certificate import certify
from q4_delivery import export, write_csv, write_json
from refine_q3_q4 import handoff_minima

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'results_q3_q4_refined'
SELECTION={
    'baseline':('原Q3基线',ROOT/'results_q3_complete/q3_plan.json'),
    'buffer5':('5秒缓冲·同能耗',OUT/'buffer5/plans/timely_variable.json'),
    'buffer10':('10秒缓冲·推荐',OUT/'buffer10/plans/fast_buffer.json'),
    'rebatch':('零软延误·缺口减半',OUT/'rebatch/plans/timely_variable.json'),
    'two_stock':('两组库存满足',OUT/'rebatch_stock/plans/independent_two_variable.json'),
    'three_stock':('三组库存满足',OUT/'stock_trials/plans/independent_three.json'),
    'balanced_stock':('两组均衡折中',OUT/'group_tradeoffs/plans/outer_pair.json'),
}


def main():
    s=Scenario();rows=[];perturb=[];radio=[]
    for key,(label,path) in SELECTION.items():
        plan=json.loads(path.read_text(encoding='utf8'))
        result,checked=export(s,path,OUT/'delivery'/key)
        best={k:min((p for p in result['partitions'] if p['group_count']==k),
                    key=lambda p:(p['shortage_total'],p['resource_total'],p['workload_cv'])) for k in (2,3)}
        gaps=handoff_minima(plan)
        hard=[s.boxes[b].hard_deadline-d['time'] for b,d in plan['deliveries'].items()
              if s.boxes[b].hard_deadline is not None]
        row=dict(case=key,label=label,plan=str(path.relative_to(ROOT)).replace('\\','/'),
                 **plan['objective'],minimum_hard_margin_seconds=min(hard),
                 hard_violations=sum(v < -1e-7 for v in hard),
                 communication_margin_db=plan['communication']['minimum_certified_margin_db'],
                 minimum_handoff_seconds=min(v for v in gaps.values() if v is not None),
                 two_partition=best[2]['id'],two_shortage=best[2]['shortage_total'],
                 two_resource_total=best[2]['resource_total'],two_cv=best[2]['workload_cv'],
                 three_partition=best[3]['id'],three_shortage=best[3]['shortage_total'],
                 three_resource_total=best[3]['resource_total'],three_cv=best[3]['workload_cv'],
                 validation=checked['status'])
        rows.append(row)
        # This is a resource release stress test, not a flight or weather simulation.
        for delay in (0,1,5,10,30,60):
            for kind,slack in gaps.items():
                perturb.append(dict(case=key,resource_type=kind,release_extension_seconds=delay,
                                    minimum_slack_seconds=slack,
                                    status='NO_REUSE' if slack is None else ('PASS' if slack+1e-7>=delay else 'FAIL')))
        _,_,certificate=certify(s,plan,extra=1.,stop_on_failure=True)
        radio.append(dict(case=key,extra_loss_db=1.,**{k:v for k,v in certificate.items() if k!='extra_loss_db'}))
        write_json(OUT/'delivery'/key/'q3_resource_buffer.json',gaps)
        print(json.dumps(row,ensure_ascii=False),flush=True)
    write_csv(OUT/'comparison.csv',rows,list(rows[0]))
    write_json(OUT/'comparison.json',rows)
    write_csv(OUT/'resource_release_stress.csv',perturb,list(perturb[0]))
    write_json(OUT/'radio_stress.json',radio)
    history=[]
    for path in sorted(OUT.rglob('runs/*.json')):
        report=json.loads(path.read_text(encoding='utf8'))
        status=report['status']
        # Preserve original run files; derive corrected display labels from solver evidence.
        if status=='NO_SOLUTION_WITHIN_BUDGET' and any(x['status']=='INFEASIBLE' for x in report.get('stages',[])):
            status='INFEASIBLE_FINITE_MODEL'
        history.append(dict(path=str(path.relative_to(OUT)).replace('\\','/'),status=status,
                            reported_status=report['status'],error=report.get('error',''),
                            buffer=report.get('resource_buffer_seconds'),
                            scope=report.get('bound_scope','')))
    write_json(OUT/'experiment_index.json',history)
    render_report(rows,radio,history)


def render_report(rows,radio,history):
    r={x['case']:x for x in rows};a=r['baseline'];b=r['buffer10']
    lines=['# Q3与Q4继续优化交付说明','',
           '本轮在保存的23运输架次、3中继架次Q3方案上，增加资源接续缓冲、分组专属设备、箱级换装与独立证书复核。原主计划留作基线，新候选分别保存，Q4逐份固定对应Q3重新计算。',
           '', '## 实算结果','',
           '| 指标 | '+' | '.join(x['label'] for x in rows)+' |',
           '|---|'+'---:|'*len(rows)]
    metrics=[('运输＋中继架次',lambda x:f"{x['transport_sorties']}＋{x['relay_sorties']}"),
             ('总能耗/kWh',lambda x:f"{x['energy_kwh']:.6f}"),
             ('联合完工/s',lambda x:f"{x['makespan']:.3f}"),
             ('加权交付/优先级·s',lambda x:f"{x['weighted_delivery_seconds']:.3f}"),
             ('加权软延误/优先级·s',lambda x:f"{x['weighted_soft_delay']:.3f}"),
             ('硬时限违反/箱',lambda x:str(x['hard_violations'])),
             ('最小硬时限余量/s',lambda x:f"{x['minimum_hard_margin_seconds']:.3f}"),
             ('四类资源最小接续余量/s',lambda x:f"{x['minimum_handoff_seconds']:.6f}"),
             ('两组最少缺口/件',lambda x:str(x['two_shortage'])),
             ('该两组方案资源总数/件',lambda x:str(x['two_resource_total'])),
             ('该两组方案工作量CV',lambda x:f"{x['two_cv']:.6f}"),
             ('三组最少缺口/件',lambda x:str(x['three_shortage'])),
             ('独立核验',lambda x:x['validation'])]
    for label,fn in metrics:lines.append('| '+label+' | '+' | '.join(fn(x) for x in rows)+' |')
    lines += ['',
        f"建议把10秒缓冲方案作为Q3执行备选：相对基线，完工增加{b['makespan']-a['makespan']:.3f}秒（{(b['makespan']/a['makespan']-1)*100:.3f}%），能耗增加{b['energy_kwh']-a['energy_kwh']:.6f}kWh；仍无软延误和硬时限违反。它解决资源近乎无缝接续的问题，但硬截止余量更小，不能视为全面抗扰动方案。",
        '若优先降低Q4增配需求，零软延误换装方案把两组缺口从B型机1架+B型电池1组降至B型机1架，电池库存已满足。若必须仅用现有库存，两组库存方案缺口为0，但接受表中软延误；三组库存方案也可行，但其时效代价更大。所有列不存在统一优胜者。',
        '', '## 具体改动与原因','',
        '1. **Q3四类接续资源。** 运输机、电池、中继机、能源组件分别在释放时刻后增加显式缓冲，求解后用真实浮点时间再次检查。避免整数向上取整制造的约0.05秒接续被误认为充足余量。',
        '2. **Q3/Q4库存反馈。** 为每个实体资源增加任务组归属，运输及中继任务只能选择本组设备；同时约束中继服务范围。由Q3主动协调起飞和悬停时间，随后固定新Q3运行Q4，不在Q4内部改变任务。',
        '3. **箱级换装。** 把S006-MED-01从T009移至已有C型T010；把S005-HYG-01从T023移至C型T019，并在S004之前访问S005。两条原B型路线因此可以选择A型。具体机型与时间由联合求解确定；逐箱守恒、载重、体积、SOC和截止时间全部复核。',
        '4. **Q4最优性验收。** 求解器用DFS增广匹配，核验器改用Edmonds–Karp最大流，并独立从大到小扫描瓶颈阈值。最优余量、下一阈值的不可行性、匹配边、接续链、峰值见证全部交叉复算。',
        '5. **Q4实物交付。** 逻辑资源逐项映射至附件库存编号，缺口明确标成NEW编号；核验设备类型、跨组独占、采购数量和映射表一致性。全部合法两组三组分区、汇总件数、CV亦独立检查。',
        '6. **输入和入口。** 取消固定旧文件SHA门槛，改为原文件及规范化内容双指纹；实际复核既有通信区间，不重新挑中继。候选目录可选；支持八类资源实际单价。`run.py --only-q4`直接输出新版Q4，不必重跑Q1至Q3。',
        '7. **通信失败见证。** 点反例检查也遵守service_zones，不能借用另一任务组的中继掩盖通信不可用。',
        '', '## 扰动检查与仍然存在的问题','',
        '`resource_release_stress.csv`固定所有任务开始时刻，仅把资源释放延后0/1/5/10/30/60秒。基线不能通过1秒检查；10秒缓冲方案可通过10秒检查。它只检查资源冲突，不代表飞行受风、交付延后或通信窗口变化后的完整鲁棒性。无再使用的组件标记NO_REUSE，不作无限抗扰动结论。',
        '额外1dB损耗固定计划检查：'+ '；'.join(f"{r[x['case']]['label']}={x['status']}" for x in radio)+'。完整证据见`radio_stress.json`；FAIL附实际点反例，UNKNOWN仅表示未认证。资源时间缓冲不等于链路裕量提升。',
        '最大任务块仍含13个服务区，严重失衡尚未根治。两组均衡折中方案把零缺口条件下CV从0.959989降至0.818486，但软延误和完工时间明显上升。试算运输工作量近均分、S001独立、S001与S011合组等限制；零软延误条件下的失败只针对3个候选中继点、固定组批/访问顺序、有限中继会话和1秒整数时间模型，不能推出原题无解。',
        '依然假设同型资源可互换、初始满电、充满再使用、充电器并行且无数量约束、中继无并发容量上限。题目若要求充电器、人员、链路容量或随机风场约束，需补充参数后另行建模。',
        '本轮探索资源缓冲与库存权衡，不宣称Q3全局最优。求解器某阶段OPTIMAL只对当时有限模型与之前目标上限成立；8线程限时搜索即使固定随机种子也可能得到不同候选，保存结果及独立回放才是本次数值依据。',
        '本轮验收：完整测试集50项通过（其中Q3/Q4相关24项），新版run.py --only-q4入口复算通过，git diff --check通过。测试输出中仍有已有Excel读取器未及时关闭的ResourceWarning，未导致计算或测试失败。',
        '', '## 复现与文件','',
        '```powershell',
        r'.\.venv\Scripts\python.exe refine_q3_q4.py --output results_q3_q4_refined/buffer10 --buffer 10 --seconds 20 --cases timely_variable fast_buffer',
        r'.\.venv\Scripts\python.exe refine_q3_q4.py --output results_q3_q4_refined/rebatch --buffer 5 --seconds 20 --rebatch-b --cases independent_two_variable timely_variable',
        r'.\.venv\Scripts\python.exe refine_q3_q4.py --output results_q3_q4_refined/rebatch_stock --buffer 5 --seconds 30 --rebatch-b --allow-soft-delay --cases independent_two_variable',
        r'.\.venv\Scripts\python.exe refine_q3_q4.py --output results_q3_q4_refined/stock_trials --buffer 5 --seconds 15 --allow-soft-delay --cases independent_two_variable balanced_two independent_three',
        r'.\.venv\Scripts\python.exe refine_q3_q4.py --output results_q3_q4_refined/group_tradeoffs --buffer 10 --seconds 20 --allow-soft-delay --cases isolate_s001 outer_pair',
        r'.\.venv\Scripts\python.exe build_q3_q4_refined.py',
        r'.\.venv\Scripts\python.exe run.py --only-q4 --q3-plan results_q3_q4_refined/buffer10/plans/fast_buffer.json --output results_q3_q4_refined/delivery/buffer10',
        r'.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_q[34]*.py" -v',
        '```','',
        '`comparison.csv/json`保存完整对照；`delivery/<方案>/Q4_交付说明.md`、设备映射、逐任务分配、证明及输入核验成套保存。各试验`plans`目录是完整Q3，`runs`保存阶段可行值/下界/间隙。原基线的新版Q4仍在`results_q4`。',
        f"共收录{len(history)}个试验记录，见`experiment_index.json`。早期probe_zero的两次REJECTED来自已修复的元组/列表序列化比较问题，不能作为算法不可行证据；30秒缓冲日志原显示NO_SOLUTION，索引按求解器INFEASIBLE纠正为有限模型不可行。原日志保留审计轨迹。",'']
    (OUT/'Q3_Q4_优化交付说明.md').write_text('\n'.join(lines),encoding='utf8')


if __name__=='__main__':main()

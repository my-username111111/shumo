"""Generate the final report and fresh audits; search logs are historical."""
import json
from hashlib import sha256
from pathlib import Path

from model import Scenario
from q4_audit import verify_fixed_q3
from q4_delivery import write_csv
from q3_upgrade import dump_json

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'results_q3_q4_balance'


def read(path):return json.loads(path.read_text(encoding='utf8'))


def main():
    s=Scenario(); rows=read(OUT/'comparison.json');by={r['case']:r for r in rows}
    cases=['baseline_2','baseline_3','two_zero_shortage','same_q3_three_groups','three_balanced','two_one_shortage']
    labels=['原方案两组','原方案三组','新主方案两组','同一新Q3三组','另选Q3的三组均衡备选','两组补1件备选']
    checks=[]
    for name in cases:
        row=by[name];path=ROOT/row['plan_path'];plan=read(path)
        check=verify_fixed_q3(s,plan)
        assert sha256(path.read_bytes()).hexdigest()==row['sha256']
        checks.append(dict(case=name,sha256=row['sha256'],**check))
    lines=['# Q3/Q4 缺口与均衡联合优化交付说明','',
        '本轮目标是同时降低资源缺口与工作量失衡。以最新26架次节能Q3（71.907249 kWh）为对照，重新组批、拆开跨通信区域的访问任务，并把组内资源峰值反馈到Q3调度；每份Q3冻结后再完整枚举其Q4。工作量为运输准备至返航时间与中继任务时间之和，CV越小越均衡。',
        '', '## 同一个Q3下的正式两组与三组结果','',
        '主方案保存在 `selection/two_zero_shortage/q3_plan.json`，以下两组和三组**共享同一份Q3**。两组缺口2→0、CV显著下降；三组在缺口不超过6件的备选中选择CV最小者，避免再次按件数优先选出单区小组。该分区政策是本轮偏好，不是题目额外硬约束。',
        '', '| 指标 | 原两组 | 新两组 | 原三组 | 同一新Q3三组 |','|---|---:|---:|---:|---:|']
    chosen=[by[x] for x in ('baseline_2','two_zero_shortage','baseline_3','same_q3_three_groups')]
    for label,key,fmt in [('资源缺口/件','shortage','.0f'),('资源总件数','resource_total','.0f'),('工作量CV','workload_cv','.6f'),
                          ('总能耗/kWh','energy_kwh','.6f'),('完工/s','makespan','.3f'),
                          ('加权软延误/优先级·s','weighted_soft_delay','.3f'),('运输＋中继总架次','total_sorties','.0f')]:
        lines.append('| '+label+' | '+' | '.join(format(r[key],fmt) for r in chosen)+' |')
    lines+=['', '新两组三组均为80箱恰好交付一次、31个硬时限货箱全部按时，通信未证明时长和中断时长均为0。保留额外1 dB损耗认证，Q3实际设备四类复用至少有5秒接续。Q4重编号的最优接续余量逐资源见证书；不能把Q3的5秒直接解释为每条Q4最少设备链都具有5秒。',
            '', '### 分区与资源明细','']
    primary=OUT/'selection/two_zero_shortage'
    selections=[('新主方案两组',read(primary/'balanced_selection.json')['selected_partition']),
                ('同一新Q3三组',read(primary/'same_q3_three_groups.json')),
                ('另选Q3三组均衡备选',read(OUT/'selection/three_balanced/balanced_selection.json')['selected_partition'])]
    for label,p in selections:
        lines+=['#### '+label+'（'+p['id']+'）','']
        total=sum(g['work_seconds'] for g in p['groups'])
        for g in p['groups']:
            lines.append(f"- {g['id']}={{{', '.join(g['zones'])}}}；{g['boxes']}箱，工作量{g['work_seconds']:.3f}秒，占{g['work_seconds']/total:.2%}。")
        need=p['resource_need'];short=p['shortage']
        lines+=['',f"需求：运输机A/B/C={need['A_aircraft']}/{need['B_aircraft']}/{need['C_aircraft']}，共享电池A/B/C={need['A_batteries']}/{need['B_batteries']}/{need['C_batteries']}，中继机/能源组件={need['relay_aircraft']}/{need['relay_components']}。",
                '缺口：'+('无。' if not p['shortage_total'] else '，'.join(f'{k}={v}' for k,v in short.items() if v)+'。'),'']
    lines+=['## 独立联合方案之间的代价比较','',
        '**三组均衡备选重新选择了Q3，不能与主方案两组混称为固定同一Q3的Q4对照。**它的三组CV更低，但完工更晚且仍需增配。各目录都保存对应Q3的全部两组、三组分区。',
        '', '| 方案 | 缺口 | CV | 能耗/kWh | 完工/s | 软延误 | 最小硬时限余量/s |','|---|---:|---:|---:|---:|---:|---:|']
    for name,label in zip(cases[2:],labels[2:]):
        r=by[name]
        lines.append(f"| {label} | {r['shortage']} | {r['workload_cv']:.6f} | {r['energy_kwh']:.6f} | {r['makespan']:.3f} | {r['weighted_soft_delay']:.3f} | {r['minimum_hard_margin_seconds']:.3f} |")
    lines+=['',
        '本轮不是对原方案所有指标的支配：原方案软延误为0，新方案允许普通物资超过期望时限，医疗和首批硬时限仍严格满足。两组零缺口以更多架次、能耗及较晚交付换取独立运行和均衡；三组极均衡备选还存在较小的硬时限余量。没有找到“零缺口、近均分、零软延误”全部同时满足的已认证方案；限时未找到与有限模型不可行均不证明原题无解。',
        '', '## 改了什么、为什么有效','',
        '1. 将S006-MED-01、S005-HYG-01换到既有可承载路线，释放部分B型需求；按现有三处中继的覆盖区域拆分跨区域访问的T002、T010、T014、T019，运输架次23→27。每箱只交付一次，拆分后用完整物理模型重新选择机型与排程。',
        '2. 原固定任务形成13区大块；新主方案形成6个块。中继按任务组提供服务，另排其时间窗口，不能跨组借用通信掩盖缺口。分组候选先用运输时长和候选点数排序，最终CV必须加上实际中继工作量。',
        '3. `q3_joint.py`增加分组资源峰值约束：对八类资源分别汇总组内容量，逐类型取正缺口后求和，禁止不同机型/电池库存互抵。Q3始终仅用真实总库存，没有把Q4待增配设备偷偷加入Q3。',
        '4. `optimize_q3_timing.py`保留被选Q4的组内资源链，防止Q3时间精修增加Q4资源需求。最终仍重新枚举、重放Q4，而不直接相信容量变量。',
        '5. `q4_delivery.py`默认采用本轮两组零缺口Q3，适配本次“缺口与均衡优先”的要求；原时效方案文件保留。独立三组备选要显式传入它的Q3文件。',
        '', '## 通信核验数值问题及修复','',
        '本轮发现：近共线的网关—运动端点三角形，按绝对行列式阈值分类后可能进入病态平面求逆，误判山体遮挡。旧三组候选T020在5374至5525秒曾保存正直连证书，但区间内存在负点余量；原物理计划还能由中继覆盖，不代表旧“直连”归属正确。',
        '已改为与几何尺度相关的退化判定；退化三角形按三条边构造最低高度包络、逐栅格裁剪，非退化三角形使用局部坐标求解。栅格单元边界做保守扩张和高度容差。搜索几何亦提高退化判定稳定性，并把缓存版本升级到4。保存分段边界只作为再细分提示，每段仍独立验算，不信任旧margin/mode。',
        '本轮交付的selection及对照基线已重新认证，geometry_version=2；三组时间精修的旧失败结果未纳入交付。历史搜索runs里的PASS代表当时版本，不能直接当作新版证书。旧仓库计划重新使用前应重新accept并冻结新通信关系，Q4只核验保存关系，不自动改写它。',
        '', '## 最优性与适用边界','',
        'Q3是有限候选、限时搜索和固定结构LP，不能称原题全局最优；日志保存每阶段值、下界和间隙。原子块及冻结时间给定后，Q4枚举全部分区并用区间峰值/匹配证书证明各分区的最低资源数。两组缺口已经达到非负下界0，只能说缺口指标最优，不能说综合方案全局最优。',
        'CV≤0.2是独立均衡备选的筛选偏好。同一主Q3的三组仍受任务块限制，最佳均衡不足以做到近均分；它已经同时减少缺口并改善CV。未新增充电器数量、带宽容量或随机风场假设，仍采用题目已有能耗与并行充电口径。',
        '', '## 复现、验收与文件','',
        '本轮38项Q3/Q4测试全部通过，包括真实负链路余量反例、近共线地形遮挡、跨组资源不得共享、不同类型库存不得互抵、电池充满时间和分区资源保护。默认run.py --only-q4已复算通过，独立核验121个合法分区、7502条资源分配；git diff --check通过。',
        '',
        '```powershell',
        '.\\.venv\\Scripts\\python.exe deliver_q3_q4_balance.py --case two_zero_shortage --input results_q3_q4_balance/two_zero_deep/plans/g2_002.json --groups 2',
        '.\\.venv\\Scripts\\python.exe deliver_q3_q4_balance.py --case three_balanced --input results_q3_q4_balance/three_six_fixed/plans/g3_000.json --groups 3',
        '.\\.venv\\Scripts\\python.exe deliver_q3_q4_balance.py --case two_one_shortage --input results_q3_q4_balance/two_polish/plans/g2_000.json --groups 2',
        '.\\.venv\\Scripts\\python.exe build_q3_q4_balance_report.py',
        '.\\.venv\\Scripts\\python.exe run.py --only-q4 --output results_q3_q4_balance/default_q4_replay',
        '.\\.venv\\Scripts\\python.exe -m unittest discover -s tests -p "test_q[34]*.py" -v',
        '```','',
        '各搜索目录configuration保存输入哈希和参数；selection保存已认证Q3、LP记录、Q4完整分区、实物编号、接续链、最优性证书和验收。`balanced_selection.json`标明所选均衡分区，`same_q3_three_groups.json`是主Q3的正式三组折中。`comparison.csv/json`是统一对照，`final_validation.json`与`manifest.json`记录本次独立回放及输入/源码指纹。', '']
    text='\n'.join(lines)
    (OUT/'Q3_Q4_缺口与均衡优化交付说明.md').write_text(text,encoding='utf8')
    dump_json(OUT/'final_validation.json',checks)
    code=['q3_joint.py','q3_certificate.py','q3_upgrade.py','optimize_q3_timing.py',
          'optimize_q3_q4_balance.py','deliver_q3_q4_balance.py','build_q3_q4_balance_report.py','q4_delivery.py']
    dump_json(OUT/'manifest.json',dict(plans=[dict(case=x['case'],sha256=x['sha256']) for x in checks],
        code={f:sha256((ROOT/f).read_bytes()).hexdigest() for f in code},
        note='Final validation supersedes historical search certificates.'))
    marker='\n## 本轮缺口与均衡选型补充\n'
    for case in ('two_zero_shortage','three_balanced','two_one_shortage'):
        folder=OUT/'selection'/case
        p=read(folder/'balanced_selection.json')['selected_partition']
        f=folder/'q4/Q4_交付说明.md'
        suffix=f"\n本轮均衡偏好选择 {p['id']}：缺口{p['shortage_total']}件，CV={p['workload_cv']:.6f}。全部Q4结果固定本目录同一份Q3。\n"
        if case=='two_zero_shortage':
            p3=read(folder/'same_q3_three_groups.json')
            suffix+=f"\n同一Q3的正式三组折中为 {p3['id']}：缺口{p3['shortage_total']}件，CV={p3['workload_cv']:.6f}。这是缺口不超过6件条件下CV最小的分区，与三组最低件数优先方案不同。\n"
        suffix+='\n另选Q3的三组极均衡方案不能与本计划混为同一输入对照。完整代价和数值核验修复见 ../../../Q3_Q4_缺口与均衡优化交付说明.md。\n'
        f.write_text(f.read_text(encoding='utf8').split(marker)[0]+marker+suffix,encoding='utf8')
    print(json.dumps(dict(status='PASS',cases=len(checks),report=str(OUT/'Q3_Q4_缺口与均衡优化交付说明.md')),ensure_ascii=False))


if __name__=='__main__':main()

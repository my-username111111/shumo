"""Protect both partition choices while rescheduling Q3 physical resources."""
import unittest
from copy import deepcopy
from ortools.sat.python import cp_model
from q3_joint import add_partition_resource_limits
from optimize_q3_time_guarded import evaluate


class MultiPartitionLimitsTests(unittest.TestCase):
    def run_model(self, windows, limits):
        model=cp_model.CpModel()
        stock={'aircraft':2,'battery':2}
        intervals={(g,k):[] for g in range(3) for k in stock}
        for (g,k),rows in windows.items():
            for i,(start,end) in enumerate(rows):
                intervals[g,k].append(model.NewFixedSizeIntervalVar(start,end-start,f'{g}_{k}_{i}'))
        add_partition_resource_limits(model,intervals,stock,3,limits)
        return cp_model.CpSolver().Solve(model)

    def test_merged_group_reuses_resource_but_three_groups_cannot(self):
        windows={(0,'aircraft'):[(0,10)],(1,'aircraft'):[(0,10)],(2,'aircraft'):[(10,20)]}
        two=dict(blocks=[[0,2],[1]],resource_need={'aircraft':2})
        self.assertEqual(self.run_model(windows,[two]),cp_model.OPTIMAL)
        three=dict(blocks=[[0],[1],[2]],resource_need={'aircraft':2})
        self.assertEqual(self.run_model(windows,[two,three]),cp_model.INFEASIBLE)
        three['resource_need']['aircraft']=3
        self.assertEqual(self.run_model(windows,[two,three]),cp_model.OPTIMAL)

    def test_battery_charge_can_break_otherwise_valid_aircraft_bound(self):
        windows={(0,'aircraft'):[(0,10)],(2,'aircraft'):[(10,20)],
                 (0,'battery'):[(0,20)],(2,'battery'):[(10,30)]}
        limit=dict(blocks=[[0,2],[1]],resource_need={'aircraft':1,'battery':1})
        self.assertEqual(self.run_model(windows,[limit]),cp_model.INFEASIBLE)
        limit['resource_need']['battery']=2
        self.assertEqual(self.run_model(windows,[limit]),cp_model.OPTIMAL)

    def test_overlapping_or_missing_blocks_rejected(self):
        for blocks in ([[0,1],[1,2]], [[0],[1]], [[0,1,2],[]]):
            with self.assertRaises(ValueError):
                self.run_model({},[dict(blocks=blocks,resource_need={'aircraft':1})])

    def test_shortage_budget_counts_types_separately(self):
        windows={(0,'battery'):[(0,10)],(1,'battery'):[(10,20)],(2,'battery'):[(20,30)]}
        limit=dict(blocks=[[0],[1],[2]],shortage_total=0,resource_total=3)
        self.assertEqual(self.run_model(windows,[limit]),cp_model.INFEASIBLE)
        limit['shortage_total']=1
        self.assertEqual(self.run_model(windows,[limit]),cp_model.OPTIMAL)
        limit['resource_total']=2
        self.assertEqual(self.run_model(windows,[limit]),cp_model.INFEASIBLE)


class AcceptanceTests(unittest.TestCase):
    def test_faster_candidate_cannot_hide_changed_resource_type_or_balance(self):
        old=dict(id='before',group_count=2,resource_need={'A':2,'B':1},
                 groups=[dict(zones=['x']),dict(zones=['y'])],workload_cv=.2,
                 shortage_total=1,resource_total=3)
        baseline=dict(objective=dict(makespan=100.,energy_kwh=10.,weighted_delivery_seconds=200.))
        plan=dict(objective=dict(makespan=90.,energy_kwh=10.,weighted_delivery_seconds=200.,weighted_soft_delay=0))
        new=deepcopy(old);new['id']='renumbered'
        q4=dict(partitions=[new])
        self.assertEqual(evaluate(plan,q4,baseline,[old])['status'],'ACCEPTED')
        new['resource_need']={'A':1,'B':2}
        self.assertEqual(evaluate(plan,q4,baseline,[old])['status'],'REJECTED')
        new['resource_need']=old['resource_need'];new['workload_cv']=.22
        self.assertEqual(evaluate(plan,q4,baseline,[old])['status'],'REJECTED')


if __name__=='__main__': unittest.main()

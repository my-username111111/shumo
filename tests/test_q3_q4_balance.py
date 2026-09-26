"""Regression checks for inventory feedback across independent task groups."""
import unittest

from ortools.sat.python import cp_model
from q3_joint import add_partition_capacity_budget, solve


class PartitionCapacityTests(unittest.TestCase):
    def status(self, windows, stock, budget):
        model=cp_model.CpModel()
        groups=1+max(g for g,_ in windows)
        intervals={(g,k):[] for g in range(groups) for k in stock}
        for (g,k), rows in windows.items():
            for i,(start,end) in enumerate(rows):
                intervals[g,k].append(model.NewFixedSizeIntervalVar(start,end-start,f'{g}_{k}_{i}'))
        add_partition_capacity_budget(model,intervals,stock,groups,budget)
        return cp_model.CpSolver().Solve(model)

    def test_nonoverlapping_groups_still_require_separate_stock(self):
        windows={(0,'A'):[(0,10)],(1,'A'):[(10,20)]}
        self.assertEqual(self.status(windows,{'A':1},0),cp_model.INFEASIBLE)
        self.assertEqual(self.status(windows,{'A':1},1),cp_model.OPTIMAL)

    def test_reuse_within_group_counts_peak_not_task_count(self):
        windows={(0,'A'):[(0,10),(10,20)],(1,'A'):[(0,20)]}
        self.assertEqual(self.status(windows,{'A':2},0),cp_model.OPTIMAL)

    def test_surplus_of_one_type_cannot_offset_another_type(self):
        windows={(0,'A'):[(0,10)],(1,'A'):[(0,10)],(0,'B'):[]}
        self.assertEqual(self.status(windows,{'A':1,'B':9},0),cp_model.INFEASIBLE)

    def test_battery_charge_release_increases_capacity(self):
        windows={(0,'aircraft'):[(0,10),(10,20)],
                 (0,'battery'):[(0,30),(10,40)],(1,'battery'):[(0,40)]}
        self.assertEqual(self.status(windows,{'aircraft':1,'battery':2},0),cp_model.INFEASIBLE)
        self.assertEqual(self.status(windows,{'aircraft':1,'battery':2},1),cp_model.OPTIMAL)

    def test_invalid_budget_rejected_before_scenario_access(self):
        for value in (-1, .5, float('nan')):
            with self.assertRaises(ValueError):
                solve(None,None,None,max_partition_shortage=value)


if __name__=='__main__': unittest.main()

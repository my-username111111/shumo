"""Regression gates for imported-plan optimization."""
from copy import deepcopy
import unittest
from optimize_q3_imported import partition_atoms, compare


class ImportedPlanTests(unittest.TestCase):
    def fixture(self):
        baseline=dict(objective=dict(makespan=100.,weighted_delivery_seconds=1000.,
                                     energy_kwh=10.,weighted_soft_delay=0.),communication={})
        protected=[dict(id=str(k),group_count=k,groups=[dict(zones=[str(i)]) for i in range(k)],
                        shortage_total=2,resource_total=10,workload_cv=.2,resource_need={}) for k in (2,3)]
        return baseline,protected,dict(partitions=deepcopy(protected))

    def test_energy_trade_requires_explicit_tolerance(self):
        baseline,protected,q4=self.fixture()
        plan=deepcopy(baseline)
        plan['objective'].update(weighted_delivery_seconds=990.,energy_kwh=10.005)
        self.assertEqual(compare(plan,q4,baseline,protected)['status'],'REJECTED')
        self.assertEqual(compare(plan,q4,baseline,protected,energy_tolerance=.001)['status'],'ACCEPTED')

    def test_time_guard_rejects_discrete_rounding_regression(self):
        baseline,protected,q4=self.fixture()
        plan=deepcopy(baseline)
        plan['objective'].update(weighted_delivery_seconds=990.,makespan=102.)
        result=compare(plan,q4,baseline,protected)
        self.assertEqual(result['status'],'REJECTED')
        self.assertIn('makespan increased',result['failures'])

    def test_both_partition_budgets_and_membership_are_checked(self):
        baseline,protected,q4=self.fixture()
        plan=deepcopy(baseline)
        plan['objective']['weighted_delivery_seconds']=990.
        q4['partitions'][0]['shortage_total']+=1
        q4['partitions'][1]['groups'][0]['zones']=['changed']
        result=compare(plan,q4,baseline,protected)
        self.assertEqual(result['status'],'REJECTED')
        self.assertIn('2-group resources increased',result['failures'])
        self.assertIn('3-group membership lost',result['failures'])

    def test_crossing_partition_refinement_preserves_every_membership(self):
        ps=[dict(groups=[dict(zones=['a','c']),dict(zones=['b','d'])]),
            dict(groups=[dict(zones=['a','b']),dict(zones=['c']),dict(zones=['d'])])]
        atoms=partition_atoms(ps)
        self.assertEqual(atoms,[['a'],['b'],['c'],['d']])
        for p in ps:
            rebuilt=[sorted(z for atom in atoms if set(atom)<=set(g['zones']) for z in atom) for g in p['groups']]
            self.assertEqual(rebuilt,[g['zones'] for g in p['groups']])

    def test_nested_partition_does_not_split_groups_unnecessarily(self):
        ps=[dict(groups=[dict(zones=['a','b']),dict(zones=['c','d'])]),
            dict(groups=[dict(zones=['a']),dict(zones=['b']),dict(zones=['c','d'])])]
        self.assertEqual(partition_atoms(ps),[['a'],['b'],['c','d']])


if __name__=='__main__':unittest.main()

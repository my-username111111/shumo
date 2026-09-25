"""Default full execution must deliver the same certified Q3/Q4 as the dedicated entry."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import run


class EntryTests(unittest.TestCase):
    def test_certified_tail_preserves_saved_relations_and_uses_exact_q4(self):
        saved=dict(transport_sorties=[],relay_sorties=[],communication_intervals=[],
                   communication={},certificate={'status':'PASS'},objective={'makespan':123.})
        result=dict(partitions=[],recommended_partition=None)
        with TemporaryDirectory() as folder:
            path=Path(folder)/'source.json';path.write_text(json.dumps(saved),encoding='utf8')
            output=Path(folder)/'out';output.mkdir()
            with patch('q4_delivery.default_plan_path',return_value=path), \
                 patch('q4_delivery.export',return_value=(result,{'status':'PASS'})) as exact, \
                 patch('q4_audit.verify_fixed_q3',return_value={'status':'PASS'}), \
                 patch('run.verify_q1',return_value={}),patch('run.verify_q2',return_value={}), \
                 patch('run.export') as export,patch('run.solve_q4',side_effect=AssertionError('legacy Q4')):
                run.run_certified_tail(None,{}, {},None,output,False)
            self.assertEqual(exact.call_args.args[1],path)
            self.assertFalse(exact.call_args.kwargs['figures'])
            self.assertIs(export.call_args.args[5],result)
            self.assertEqual(json.loads((output/'q3_plan.json').read_text()),saved)


if __name__=='__main__':unittest.main()

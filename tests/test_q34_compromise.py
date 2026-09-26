"""Protect warm-start ownership without changing the physical model."""
import unittest
from types import SimpleNamespace
from q3_joint import relay_hint_matches

class RelayHintOwnershipTests(unittest.TestCase):
    def test_same_location_wrong_group_is_not_a_compatible_hint(self):
        site=SimpleNamespace(lon=109.,lat=23.,agl=300.)
        row=dict(lon=109.,lat=23.,agl=300.,service_zones=['S003','S005'])
        self.assertFalse(relay_hint_matches(row,site,{'S001','S002'}))
        self.assertTrue(relay_hint_matches(row,site,{'S003','S005','S007'}))

    def test_historical_unknown_group_does_not_invent_ownership(self):
        site=SimpleNamespace(lon=109.,lat=23.,agl=300.)
        row=dict(lon=109.,lat=23.,agl=300.)
        self.assertTrue(relay_hint_matches(row,site,{'S001'}))
        row['agl']=200.
        self.assertFalse(relay_hint_matches(row,site,{'S001'}))

if __name__=='__main__':unittest.main()

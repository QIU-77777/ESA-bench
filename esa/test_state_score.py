import unittest
from esa.state_score import score_state, predicate

class StateScoreTests(unittest.TestCase):
    def test_predicates(self):
        self.assertTrue(predicate(3, {"op":"gte", "value":2}))
        self.assertFalse(predicate(None, {"op":"gte", "value":2}))
        with self.assertRaises(ValueError): predicate(3, {"op":"unknown"})
    def test_required_state(self):
        r={"expected_state":{"navigation.active":True}}
        self.assertEqual(score_state(r,{"state":{}},{"state":{"navigation":{"active":True}}},[])[0],1)
        self.assertEqual(score_state(r,None,None,[])[0],0)
    def test_transient_forbidden_change(self):
        r={"forbidden_changes":["vehicle"]}
        self.assertEqual(score_state(r,{"state":{}},{"state":{}},[{"changes":[{"path":"vehicle.window.front_left"}]}])[0],0)
    def test_ancestor_transient_change(self):
        r={"forbidden_changes":["vehicle.volume"]}
        self.assertEqual(score_state(r,{"state":{}},{"state":{}},[{"changes":[{"path":"/vehicle"}]}])[0],0)
    def test_order(self):
        r={"process_constraints":[{"type":"ordered_tools","tools":["read","write"]}]}
        events=[{"tool":x,"status":"success"} for x in ["read","write"]]
        self.assertEqual(score_state(r,{}, {}, events)[0],1)
        self.assertEqual(score_state(r,{}, {}, list(reversed(events)))[0],0)
        self.assertEqual(score_state(r,{}, {}, events,True)[0],0)
if __name__ == "__main__": unittest.main()

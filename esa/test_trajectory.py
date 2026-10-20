import unittest
from esa.esa_agent import ESAAgent

class TrajectoryTests(unittest.TestCase):
    def setUp(self):
        self.agent=object.__new__(ESAAgent)
        self.agent._model_id=lambda:"fake"
    def test_multiturn_and_missing_usage(self):
        result={"userMessages":["第一轮","第二轮"],"instruction":"other","sessionId":"s", "iterations":[
            {"turnIndex":0,"iteration":1,"message":"one","status":"completed","usage":{"input_tokens":10,"output_tokens":2}},
            {"turnIndex":1,"iteration":1,"message":"two","status":"failed"}]}
        out=self.agent._build_trajectory(result)
        self.assertEqual(["user","agent","user","agent"],[s["source"] for s in out["steps"]])
        self.assertEqual("第一轮",out["steps"][0]["message"])
        self.assertEqual("第二轮",out["steps"][2]["message"])
        self.assertEqual("error",out["steps"][3]["extra"]["stop_reason"])
        self.assertEqual(.5,out["final_metrics"]["extra"]["usage_coverage"])
        self.assertNotIn("total_cached_tokens",out["final_metrics"])
    def test_no_usage(self):
        out=self.agent._build_trajectory({"iterations":[{"status":"completed","message":"x"}]})
        self.assertNotIn("total_prompt_tokens",out["final_metrics"])
if __name__=="__main__": unittest.main()

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
    def test_scripted_dialogue_uses_actual_user_turns(self):
        result={"userMessages":["planned", "undelivered"], "dialogue":[
            {"role":"user", "content":"调空调", "turn_index":0},
            {"role":"assistant", "content":"温度多少？", "turn_index":0},
            {"role":"user", "content":"24度", "turn_index":1},
            {"role":"assistant", "content":"已设置", "turn_index":1}], "iterations":[
            {"turnIndex":0,"iteration":1,"message":"温度多少？"},
            {"turnIndex":1,"iteration":1,"message":"已设置"}]}
        out=self.agent._build_trajectory(result)
        self.assertEqual(["调空调","24度"],[step["message"] for step in out["steps"] if step["source"]=="user"])
        self.assertEqual([0,1],[step["extra"]["turn_index"] for step in out["steps"] if step["source"]=="user"])

    def test_no_usage(self):
        out=self.agent._build_trajectory({"iterations":[{"status":"completed","message":"x"}]})
        self.assertNotIn("total_prompt_tokens",out["final_metrics"])
if __name__=="__main__": unittest.main()

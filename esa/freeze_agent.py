#!/usr/bin/env python3
"""复制可选指令与模型配置，工具许可由环境 session 提供。"""
import argparse
import copy
import json
from pathlib import Path
import tomllib

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--config",required=True)
    parser.add_argument("--output",required=True)
    parser.add_argument("--agent",default="assistant")
    parser.add_argument("--agent-id",default="esa_eval")
    parser.add_argument("--model")
    args=parser.parse_args()
    with open(args.config,"rb") as handle: source=tomllib.load(handle)
    react=copy.deepcopy(source["agents"]["definitions"][args.agent]["react"])
    react.pop("tools",None)
    react.pop("as_tool_schema",None)
    react.pop("truncation",None)
    react["context_continuity"]="replay"
    if args.model: react.setdefault("model",{})["model_id"]=args.model
    payload={"id":args.agent_id,"engine":"react","react":react}
    Path(args.output).write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n")
if __name__=="__main__":main()

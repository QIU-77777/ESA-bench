import test from "node:test";
import assert from "node:assert/strict";
import {createServer} from "node:http";
import {mkdtempSync,readFileSync,rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {run} from "./driver.mjs";

test("Redis environment and Go runtime perform navigation and read updated state", {skip: !process.env.ESA_INTEGRATION}, async()=> {
  const calls = [["get_favorite_poi",{address_type:"company",limit:10}],
    ["start_navi",{poi_list:["200001:上海市徐汇区科技园 A 座:121.42,31.19"]}], ["get_navi_info",{}]];
  let round=0;
  const model=createServer(async(req,res)=> {
    try {
    let data="";for await(const c of req)data+=c;
    const body=JSON.parse(data);
    assert.equal(body.previous_response_id,undefined);assert.equal(body.store,false);
    assert.equal(body.tools.length,59);
    assert(body.input.some(x=>x.role==="user"));
    if(round>0) {
      const prior=body.input.filter(x=>x.type==="function_call_output").at(-1);
      assert(prior);const toolResult=JSON.parse(prior.output);assert.equal(toolResult.isError,false);
      if(round===3) assert.equal(toolResult.structuredContent.active,true);
    }
    const output=round<3 ? [{type:"function_call",call_id:"integration-"+round,name:calls[round][0],arguments:JSON.stringify(calls[round][1])}]
      : [{type:"message",role:"assistant",content:[{type:"output_text",text:"导航已启动并已查询验证。"}]}];
    round++;
    res.setHeader("Content-Type","application/json");res.end(JSON.stringify({id:"r"+round,status:"completed",output,usage:{input_tokens:100,output_tokens:10}}));
    } catch(error) {res.statusCode=500;res.end(JSON.stringify({error:String(error)}));}
  });
  await new Promise(r=>model.listen(0,"127.0.0.1",r));
  const directory=mkdtempSync(join(tmpdir(),"esa-local-integration-"));
  try {
    const fixture=JSON.parse(readFileSync("tasks/esa-sc-a1/environment/esa/initial_state.json"));
    const result=await run({caseId:"local-integration",instruction:fixture.instruction,initialContext:fixture.context,
      datasetVersion:fixture.dataset_version,mockSystemUrl:process.env.ESA_MOCK_URL??"http://127.0.0.1:18003",
      agentApiUrl:process.env.ESA_RUNTIME_URL??"http://127.0.0.1:18080",outputPath:join(directory,"result.json"),
      agentPayload:{engine:"react",react:{instructions:"使用本地工具",model:{model_id:"fake-model",stream:false},
        model_params:{explicit_api_base:"http://127.0.0.1:"+model.address().port}}}});
    assert.deepEqual(result.errors,[]);assert.equal(result.turnStatus,"completed");
    assert.equal(result.initialSnapshot.state.navigation.active,false);
    assert.equal(result.finalSnapshot.state.navigation.active,true);
    assert.equal(result.finalSnapshot.state.navigation.destination.id,"200001");
    assert.equal(result.iterations.length,4);assert.equal(result.toolCalls.length,3);
    assert(result.toolEvents.some(e=>e.tool==="start_navi"&&e.changes.length));
    assert.equal(result.toolCalls[1].callId,"integration-1");
    const response=await fetch((process.env.ESA_MOCK_URL??"http://127.0.0.1:18003")+"/eval/api/v1/sessions/"+result.sessionId,{method:"DELETE"});
    assert.equal(response.status,200);
  } finally {model.closeAllConnections();await new Promise(r=>model.close(r));rmSync(directory,{recursive:true,force:true});}
});

import test from "node:test";
import assert from "node:assert/strict";
import {createServer} from "node:http";
import {mkdtempSync,readFileSync,rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {run} from "./driver.mjs";

test("driver saves actual initial/final state and handles failed runtime", async () => {
  let active=false; let failed=false;
  const server=createServer(async(req,res)=>{
    let text=""; for await(const c of req)text+=c;
    const body=text?JSON.parse(text):{};
    let payload;
    if(req.url==="/eval/api/v1/sessions") { active=false; payload={session_id:"s1"}; }
    else if(req.url.endsWith("/snapshot")) payload={state:{navigation:{active}},state_version:active?1:0};
    else if(req.url.endsWith("/events")) payload={events:[{tool:"start_navi",status:"success"}],truncated:false};
    else if(req.url==="/v1/run") {assert.equal(body.sessionId,"s1"); active=true;payload={status:failed?"failed":"completed",error:failed?"model failure":undefined,iterations:[],reply:"完成"};}
    else {res.statusCode=404;payload={};}
    res.setHeader("Content-Type","application/json");res.end(JSON.stringify(payload));
  });
  await new Promise(r=>server.listen(0,"127.0.0.1",r));
  const root=mkdtempSync(join(tmpdir(),"esa-driver-test-"));
  try {
    const url="http://127.0.0.1:"+server.address().port;
    const config={mockSystemUrl:url,agentApiUrl:url,outputPath:join(root,"result.json"),instruction:"导航"};
    const good=await run(config);
    assert.equal(good.turnStatus,"completed");assert.equal(good.initialSnapshot.state.navigation.active,false);
    assert.equal(good.finalSnapshot.state.navigation.active,true);
    assert.equal(JSON.parse(readFileSync(join(root,"snapshot-final.json"))).state_version,1);
    failed=true; const bad=await run(config); assert.equal(bad.turnStatus,"failed");assert.deepEqual(bad.errors,["model failure"]);
  } finally {await new Promise(r=>server.close(r));rmSync(root,{recursive:true,force:true});}
});

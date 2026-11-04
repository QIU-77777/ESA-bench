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

async function fixture(callback, replies = ["请确认温度？", "已设置24度"]) {
  const requests = []; let modelRuns = 0;
  const server = createServer(async (req, res) => {
    let text = ""; for await (const chunk of req) text += chunk;
    const body = text ? JSON.parse(text) : {};
    let payload;
    if (req.url === "/eval/api/v1/sessions") payload = {session_id: "model-session"};
    else if (req.url.endsWith("/snapshot")) payload = {state: {vehicle: {ac_temperature: modelRuns > 1 ? 24 : 23}}};
    else if (req.url.endsWith("/events")) payload = {events: [{call_id: "call-1", tool: "execute_vehicle_control", status: "ok"}], truncated: false};
    else if (req.url === "/v1/run") {
      requests.push(body);
      const index = modelRuns++;
      const call = {callId: "call-" + index, name: "execute_vehicle_control", arguments: "{}", status: "ok"};
      const dialogue = [{role: "user", content: body.user_messages?.[0] ?? body.instruction},
        {type: "message", role: "assistant", content: [{type: "output_text", text: replies[index]}]}];
      payload = {status: "completed", reply: replies[index], iterations: [{turnIndex: 0, iteration: 1, toolCalls: [call]}],
        toolCalls: [call], transcript: [...(body.transcript ?? []), ...dialogue],
        turns: [{reply: replies[index], status: "completed"}], runtimeVersion: "test"};
      if (body.user_messages?.length > 1) {
        payload.turns = body.user_messages.map((message, i) => ({instruction: message, reply: "回复" + i}));
        payload.iterations = body.user_messages.map((message, i) => ({turnIndex: i, toolCalls: [{...call, callId: "call-" + i}]}));
        payload.toolCalls = payload.iterations.flatMap(item => item.toolCalls);
      }
    } else {res.statusCode = 404; payload = {};}
    res.setHeader("Content-Type", "application/json"); res.end(JSON.stringify(payload));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const root = mkdtempSync(join(tmpdir(), "esa-script-test-"));
  try {
    const url = "http://127.0.0.1:" + server.address().port;
    await callback({config: {mockSystemUrl: url, agentApiUrl: url, outputPath: join(root, "result.json"), instruction: "调空调"}, requests, root});
  } finally {
    await new Promise(resolve => server.close(resolve));
    assert.ok(root.startsWith(join(tmpdir(), "esa-script-test-")));
    rmSync(root, {recursive: true, force: true});
  }
}

test("matching clarification uses transcript and annotates real calls and dialogue", async () => {
  await fixture(async ({config, requests, root}) => {
    const result = await run({...config, userScript: [{reply_pattern: "确认.*温度", user_reply: "24度"}]});
    assert.equal(result.turnStatus, "completed");
    assert.equal(requests.length, 2);
    assert.deepEqual(requests[0].user_messages, ["调空调"]);
    assert.deepEqual(requests[1].user_messages, ["24度"]);
    assert.equal(requests[1].transcript.length, 2);
    assert.deepEqual(result.dialogue.map(item => [item.role, item.turn_index]), [["user", 0], ["assistant", 0], ["user", 1], ["assistant", 1]]);
    assert.deepEqual(result.toolCalls.map(item => item.turn_index), [0, 1]);
    assert.deepEqual(result.iterations.map(item => item.turnIndex), [0, 1]);
    assert.equal(result.toolEvents[0].turn_index, 1);
    assert.deepEqual(JSON.parse(readFileSync(join(root, "dialogue.json"))), result.dialogue);
    assert.equal(result.sessionId, "model-session");
  });
});

test("unmatched clarification stops without supplying the scripted answer", async () => {
  await fixture(async ({config, requests}) => {
    const result = await run({...config, user_script: [{reply_pattern: "确认温度", user_reply: "24度"}]});
    assert.equal(result.turnStatus, "completed");
    assert.equal(result.user_script_complete, false);
    assert.equal(requests.length, 1);
    assert.deepEqual(result.userMessages, ["调空调"]);
    assert.ok(!JSON.stringify(result.dialogue).includes("24度"));
    assert.match(result.script_failure, /reply_pattern did not match/);
    assert.deepEqual(result.errors, []);
  }, ["我已经全部处理好了"]);
});

test("fixed user_messages retain one runtime request and correct call turn indexes", async () => {
  await fixture(async ({config, requests}) => {
    const result = await run({...config, userMessages: ["第一轮", "第二轮"]});
    assert.equal(requests.length, 1);
    assert.deepEqual(requests[0].user_messages, ["第一轮", "第二轮"]);
    assert.equal(requests[0].transcript, undefined);
    assert.deepEqual(result.toolCalls.map(call => call.turn_index), [0, 1]);
    assert.deepEqual(result.dialogue.map(item => item.turn_index), [0, 0, 1, 1]);
  });
});

test("invalid script regex is rejected before model execution", async () => {
  await fixture(async ({config, requests}) => {
    await assert.rejects(run({...config, userScript: [{reply_pattern: "[", user_reply: "answer"}]}), SyntaxError);
    assert.equal(requests.length, 0);
  });
});

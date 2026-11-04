import test from "node:test";
import assert from "node:assert/strict";
import {createServer} from "node:http";
import {mkdtempSync,readFileSync,rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {run} from "./driver.mjs";

function stubServer(options) {
  const state = {sessions: 0, turns: 0, rounds: 0, events: []};
  const server = createServer(async (req, res) => {
    let text = ""; for await (const chunk of req) text += chunk;
    const body = text ? JSON.parse(text) : {};
    const url = new URL(req.url, "http://127.0.0.1");
    let payload; let status = 200;
    if (req.method === "GET" && url.pathname === "/v1/health") {
      payload = {status: "ok", model_id: options.modelId ?? "stub-agent", version: "9.9.9"};
    } else if (url.pathname === "/eval/api/v1/sessions") {
      state.sessions += 1; payload = {session_id: "s1"};
    } else if (url.pathname.endsWith("/round")) {
      state.rounds += 1; state.events.push({sequence: state.events.length + 1, call_id: null, tool: "session/round", status: "ok", changes: []});
      payload = {round: true};
    } else if (url.pathname.endsWith("/snapshot")) {
      payload = {state: {navigation: {active: state.turns > 0}}, state_version: state.turns, manifest: {environment_version: "1.0.0"}};
    } else if (url.pathname.endsWith("/events")) {
      const after = Number(url.searchParams.get("after") || 0);
      payload = {events: state.events.filter(event => event.sequence > after), truncated: false, total_events: state.events.length};
    } else if (url.pathname === "/v1/turn") {
      state.turns += 1;
      state.events.push({sequence: state.events.length + 1, call_id: "call-" + state.turns, tool: "start_navi",
        arguments: {poi_list: ["200001:公司:121.42,31.19"]}, result: {active: true}, status: "ok",
        before_version: state.turns - 1, after_version: state.turns, changes: [{path: "/navigation/active", before: false, after: true}]});
      payload = {reply: options.replies ? options.replies[state.turns - 1] : "已开始导航", status: "completed",
        usage: {input_tokens: 3, output_tokens: 5}};
      if (options.failTurn === state.turns) { payload = {reply: "", status: "failed", error: "agent turn did not complete"}; }
    } else {
      status = 404; payload = {};
    }
    res.statusCode = status;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify(payload));
  });
  return {server, state};
}

async function withServer(options, callback) {
  const {server, state} = stubServer(options);
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const root = mkdtempSync(join(tmpdir(), "esa-cockpit-test-"));
  try {
    await callback("http://127.0.0.1:" + server.address().port, root, state);
  } finally {
    await new Promise(resolve => server.close(resolve));
    rmSync(root, {recursive: true, force: true});
  }
}

function config(base, root, extra) {
  return {mockSystemUrl: base, agentApiUrl: base, agentProtocol: "turn", evalPrefix: "/eval",
    caseId: "esa-cockpit-navi", instruction: "导航去公司。", userMessages: ["导航去公司。"],
    initialState: {vehicle: {gear: "D"}}, toolProfile: ["get_favorite_poi", "start_navi"],
    datasetVersion: "esa-cockpit-v1", seed: 0, turnTimeoutSec: 30,
    outputPath: join(root, "result.json"), ...extra};
}

test("cockpit branch maps turns, calls and dialogue with turn indexes", async () => {
  await withServer({}, async (base, root, state) => {
    const result = await run(config(base, root));
    assert.equal(result.turnStatus, "completed");
    assert.deepEqual(result.errors, []);
    assert.equal(result.sessionId, "s1");
    assert.equal(result.reply, "已开始导航");
    assert.equal(state.rounds, 1);
    assert.equal(result.iterations.length, 1);
    assert.equal(result.iterations[0].turnIndex, 0);
    assert.deepEqual(result.iterations[0].usage, {input_tokens: 3, output_tokens: 5});
    assert.equal(result.toolCalls.length, 1);
    assert.equal(result.toolCalls[0].name, "start_navi");
    assert.equal(result.toolCalls[0].turn_index, 0);
    assert.equal(result.toolCalls[0].call_id, "call-1");
    assert.deepEqual(result.dialogue, [{role: "user", content: "导航去公司。", turn_index: 0},
      {role: "assistant", content: "已开始导航", turn_index: 0}]);
    assert.equal(result.toolEvents.find(event => event.call_id === "call-1").turn_index, 0);
    assert.equal(result.toolEvents[0].tool, "session/round");
    assert.equal(result.manifest.runtime, "9.9.9");
    for (const name of ["snapshot-initial.json", "snapshot-final.json", "state-events.json",
      "environment-manifest.json", "dialogue.json"]) {
      assert.ok(readFileSync(join(root, name)).length > 0, name);
    }
  });
});

test("cockpit branch follows user_script and records per-turn calls", async () => {
  await withServer({replies: ["空调温度需要设置为多少度？", "已把空调温度调到22度。"]}, async (base, root, state) => {
    const result = await run(config(base, root, {
      caseId: "esa-cockpit-climate", instruction: "帮我调整空调温度。", userMessages: ["帮我调整空调温度。"],
      userScript: [{reply_pattern: "温度|多少度", user_reply: "调到22度。"}], toolProfile: ["set_ac_temperature"]}));
    assert.equal(result.turnStatus, "completed");
    assert.equal(result.user_script_complete, true);
    assert.equal(result.turns.length, 2);
    assert.equal(result.toolCalls.length, 2);
    assert.deepEqual(result.toolCalls.map(call => call.turn_index), [0, 1]);
    assert.deepEqual(result.dialogue.map(item => item.turn_index), [0, 0, 1, 1]);
    assert.equal(result.iterations[1].turnIndex, 1);
    assert.equal(state.rounds, 2);
  });
});

test("cockpit branch rejects a model identity mismatch before any turn", async () => {
  await withServer({}, async (base, root, state) => {
    const result = await run(config(base, root, {expectedModelId: "step-5-preview"}));
    assert.equal(result.turnStatus, "failed");
    assert.equal(result.errors.length, 1);
    assert.match(result.errors[0], /不一致/);
    assert.equal(state.sessions, 0);
    assert.equal(state.turns, 0);
    assert.equal(result.agentModelId, "stub-agent");
    assert.equal(result.expectedModelId, "step-5-preview");
  });
});

test("cockpit branch accepts the provider-prefixed model identity", async () => {
  await withServer({}, async (base, root, state) => {
    const result = await run(config(base, root, {expectedModelId: "step-5-preview"}));
    assert.equal(result.turnStatus, "failed");
  });
});

test("cockpit branch records agent turn failure as execution error", async () => {
  await withServer({failTurn: 1}, async (base, root) => {
    const result = await run(config(base, root));
    assert.equal(result.turnStatus, "failed");
    assert.deepEqual(result.errors, ["agent turn did not complete"]);
  });
});

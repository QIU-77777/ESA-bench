#!/usr/bin/env node
// 本地 ESA 驱动：创建隔离环境，运行 ReAct，保存状态与调用证据。
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";

function args(argv) {
  const result = {};
  for (let i = 2; i < argv.length; i++) if (argv[i].startsWith("--")) result[argv[i].slice(2)] = argv[++i];
  return result;
}
function log(event, detail = {}) { console.log(JSON.stringify({ts: new Date().toISOString(), event, ...detail})); }
async function json(url, {method = "GET", body, timeoutMs = 30000} = {}) {
  const response = await fetch(url, {method, headers: {"Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(timeoutMs)});
  const text = await response.text();
  if (!response.ok) throw new Error(method + " " + url + " -> " + response.status);
  return text ? JSON.parse(text) : {};
}
function scriptFor(config) {
  const script = config.userScript ?? config.user_script;
  if (script === undefined || script === null) return null;
  if (!Array.isArray(script)) throw new Error("user_script must be an array");
  return script.map((entry) => {
    if (!entry || typeof entry.reply_pattern !== "string" || typeof entry.user_reply !== "string") {
      throw new Error("user_script entries require reply_pattern and user_reply strings");
    }
    return {...entry, pattern: new RegExp(entry.reply_pattern, "u")};
  });
}
function callsFrom(output, turnIndex) {
  const iterations = output.iterations ?? [];
  const calls = output.toolCalls ?? iterations.flatMap(item => item.toolCalls ?? []);
  const indexes = new Map(iterations.flatMap(item => (item.toolCalls ?? []).map(call =>
    [call.callId ?? call.call_id, item.turnIndex ?? 0])));
  return calls.map(call => {
    const index = turnIndex ?? call.turnIndex ?? indexes.get(call.callId ?? call.call_id) ?? 0;
    return {...call, turnIndex: index, turn_index: index, call_id: call.callId ?? call.call_id};
  });
}
export async function run(config) {
  const script = scriptFor(config);
  const mock = (config.mockSystemUrl ?? "http://mock-system:8003").replace(/\/$/, "");
  const internal = (config.mockSystemInternalUrl ?? mock).replace(/\/$/, "");
  const runtime = (config.agentApiUrl ?? "http://agent-api:8080").replace(/\/$/, "");
  const prefix = config.evalPrefix ?? "/eval";
  const startedAt = new Date().toISOString();
  const created = await json(mock + prefix + "/api/v1/sessions", {method: "POST", body: {
    type: "car", context: config.initialContext ?? {}, initial_state: config.initialState ?? {},
    data_overlays: config.dataOverlays ?? {}, tool_profile: config.toolProfile,
    faults: config.faults ?? [], seed: config.seed ?? 0, dataset_version: config.datasetVersion,
  }});
  const sessionId = created.session_id;
  if (!sessionId) throw new Error("环境没有返回 session_id");
  const evidence = mock + "/api/v1/rl/sessions/" + encodeURIComponent(sessionId);
  const initialSnapshot = await json(evidence + "/snapshot");
  const agent = config.agentPayload ?? (config.agentPayloadPath ? JSON.parse(readFileSync(config.agentPayloadPath, "utf8")) : {});
  const baseRequest = {agent, mockSystemUrl: internal, sessionId, toolProfile: config.toolProfile,
    maxIterations: config.maxIterations ?? 12, turnTimeoutSec: config.turnTimeoutSec ?? 240};
  let output = {}; const errors = []; const dialogue = [];
  let userScriptComplete = script === null ? null : false;
  let scriptFailure = null;
  let actualMessages = config.userMessages?.length ? [...config.userMessages] : [config.instruction ?? ""];
  try {
    if (script === null) {
      // Fixed user_messages keep their original single-request runtime path.
      output = await json(runtime + "/v1/run", {method: "POST",
        timeoutMs: ((config.turnTimeoutSec ?? 240) * Math.max(1, actualMessages.length) + 30) * 1000,
        body: {...baseRequest, instruction: config.instruction ?? "", user_messages: config.userMessages}});
      if (output.error) errors.push(output.error);
      for (let i = 0; i < actualMessages.length; i++) {
        const turn = output.turns?.[i];
        if (i > 0 && !turn && !output.iterations?.some(item => item.turnIndex === i)) break;
        dialogue.push({role: "user", content: actualMessages[i], turn_index: i});
        const reply = turn?.reply ?? (i === 0 && actualMessages.length === 1 ? output.reply : undefined);
        if (typeof reply === "string") dialogue.push({role: "assistant", content: reply, turn_index: i});
      }
      actualMessages = dialogue.filter(item => item.role === "user").map(item => item.content);
      output.toolCalls = callsFrom(output);
    } else {
      if (actualMessages.length !== 1) throw new Error("user_script requires exactly one initial user message");
      let transcript = [];
      let message = actualMessages[0];
      const iterations = [], toolCalls = [], turns = [];
      actualMessages = [];
      output = {status: "completed", iterations, toolCalls, turns, reply: ""};
      for (let turnIndex = 0; turnIndex <= script.length; turnIndex++) {
        actualMessages.push(message);
        dialogue.push({role: "user", content: message, turn_index: turnIndex});
        const turnOutput = await json(runtime + "/v1/run", {method: "POST",
          timeoutMs: ((config.turnTimeoutSec ?? 240) + 30) * 1000,
          body: {...baseRequest, user_messages: [message], transcript}});
        const turnIterations = (turnOutput.iterations ?? []).map(item => ({...item, turnIndex,
          toolCalls: (item.toolCalls ?? []).map(call => ({...call, turnIndex, turn_index: turnIndex, call_id: call.callId ?? call.call_id}))}));
        iterations.push(...turnIterations);
        toolCalls.push(...callsFrom(turnOutput, turnIndex));
        turns.push({...turnOutput.turns?.[0], instruction: message, reply: turnOutput.reply ?? "", turnIndex,
          status: turnOutput.status ?? "completed", iterations: turnIterations});
        output.reply = turnOutput.reply ?? "";
        output.runtimeVersion = turnOutput.runtimeVersion;
        dialogue.push({role: "assistant", content: output.reply, turn_index: turnIndex});
        if (turnOutput.error || turnOutput.status !== "completed") {
          errors.push(turnOutput.error || "runtime turn did not complete");
          output.status = "failed";
          break;
        }
        if (turnIndex === script.length) {
          userScriptComplete = true;
          break;
        }
        const branch = script[turnIndex];
        if (!branch.pattern.test(output.reply)) {
          scriptFailure = "user_script reply_pattern did not match at turn " + turnIndex;
          break;
        }
        if (!Array.isArray(turnOutput.transcript)) {
          errors.push("runtime did not return transcript for scripted continuation");
          output.status = "failed";
          break;
        }
        transcript = turnOutput.transcript;
        message = branch.user_reply;
      }
    }
  } catch (error) { errors.push(String(error)); }
  const finalSnapshot = await json(evidence + "/snapshot");
  const eventData = await json(evidence + "/events");
  const iterations = output.iterations ?? [];
  const toolCalls = output.toolCalls ?? [];
  const callTurns = new Map(toolCalls.map(call => [call.call_id, call.turn_index]));
  const toolEvents = (eventData.events ?? []).map(event => ({...event,
    ...(callTurns.has(event.call_id) ? {turn_index: callTurns.get(event.call_id)} : {})}));
  const result = {caseId: config.caseId ?? null, sessionId, startedAt, finishedAt: new Date().toISOString(),
    instruction: config.instruction ?? "", userMessages: actualMessages, dialogue,
    user_script_complete: userScriptComplete, script_failure: scriptFailure,
    reply: output.reply ?? "", turns: output.turns ?? [], turnStatus: errors.length ? "failed" : (output.status ?? "completed"),
    iterations, toolCalls, toolEvents, initialSnapshot, finalSnapshot,
    eventMetadata: {truncated: eventData.truncated ?? false}, eventEvidence: {...eventData, events: toolEvents}, errors,
    manifest: {runtime: output.runtimeVersion, environment: finalSnapshot.manifest ?? created.manifest}};
  const outputPath = config.outputPath ?? "/logs/agent/driver-result.json";
  mkdirSync(dirname(outputPath), {recursive: true});
  writeFileSync(outputPath, JSON.stringify(result, null, 2));
  for (const [name, payload] of [["snapshot-initial.json", initialSnapshot], ["snapshot-final.json", finalSnapshot],
    ["state-events.json", result.eventEvidence], ["environment-manifest.json", result.manifest], ["dialogue.json", dialogue]]) {
    writeFileSync(resolve(dirname(outputPath), name), JSON.stringify(payload, null, 2));
  }
  log("driver_done", {outputPath, sessionId, iterations: iterations.length, status: result.turnStatus});
  return result;
}
if (process.argv[1] && import.meta.url === new URL("file://" + resolve(process.argv[1])).href) {
  const options = args(process.argv);
  if (!options.config) { console.error("Usage: node driver.mjs --config FILE"); process.exit(1); }
  run(JSON.parse(readFileSync(options.config, "utf8"))).then(result => {
    if (result.errors.length || result.turnStatus !== "completed") process.exitCode = 1;
  }).catch(error => { log("driver_failed", {error: String(error)}); process.exitCode = 1; });
}

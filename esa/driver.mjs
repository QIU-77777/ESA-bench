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
  if (!response.ok) throw new Error(method + " " + url + " -> " + response.status + ": " + text.slice(0, 500));
  return text ? JSON.parse(text) : {};
}
export async function run(config) {
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
  let output = {}; const errors = [];
  try {
    output = await json(runtime + "/v1/run", {method: "POST", timeoutMs: ((config.turnTimeoutSec ?? 240) * Math.max(1, config.userMessages?.length ?? 1) + 30) * 1000,
      body: {agent, instruction: config.instruction ?? "", user_messages: config.userMessages,
        mockSystemUrl: internal, sessionId, toolProfile: config.toolProfile,
        maxIterations: config.maxIterations ?? 12, turnTimeoutSec: config.turnTimeoutSec ?? 240}});
    if (output.error) errors.push(output.error);
  } catch (error) { errors.push(String(error)); }
  // 状态证据读取失败必须使试验失败，避免将不完整证据用作正常评分。
  const finalSnapshot = await json(evidence + "/snapshot");
  const eventData = await json(evidence + "/events");
  const iterations = output.iterations ?? [];
  const toolCalls = output.toolCalls ?? iterations.flatMap(item => item.toolCalls ?? []);
  const result = {caseId: config.caseId ?? null, sessionId, startedAt, finishedAt: new Date().toISOString(),
    instruction: config.instruction ?? "", userMessages: config.userMessages,
    reply: output.reply ?? "", turns: output.turns ?? [], turnStatus: errors.length ? "failed" : (output.status ?? "completed"),
    iterations, toolCalls, toolEvents: eventData.events ?? [], initialSnapshot, finalSnapshot,
    eventMetadata: {truncated: eventData.truncated ?? false}, eventEvidence: eventData, errors,
    manifest: {runtime: output.runtimeVersion, environment: finalSnapshot.manifest ?? created.manifest}};
  const outputPath = config.outputPath ?? "/logs/agent/driver-result.json";
  mkdirSync(dirname(outputPath), {recursive: true});
  writeFileSync(outputPath, JSON.stringify(result, null, 2));
  for (const [name, payload] of [["snapshot-initial.json", initialSnapshot], ["snapshot-final.json", finalSnapshot],
    ["state-events.json", eventData], ["environment-manifest.json", result.manifest]]) {
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

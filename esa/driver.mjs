#!/usr/bin/env node
// ESA 评测驱动：在 Harbor 任务容器内建立 mock 会话，驱动 agent-runtime 跑完一轮对话，
// 把每次迭代的推理、工具调用、工具结果与用量落到一个 JSON 文件，供 Harbor 侧构造轨迹与判分。
//
// 只使用 Node 22 内置的 fetch 与 WebSocket，不需要安装任何依赖。
//
// 用法：
//   node driver.mjs --config /logs/agent/driver-config.json
//
// 配置文件字段：
//   caseId          用例编号，写进 rollout 关联信息
//   instruction     用户输入文本
//   initialContext  建会话时写入的车辆上下文（CarSessionContext 结构）
//   mockSystemUrl   mock-system 地址，如 http://mock-system:8003
//   agentApiUrl     agent-runtime 地址，如 http://agent-api:8080
//   agentId         agent-runtime 里的 agent 定义名，缺省 assistant
//   evalPrefix      mock-system 评测前缀，缺省 /eval
//   turnTimeoutSec  单轮超时，缺省 240
//   outputPath      输出 JSON 路径

import { readFileSync, writeFileSync } from "node:fs";
import process from "node:process";

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith("--")) continue;
    const key = token.slice(2);
    const value = argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[++i] : "true";
    args[key] = value;
  }
  return args;
}

function nowIso() {
  return new Date().toISOString();
}

function log(event, detail) {
  process.stdout.write(JSON.stringify({ ts: nowIso(), event, ...detail }) + "\n");
}

async function httpJson(url, { method = "GET", body } = {}) {
  const response = await fetch(url, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`${method} ${url} -> ${response.status}: ${text.slice(0, 400)}`);
  }
  return text ? JSON.parse(text) : {};
}

// 建 mock 会话并写入初始车辆上下文，返回会话 ID。
async function createMockSession(config) {
  const url = `${config.mockSystemUrl}${config.evalPrefix}/api/v1/sessions`;
  const created = await httpJson(url, {
    method: "POST",
    body: { type: "car", context: config.initialContext ?? {} },
  });
  const sessionId = created.session_id;
  if (!sessionId) {
    throw new Error("mock-system 未返回 session_id: " + JSON.stringify(created).slice(0, 300));
  }
  log("mock_session_created", { sessionId });
  return sessionId;
}

// 读取工具调用旁路记录。评测打开 [rl] enabled，工具调用会写进 Redis。
async function fetchToolEvents(config, sessionId) {
  // 旁路接口只挂在根路径上，不带评测前缀。
  const url = `${config.mockSystemUrl}/api/v1/rl/sessions/${sessionId}/events`;
  try {
    const payload = await httpJson(url);
    return Array.isArray(payload.events) ? payload.events : [];
  } catch (error) {
    log("tool_events_unavailable", { error: String(error).slice(0, 300) });
    return [];
  }
}

function itemText(item) {
  const parts = [];
  for (const part of item?.content ?? []) {
    if (part?.text) parts.push(part.text);
  }
  if (Array.isArray(item?.text)) {
    for (const line of item.text) if (line) parts.push(line);
  }
  return parts.join("");
}

function pickUsage(step) {
  const keys = ["input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "total_tokens"];
  const usage = {};
  for (const key of keys) {
    const camel = key.replace(/_(.)/g, (_, c) => c.toUpperCase());
    const value = step[camel] ?? step[key];
    if (typeof value === "number") usage[key] = value;
  }
  return Object.keys(usage).length > 0 ? usage : null;
}

// 把事件流切成 ReAct 的每次迭代。
//
// 事件顺序固定：一次迭代的推理与工具调用项先到，随后是标记该次迭代结束的
// model_call 轨迹步骤，再是该次迭代的工具结果项。工具调用项与工具结果项用的是
// 两套编号（前者来自服务端，后者来自上游），按出现顺序配对。
function collectIterations(events) {
  const iterations = [];
  const errors = [];
  let turnId = null;
  let turnStatus = null;

  let reasoning = new Map();
  let messages = new Map();
  let toolCalls = [];
  const awaitingOutput = [];
  const awaitingStep = [];

  for (const event of events) {
    const params = event?.params ?? {};
    switch (event?.method) {
      case "turn/started":
        turnId = params.turnId ?? turnId;
        break;

      case "turn/completed":
        turnId = params.turn?.id ?? params.turnId ?? turnId;
        turnStatus = params.turn?.status ?? params.status ?? null;
        break;

      case "item/reasoning/delta":
        reasoning.set(params.itemId, (reasoning.get(params.itemId) ?? "") + (params.delta ?? ""));
        break;

      case "item/agentMessage/delta":
        messages.set(params.itemId, (messages.get(params.itemId) ?? "") + (params.delta ?? ""));
        break;

      case "item/completed": {
        const item = params.item;
        if (!item) break;
        if (item.kind === "reasoning") {
          const text = itemText(item);
          if (text) reasoning.set(item.id, text);
        } else if (item.kind === "agentMessage") {
          const text = itemText(item);
          if (text) messages.set(item.id, text);
        } else if (item.kind === "toolCall") {
          const call = {
            callId: item.call_id ?? item.id ?? null,
            name: item.name ?? null,
            arguments: item.arguments ?? null,
            output: null,
            outputChars: null,
            status: item.status ?? null,
            latencyMs: null,
            overrideHit: null,
          };
          toolCalls.push(call);
          awaitingOutput.push(call);
          awaitingStep.push(call);
        } else if (item.kind === "toolResult") {
          const target = awaitingOutput.shift();
          if (target) {
            target.output = item.output ?? null;
            target.upstreamCallId = item.call_id ?? null;
          }
        }
        break;
      }

      case "rollout/trajectoryStep": {
        const step = params.step ?? {};
        if (step.kind === "model_call") {
          iterations.push({
            iteration: step.iteration ?? iterations.length + 1,
            receivedAt: event.receivedAt ?? null,
            reasoning: [...reasoning.values()].join("\n").trim(),
            message: [...messages.values()].join("").trim(),
            toolCalls,
            latencyMs: step.latencyMs ?? null,
            responseId: step.responseId ?? null,
            traceId: step.traceId ?? null,
            chatId: step.chatId ?? null,
            modelCallId: step.modelCallId ?? null,
            status: step.status ?? null,
            fallback: step.fallback ?? false,
            usage: pickUsage(step),
          });
          reasoning = new Map();
          messages = new Map();
          toolCalls = [];
        } else if (step.kind === "tool_call") {
          const target = awaitingStep.shift();
          if (target) {
            target.latencyMs = step.latencyMs ?? null;
            target.status = step.status ?? target.status;
            target.outputChars = step.outputChars ?? null;
            if (target.output === null && step.observationSummary) {
              target.output = step.observationSummary;
            }
          }
        }
        break;
      }

      case "error":
        errors.push(params);
        break;

      case "transport/error":
      case "driver/timeout":
        errors.push({ method: event.method, ...params });
        break;

      default:
        break;
    }
  }

  // 末次迭代没有工具调用时，收尾的推理与回复仍要成为一步。
  if (reasoning.size > 0 || messages.size > 0 || toolCalls.length > 0) {
    iterations.push({
      iteration: iterations.length + 1,
      receivedAt: null,
      reasoning: [...reasoning.values()].join("\n").trim(),
      message: [...messages.values()].join("").trim(),
      toolCalls,
      latencyMs: null,
      responseId: null,
      traceId: null,
      chatId: null,
      modelCallId: null,
      status: null,
      fallback: false,
      usage: null,
    });
  }

  const flatCalls = iterations.flatMap((it) => it.toolCalls);
  const reply = iterations.map((it) => it.message).filter(Boolean).join("\n");
  const allReasoning = iterations.map((it) => it.reasoning).filter(Boolean);

  return { iterations, toolCalls: flatCalls, reply, reasoning: allReasoning, turnId, turnStatus, errors };
}

async function runTurn(config, sessionId) {
  const wsBase = config.agentApiUrl.replace(/^http/, "ws");
  const endpoint = `${wsBase}/v1/agent`;
  const events = [];
  const deadline = Date.now() + (config.turnTimeoutSec ?? 240) * 1000;

  const socket = new WebSocket(endpoint);
  const pending = new Map();
  let nextId = 1;

  const send = (method, params) =>
    new Promise((resolve, reject) => {
      const id = String(nextId++);
      pending.set(id, { resolve, reject });
      socket.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
    });

  let settle;
  const settled = new Promise((resolve) => {
    settle = resolve;
  });

  socket.addEventListener("message", (raw) => {
    let frame;
    try {
      frame = JSON.parse(typeof raw.data === "string" ? raw.data : raw.data.toString());
    } catch {
      return;
    }
    if (frame.id && pending.has(String(frame.id))) {
      const { resolve, reject } = pending.get(String(frame.id));
      pending.delete(String(frame.id));
      if (frame.error) reject(new Error(JSON.stringify(frame.error)));
      else resolve(frame.result);
      return;
    }
    if (frame.method) {
      frame.receivedAt = nowIso();
      events.push(frame);
      if (frame.method === "turn/completed") settle();
    }
  });

  socket.addEventListener("error", (event) => {
    events.push({
      method: "transport/error",
      receivedAt: nowIso(),
      params: { message: String(event?.message ?? event) },
    });
    settle();
  });

  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("websocket 连接失败: " + endpoint)), { once: true });
  });

  // agent 载荷整体替换 catalog 里的定义：指令与模型参数取自 configs/config.toml，
  // 工具清单换成评测暴露集合，避免声明未注册的工具导致建会话失败。
  const agentPayload = config.agentPayloadPath
    ? JSON.parse(readFileSync(config.agentPayloadPath, "utf8"))
    : undefined;

  await send("initialize", {
    agentId: config.agentId ?? "assistant",
    ...(agentPayload ? { agent: agentPayload } : {}),
    settingsOverlay: {
      mock_system: {
        // 会话路径必须带会话 ID，客户端据此推出 MCP 路径与会话接口路径。
        // 这里用 agent-runtime 视角的地址，驱动自己访问 mock-system 用另一个地址。
        session_url: `${config.mockSystemInternalUrl}${config.evalPrefix}/api/v1/sessions/${sessionId}`,
        session_id: sessionId,
      },
      rollout: {
        run_id: config.runId ?? "harbor-eval",
        policy_version: config.policyVersion ?? "v1",
        session_id: sessionId,
        case_id: config.caseId ?? "unknown",
      },
    },
  });
  socket.send(JSON.stringify({ jsonrpc: "2.0", method: "initialized", params: {} }));
  const thread = await send("thread/start", {});
  const threadId = thread?.thread?.id ?? null;

  await send("turn/start", {
    threadId,
    input: [{ type: "text", text: config.instruction }],
  });
  log("turn_started", { threadId, sessionId });

  const timedOut = await Promise.race([
    settled.then(() => false),
    new Promise((resolve) => setTimeout(() => resolve(true), Math.max(1000, deadline - Date.now()))),
  ]);
  if (timedOut) {
    events.push({
      method: "driver/timeout",
      receivedAt: nowIso(),
      params: { timeoutSec: config.turnTimeoutSec ?? 240 },
    });
    log("turn_timeout", { sessionId });
  }

  try {
    socket.close();
  } catch {
    // 关闭失败不影响已收集的事件。
  }

  return { events, threadId };
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.config) {
    throw new Error("缺少 --config 参数");
  }
  const config = JSON.parse(readFileSync(args.config, "utf8"));
  config.mockSystemUrl = (config.mockSystemUrl ?? "http://mock-system:8003").replace(/\/$/, "");
  config.agentApiUrl = (config.agentApiUrl ?? "http://agent-api:8080").replace(/\/$/, "");
  config.evalPrefix = config.evalPrefix ?? "/eval";
  config.mockSystemInternalUrl = (config.mockSystemInternalUrl ?? config.mockSystemUrl).replace(/\/$/, "");

  const startedAt = nowIso();
  const sessionId = await createMockSession(config);
  const { events, threadId } = await runTurn(config, sessionId);
  const toolEvents = await fetchToolEvents(config, sessionId);
  const summary = collectIterations(events);

  // 旁路记录带着预设命中标记，按出现顺序回填到工具调用上。
  const overrides = toolEvents.filter((e) => e?.kind === "tool_call");
  summary.toolCalls.forEach((call, index) => {
    const event = overrides[index];
    if (event && event.tool === call.name) {
      call.overrideHit = event.override_hit ?? null;
    }
  });

  const result = {
    caseId: config.caseId ?? null,
    sessionId,
    threadId,
    startedAt,
    finishedAt: nowIso(),
    instruction: config.instruction,
    reply: summary.reply,
    reasoning: summary.reasoning,
    turnId: summary.turnId,
    turnStatus: summary.turnStatus,
    iterations: summary.iterations,
    toolCalls: summary.toolCalls,
    toolEvents,
    errors: summary.errors,
    rawEvents: events,
  };

  const outputPath = config.outputPath ?? "/logs/agent/driver-result.json";
  writeFileSync(outputPath, JSON.stringify(result, null, 2), "utf8");
  log("driver_done", {
    outputPath,
    iterations: summary.iterations.length,
    replyChars: summary.reply.length,
    toolCalls: summary.toolCalls.length,
    toolOutputs: summary.toolCalls.filter((c) => c.output !== null).length,
    toolEvents: toolEvents.length,
  });
}

main().catch((error) => {
  log("driver_failed", { error: String(error?.stack ?? error).slice(0, 2000) });
  process.exit(1);
});

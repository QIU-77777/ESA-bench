#!/usr/bin/env node
// 座舱评测驱动：在 Harbor 任务容器内建立 mock 会话，驱动 agent-runtime 跑完一轮对话，
// 把事件流、工具调用记录与答复落到一个 JSON 文件，供 Harbor 侧构造轨迹与判分。
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

import { writeFileSync } from "node:fs";
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
  const line = JSON.stringify({ ts: nowIso(), event, ...detail });
  process.stdout.write(line + "\n");
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
  log("mock_session_created", { sessionId, mcpPath: created.mcp_path });
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

// 把收集到的事件整理成可读的对话轮次。
function summarize(events) {
  const assistantText = [];
  const reasoning = [];
  const toolCalls = [];
  const toolResults = new Map();
  const inFlight = new Map();
  let turnId = null;
  let turnStatus = null;
  const errors = [];

  for (const event of events) {
    switch (event?.method) {
      case "turn/started":
        turnId = event.params?.turnId ?? turnId;
        break;
      case "turn/completed":
        turnId = event.params?.turnId ?? turnId;
        turnStatus = event.params?.status ?? null;
        break;
      case "item/started": {
        const item = event.params?.item;
        if (!item) break;
        if (item.kind === "toolCall" || item.kind === "tool_call" || item.kind === "function_call") {
          inFlight.set(item.id ?? item.call_id, item);
        }
        break;
      }
      case "item/completed": {
        const item = event.params?.item;
        if (!item) break;
        if (item.kind === "agentMessage" || (item.kind === "message" && item.role === "assistant")) {
          for (const part of item.content ?? []) {
            if (part?.text) assistantText.push(part.text);
          }
        } else if (item.kind === "toolCall" || item.kind === "tool_call" || item.kind === "function_call") {
          toolCalls.push({
            callId: item.call_id ?? item.id ?? null,
            name: item.name ?? null,
            arguments: item.arguments ?? null,
            output: null,
            status: item.status ?? null,
          });
        } else if (item.kind === "toolResult") {
          toolResults.set(item.call_id ?? item.id ?? null, item.output ?? null);
        } else if (item.kind === "reasoning") {
          for (const line of item.text ?? []) {
            if (line) reasoning.push(line);
          }
        }
        break;
      }
      case "error":
        errors.push(event.params ?? {});
        break;
      default:
        break;
    }
  }

  // 工具结果单独下发，按调用编号回填到对应的调用记录上。
  for (const call of toolCalls) {
    if (call.callId && toolResults.has(call.callId)) {
      call.output = toolResults.get(call.callId);
    }
  }

  return { assistantText: assistantText.join("\n"), reasoning, toolCalls, turnId, turnStatus, errors };
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
      events.push(frame);
      if (frame.method === "turn/completed") {
        settle();
      } else if (frame.method === "error") {
        // 通知型 error 不终止本轮，交由 turn/completed 收尾；保留事件供判分。
      }
    }
  });

  socket.addEventListener("error", (event) => {
    events.push({ method: "transport/error", params: { message: String(event?.message ?? event) } });
    settle();
  });

  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("websocket 连接失败: " + endpoint)), { once: true });
  });

  // agent 载荷整体替换 catalog 里的定义：指令与模型参数取自 configs/config.toml，
  // 工具清单换成评测暴露集合，避免声明未注册的工具导致建会话失败。
  const agentPayload = config.agentPayloadPath
    ? JSON.parse((await import("node:fs")).readFileSync(config.agentPayloadPath, "utf8"))
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
    events.push({ method: "driver/timeout", params: { timeoutSec: config.turnTimeoutSec ?? 240 } });
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
  const { readFileSync } = await import("node:fs");
  const config = JSON.parse(readFileSync(args.config, "utf8"));
  config.mockSystemUrl = (config.mockSystemUrl ?? "http://mock-system:8003").replace(/\/$/, "");
  config.agentApiUrl = (config.agentApiUrl ?? "http://agent-api:8080").replace(/\/$/, "");
  config.evalPrefix = config.evalPrefix ?? "/eval";
  config.mockSystemInternalUrl = (config.mockSystemInternalUrl ?? config.mockSystemUrl).replace(/\/$/, "");

  const startedAt = nowIso();
  const sessionId = await createMockSession(config);
  const { events, threadId } = await runTurn(config, sessionId);
  const toolEvents = await fetchToolEvents(config, sessionId);
  const summary = summarize(events);

  const result = {
    caseId: config.caseId ?? null,
    sessionId,
    threadId,
    startedAt,
    finishedAt: nowIso(),
    instruction: config.instruction,
    reply: summary.assistantText,
    reasoning: summary.reasoning,
    turnId: summary.turnId,
    turnStatus: summary.turnStatus,
    toolCalls: summary.toolCalls,
    toolEvents,
    errors: summary.errors,
    rawEvents: events,
  };

  const outputPath = config.outputPath ?? "/logs/agent/driver-result.json";
  writeFileSync(outputPath, JSON.stringify(result, null, 2), "utf8");
  log("driver_done", {
    outputPath,
    replyChars: summary.assistantText.length,
    toolCalls: summary.toolCalls.length,
    toolEvents: toolEvents.length,
  });
}

main().catch((error) => {
  log("driver_failed", { error: String(error?.stack ?? error).slice(0, 2000) });
  process.exit(1);
});

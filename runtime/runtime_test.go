package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func testAgent(endpoint string) map[string]any {
	return map[string]any{"react": map[string]any{"instructions": "system instruction", "model": map[string]any{"model_id": "fake", "stream": false}, "model_params": map[string]any{"explicit_api_base": endpoint}, "tools": map[string]any{"legacy": map[string]any{}}}}
}
func message(s string) any {
	return map[string]any{"type": "message", "id": "must-not-replay", "role": "assistant", "content": []any{map[string]any{"type": "output_text", "text": s}}}
}
func function(id string) any {
	return map[string]any{"type": "function_call", "id": "item-secret-id", "call_id": id, "name": "echo", "arguments": "{\"value\":42}"}
}
func response(items ...any) map[string]any {
	return map[string]any{"id": "resp-logged-only", "status": "completed", "output": items}
}
func fakeMCP(t *testing.T, call func(map[string]any) map[string]any) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/eval/api/v1/sessions/session/round" {
			if r.Method != "POST" {
				t.Error("round method must be POST")
			}
			json.NewEncoder(w).Encode(map[string]any{"status": "ok"})
			return
		}
		if r.Method != "POST" || r.URL.Path != "/eval/mcp/session" {
			t.Errorf("wrong MCP endpoint: %s %s", r.Method, r.URL.Path)
		}
		var b map[string]any
		json.NewDecoder(r.Body).Decode(&b)
		var result any
		if b["method"] == "tools/list" {
			result = map[string]any{"tools": []any{map[string]any{"name": "echo", "description": "Echo tool", "inputSchema": map[string]any{"type": "object", "properties": map[string]any{"value": map[string]any{"type": "number"}}}}}}
		} else {
			params := object(b["params"])
			if params["call_id"] == "" || object(params["arguments"])["_call_id"] != params["call_id"] {
				t.Error("MCP call ids do not match")
			}
			result = call(params)
		}
		json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": b["id"], "result": result})
	}))
	t.Cleanup(server.Close)
	return server
}
func TestReplayMultiTurnAndUsage(t *testing.T) {
	mcp := fakeMCP(t, func(p map[string]any) map[string]any {
		return map[string]any{"content": []any{map[string]any{"type": "text", "text": "42"}}}
	})
	var n atomic.Int32
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var b map[string]any
		json.NewDecoder(r.Body).Decode(&b)
		if r.URL.Path != "/v1/responses" || b["store"] != false || b["previous_response_id"] != nil || b["instructions"] != "system instruction" || b["model"] != "override" {
			t.Errorf("bad Responses contract: %s", marshal(b))
		}
		tools := b["tools"].([]any)
		if len(tools) != 1 || object(tools[0])["name"] != "echo" {
			t.Error("session tools not used")
		}
		input := b["input"].([]any)
		serialized := marshal(input)
		if strings.Contains(serialized, "reasoning") || strings.Contains(serialized, "resp-logged-only") || strings.Contains(serialized, "item-secret-id") || strings.Contains(serialized, "must-not-replay") {
			t.Error("IDs/reasoning replayed")
		}
		count := n.Add(1)
		var out map[string]any
		switch count {
		case 1:
			out = response(map[string]any{"type": "reasoning", "summary": []any{map[string]any{"text": "plan"}}}, function("call-1"))
			out["usage"] = map[string]any{"input_tokens": 10, "output_tokens": 4, "input_tokens_details": map[string]any{"cached_tokens": 2}}
		case 2:
			if len(input) != 3 || object(input[1])["call_id"] != object(input[2])["call_id"] {
				t.Error("call replay pairing failed")
			}
			out = response(message("first"))
		default:
			if len(input) != 5 || object(input[4])["content"] != "second user" {
				t.Errorf("history missing: %s", serialized)
			}
			out = response(message("second"))
		}
		json.NewEncoder(w).Encode(out)
	}))
	defer model.Close()
	req := RunRequest{Agent: testAgent(model.URL + "/v1"), Model: "override", MockSystemURL: mcp.URL, SessionID: "session", UserMessages: []string{"first user", "second user"}, ToolProfile: []string{"echo"}}
	result := (&Runtime{}).Run(context.Background(), req)
	if result.Status != "completed" || result.Reply != "second" || len(result.Iterations) != 3 || len(result.Turns) != 2 || len(result.ToolCalls) != 1 {
		t.Fatalf("bad result: %s", marshal(result))
	}
	if result.Iterations[0].Reasoning != "plan" || result.Iterations[0].Usage["cached_tokens"] != float64(2) || result.Iterations[1].Usage != nil || result.Iterations[2].TurnIndex != 1 {
		t.Error("incorrect iteration metadata")
	}
}
func TestSSEAndDisconnect(t *testing.T) {
	for _, complete := range []bool{true, false} {
		t.Run(fmt.Sprint(complete), func(t *testing.T) {
			var calls atomic.Int32
			mcp := fakeMCP(t, func(p map[string]any) map[string]any { calls.Add(1); return map[string]any{"content": []any{}} })
			var n atomic.Int32
			model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				emit := func(v any) { fmt.Fprintf(w, "data: %s\n\n", marshal(v)); w.(http.Flusher).Flush() }
				if n.Add(1) > 1 {
					emit(map[string]any{"type": "response.completed", "response": response(message("done"))})
					return
				}
				emit(map[string]any{"type": "response.created", "response": map[string]any{"id": "stream-id", "status": "in_progress"}})
				emit(map[string]any{"type": "response.output_item.added", "output_index": 0, "item": map[string]any{"type": "function_call", "name": "echo", "call_id": "stream-call", "arguments": ""}})
				emit(map[string]any{"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "{\"value\":"})
				emit(map[string]any{"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "42}"})
				if complete {
					emit(map[string]any{"type": "response.output_item.done", "output_index": 0, "item": function("stream-call")})
					emit(map[string]any{"type": "response.completed", "response": response(function("stream-call"))})
				}
			}))
			defer model.Close()
			req := RunRequest{Agent: testAgent(model.URL), MockSystemURL: mcp.URL, SessionID: "session", Instruction: "run"}
			object(object(req.Agent["react"])["model"])["stream"] = true
			result := (&Runtime{}).Run(context.Background(), req)
			if complete {
				if result.Status != "completed" || calls.Load() != 1 || result.Reply != "done" {
					t.Fatal(marshal(result))
				}
			} else {
				if result.Status != "failed" || calls.Load() != 0 || len(result.Iterations) != 1 || result.ToolCalls[0].Status != "cancelled" || result.ToolCalls[0].Arguments != "{\"value\":42}" {
					t.Fatal(marshal(result))
				}
			}
		})
	}
}
func TestToolErrorReplayed(t *testing.T) {
	mcp := fakeMCP(t, func(p map[string]any) map[string]any {
		return map[string]any{"isError": true, "content": []any{map[string]any{"type": "text", "text": "denied"}}}
	})
	var n atomic.Int32
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var b map[string]any
		json.NewDecoder(r.Body).Decode(&b)
		if n.Add(1) == 1 {
			json.NewEncoder(w).Encode(response(function("error-call")))
		} else {
			if !strings.Contains(marshal(b["input"]), "denied") {
				t.Error("tool error missing from history")
			}
			json.NewEncoder(w).Encode(response(message("failed tool acknowledged")))
		}
	}))
	defer model.Close()
	result := (&Runtime{}).Run(context.Background(), RunRequest{Agent: testAgent(model.URL), MockSystemURL: mcp.URL, SessionID: "session", Instruction: "run"})
	if result.Status != "completed" || result.ToolCalls[0].Status != "failed" {
		t.Fatal(marshal(result))
	}
}
func TestLimitsTimeoutProfileAndCredentialRedaction(t *testing.T) {
	mcp := fakeMCP(t, func(p map[string]any) map[string]any { return map[string]any{"content": []any{}} })
	for _, mode := range []string{"limit", "timeout", "budget", "profile", "key", "model-failure"} {
		t.Run(mode, func(t *testing.T) {
			model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch mode {
				case "timeout":
					io.Copy(io.Discard, r.Body)
					<-r.Context().Done()
					return
				case "key":
					json.NewEncoder(w).Encode(response(message("test-credential-sentinel")))
				case "model-failure":
					w.WriteHeader(401)
					fmt.Fprint(w, "test-credential-sentinel")
				default:
					json.NewEncoder(w).Encode(response(function("one")))
				}
			}))
			defer model.Close()
			req := RunRequest{Agent: testAgent(model.URL), MockSystemURL: mcp.URL, SessionID: "session", Instruction: "run", MaxIterations: 1}
			rt := &Runtime{}
			switch mode {
			case "timeout":
				req.TurnTimeoutSec = .02
			case "budget":
				rt.ContextBytes = 1
			case "profile":
				req.ToolProfile = []string{"unknown"}
			case "key", "model-failure":
				object(object(req.Agent["react"])["model_params"])["explicit_api_key"] = "test-credential-sentinel"
			}
			result := rt.Run(context.Background(), req)
			expected := map[string]string{"limit": "limit_exceeded", "timeout": "timeout", "budget": "failed", "profile": "failed", "key": "completed", "model-failure": "failed"}[mode]
			if result.Status != expected {
				t.Fatalf("wanted %s: %s", expected, marshal(result))
			}
			if strings.Contains(marshal(result), "test-credential-sentinel") {
				t.Error("credential leaked")
			}
			if mode != "profile" && len(result.Iterations) != 1 {
				t.Error("failed model attempt not recorded")
			}
		})
	}
}
func TestHTTPValidationAndCancellation(t *testing.T) {
	rt := &Runtime{}
	handler := rt.Handler()
	for _, test := range []struct {
		method, path, body string
		status             int
	}{{"GET", "/v1/health", "", 200}, {"POST", "/v1/run", "{}", 400}, {"GET", "/v1/run", "", 405}, {"POST", "/v1/run", "{} {}", 400}, {"POST", "/v1/run", "{", 400}} {
		r := httptest.NewRequest(test.method, test.path, strings.NewReader(test.body))
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		if w.Code != test.status {
			t.Errorf("status %d", w.Code)
		}
	}
	mcp := fakeMCP(t, func(p map[string]any) map[string]any { return map[string]any{} })
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.Copy(io.Discard, r.Body); <-r.Context().Done() }))
	defer model.Close()
	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(20*time.Millisecond, cancel)
	result := rt.Run(ctx, RunRequest{Agent: testAgent(model.URL), MockSystemURL: mcp.URL, SessionID: "session", Instruction: "run"})
	if result.Status != "cancelled" {
		t.Fatal(marshal(result))
	}
}
func TestToolTransportFailureStopsLoop(t *testing.T) {
	for _, mode := range []string{"disconnect", "timeout"} {
		t.Run(mode, func(t *testing.T) {
			var calls atomic.Int32
			mcp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var b map[string]any
				json.NewDecoder(r.Body).Decode(&b)
				if strings.HasSuffix(r.URL.Path, "/round") {
					json.NewEncoder(w).Encode(map[string]any{"status": "ok"})
					return
				}
				if b["method"] == "tools/list" {
					json.NewEncoder(w).Encode(map[string]any{"result": map[string]any{"tools": []any{map[string]any{"name": "echo", "inputSchema": map[string]any{"type": "object"}}}}})
					return
				}
				calls.Add(1)
				if mode == "timeout" {
					<-r.Context().Done()
					return
				}
				conn, _, _ := w.(http.Hijacker).Hijack()
				conn.Close()
			}))
			defer mcp.Close()
			model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				json.NewEncoder(w).Encode(response(function("first"), function("second")))
			}))
			defer model.Close()
			req := RunRequest{Agent: testAgent(model.URL), MockSystemURL: mcp.URL, SessionID: "session", Instruction: "run", TurnTimeoutSec: .2}
			result := (&Runtime{}).Run(context.Background(), req)
			expected := "failed"
			if mode == "timeout" {
				expected = "timeout"
			}
			if result.Status != expected || calls.Load() != 1 || len(result.Iterations) != 1 || result.ToolCalls[1].Status != "cancelled" {
				t.Fatal(marshal(result))
			}
		})
	}
}
func TestSSETextReasoningAndTerminalErrors(t *testing.T) {
	prefix := "data: {\"type\":\"response.output_text.delta\",\"output_index\":0,\"delta\":\"partial\"}\n\ndata: {\"type\":\"response.reasoning_summary_text.delta\",\"output_index\":1,\"delta\":\"plan\"}\n\n"
	for _, suffix := range []string{"", "data: {\"type\":\"response.failed\"}\n\n", "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\"}}\n\n"} {
		out, err := readSSE(strings.NewReader(prefix + suffix))
		if (err == nil) != strings.Contains(suffix, "response.completed") {
			t.Fatal("incorrect stream terminal result")
		}
		items := out["output"].([]any)
		if len(items) != 2 || text(object(object(items[0])["content"].([]any)[0])["text"]) != "partial" {
			t.Fatal(marshal(out))
		}
	}
}
func TestRoundInitialization(t *testing.T) {
	for _, fail := range []bool{false, true} {
		t.Run(fmt.Sprint(fail), func(t *testing.T) {
			var rounds, models atomic.Int32
			mcp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if strings.HasSuffix(r.URL.Path, "/round") {
					rounds.Add(1)
					if fail {
						w.WriteHeader(500)
						return
					}
					json.NewEncoder(w).Encode(map[string]any{"state_version": rounds.Load()})
					return
				}
				if rounds.Load() <= models.Load() {
					t.Error("tools listed before round initialization")
				}
				json.NewEncoder(w).Encode(map[string]any{"result": map[string]any{"tools": []any{}}})
			}))
			defer mcp.Close()
			model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if rounds.Load() != models.Load()+1 {
					t.Error("model ran before round initialization")
				}
				models.Add(1)
				json.NewEncoder(w).Encode(response(message("done")))
			}))
			defer model.Close()
			result := (&Runtime{}).Run(context.Background(), RunRequest{Agent: testAgent(model.URL), MockSystemURL: mcp.URL, SessionID: "session", UserMessages: []string{"first", "second"}})
			if fail {
				if result.Status != "failed" || models.Load() != 0 || rounds.Load() != 1 {
					t.Fatal(marshal(result))
				}
			} else {
				if result.Status != "completed" || rounds.Load() != 2 || models.Load() != 2 {
					t.Fatal(marshal(result))
				}
			}
		})
	}
}

func TestEngineValidation(t *testing.T) {
	result := (&Runtime{}).Run(context.Background(), RunRequest{Agent: map[string]any{"engine": "voice"}})
	if result.Status != "failed" || result.Error != "agent engine must be react" {
		t.Fatal(marshal(result))
	}
}

func TestEscapedCredentialRedaction(t *testing.T) {
	key := "credential\"with\\escape"
	value := redactValue(map[string]any{"nested": []any{key}}, key)
	if strings.Contains(marshal(value), "credential") {
		t.Fatal("escaped credential leaked")
	}
}

func TestMalformedArgumentsAndDuplicateCallIDs(t *testing.T) {
	var calls atomic.Int32
	mcp := fakeMCP(t, func(p map[string]any) map[string]any { calls.Add(1); return map[string]any{} })
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(response(function("same"), function("same")))
	}))
	defer model.Close()
	result := (&Runtime{}).Run(context.Background(), RunRequest{Agent: testAgent(model.URL), MockSystemURL: mcp.URL, SessionID: "session", Instruction: "run"})
	if result.Status != "failed" || calls.Load() != 0 {
		t.Fatal(marshal(result))
	}
	for _, args := range []string{"null", "[]", "broken"} {
		_, err := (&Runtime{}).callTool(context.Background(), RunRequest{MockSystemURL: mcp.URL, SessionID: "session"}, ToolCall{CallID: "id", Name: "echo", Arguments: args})
		if err == nil {
			t.Error("invalid arguments accepted")
		}
	}
}

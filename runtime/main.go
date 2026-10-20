package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const runtimeVersion = "esa-local-runtime/1.0.0"
const maxHTTPBytes = 16 << 20

type RunRequest struct {
	Agent          map[string]any `json:"agent"`
	Model          any            `json:"model,omitempty"`
	Instruction    string         `json:"instruction"`
	UserMessages   []string       `json:"user_messages,omitempty"`
	MockSystemURL  string         `json:"mockSystemUrl"`
	SessionID      string         `json:"sessionId"`
	ToolProfile    []string       `json:"toolProfile,omitempty"`
	MaxIterations  int            `json:"maxIterations,omitempty"`
	TurnTimeoutSec float64        `json:"turnTimeoutSec,omitempty"`
}
type ToolCall struct {
	CallID      string `json:"callId"`
	Name        string `json:"name"`
	Arguments   string `json:"arguments"`
	Output      string `json:"output"`
	Status      string `json:"status"`
	LatencyMS   int64  `json:"latencyMs"`
	OutputChars int    `json:"outputChars"`
}
type Iteration struct {
	Iteration  int            `json:"iteration"`
	TurnIndex  int            `json:"turnIndex"`
	Message    string         `json:"message"`
	Reasoning  string         `json:"reasoning"`
	ToolCalls  []ToolCall     `json:"toolCalls"`
	Usage      map[string]any `json:"usage,omitempty"`
	LatencyMS  int64          `json:"latencyMs"`
	Status     string         `json:"status"`
	ResponseID string         `json:"responseId,omitempty"`
	ReceivedAt string         `json:"receivedAt"`
	Error      string         `json:"error,omitempty"`
}
type Turn struct {
	Instruction string      `json:"instruction"`
	Iterations  []Iteration `json:"iterations"`
	Reply       string      `json:"reply"`
	Status      string      `json:"status"`
	Error       string      `json:"error,omitempty"`
}
type RunResult struct {
	Iterations     []Iteration `json:"iterations"`
	ToolCalls      []ToolCall  `json:"toolCalls"`
	Reply          string      `json:"reply"`
	Status         string      `json:"status"`
	Error          string      `json:"error,omitempty"`
	Turns          []Turn      `json:"turns"`
	RuntimeVersion string      `json:"runtimeVersion"`
}
type Runtime struct {
	Client       *http.Client
	ContextBytes int
}

func object(v any) map[string]any {
	m, _ := v.(map[string]any)
	if m == nil {
		return map[string]any{}
	}
	return m
}
func text(v any) string { s, _ := v.(string); return s }
func envValue(s string) string {
	if strings.HasPrefix(s, "${") && strings.HasSuffix(s, "}") {
		return os.Getenv(s[2 : len(s)-1])
	}
	return s
}
func failureStatus(ctx context.Context) string {
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return "timeout"
	}
	if ctx.Err() != nil {
		return "cancelled"
	}
	return "failed"
}
func marshal(v any) string { b, _ := json.Marshal(v); return string(b) }
func redact(s, key string) string {
	if key != "" {
		s = strings.ReplaceAll(s, key, "[REDACTED]")
	}
	return s
}
func redactValue(v any, key string) any {
	switch x := v.(type) {
	case string:
		return redact(x, key)
	case []any:
		for i := range x {
			x[i] = redactValue(x[i], key)
		}
		return x
	case map[string]any:
		clean := map[string]any{}
		for k, value := range x {
			clean[redact(k, key)] = redactValue(value, key)
		}
		return clean
	default:
		return v
	}
}

func (rt *Runtime) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/health", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" {
			http.Error(w, "method not allowed", 405)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"status": "ok", "version": runtimeVersion})
	})
	mux.HandleFunc("/v1/run", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" {
			http.Error(w, "method not allowed", 405)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, maxHTTPBytes)
		var req RunRequest
		d := json.NewDecoder(r.Body)
		if err := d.Decode(&req); err != nil {
			http.Error(w, "invalid run request", 400)
			return
		}
		var extra any
		if d.Decode(&extra) != io.EOF {
			http.Error(w, "trailing request data", 400)
			return
		}
		if req.SessionID == "" || req.MockSystemURL == "" || (len(req.UserMessages) == 0 && req.Instruction == "") || req.MaxIterations < 0 || req.MaxIterations > 100 || req.TurnTimeoutSec < 0 || req.TurnTimeoutSec > 3600 {
			http.Error(w, "invalid run limits, messages or session", 400)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(rt.Run(r.Context(), req))
	})
	return mux
}
func (rt *Runtime) Run(parent context.Context, req RunRequest) RunResult {
	result := RunResult{Iterations: []Iteration{}, ToolCalls: []ToolCall{}, Turns: []Turn{}, Status: "completed", RuntimeVersion: runtimeVersion}
	if engine := text(req.Agent["engine"]); engine != "" && engine != "react" {
		result.Status = "failed"
		result.Error = "agent engine must be react"
		return result
	}
	cfg, err := modelConfig(req)
	if err != nil {
		result.Status = "failed"
		result.Error = err.Error()
		return result
	}
	if req.MaxIterations == 0 {
		req.MaxIterations = 12
	}
	if req.TurnTimeoutSec == 0 {
		req.TurnTimeoutSec = 240
	}
	messages := req.UserMessages
	if len(messages) == 0 {
		messages = []string{req.Instruction}
	}
	transcript := []any{}
	callIDs := map[string]bool{}
	for ti, msg := range messages {
		ctx, cancel := context.WithTimeout(parent, time.Duration(req.TurnTimeoutSec*float64(time.Second)))
		turn := Turn{Instruction: msg, Iterations: []Iteration{}, Status: "completed"}
		var tools []any
		listErr := rt.beginRound(ctx, req)
		if listErr == nil {
			tools, listErr = rt.listTools(ctx, req)
		}
		if listErr != nil {
			turn.Status = failureStatus(ctx)
			turn.Error = listErr.Error()
		} else {
			transcript = append(transcript, map[string]any{"role": "user", "content": msg})
			for n := 1; n <= req.MaxIterations; n++ {
				it := Iteration{Iteration: n, TurnIndex: ti, ToolCalls: []ToolCall{}, Status: "completed", ReceivedAt: time.Now().UTC().Format(time.RFC3339Nano)}
				start := time.Now()
				body := cfg.body(transcript, tools)
				budget := rt.ContextBytes
				if budget <= 0 {
					budget = 1 << 20
				}
				var output map[string]any
				var modelErr error
				if len(marshal(body)) > budget {
					modelErr = errors.New("context budget exceeded; transcript was not truncated")
				} else {
					output, modelErr = rt.respond(ctx, cfg, body)
				}
				it.LatencyMS = time.Since(start).Milliseconds()
				it.ReceivedAt = time.Now().UTC().Format(time.RFC3339Nano)
				it.ResponseID = text(output["id"])
				it.Usage = normalizeUsage(object(output["usage"]))
				items, _ := output["output"].([]any)
				replay := []any{}
				for _, raw := range items {
					item := object(raw)
					switch text(item["type"]) {
					case "message":
						contents, _ := item["content"].([]any)
						clean := []any{}
						for _, rawContent := range contents {
							content := object(rawContent)
							if text(content["type"]) == "output_text" {
								it.Message += text(content["text"])
								clean = append(clean, map[string]any{"type": "output_text", "text": text(content["text"])})
							}
							if text(content["type"]) == "refusal" {
								it.Message += text(content["refusal"])
								clean = append(clean, map[string]any{"type": "refusal", "refusal": text(content["refusal"])})
							}
						}
						if len(clean) > 0 {
							replay = append(replay, map[string]any{"type": "message", "role": "assistant", "content": clean})
						}
					case "reasoning":
						for _, field := range []string{"summary", "content"} {
							parts, _ := item[field].([]any)
							for _, part := range parts {
								it.Reasoning += text(object(part)["text"])
							}
						}
					case "function_call":
						call := ToolCall{CallID: text(item["call_id"]), Name: text(item["name"]), Arguments: text(item["arguments"]), Status: "pending"}
						it.ToolCalls = append(it.ToolCalls, call)
						replay = append(replay, map[string]any{"type": "function_call", "call_id": call.CallID, "name": call.Name, "arguments": call.Arguments})
					}
				}
				if modelErr == nil && it.Message == "" && len(it.ToolCalls) == 0 {
					modelErr = errors.New("model response has no assistant message or function call")
				}
				if modelErr == nil && text(output["status"]) != "completed" {
					modelErr = errors.New("model response did not complete")
				}
				if modelErr == nil {
					for _, call := range it.ToolCalls {
						if call.CallID == "" || callIDs[call.CallID] {
							modelErr = errors.New("missing or duplicate model call_id")
							break
						}
						callIDs[call.CallID] = true
					}
				}
				if modelErr != nil {
					it.Status = failureStatus(ctx)
					it.Error = redact(modelErr.Error(), cfg.key)
					for j := range it.ToolCalls {
						it.ToolCalls[j].Status = "cancelled"
					}
				} else {
					transcript = append(transcript, replay...)
					for j := range it.ToolCalls {
						callStart := time.Now()
						call := &it.ToolCalls[j]
						output, callErr := rt.callTool(ctx, req, *call)
						call.LatencyMS = time.Since(callStart).Milliseconds()
						call.Status = "completed"
						if callErr != nil {
							call.Status = "failed"
							if output == "" {
								output = marshal(map[string]any{"isError": true, "error": callErr.Error()})
							}
						}
						call.Output = output
						call.OutputChars = len([]rune(output))
						var transport transportError
						if ctx.Err() != nil || errors.As(callErr, &transport) {
							call.Status = "failed"
							if ctx.Err() != nil {
								call.Status = "cancelled"
							}
							it.Status = failureStatus(ctx)
							it.Error = "tool execution interrupted"
							for k := j + 1; k < len(it.ToolCalls); k++ {
								it.ToolCalls[k].Status = "cancelled"
							}
							break
						}
						transcript = append(transcript, map[string]any{"type": "function_call_output", "call_id": call.CallID, "output": output})
					}
				}
				turn.Iterations = append(turn.Iterations, it)
				result.Iterations = append(result.Iterations, it)
				result.ToolCalls = append(result.ToolCalls, it.ToolCalls...)
				if it.Status != "completed" {
					turn.Status = it.Status
					turn.Error = it.Error
					break
				}
				if len(it.ToolCalls) == 0 {
					turn.Reply = it.Message
					break
				}
				if n == req.MaxIterations {
					turn.Status = "limit_exceeded"
					turn.Error = "maximum iterations reached"
				}
			}
		}
		cancel()
		result.Turns = append(result.Turns, turn)
		result.Reply = turn.Reply
		if turn.Status != "completed" {
			result.Status = turn.Status
			result.Error = redact(turn.Error, cfg.key)
			break
		}
	}
	// Credential redaction applies to every returned field, including model/tool echo.
	if cfg.key != "" {
		var value any
		_ = json.Unmarshal([]byte(marshal(result)), &value)
		_ = json.Unmarshal([]byte(marshal(redactValue(value, cfg.key))), &result)
	}
	return result
}
func normalizeUsage(u map[string]any) map[string]any {
	if len(u) == 0 {
		return nil
	}
	out := map[string]any{}
	for _, k := range []string{"input_tokens", "output_tokens", "total_tokens"} {
		if v, ok := u[k]; ok {
			out[k] = v
		}
	}
	if v, ok := object(u["input_tokens_details"])["cached_tokens"]; ok {
		out["cached_tokens"] = v
	}
	if v, ok := u["cached_tokens"]; ok {
		out["cached_tokens"] = v
	}
	for _, k := range []string{"input_tokens_details", "output_tokens_details"} {
		if v, ok := u[k]; ok {
			out[k] = v
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}
func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	budget := 1 << 20
	if v := os.Getenv("RUNTIME_CONTEXT_MAX_BYTES"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 {
			log.Fatal("invalid RUNTIME_CONTEXT_MAX_BYTES")
		}
		budget = n
	}
	rt := &Runtime{Client: &http.Client{}, ContextBytes: budget}
	server := &http.Server{Addr: ":" + port, Handler: rt.Handler(), ReadHeaderTimeout: 10 * time.Second, ReadTimeout: 30 * time.Second, IdleTimeout: 60 * time.Second}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		server.Shutdown(shutdown)
	}()
	fmt.Println("ESA local runtime listening on :" + port)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal("HTTP server failed")
	}
}

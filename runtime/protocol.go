package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
)

type transportError struct{ message string }

func (e transportError) Error() string { return e.message }

type modelSettings struct {
	endpoint, key, instructions string
	params                      map[string]any
	stream                      bool
}

func modelConfig(req RunRequest) (modelSettings, error) {
	react := object(req.Agent["react"])
	model := map[string]any{}
	for k, v := range object(react["model"]) {
		model[k] = v
	}
	if req.Model != nil {
		if s, ok := req.Model.(string); ok {
			model["model_id"] = s
		} else {
			for k, v := range object(req.Model) {
				model[k] = v
			}
		}
	}
	mp := object(react["model_params"])
	base := envValue(text(mp["explicit_api_base"]))
	if base == "" {
		base = os.Getenv("RESPONSES_API_BASE_URL")
	}
	key := envValue(text(mp["explicit_api_key"]))
	if key == "" {
		key = os.Getenv("RESPONSES_API_KEY")
	}
	u, err := url.Parse(base)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return modelSettings{}, errors.New("valid RESPONSES_API_BASE_URL is required")
	}
	endpoint := strings.TrimRight(base, "/")
	if !strings.HasSuffix(endpoint, "/responses") {
		endpoint += "/responses"
	}
	id := text(model["model_id"])
	if id == "" {
		id = text(model["model"])
	}
	if id == "" {
		return modelSettings{}, errors.New("model_id is required")
	}
	params := map[string]any{"model": id}
	for _, k := range []string{"temperature", "top_p", "max_output_tokens"} {
		if v, ok := model[k]; ok {
			params[k] = v
		}
	}
	if s := text(model["reasoning_effort"]); s != "" {
		params["reasoning"] = map[string]any{"effort": s}
	}
	stream := true
	if v, ok := model["stream"].(bool); ok {
		stream = v
	}
	return modelSettings{endpoint: endpoint, key: key, instructions: text(react["instructions"]), params: params, stream: stream}, nil
}
func (c modelSettings) body(transcript, tools []any) map[string]any {
	b := map[string]any{}
	for k, v := range c.params {
		b[k] = v
	}
	b["instructions"] = c.instructions
	b["input"] = transcript
	b["tools"] = tools
	b["store"] = false
	b["stream"] = c.stream
	return b
}
func (rt *Runtime) client() *http.Client {
	if rt.Client != nil {
		return rt.Client
	}
	return http.DefaultClient
}
func (rt *Runtime) post(ctx context.Context, endpoint, key string, body any) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, "POST", endpoint, bytes.NewBufferString(marshal(body)))
	if err != nil {
		return nil, errors.New("invalid HTTP endpoint")
	}
	req.Header.Set("Content-Type", "application/json")
	if key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	resp, err := rt.client().Do(req)
	if err != nil {
		return nil, transportError{"upstream HTTP request failed"}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		resp.Body.Close()
		return nil, fmt.Errorf("upstream HTTP status %d", resp.StatusCode)
	}
	return resp, nil
}
func decodeBounded(r io.Reader, v any) error {
	data, err := io.ReadAll(io.LimitReader(r, maxHTTPBytes+1))
	if err != nil {
		return err
	}
	if len(data) > maxHTTPBytes {
		return errors.New("upstream response size limit exceeded")
	}
	return json.Unmarshal(data, v)
}
func (rt *Runtime) respond(ctx context.Context, c modelSettings, body any, emit func(map[string]any)) (map[string]any, error) {
	resp, err := rt.post(ctx, c.endpoint, c.key, body)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") {
		return readSSE(resp.Body, emit)
	}
	out := map[string]any{}
	if err := decodeBounded(resp.Body, &out); err != nil {
		return out, errors.New("invalid Responses JSON")
	}
	return out, nil
}

// SSE output-item completion is authoritative; deltas are retained on a disconnected stream.
func readSSE(r io.Reader, emit func(map[string]any)) (map[string]any, error) {
	out := map[string]any{}
	items := map[int]map[string]any{}
	scanner := bufio.NewScanner(io.LimitReader(r, maxHTTPBytes+1))
	scanner.Buffer(make([]byte, 4096), maxHTTPBytes)
	data := []string{}
	total := 0
	terminal := false
	snapshot := func() {
		indexes := []int{}
		for i := range items {
			indexes = append(indexes, i)
		}
		sort.Ints(indexes)
		list := []any{}
		for _, i := range indexes {
			list = append(list, items[i])
		}
		out["output"] = list
	}
	process := func() error {
		if len(data) == 0 {
			return nil
		}
		payload := strings.Join(data, "\n")
		data = nil
		if payload == "[DONE]" {
			return nil
		}
		event := map[string]any{}
		if err := json.Unmarshal([]byte(payload), &event); err != nil {
			return errors.New("invalid Responses SSE event")
		}
		typ := text(event["type"])
		if response, ok := event["response"].(map[string]any); ok {
			for k, v := range response {
				out[k] = v
			}
		}
		index := 0
		if n, ok := event["output_index"].(float64); ok {
			index = int(n)
		}
		switch typ {
		case "response.output_item.added", "response.output_item.done":
			items[index] = object(event["item"])
		case "response.function_call_arguments.delta":
			item := items[index]
			if item == nil {
				item = map[string]any{"type": "function_call"}
				items[index] = item
			}
			item["arguments"] = text(item["arguments"]) + text(event["delta"])
		case "response.output_text.delta", "response.reasoning_summary_text.delta", "response.reasoning_text.delta":
			if emit != nil {
				name := "text_delta"
				if strings.Contains(typ, "reasoning") {
					name = "reasoning_delta"
				}
				emit(map[string]any{"type": name, "text": text(event["delta"])})
			}
			item := items[index]
			if item == nil {
				item = map[string]any{}
				items[index] = item
			}
			if typ == "response.output_text.delta" {
				item["type"] = "message"
				content, _ := item["content"].([]any)
				if len(content) == 0 {
					content = []any{map[string]any{"type": "output_text", "text": ""}}
				}
				p := object(content[0])
				p["text"] = text(p["text"]) + text(event["delta"])
				item["content"] = content
			} else {
				item["type"] = "reasoning"
				summary, _ := item["summary"].([]any)
				if len(summary) == 0 {
					summary = []any{map[string]any{"type": "summary_text", "text": ""}}
				}
				p := object(summary[0])
				p["text"] = text(p["text"]) + text(event["delta"])
				item["summary"] = summary
			}
		case "response.completed":
			terminal = true
			if response := object(event["response"]); response["output"] == nil {
				snapshot()
			}
			return nil
		case "response.failed", "response.incomplete", "error":
			snapshot()
			terminal = true
			return errors.New("Responses stream reported failure")
		}
		return nil
	}
	for scanner.Scan() {
		line := scanner.Text()
		total += len(line) + 1
		if total > maxHTTPBytes {
			snapshot()
			return out, errors.New("Responses stream size limit exceeded")
		}
		if line == "" {
			if err := process(); err != nil {
				snapshot()
				return out, err
			}
			if terminal {
				return out, nil
			}
		} else if strings.HasPrefix(line, "data:") {
			data = append(data, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
		}
	}
	if err := scanner.Err(); err != nil {
		snapshot()
		return out, errors.New("Responses stream read failed")
	}
	if err := process(); err != nil {
		snapshot()
		return out, err
	}
	if terminal {
		return out, nil
	}
	snapshot()
	return out, errors.New("Responses stream disconnected before completion")
}
func mcpEndpoint(req RunRequest) (string, error) {
	u, err := url.Parse(req.MockSystemURL)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return "", errors.New("invalid MCP endpoint")
	}
	return strings.TrimRight(req.MockSystemURL, "/") + "/eval/mcp/" + url.PathEscape(req.SessionID), nil
}
func (rt *Runtime) rpc(ctx context.Context, req RunRequest, method string, params any) (map[string]any, error) {
	endpoint, err := mcpEndpoint(req)
	if err != nil {
		return nil, err
	}
	resp, err := rt.post(ctx, endpoint, "", map[string]any{"jsonrpc": "2.0", "id": "runtime", "method": method, "params": params})
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	out := map[string]any{}
	if err := decodeBounded(resp.Body, &out); err != nil {
		return nil, transportError{"invalid or interrupted MCP JSON response"}
	}
	if out["error"] != nil {
		return nil, fmt.Errorf("MCP error: %s", marshal(out["error"]))
	}
	result, ok := out["result"].(map[string]any)
	if !ok {
		return nil, errors.New("missing MCP result")
	}
	return result, nil
}
func (rt *Runtime) beginRound(ctx context.Context, req RunRequest) error {
	if _, err := mcpEndpoint(req); err != nil {
		return err
	}
	endpoint := strings.TrimRight(req.MockSystemURL, "/") + "/eval/api/v1/sessions/" + url.PathEscape(req.SessionID) + "/round"
	resp, err := rt.post(ctx, endpoint, "", map[string]any{})
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	var result map[string]any
	if err := decodeBounded(resp.Body, &result); err != nil {
		return errors.New("invalid round initialization response")
	}
	return nil
}

func (rt *Runtime) listTools(ctx context.Context, req RunRequest) ([]any, error) {
	result, err := rt.rpc(ctx, req, "tools/list", map[string]any{})
	if err != nil {
		return nil, err
	}
	raw, ok := result["tools"].([]any)
	if !ok {
		return nil, errors.New("MCP tools/list missing tools")
	}
	if result["nextCursor"] != nil {
		return nil, errors.New("paginated tools/list is unsupported")
	}
	tools := []any{}
	names := map[string]bool{}
	for _, v := range raw {
		tool := object(v)
		name := text(tool["name"])
		schema, ok := tool["inputSchema"].(map[string]any)
		if name == "" || names[name] || !ok {
			return nil, errors.New("invalid or duplicate MCP tool schema")
		}
		names[name] = true
		tools = append(tools, map[string]any{"type": "function", "name": name, "description": text(tool["description"]), "parameters": schema, "strict": false})
	}
	if req.ToolProfile != nil {
		profile := map[string]bool{}
		for _, name := range req.ToolProfile {
			if profile[name] || !names[name] {
				return nil, errors.New("toolProfile differs from session tools/list")
			}
			profile[name] = true
		}
		if len(profile) != len(names) {
			return nil, errors.New("toolProfile differs from session tools/list")
		}
	}
	return tools, nil
}
func (rt *Runtime) callTool(ctx context.Context, req RunRequest, call ToolCall) (string, error) {
	args := map[string]any{}
	if err := json.Unmarshal([]byte(call.Arguments), &args); err != nil || args == nil {
		return "", errors.New("function arguments must be a JSON object")
	}
	args["_call_id"] = call.CallID
	result, err := rt.rpc(ctx, req, "tools/call", map[string]any{"name": call.Name, "arguments": args, "call_id": call.CallID})
	if err != nil {
		return "", err
	}
	output := marshal(result)
	if result["isError"] == true {
		return output, errors.New("MCP tool execution failed")
	}
	return output, nil
}

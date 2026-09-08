// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"nvpair-shared/schedulerwire"
)

type captureWriter struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (c *captureWriter) Read(p []byte) (int, error) { return 0, io.EOF }
func (c *captureWriter) Close() error               { return nil }
func (c *captureWriter) Write(p []byte) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.buf.Write(p)
}
func (c *captureWriter) String() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.buf.String()
}

func TestConcurrencyGate_DisabledAllowsAll(t *testing.T) {
	p := testProxy(NewDiscovery(), 1234)
	gate := newConcurrencyGate(p)
	gate.setMaxNodeConcurrency(0)

	if gate.enabled() {
		t.Fatal("gate should be disabled when maxNodeConcurrency is 0")
	}

	cands := []candidate{{id: "node-1"}, {id: "node-2"}}
	filtered := gate.filterIdleCandidates(cands)
	if len(filtered) != 2 {
		t.Fatalf("expected all 2 candidates, got %d", len(filtered))
	}

	release := gate.acquire("node-1")
	release()
	if count := gate.inFlightCount("node-1"); count != 0 {
		t.Fatalf("expected 0 in-flight count, got %d", count)
	}
}

func TestConcurrencyGate_FilterIdleCandidates_InFlight(t *testing.T) {
	p := testProxy(NewDiscovery(), 1234)
	gate := newConcurrencyGate(p)
	gate.setMaxNodeConcurrency(1)

	if !gate.enabled() {
		t.Fatal("gate should be enabled when maxNodeConcurrency is 1")
	}

	cands := []candidate{{id: "node-1"}, {id: "node-2"}}

	// Both available initially
	avail := gate.filterIdleCandidates(cands)
	if len(avail) != 2 {
		t.Fatalf("expected 2 available candidates, got %d", len(avail))
	}

	// Acquire node-1
	releaseNode1 := gate.acquire("node-1")
	if gate.inFlightCount("node-1") != 1 {
		t.Fatalf("expected node-1 inFlight = 1, got %d", gate.inFlightCount("node-1"))
	}

	// Now only node-2 should be available
	avail = gate.filterIdleCandidates(cands)
	if len(avail) != 1 || avail[0].id != "node-2" {
		t.Fatalf("expected only node-2 to be available, got %+v", avail)
	}

	// Acquire node-2 as well
	releaseNode2 := gate.acquire("node-2")
	avail = gate.filterIdleCandidates(cands)
	if len(avail) != 0 {
		t.Fatalf("expected 0 available candidates when both busy, got %d", len(avail))
	}

	// Release node-1
	releaseNode1()
	// Idempotent release check
	releaseNode1()
	if gate.inFlightCount("node-1") != 0 {
		t.Fatalf("expected node-1 inFlight = 0 after release, got %d", gate.inFlightCount("node-1"))
	}

	avail = gate.filterIdleCandidates(cands)
	if len(avail) != 1 || avail[0].id != "node-1" {
		t.Fatalf("expected node-1 to be available again, got %+v", avail)
	}

	releaseNode2()
}

func TestConcurrencyGate_FilterIdleCandidates_Pending(t *testing.T) {
	p := testProxy(NewDiscovery(), 1234)
	gate := newConcurrencyGate(p)
	gate.setMaxNodeConcurrency(1)

	// Set scheduler priority snapshot where node-1 has 1 pending workload from cluster
	p.SetPrioritySnapshot(schedulerwire.Priority{
		Nodes: []string{"node-1", "node-2"},
		Ranks: []schedulerwire.NodeRank{
			{ID: "node-1", Pending: 1, Rank: 0},
			{ID: "node-2", Pending: 0, Rank: 1},
		},
	})

	cands := []candidate{{id: "node-1"}, {id: "node-2"}}
	avail := gate.filterIdleCandidates(cands)
	if len(avail) != 1 || avail[0].id != "node-2" {
		t.Fatalf("expected node-1 with pending=1 to be filtered out, got %+v", avail)
	}
}

func TestConcurrencyGate_WriteOverloadedResponse(t *testing.T) {
	cw := &captureWriter{}
	codec := NewCodec(cw)
	p := NewProxy(codec, NewDiscovery(), 1234)
	gate := newConcurrencyGate(p)
	gate.setMaxNodeConcurrency(1)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)

	start := time.Now()
	gate.writeOverloadedResponse(rec, req, "test-req-123", "gemma-4-31b", start)

	if rec.Code != 529 {
		t.Fatalf("expected HTTP 529, got %d", rec.Code)
	}
	if retryAfter := rec.Header().Get("Retry-After"); retryAfter != "2" {
		t.Fatalf("expected Retry-After: 2, got %q", retryAfter)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("expected Content-Type: application/json, got %q", ct)
	}

	var parsed struct {
		Error struct {
			Message string  `json:"message"`
			Type    string  `json:"type"`
			Code    int     `json:"code"`
			Param   *string `json:"param"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &parsed); err != nil {
		t.Fatalf("failed to parse response JSON: %v", err)
	}
	if parsed.Error.Code != 529 || parsed.Error.Type != "overloaded_error" {
		t.Fatalf("unexpected error payload: %+v", parsed.Error)
	}

	// Verify proxy/request notification was emitted
	output := cw.String()
	if !strings.Contains(output, `"proxy/request"`) {
		t.Fatalf("proxy/request notification was not emitted, got: %s", output)
	}
	if !strings.Contains(output, `"status":529`) {
		t.Fatalf("proxy/request notification missing status 529, got: %s", output)
	}
}

func TestResolveMaxNodeConcurrency(t *testing.T) {
	// 1. Flag takes precedence
	os.Setenv("NVPAIR_MAX_NODE_CONCURRENCY", "5")
	defer os.Unsetenv("NVPAIR_MAX_NODE_CONCURRENCY")
	if got := resolveMaxNodeConcurrency(2); got != 2 {
		t.Fatalf("expected flag value 2, got %d", got)
	}

	// 2. Env used if flag is 0
	if got := resolveMaxNodeConcurrency(0); got != 5 {
		t.Fatalf("expected env value 5, got %d", got)
	}

	// 3. Fallback to 0 if invalid
	os.Setenv("NVPAIR_MAX_NODE_CONCURRENCY", "invalid")
	if got := resolveMaxNodeConcurrency(0); got != 0 {
		t.Fatalf("expected 0 for invalid env, got %d", got)
	}
}

func TestHandleHTTP_ConcurrencyGate_RejectsWhenBusy(t *testing.T) {
	block := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-block
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"choices":[]}`))
	}))
	defer server.Close()

	disc := NewDiscovery()
	disc.manual["node-1"] = nodeForModel(t, "node-1", server.URL, "gemma-4-31b")

	p := testProxy(disc, 1234)
	p.SetMaxNodeConcurrency(1)

	// First request blocks on the server
	firstDone := make(chan int)
	go func() {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"gemma-4-31b"}`))
		p.handleHTTP(rec, req)
		firstDone <- rec.Code
	}()

	// Wait for first request to acquire slot
	for range 50 {
		if p.concurrencyGate.inFlightCount("node-1") == 1 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if p.concurrencyGate.inFlightCount("node-1") != 1 {
		close(block)
		t.Fatal("first request failed to acquire in-flight slot")
	}

	// Second request while first is in-flight should immediately return 529
	rec2 := httptest.NewRecorder()
	req2 := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"gemma-4-31b"}`))
	p.handleHTTP(rec2, req2)
	if rec2.Code != 529 {
		close(block)
		t.Fatalf("expected 529 for concurrent request, got %d", rec2.Code)
	}

	// Unblock first request
	close(block)
	code1 := <-firstDone
	if code1 != http.StatusOK {
		t.Fatalf("expected first request to succeed with 200, got %d", code1)
	}

	// Wait for slot release
	for range 50 {
		if p.concurrencyGate.inFlightCount("node-1") == 0 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}

	// Third request should succeed with 200
	rec3 := httptest.NewRecorder()
	req3 := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"gemma-4-31b"}`))
	p.handleHTTP(rec3, req3)
	if rec3.Code != http.StatusOK {
		t.Fatalf("expected third request to succeed with 200, got %d", rec3.Code)
	}
}


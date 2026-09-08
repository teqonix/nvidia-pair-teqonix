// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

package main

import (
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"sync"
	"time"

	"nvpair-shared/cors"
)

// concurrencyGate manages per-node inference concurrency limits to prevent
// upstream model-swapping thrashing and engine protocol aborts when multiple
// models are aliased across nodes.
type concurrencyGate struct {
	p                  *Proxy
	mu                 sync.Mutex
	maxNodeConcurrency int
	inFlight           map[string]int
}

func newConcurrencyGate(p *Proxy) *concurrencyGate {
	return &concurrencyGate{
		p:        p,
		inFlight: make(map[string]int),
	}
}

func (g *concurrencyGate) enabled() bool {
	if g == nil {
		return false
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.maxNodeConcurrency > 0
}

func (g *concurrencyGate) setMaxNodeConcurrency(limit int) {
	if g == nil {
		return
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if limit < 0 {
		limit = 0
	}
	g.maxNodeConcurrency = limit
	if limit > 0 {
		slog.Info("node concurrency gate enabled", "max_node_concurrency", limit)
	}
}

func (g *concurrencyGate) inFlightCount(nodeID string) int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.inFlight[nodeID]
}

// filterIdleCandidates returns the subset of candidates whose current load
// (local in-flight + cluster-wide pending) is strictly below maxNodeConcurrency.
func (g *concurrencyGate) filterIdleCandidates(candidates []candidate) []candidate {
	if !g.enabled() {
		return candidates
	}
	g.mu.Lock()
	limit := g.maxNodeConcurrency
	g.mu.Unlock()

	idle := make([]candidate, 0, len(candidates))
	for _, cand := range candidates {
		inFlight := g.inFlightCount(cand.id)
		pending := g.p.priorityPendingCount(cand.id)
		if inFlight+pending < limit {
			idle = append(idle, cand)
		}
	}
	return idle
}

// acquire reserves a concurrency slot for the given nodeID and returns a release
// function. The caller must invoke release when inference completes or fails over.
func (g *concurrencyGate) acquire(nodeID string) func() {
	if !g.enabled() || nodeID == "" {
		return func() {}
	}
	g.mu.Lock()
	g.inFlight[nodeID]++
	g.mu.Unlock()

	var once sync.Once
	return func() {
		once.Do(func() {
			g.mu.Lock()
			if g.inFlight[nodeID] > 0 {
				g.inFlight[nodeID]--
			}
			g.mu.Unlock()
		})
	}
}

// writeOverloadedResponse writes an HTTP 529 OpenAI-compatible response and emits
// the proxy/request event.
func (g *concurrencyGate) writeOverloadedResponse(w http.ResponseWriter, r *http.Request, reqID, model string, start time.Time) {
	if cors.WritePreflight(w, r) {
		return
	}
	cors.Apply(w.Header())
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Retry-After", "2")
	w.WriteHeader(529)

	respBody := `{"error":{"message":"All eligible nodes capable of running this model are currently busy.","type":"overloaded_error","param":null,"code":529}}`
	_, _ = w.Write([]byte(respBody))

	slog.Warn("proxy request rejected: all eligible nodes busy",
		"id", reqID, "method", r.Method, "path", r.URL.Path,
		"remote", r.RemoteAddr, "model", model)

	g.p.codec.Notify("proxy/request", RequestEvent{
		ID:       reqID,
		Method:   r.Method,
		Path:     r.URL.Path,
		Status:   529,
		Duration: time.Since(start).Milliseconds(),
		Error:    "all eligible nodes busy",
	})
}

func (p *Proxy) priorityPendingCount(nodeID string) int {
	p.priorityMu.RLock()
	defer p.priorityMu.RUnlock()
	if p.priorityPending == nil {
		return 0
	}
	return p.priorityPending[nodeID]
}

// SetMaxNodeConcurrency configures the concurrency limit on the proxy.
func (p *Proxy) SetMaxNodeConcurrency(limit int) {
	if p.concurrencyGate != nil {
		p.concurrencyGate.setMaxNodeConcurrency(limit)
	}
}

// resolveMaxNodeConcurrency resolves the effective limit from CLI flag or env var.
func resolveMaxNodeConcurrency(flagVal int) int {
	if flagVal > 0 {
		return flagVal
	}
	if env := os.Getenv("NVPAIR_MAX_NODE_CONCURRENCY"); env != "" {
		if val, err := strconv.Atoi(env); err == nil && val > 0 {
			return val
		}
	}
	return 0
}

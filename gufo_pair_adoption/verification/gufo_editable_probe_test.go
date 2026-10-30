// SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
// SPDX-License-Identifier: Apache-2.0

// Probe: the bundled linux/amd64 runtime carries an editable_launch policy
// with llama.cpp-specific controls (--host/--port/--cors-origins). deepMerge
// leaves it in place unless we override it. lifecycle.go:326 runs
// validateEffectiveLaunch on the start command at StartIndex, so the bundled
// policy may reject (or rewrite) `gufo-pairctl start`.

package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func mergedPlatform(t *testing.T) Platform {
	t.Helper()
	reg := NewRegistry()
	if err := reg.LoadFS(bundledManifests, "manifests"); err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	raw, err := os.ReadFile(gufoOverrideSrc)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "llamacpp.json"), raw, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := reg.LoadOverrideDir(dir); err != nil {
		t.Fatal(err)
	}
	m, _ := reg.Get("llamacpp")
	return m.Platforms["linux/amd64"]
}

func TestProbeEditableLaunchSurvival(t *testing.T) {
	p := mergedPlatform(t)
	b, _ := json.MarshalIndent(p.Runtime.EditableLaunch, "", "  ")
	t.Logf("effective editable_launch: %s", string(b))
	t.Logf("runtime.start = %v", p.Runtime.Start)
	if p.Runtime.EditableLaunch != nil {
		t.Logf("StartIndex=%d -> PAIR applies validateEffectiveLaunch to start[%d] = %v",
			p.Runtime.EditableLaunch.StartIndex, p.Runtime.EditableLaunch.StartIndex,
			p.Runtime.Start[p.Runtime.EditableLaunch.StartIndex])
	}
}

// Simulate what doStart does at the StartIndex command.
func TestProbeStartCommandValidation(t *testing.T) {
	p := mergedPlatform(t)
	rt := p.Runtime
	if len(rt.Start) == 0 {
		t.Fatal("no start commands")
	}
	vars := map[string]string{
		"cli":         "/usr/local/bin/gufo-pairctl",
		"host":        "127.0.0.1",
		"port":        "8081",
		"install_dir": "/tmp/llamacpp-install",
		"models_dir":  "/home/u/.llamacpp",
		"bin":         "/tmp/llamacpp-install/llama-server",
	}
	idx := 0
	if rt.EditableLaunch != nil {
		idx = rt.EditableLaunch.StartIndex
	}
	launch, err := resolveRuntimeCommand(rt, idx, vars)
	if err != nil {
		t.Fatalf("resolveRuntimeCommand(start[%d]): %v", idx, err)
	}
	t.Logf("resolved launch: bin=%q args=%v", launch.Bin, launch.Args)

	if rt.EditableLaunch != nil && idx == rt.EditableLaunch.StartIndex {
		if err := validateEffectiveLaunch(rt, launch, vars["host"], vars["port"]); err != nil {
			t.Errorf("BLOCKING: PAIR rejects our start command because of the surviving "+
				"bundled editable_launch policy: %v", err)
		} else {
			t.Log("validateEffectiveLaunch accepted our start command")
		}
	}
}

// Also check what the *editable* launch text PAIR would show/allow looks like.
func TestProbeArgumentText(t *testing.T) {
	p := mergedPlatform(t)
	rt := p.Runtime
	if rt.EditableLaunch == nil {
		t.Skip("no editable_launch")
	}
	vars := map[string]string{
		"cli": "/usr/local/bin/gufo-pairctl", "host": "127.0.0.1", "port": "8081",
		"install_dir": "/tmp/i", "models_dir": "/tmp/m",
	}
	launch, err := resolveRuntimeCommand(rt, rt.EditableLaunch.StartIndex, vars)
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	txt, err := launch.argumentText(rt.EditableLaunch)
	t.Logf("argumentText -> %q err=%v", txt, err)
}

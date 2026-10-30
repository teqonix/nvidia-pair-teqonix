// SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
// SPDX-License-Identifier: Apache-2.0

// Probe: how do we clear the bundled editable_launch policy so PAIR stops
// trying to append/validate llama.cpp launch flags on `gufo-pairctl start`?
//
// Each variant lives in manifest_check/variants/*.json and is merged onto the
// real bundled manifest by the real loader. We report, per variant:
//   - accepted / rejected (rejected = override silently skipped)
//   - the effective editable_launch after deepMerge
//   - the resolved start command
//   - whether validateEffectiveLaunch accepts it

package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const variantsDir = "/var/lib/turnstone/gufo_pair_adoption/manifest_check/variants"

func probeVariant(t *testing.T, path string) {
	t.Helper()
	name := filepath.Base(path)

	reg := NewRegistry()
	if err := reg.LoadFS(bundledManifests, "manifests"); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read variant: %v", err)
	}
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "llamacpp.json"), raw, 0o644); err != nil {
		t.Fatal(err)
	}
	_ = reg.LoadOverrideDir(dir)

	m, ok := reg.Get("llamacpp")
	if !ok {
		t.Fatalf("%s: engine missing", name)
	}
	if m.DisplayName == "llama.cpp" {
		t.Errorf("%s: REJECTED — override silently skipped, bundled manifest still in force", name)
		return
	}
	p := m.Platforms["linux/amd64"]
	eb, _ := json.Marshal(p.Runtime.EditableLaunch)
	t.Logf("%s: accepted; editable_launch=%s", name, string(eb))
	t.Logf("    start=%v", p.Runtime.Start)

	vars := map[string]string{
		"cli": "/usr/local/bin/gufo-pairctl", "host": "127.0.0.1", "port": "8081",
		"install_dir": "/tmp/i", "models_dir": "/tmp/m", "bin": "/tmp/i/llama-server",
	}
	idx := 0
	if p.Runtime.EditableLaunch != nil {
		idx = p.Runtime.EditableLaunch.StartIndex
	}
	if idx < 0 || idx >= len(p.Runtime.Start) {
		t.Errorf("%s: start_index %d out of range (len(start)=%d)", name, idx, len(p.Runtime.Start))
		return
	}
	launch, err := resolveRuntimeCommand(p.Runtime, idx, vars)
	if err != nil {
		t.Errorf("%s: resolveRuntimeCommand(start[%d]) failed: %v", name, idx, err)
		return
	}
	t.Logf("    resolved bin=%q args=%v", launch.Bin, launch.Args)

	if p.Runtime.EditableLaunch != nil && idx == p.Runtime.EditableLaunch.StartIndex {
		if err := validateEffectiveLaunch(p.Runtime, launch, vars["host"], vars["port"]); err != nil {
			t.Errorf("%s: BLOCKING validateEffectiveLaunch REJECTS: %v", name, err)
		} else {
			t.Log("    validateEffectiveLaunch ACCEPTS")
		}
	} else {
		t.Log("    editable validation does not apply -> start runs verbatim")
	}
}

func TestProbeClearEditableLaunch(t *testing.T) {
	entries, err := os.ReadDir(variantsDir)
	if err != nil {
		t.Skipf("no variants dir (%v)", err)
	}
	found := 0
	for _, e := range entries {
		if e.IsDir() || !strings.EqualFold(filepath.Ext(e.Name()), ".json") {
			continue
		}
		found++
		name := e.Name()
		t.Run(name, func(t *testing.T) {
			probeVariant(t, filepath.Join(variantsDir, name))
		})
	}
	if found == 0 {
		t.Skip("no variant files")
	}
}

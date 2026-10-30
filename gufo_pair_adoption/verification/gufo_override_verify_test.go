// SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
// SPDX-License-Identifier: Apache-2.0

// Verification harness for the GUFO masquerade manifest override.
//
// This is NOT a unit test of nvpair-engine-manager. It is a probe that runs the
// REAL manifest loader (LoadFS + LoadOverrideDir + deepMerge + Validate) over
// the staged override, so we learn what PAIR would actually do rather than
// what we assume it would do.
//
// Run from nvpair-engine-manager/:
//   go test -run TestGufoOverride -v

package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

const gufoOverrideSrc = "/var/lib/turnstone/gufo_pair_adoption/manifest/llamacpp.json"

func loadMerged(t *testing.T) *Manifest {
	t.Helper()
	reg := NewRegistry()
	if err := reg.LoadFS(bundledManifests, "manifests"); err != nil {
		t.Fatalf("load bundled manifests: %v", err)
	}

	dir := t.TempDir()
	raw, err := os.ReadFile(gufoOverrideSrc)
	if err != nil {
		t.Fatalf("read staged override %s: %v", gufoOverrideSrc, err)
	}
	if err := os.WriteFile(filepath.Join(dir, "llamacpp.json"), raw, 0o644); err != nil {
		t.Fatalf("write override into dir: %v", err)
	}
	before, _ := reg.Get("llamacpp")
	beforeRun := len(before.Platforms["linux/amd64"].Install.Run)

	if err := reg.LoadOverrideDir(dir); err != nil {
		t.Fatalf("LoadOverrideDir: %v", err)
	}

	m, ok := reg.Get("llamacpp")
	if !ok {
		t.Fatal("llamacpp engine missing after override merge")
	}
	// LoadOverrideDir skips an invalid override and keeps the bundled manifest.
	// Detect that silently-rejected case loudly: if the display name did not
	// change, the override was rejected wholesale.
	if m.DisplayName == "llama.cpp" && beforeRun > 0 {
		b, _ := json.MarshalIndent(m.Platforms["linux/amd64"].Install, "", "  ")
		t.Fatalf("OVERRIDE WAS SILENTLY REJECTED — bundled manifest still in force.\n"+
			"display_name still %q, bundled install.run len=%d\ninstall now: %s",
			m.DisplayName, beforeRun, string(b))
	}
	return m
}

func TestGufoOverrideIsAccepted(t *testing.T) {
	m := loadMerged(t)
	t.Logf("override accepted: display_name=%q manifest_version=%d", m.DisplayName, m.ManifestVersion)
}

func TestGufoOverrideKillsLlamaDownload(t *testing.T) {
	m := loadMerged(t)
	p := m.Platforms["linux/amd64"]
	b, _ := json.MarshalIndent(p.Install, "", "  ")
	t.Logf("effective linux/amd64 install: %s", string(b))
	if p.Install != nil && (len(p.Install.Artifacts) > 0 || p.Install.Fetch != nil) {
		t.Errorf("HAZARD: install still carries fetch/artifacts — PAIR can still download real llama.cpp")
		for _, a := range p.Install.Artifacts {
			t.Errorf("  artifact %q -> %s", a.Name, a.URL)
		}
	}
}

func TestGufoOverrideRuntimeIsCommandMode(t *testing.T) {
	m := loadMerged(t)
	rt := m.Platforms["linux/amd64"].Runtime
	b, _ := json.MarshalIndent(rt, "", "  ")
	t.Logf("effective linux/amd64 runtime: %s", string(b))
	if got := rt.modeOrDefault(); got != "command" {
		t.Errorf("runtime.mode = %q, want %q", got, "command")
	}
	if len(rt.Start) == 0 {
		t.Error("runtime.start empty — command mode requires it")
	}
	if rt.Ready == nil || rt.Ready.JSONMatch == nil || rt.Ready.JSONMatch.Field != "role" {
		t.Error("ready probe must json_match on role")
	}
}

func TestGufoOverrideDetectAndUninstall(t *testing.T) {
	m := loadMerged(t)
	p := m.Platforms["linux/amd64"]
	t.Logf("detect=%v models_dir=%q uninstall=%+v", p.Detect, p.ModelsDir, p.Uninstall)
	if len(p.Detect) == 0 {
		t.Error("detect empty — install.go cannot no-op, so it may try to install")
	}
	if p.ModelsDir == "" {
		t.Error("models_dir empty — uninstall safety guarantee requires it")
	}
}

func TestGufoOverrideActionsIntact(t *testing.T) {
	m := loadMerged(t)
	want := []string{"list_models", "loaded_models", "pull_model", "load_model", "unload_model", "delete_model"}
	for _, name := range want {
		a, ok := m.Actions[name]
		if !ok {
			t.Errorf("action %q missing after merge", name)
			continue
		}
		if err := a.validate(name); err != nil {
			t.Errorf("action %q invalid after merge: %v", name, err)
		}
	}
}

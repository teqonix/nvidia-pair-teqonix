// SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
// SPDX-License-Identifier: Apache-2.0

// Probe: can an override neutralise the bundled linux/amd64 install artifacts?
// deepMerge replaces arrays wholesale, so [] should win over the bundled list —
// but validate() has two "required" rules that might reject the empty form.

package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func mergeVariant(t *testing.T, name, override string) (*Manifest, error) {
	t.Helper()
	reg := NewRegistry()
	if err := reg.LoadFS(bundledManifests, "manifests"); err != nil {
		t.Fatalf("bundled: %v", err)
	}
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "llamacpp.json"), []byte(override), 0o644); err != nil {
		t.Fatal(err)
	}
	_ = reg.LoadOverrideDir(dir) // bad overrides are skipped, not fatal
	m, ok := reg.Get("llamacpp")
	if !ok {
		t.Fatal("engine gone")
	}
	p := m.Platforms["linux/amd64"]
	b, _ := json.Marshal(p.Install)
	t.Logf("%-28s -> install=%s", name, string(b))
	return m, nil
}

func TestProbeNeutraliseArtifacts(t *testing.T) {
	// Variant A: empty arrays at platform level.
	mergeVariant(t, "platform artifacts:[] run:[]", `{
  "engine": "llamacpp",
  "display_name": "GUFO variant A",
  "manifest_version": 1,
  "models_dir": "~/.llamacpp",
  "platforms": {"linux/amd64": {
    "detect": ["/usr/local/bin/gufo-pairctl"],
    "install": {"mode": "user", "artifacts": [], "run": []}
  }}
}`)

	// Variant B: empty artifacts, no run key at all.
	mergeVariant(t, "platform artifacts:[] only", `{
  "engine": "llamacpp",
  "display_name": "GUFO variant B",
  "manifest_version": 1,
  "models_dir": "~/.llamacpp",
  "platforms": {"linux/amd64": {
    "detect": ["/usr/local/bin/gufo-pairctl"],
    "install": {"mode": "user", "artifacts": []}
  }}
}`)

	// Variant C: script install (replacement of the whole install semantic).
	mergeVariant(t, "platform script install", `{
  "engine": "llamacpp",
  "display_name": "GUFO variant C",
  "manifest_version": 1,
  "models_dir": "~/.llamacpp",
  "platforms": {"linux/amd64": {
    "detect": ["/usr/local/bin/gufo-pairctl"],
    "install": {"mode": "user", "script": ["sh","-c","true"]}
  }}
}`)
}

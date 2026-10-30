# Manifest verification harness

These tests prove the PAIR manifest override loads correctly against
**upstream PAIR's real Go engine-manager loader** - not a Python simulation of
`deepMerge`. They are the evidence behind the two blocker findings in `PLAN.md` §4.

The harness is *not* shipped as source: it runs inside a throwaway writable copy
of PAIR's `nvpair-engine-manager` package (kept out of the repo under
`manifest_check/`, since it is upstream code we don't own).

## Rebuilding the harness

```bash
# 1. toolchain (no system Go on these nodes)
curl -LO https://go.dev/dl/go1.24.2.linux-amd64.tar.gz
tar -C /tmp -xzf go1.24.2.linux-amd64.tar.gz          # -> /tmp/go
export PATH=/tmp/go/bin:$PATH
export HOME=/var/lib/turnstone/gohome
export GOCACHE=/var/lib/turnstone/gocache GOMODCACHE=/var/lib/turnstone/gomodcache
export GOTOOLCHAIN=auto        # PAIR's go.mod pins 1.25; Go fetches it

# 2. writable copy of the upstream package. All of these are required:
#    *.go  manifests/  catalog/  testdata/  services/shared
#    Cherry-picking individual .go files fails on internal symbols
#    (formatLaunchText, expandPath, applyLiteralLaunch, validateDownloadURL).
#    Omitting testdata/fakeengine fails TestMain; omitting
#    catalog/ollama-models.json fails the build outright.

# 3. drop these tests in next to the package and run
cp gufo_*_test.go <copy>/nvpair-engine-manager/
go test -run 'TestGufoOverride|TestProbeEditableLaunchSurvival|TestProbeStartCommandValidation' -v ./
```

## What each test proves

| Test | Proves |
|---|---|
| `TestGufoOverrideIsAccepted` | Override loads with no validation error |
| `TestGufoOverrideKillsLlamaDownload` | Effective install is `{"mode":"user"}` - no artifacts, no run, so no llama.cpp fetch is *possible* |
| `TestGufoOverrideRuntimeIsCommandMode` | `mode: command`, port `8081`, bind `127.0.0.1` survive the merge |
| `TestGufoOverrideDetectAndUninstall` | Merged `uninstall.run = [gufo-pairctl destroy]`; inherited `remove: [{install_dir}]` is gated by `installedByPAIR` (see PLAN §4) |
| `TestGufoOverrideActionsIntact` | Action exclusivity (exactly one of `http`/`cmd`/`remove_path`) holds |
| `TestProbeEditableLaunchSurvival` | `editable_launch` variants - only explicit `null` clears it (PLAN §4 Blocker 2) |
| `TestProbeStartCommandValidation` | Loader resolves start to `bin=/usr/local/bin/gufo-pairctl args=[start]` |

## Variants (`E_*.json`)

The four `editable_launch` probe inputs, kept so the negative results are
reproducible rather than folklore:

| File | Override | Loader verdict |
|---|---|---|
| `E_1_key_absent.json` | key absent | reject - `launch does not match its fixed startup arguments` |
| `E_2_explicit_null.json` | `null` | **accept** - validation skipped |
| `E_3_empty_obj.json` | `{}` | reject - `fixed_args`/`controls` survive |
| `E_4_arrays_emptied.json` | arrays emptied | reject - `editable launch controls must bind managed field server.port` |

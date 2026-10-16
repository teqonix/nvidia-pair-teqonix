# mbp-ai-core: wipe PAIR → install 1.0.0 as `turnstone` (launchd daemon)

Target: `turnstone@mbp-ai-core.lan` — macOS 26.6.2, Darwin arm64 (Mac Studio, 128 GB).

Read **THE FOUR THINGS THAT WILL BITE YOU** before running anything.

---

## THE FOUR THINGS THAT WILL BITE YOU

> [!CAUTION]
> **1. `nvpair-ui-broker` exits instantly on stdin EOF.**
> `readLoop()` treats `io.EOF` as terminal (`broker.go:2648`). launchd gives a daemon
> stdin from `/dev/null`, which is an *immediate EOF*. A naive LaunchDaemon that execs
> `nvpair-ui-broker` directly will start, see EOF, tear down every worker, and exit 0 —
> looking healthy in `launchctl list` with a zero exit code while serving nothing.
> **Verified locally:** `read` from `/dev/null` → `rc=1` instantly; from a FIFO opened
> `0<>fifo` → blocks forever. The wrapper must hold stdin open with a FIFO, or you must
> keep the TUI as the supervisor (the pattern this node already used).

> [!WARNING]
> **2. You cannot stop the old PAIR as `turnstone`.**
> All live `nvpair-*` processes run as **`teqonix`**. `kill -0 <pid>` → `operation not
> permitted`. Step 1 (`00_teardown_old_pair.sh`) **must** be run as `teqonix` or with
> `sudo`. Do not skip it — `:1234`/`:11434` stay bound by teqonix's proxies and your new
> instance silently lands on fallback ports.

> [!WARNING]
> **3. LM Studio models are MLX, not GGUF — nothing carries over.**
> The REST API reports `"compatibility_type": "gguf"` for all 19 models. **That field is
> LM Studio's internal metadata, not the on-disk container.** Actual on-disk counts under
> `/Users/teqonix/.lmstudio/models`: **22 `.safetensors` (MLX) vs 1 `.gguf`**. The
> friendly aliases are symlinks to MLX dirs:
> ```
> aliases/gemma-4-31b         -> mlx-community/gemma-4-31b-it-8bit
> aliases/qwen-3.8-27b        -> lmstudio-community/Qwen3.8-27B-MLX-4bit
> aliases/gemma-4-26b-a4b-8bit-> mlx-community/gemma-4-26b-a4b-8bit
> ```
> llama.cpp cannot read MLX. "Migration" = **full re-download of every model as GGUF**.
> Budget hundreds of GB and real time. Only reusable GGUF: `NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf`.

> [!IMPORTANT]
> **4. llama.cpp's facade port is `8080`, not `1234`.**
> Hard-coded in `services/shared/engines/engines.go`: `ollama=11434`, `lmstudio=1234`,
> `llamacpp=8080`. To keep clients on `:1234` you must (a) front **only** `llamacpp`
> (`--proxy-engines llamacpp`) so `1234` is free, and (b) pre-seed the persisted port
> file `llamacpp-proxy-port.json` with `{"port":1234}` — `chooseStartupPort()` makes the
> persisted value win over the default. `30_install_turnstone.sh` does both.

---

## Prerequisites

| Need | Why | Check |
|:--|:--|:--|
| `sudo` on the Mac **or** `teqonix` shell | teardown + `/Library/LaunchDaemons` | `sudo -n true` |
| Go **1.25** on a Linux box + `podman` | cross-compile darwin/arm64 (no Go on the Mac) | `go version` |
| Hugging Face token (read) | `google/gemma-*` are **gated** | `hf auth whoami` |
| ~400 GB free | GGUF cache | `df -h /System/Volumes/Data` (1.0 TiB free ✓) |

---

## Step 0 — Preflight (read-only, safe)

```bash
ssh turnstone@mbp-ai-core.lan 'bash -s' < 00_preflight.sh
```

Confirms port owners, process owners, the MLX-not-GGUF reality, and free disk.

## Step 1 — Tear down the old PAIR (**as `teqonix` or root**)

```bash
# as teqonix:
ssh teqonix@mbp-ai-core.lan 'bash -s' < 00_teardown_old_pair.sh
# or, from any account with sudo:
ssh turnstone@mbp-ai-core.lan 'sudo bash -s' < 00_teardown_old_pair.sh
```

Stops the TUI tmux session, the `.app`-supervised workers, and LM Studio's server, then
frees `:1234`/`:11434`. **Backs up nothing you own** — teqonix's config is unreadable by
`turnstone` by design; back it up as teqonix if you care about it.

> [!NOTE]
> Do **not** delete `/Applications/PAIR.app` yet. Its `cli-bin` is world-writable
> (`drwxrwxr-x turnstone:staff`), which tempts a same-place upgrade. That breaks the app's
> code signature and mixes versions under a live GUI install. `30_install_turnstone.sh`
> installs to a clean, separate `turnstone`-owned root instead.

## Step 2 — Cross-compile 1.0.0 for darwin/arm64

Run on a **Linux** node with Go 1.25 + podman (your `/workspace` worker). `build.sh`
never sets `GOOS`/`GOARCH`, so plain env-var cross-compilation works — and there is **zero
cgo** in the tree (verified: no `import "C"` anywhere in `services/` or `scripts/`), so no
macOS SDK is needed on the build host.

```bash
bash 20_build_darwin_arm64.sh
```

Produces `dist/pair-1.0.0-darwin-arm64/` + `SHA256SUMS`.

## Step 3 — Install as `turnstone`

```bash
scp -r dist/pair-1.0.0-darwin-arm64 turnstone@mbp-ai-core.lan:/Users/turnstone/
ssh turnstone@mbp-ai-core.lan 'bash -s' < 30_install_turnstone.sh
```

Installs to `/Users/turnstone/pair/1.0.0`, drops the FIFO-holding supervisor wrapper,
and pre-seeds `llamacpp-proxy-port.json` → `1234`.

## Step 4 — launchd daemon (starts at boot)

```bash
ssh turnstone@mbp-ai-core.lan 'sudo bash -s' < 40_install_launchd.sh
```

`/Library/LaunchDaemons/com.nvidia.nvpair.turnstone.plist` with `UserName=turnstone`,
`RunAtLoad`, `KeepAlive`. Replaces the stale `com.nvidia.nvpair` **LaunchAgent** in
turnstone's `~/Library/LaunchAgents` (which pointed at a `run_pair_tmux.sh` that fell back
to the teqonix `.app` binary — a cross-user footgun).

## Step 5 — Provision GGUF models

```bash
export HF_TOKEN=hf_xxx
bash 50_provision_gguf.sh            # streams to the node
```

> [!TIP]
> `pull_model` is a **JSON-RPC action over the broker's IPC**, not a CLI verb
> (`manager.go:406`, `pull.go:38`) — you drive it from the TUI, not from a shell. For
> scripted bulk provisioning, direct `curl` against Hugging Face into `~/.llamacpp` is the
> honest tool. The engine manifest's `pull_model` maps to `POST /models` on a *running*
> llama-server, which is a chicken-and-egg for cache warm-up.

## Step 6 — Verify

```bash
ssh turnstone@mbp-ai-core.lan 'bash -s' < 60_verify.sh
```

---

## Engine switch / eviction notes

- `llamacpp.json` runtime pins `--sleep-idle-seconds 300` in `editable_launch.fixed_args`,
  binds `127.0.0.1`, base engine port `8081`, ready-check `GET /props` expecting
  `role == "router"` (60 s timeout), stop = `SIGTERM` + 10 s grace.
- `LLAMA_CACHE` is set to `{models_dir}` = `~/.llamacpp` automatically.
- 300 s idle eviction is the manifest default — the plan's eviction check applies as-is.

## Rollback

```bash
sudo launchctl bootout system /Library/LaunchDaemons/com.nvidia.nvpair.turnstone.plist
# then, as teqonix, restart the old stack:
open -a PAIR   # as teqonix, restores the 0.91.7 GUI-supervised router
```
Config backups live in `/Users/turnstone/pair_backup_*/`.

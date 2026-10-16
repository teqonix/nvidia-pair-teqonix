#!/usr/bin/env bash
# 20_build_darwin_arm64.sh
#
# Cross-compile PAIR 1.0.0 services for darwin/arm64 (mbp-ai-core = Apple Silicon).
# Runs on a LINUX build node with podman. No macOS SDK needed: verified there is ZERO
# cgo in this tree (no `import "C"` anywhere under services/ or scripts/), so plain
# GOOS/GOARCH cross-compilation is complete.
#
# Why not build.sh directly on the Mac: `go` is not installed there and we are not
# installing a toolchain into a user's home for a one-shot build.
# Why build.sh still works: it derives PLATFORM from `uname -s` only for the banner;
# every `go build` honours the GOOS/GOARCH we export.
#
# Usage:  bash 20_build_darwin_arm64.sh
# Output: ./dist/pair-1.0.0-darwin-arm64/  (+ .tar.gz + SHA256SUMS)
set -euo pipefail

REPO="${REPO:-/workspace/nvidia-pair-teqonix}"
OUT="$(cd "$(dirname "$0")" && pwd)/dist"
PKG="pair-1.0.0-darwin-arm64"
GO_IMAGE="${GO_IMAGE:-golang:1.25}"
GOMOD_VOL="${GOMOD_VOL:-pair_gomod_125}"   # node-local; created on first run
GOOS_TARGET=darwin
GOARCH_TARGET=arm64

cd "$REPO"

echo "==> Repo:      $REPO"
echo "==> Target:    ${GOOS_TARGET}/${GOARCH_TARGET}"
echo "==> Image:     ${GO_IMAGE}"
echo "==> Mod cache: volume ${GOMOD_VOL}"

command -v podman >/dev/null || { echo "podman not found on this build node"; exit 1; }

# --- sanity: the two things build.sh insists on outside services/ -------------------
[ -f desktop/package.json ]        || { echo "missing desktop/package.json (release version source)"; exit 1; }
[ -d scripts/inference-dispatcher ] || { echo "missing scripts/inference-dispatcher (13th component)"; exit 1; }
echo "==> release version: $(jq -r .version desktop/package.json)   services: $(jq -r .services services/versions.json)"

# --- cgo guard: if this ever regresses, cross-compiling stops being trivial --------
if grep -rl 'import "C"' services/ scripts/ 2>/dev/null | grep -q .; then
  echo "ERROR: cgo present in the tree - cross-compiling darwin/arm64 from Linux now needs a macOS SDK."
  echo "       Build on the Mac instead (brew install go@1.25)."
  grep -rl 'import "C"' services/ scripts/ 2>/dev/null | head
  exit 1
fi
echo "==> cgo check: clean (cross-compile is safe)"

# --- build in-container ----------------------------------------------------------
# jq is not in golang:1.25; install it inside the container rather than on the host.
# HOME is pinned inside the volume so the module cache + build cache persist.
podman volume create "$GOMOD_VOL" >/dev/null 2>&1 || true

podman run --rm \
  -v "$REPO":/src:z \
  -v "${GOMOD_VOL}":/gpc:z \
  -w /src/services \
  -e GOOS="$GOOS_TARGET" \
  -e GOARCH="$GOARCH_TARGET" \
  -e CGO_ENABLED=0 \
  -e GOPATH=/gpc/gopath \
  -e GOCACHE=/gpc/gocache \
  -e HOME=/gpc/home \
  -e GOFLAGS=-buildvcs=false \
  "$GO_IMAGE" \
  bash -euo pipefail -c '
    if ! command -v jq >/dev/null 2>&1; then
      echo "installing jq in-container..."
      apt-get update -qq >/dev/null 2>&1
      apt-get install -y -qq jq >/dev/null 2>&1
    fi
    jq --version
    bash ./build.sh
  '

# --- collect --------------------------------------------------------------------
BIN_DIR="$REPO/services/build/bin"
[ -d "$BIN_DIR" ] || { echo "build produced no $BIN_DIR"; exit 1; }

rm -rf "$OUT/$PKG"
mkdir -p "$OUT/$PKG/cli-bin"
cp -a "$BIN_DIR"/. "$OUT/$PKG/cli-bin/"

# manifest.json — node-agnostic (no node UUIDs), so this artifact is portable across
# any darwin/arm64 host. Mirrors the services/build manifest shape.
python3 - "$BIN_DIR" "$OUT/$PKG/manifest.json" <<'PY'
import hashlib, json, os, sys, datetime
bin_dir, out = sys.argv[1], sys.argv[2]
files = {}
for name in sorted(os.listdir(bin_dir)):
    p = os.path.join(bin_dir, name)
    if not os.path.isfile(p):
        continue
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    st = os.stat(p)
    files[name] = {"sha256": h.hexdigest(), "size": st.st_size}
man = {
    "source": "services-build-cross",
    "product": "1.0.0",
    "platform": "darwin",
    "arch": "arm64",
    "components": json.load(open(os.path.join(os.path.dirname(bin_dir), "..", "versions.json")))["components"],
    "files": files,
    "builtAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
json.dump(man, open(out, 'w'), indent=2)
print(f"manifest: {len(files)} files")
PY

( cd "$OUT/$PKG/cli-bin" && shasum -a 256 > "$OUT/$PKG/SHA256SUMS" )
tar -czf "$OUT/$PKG.tar.gz" -C "$OUT" "$PKG"
shasum -a 256 "$OUT/$PKG.tar.gz" | tee "$OUT/$PKG.tar.gz.sha256"

echo
echo "==> DONE"
echo "    artifact dir : $OUT/$PKG"
echo "    tarball      : $OUT/$PKG.tar.gz"
echo
echo "    binaries:"
ls -1 "$OUT/$PKG/cli-bin" | sed 's/^/      /'
echo
echo "    NOTE: ollama-proxy / lmstudio-proxy are GONE in 1.0.0 - a single nvpair-proxy"
echo "          hosts both facades (:11434 ollama, :1234 lmstudio, :8080 llamacpp)."

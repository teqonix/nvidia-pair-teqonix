#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
#
# build_gufo.sh — build the GUFO image off the host toolchain.
#
# WHY A CONTAINER AND NOT THE HOST
# GUFO is qualified against GCC 15.3 + ROCm 7.2.3. amd-ai-core-two has
# GCC 14.2 + TheRock 7.13.0 and is MISSING all of the required -dev packages
# (hipblas, hipblaslt, rocblas, hipcub, rocprim, rocwmma, icu, png, jpeg,
# webp). Installing a competing ROCm/hipBLAS stack on a host whose GPU is
# actively serving inference is a poor idea, and a GCC 14.2 build is an
# unvalidated gamble on the 8060S.
#
# WHY THE NIX ROUTE IS THE DEFAULT (route=nix)
# The ROCr queue-memory fix (.devops/nix/patches/rocr-fixed-cwsr.patch) does
# NOT patch GUFO — it patches libhsakmt/src/queues.c and
# runtime/hsa-runtime/core/runtime/*.cpp, i.e. AMD's ROCr runtime itself.
# Verified: `git apply --check` against the GUFO tree fails outright.
#
#   Consequence: a non-Nix build CANNOT include this protection without
#   rebuilding ROCm from source. The Nix package applies it automatically:
#
#     rocmRuntime = rocmPackages.rocm-runtime.overrideAttrs (old: {
#       patches = (old.patches or []) ++ [ ./patches/rocr-fixed-cwsr.patch ]; });
#
#   The patch forces non-paged GTT for queue save/restore areas on gfx1151
#   (EngineId 11.5.1) so host memory compaction cannot evict GPU queues.
#   core-two runs 125 GiB unified memory already at ~43% VRAM / ~38 GiB used
#   while Halogen serves — precisely the compaction-prone regime the patch
#   exists for. Running Flash-Next without it risks queue-eviction stalls.
#
#   => Use route=nix unless you have independently rebuilt patched ROCr 7.2.3.
#
# Both routes compile only; neither needs /dev/kfd, so a running Halogen is
# unaffected during the build.
#
# Exit: 0 built+verified | 1 failed | 2 usage

set -uo pipefail

HOST="${HOST:-amd-ai-core-two.lan}"
GUFO_SRC="${GUFO_SRC:-/workspace/gufo-teqonix}"
IMAGE="${GUFO_IMAGE:-localhost/gufo:latest}"
ROUTE="nix"                                   # nix | plain
BUILD_BASE="${BUILD_BASE:-}"                  # plain-route base with qualified ROCm
NIX_BASE="${NIX_BASE:-nixos/nix:latest}"
JOBS="${JOBS:-4}"
RUN_BUILD=0
VERIFY_ONLY=0
KEEP_BUILD_DIR=1

say()  { printf '%s\n' "$*"; }
log()  { printf '[build] %s\n' "$*"; }
err()  { printf '[build] ERROR: %s\n' "$*" >&2; }
warn(){ printf '[build] WARN: %s\n' "$*" >&2; }
hdr()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
rssh() { ssh -o ConnectTimeout=20 -o BatchMode=yes "$HOST" "$1" 2>/dev/null; }

usage() {
  cat <<USAGE
usage: build_gufo.sh [--host HOST] [--route nix|plain] [options]

  --host HOST        build target (default: $HOST)
  --src PATH         GUFO source on the shared workspace (default: $GUFO_SRC)
  --route nix        RECOMMENDED. Build via GUFO's flake inside nixos/nix.
                    Applies the ROCr fixed-cwsr patch; uses the qualified
                    GCC 15.3 + ROCm 7.2.3 closure. (default)
  --route plain      CMake build against a base image's ROCm.
                    NO queue-eviction protection unless the base already ships
                    a patched ROCr 7.2.3. Requires --base IMAGE.
  --base IMAGE       base image for --route plain (qualified ROCm/HIP toolchain)
  --nix-base IMAGE   base for --route nix (default: $NIX_BASE)
  --image NAME       output image tag (default: $IMAGE)
  --jobs N         parallel compile jobs (default: $JOBS)
  --run            execute the build now instead of printing the command
  --verify-only    skip the build; just verify --image is present and working

Typical detached run (recommended — this is a long compile):
  ssh $HOST 'cd ~/gufo-build-src && nohup \\
    bash /usr/local/src/gufo-pair/ops/build_gufo.sh --run > ~/gufo-build.log 2>&1 &'
  ssh $HOST 'tail -f ~/gufo-build.log'
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --src) GUFO_SRC="$2"; shift 2 ;;
    --route) ROUTE="$2"; shift 2 ;;
    --base) BUILD_BASE="$2"; shift 2 ;;
    --nix-base) NIX_BASE="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --jobs) JOBS="$2"; shift 2 ;;
    --run) RUN_BUILD=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown arg: $1"; usage; exit 2 ;;
  esac
done

case "$ROUTE" in nix|plain) ;; *) err "--route must be nix or plain"; exit 2 ;; esac
[[ "$JOBS" =~ ^[0-9]+$ ]] || { err "--jobs must be an integer"; exit 2; }
if [[ "$ROUTE" == "plain" && -z "$BUILD_BASE" ]]; then
  err "--route plain requires --base IMAGE with a qualified ROCm toolchain"
  err "  (or use --route nix, which needs no base beyond $NIX_BASE)"
  exit 2
fi

hdr "0. Source check"
[[ -d "$GUFO_SRC" ]] || { err "GUFO source not found: $GUFO_SRC"; exit 1; }
[[ -f "$GUFO_SRC/CMakeLists.txt" ]] || { err "no CMakeLists.txt in $GUFO_SRC"; exit 1; }
grep -q "ENGINE_ENABLE_HIP" "$GUFO_SRC/CMakeLists.txt" \
  || { err "ENGINE_ENABLE_HIP absent — source may not build the gfx1151 backend"; exit 1; }
grep -n "option(ENGINE_ENABLE_HIP" "$GUFO_SRC/CMakeLists.txt" | sed 's/^/    /'

PATCH_FILE="$GUFO_SRC/.devops/nix/patches/rocr-fixed-cwsr.patch"
if [[ "$ROUTE" == "nix" ]]; then
  [[ -f "$PATCH_FILE" ]] && log "ROCr fixed-cwsr patch present (applied by the flake)" \
                        || warn "patch file missing — nix build may fail or lack the fix"
  [[ -f "$GUFO_SRC/flake.lock" ]] || warn "flake.lock missing — dependency pinning unavailable"
else
  say ""
  say "  \033[33m*** NO QUEUE-EVICTION PROTECTION ***\033[0m"
  say "  route=plain cannot apply rocr-fixed-cwsr.patch (it targets ROCr, not GUFO)."
  say "  On this host's 125 GiB unified memory, compaction may evict GPU queues."
  say "  Proceed only if $BUILD_BASE ships a patched ROCr 7.2.3."
  say ""
fi

# ------------------------------------------------------- 1. Containerfile
hdr "1. Generating build Containerfile (route=$ROUTE)"
BUILD_CF="/tmp/gufo-build.Containerfile.$$"

if [[ "$ROUTE" == "nix" ]]; then
  # Both stages are the same nix base so /nix/store absolute paths survive the
  # COPY. GUFO's package is a makeWrapper'd script referencing store paths, so
  # the store MUST be present at the same locations in the runtime image.
  cat >| "$BUILD_CF" <<EOF
# Generated by build_gufo.sh (route=nix) — $(date -u +%Y-%m-%dT%H:%M:%SZ)
FROM ${NIX_BASE} AS build
RUN mkdir -p /src
COPY . /src
WORKDIR /src
RUN nix --extra-experimental-features 'nix-command flakes' \\
      build .#default -L \\
      --out-link /gufo-out \\
      --max-jobs ${JOBS}

FROM ${NIX_BASE}
# Preserve the store so wrapper scripts resolve their dependencies.
COPY --from=build /nix /nix
COPY --from=build /gufo-out /opt/gufo
# ffmpeg/ffprobe are invoked as separate executables at runtime.
RUN ln -sf \$(find /nix/store -maxdepth 2 -name ffmpeg -type f | head -1) /usr/local/bin/ffmpeg 2>/dev/null || true
RUN ln -sf \$(find /nix/store -maxdepth 2 -name ffprobe -type f | head -1) /usr/local/bin/ffprobe 2>/dev/null || true
ENV GUFO_HOME=/opt/gufo
ENTRYPOINT ["/opt/gufo/bin/gufo"]
EOF
else
  cat >| "$BUILD_CF" <<EOF
# Generated by build_gufo.sh (route=plain) — $(date -u +%Y-%m-%dT%H:%M:%SZ)
FROM ${BUILD_BASE}

RUN apt-get update && apt-get install -y --no-install-recommends \\
      git cmake ninja-build pkg-config \\
      libicu-dev libcurl4-openssl-dev libssl-dev \\
      libpng-dev libjpeg-dev libwebp-dev ffmpeg \\
    && rm -rf /var/lib/apt/lists/*

# ROCm math/kernel headers. Present in rocm/* bases; installed for others.
RUN apt-get update && apt-get install -y --no-install-recommends \\
      hipblas-dev hipblaslt-dev rocblas-dev \\
      hipcub-dev rocprim-dev rocwmma-dev \\
    || echo "NOTE: some ROCm -dev packages unavailable in this base; verify find_package(hipblas)"

WORKDIR /src
COPY . /src

# Qualified toolchain per GUFO README; base may already provide it.
RUN cmake --preset release \\
      -DCMAKE_BUILD_TYPE=Release \\
      -DENGINE_ENABLE_HIP=ON \\
      -DCMAKE_PREFIX_PATH="/opt/rocm" \\
      -DCMAKE_HIP_COMPILER="/opt/rocm/llvm/bin/clang++" \\
    && cmake --build --preset release --parallel ${JOBS}

RUN cmake --install build/release --prefix /opt/gufo

FROM ${BUILD_BASE}
COPY --from=0 /opt/gufo /opt/gufo
RUN ln -sf /opt/gufo/bin/gufo /usr/local/bin/gufo
ENTRYPOINT ["/usr/local/bin/gufo"]
EOF
fi
log "Containerfile -> $BUILD_CF"

# ------------------------------------------------------- 2. ship source
RREMOTE="/home/turnstone/gufo-build-src"
if [[ $VERIFY_ONLY -eq 1 ]]; then
  hdr "2. Skipped (verify-only)"
else
  hdr "2. Shipping source to $HOST"
  rssh "rm -rf $RREMOTE && mkdir -p $RREMOTE"
  # Workspace is shared SMB; still rsync to exclude build junk and .jj/.git objects.
  rsync -a --delete \
    --exclude '.git' --exclude 'build' --exclude '.jj' --exclude '*.egg-info' \
    -e "ssh -o BatchMode=yes -o ConnectTimeout=15" \
    "$GUFO_SRC/" "$HOST:$RREMOTE/" \
    || { err "rsync of GUFO source failed"; exit 1; }
  rssh "cp $BUILD_CF $RREMOTE/Containerfile.build" || { err "could not copy Containerfile"; exit 1; }
  log "source staged at $HOST:$RREMOTE"
fi

# ------------------------------------------------------- 3. build
hdr "3. Build command for $IMAGE"
BUILD_CMD="cd $RREMOTE && podman build -f Containerfile.build -t $IMAGE ."
say "This is a long compile (nix route also downloads the ROCm 7.2.3 closure)."
say ""
say "  ssh $HOST '$BUILD_CMD > ~/gufo-build.log 2>&1 &' "
say "  ssh $HOST 'tail -f ~/gufo-build.log'"
say ""

if [[ $VERIFY_ONLY -eq 1 ]]; then
  log "verify-only: skipping build"
elif [[ $RUN_BUILD -eq 1 ]]; then
  log "starting build (route=$ROUTE) ..."
  if rssh "$BUILD_CMD"; then
    log "build finished"
  else
    err "podman build failed — see output above"
    exit 1
  fi
else
  say "Not started. Re-run with --run, or use the detached command above."
  say "Then verify with: $0 --host $HOST --verify-only"
  exit 0
fi

# ------------------------------------------------------- 4. verify
hdr "4. Verifying the built artifact"
DIAG=$(rssh "podman run --rm $IMAGE diagnose 2>&1 | head -60")
if printf '%s' "$DIAG" | grep -qiE "gfx1151|hip|rocm"; then
  log "diagnose reports HIP/gfx1151 — backend compiled in"
  printf '%s\n' "$DIAG" | head -20 | sed 's/^/    /'
else
  err "gufo diagnose shows no HIP/gfx1151 evidence — backend likely NOT compiled"
  printf '%s\n' "$DIAG" | head -25 | sed 's/^/    /'
  exit 1
fi

if [[ "$ROUTE" == "nix" ]]; then
  # Prove the runtime image actually carries the patched ROCr, not a stock one.
  ROCR=$(rssh "podman run --rm $IMAGE /bin/sh -c 'ls /nix/store | grep -i rocm-runtime | head -3' 2>/dev/null")
  if [[ -n "$ROCR" ]]; then
    log "patched ROCr runtime present in store:"
    printf '%s\n' "$ROCR" | sed 's/^/    /'
    log "NOTE: store name does not encode the patch; confirm via build log that"
    log "      rocm-runtime was rebuilt with rocr-fixed-cwsr.patch applied."
  else
    warn "could not locate rocm-runtime in the store — verify manually"
  fi
fi

log "image verified: $IMAGE"
say "Next: ops/deploy_stack.sh --host $HOST --stage-only"
exit 0

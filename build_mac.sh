#!/bin/bash
# build_mac.sh - Build PAIR components for macOS (Apple Silicon)
# Run this from the root of the PAIR repository.

set -e

# --- Configuration ---
BUILD_DIR="build/macos"
BIN_DIR="$BUILD_DIR/cli-bin"
LOG_DIR="$BUILD_DIR/logs"
# Use arm64 for M1/M2/M3 Mac, x86_64 for Intel
GOARCH="arm64" 
GOOS="darwin"

# List of services to build (must match directory names in services/)
SERVICES=(
    "nvpair-ui-broker"
    "nvpair-node-scanner"
    "nvpair-node-info"
    "nvpair-proxy"
    "nvpair-engine-manager"
    "nvpair-workload-manager"
    "nvpair-cluster-manager"
    "nvpair-errors"
    "nvpair-node-settings"
    "nvpair-manual-nodes"
    "nvpair-job-scheduler"
    "nvpair-tui"
)

echo "🚀 Starting PAIR build for macOS ($GOARCH)..."

# Prepare directories
rm -rf "$BUILD_DIR"
mkdir -p "$BIN_DIR"
mkdir -p "$LOG_DIR"

# Build each service
for svc in "${SERVICES[@]}"; do
    echo "📦 Building $svc..."
    # We build from the root of the services folder to handle internal module deps
    GOOS=$GOOS GOARCH=$GOARCH go build -trimpath -ldflags="-s -w" \
        -o "$BIN_DIR/$svc" "./services/$svc"
done

echo "✅ All binaries compiled to $BIN_DIR"

# --- Post-Build Instructions ---
echo "------------------------------------------------------------------"
echo "NEXT STEPS FOR INSTALLATION:"
echo "1. Copy the '$BUILD_DIR' folder to your desired installation path"
echo "   (e.g., /Users/turnstone/pair/1.0.0/)"
echo "2. Ensure your broker-shim.sh points to the new BIN_DIR path."
echo "3. Run 'codesign' on the binaries if you have a developer account,"
echo "   otherwise, you may need to 'Allow' them in System Settings -> Security."
echo "4. Place the binaries in a folder, then run:"
echo "   sudo xattr -rd com.apple.quarantine $BIN_DIR"
echo "   (This prevents macOS from blocking the binaries as 'untrusted' downloaded files)"
echo "------------------------------------------------------------------"
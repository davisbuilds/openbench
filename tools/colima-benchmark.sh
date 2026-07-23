#!/usr/bin/env bash
# ============================================================================
# colima-benchmark.sh — Pinned Colima VM setup for OpenBench
# ============================================================================
#
# Usage:
#   bash tools/colima-benchmark.sh           # full setup (idempotent)
#   bash tools/colima-benchmark.sh --restart  # full stop + recreate + setup
#   bash tools/colima-benchmark.sh --status   # print VM resources + exit
#
# What this does (idempotently):
#   1. Stop any running colima (unless --no-stop)
#   2. Start (or recreate) colima with pinned CPU/memory/disk:
#        colima start --cpu 4 --memory 12 --disk 100
#   3. Enable brew services autostart for colima so it survives reboot
#   4. Install docker buildx plugin (via docker buildx install or brew)
#   5. Smoke-test: run docker info + a tiny hello-world container
#
# Requirements: colima, docker CLI, brew (for services autostart)
# ============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Pinned resource reservation — must match .openbench/env-requirements.toml.
COLIMA_CPUS=4
COLIMA_MEMORY=12   # GiB
COLIMA_DISK=100    # GiB

# ---- helpers ---------------------------------------------------------------
info()  { printf "\033[36m[colima-benchmark]\033[0m %s\n" "$*"; }
ok()    { printf "\033[32m  ✓\033[0m %s\n" "$*"; }
warn()  { printf "\033[33m  ⚠\033[0m %s\n" "$*" >&2; }
fail()  { printf "\033[31m  ✗\033[0m %s\n" "$*" >&2; exit 1; }

# ---- status probe ----------------------------------------------------------
show_status() {
  if ! command -v colima &>/dev/null; then
    fail "colima not installed — run: brew install colima"
  fi
  if ! colima status 2>/dev/null | grep -q "Running"; then
    info "colima is NOT running"
    return
  fi
  info "colima VM status:"
  colima status 2>/dev/null || true
  echo ""
  info "docker info resource summary:"
  docker info --format '{{.ServerVersion}}' 2>/dev/null && \
    docker info --format 'CPUs: {{.NCPU}}  Memory: {{.MemTotal}}' 2>/dev/null || \
    warn "docker daemon not reachable inside colima"
}

# ---- brew services autostart -----------------------------------------------
enable_brew_autostart() {
  if ! command -v brew &>/dev/null; then
    warn "brew not installed — skipping brew services autostart"
    return
  fi
  # colima registers itself as a brew service on install; ensure it's started.
  if brew services list 2>/dev/null | grep -qE "^colima\s+started"; then
    ok "colima brew service already started"
    return
  fi
  info "starting colima brew service (autostart on reboot)..."
  brew services start colima 2>&1 || warn "brew services start colima failed (may need sudo)"
  if brew services list 2>/dev/null | grep -qE "^colima\s+started"; then
    ok "colima brew service started"
  else
    warn "could not verify colima brew service — check manually: brew services list"
  fi
}

# ---- buildx plugin -----------------------------------------------------------
install_buildx() {
  if docker buildx version &>/dev/null; then
    ok "docker buildx already installed: $(docker buildx version 2>/dev/null | head -1)"
    return
  fi
  info "installing docker buildx plugin..."
  if command -v brew &>/dev/null; then
    # On macOS, the docker-buildx plugin is bundled with Docker Desktop/CLI,
    # but on colima (which uses its own containerd runtime) we need it explicitly.
    # colima installs buildx automatically in recent versions; try a direct
    # install only if it's missing.
    if docker buildx install 2>/dev/null; then
      ok "docker buildx installed via 'docker buildx install'"
      return
    fi
    # Fallback: download the buildx binary manually.
    BUILDX_VERSION=$(curl -sL "https://api.github.com/repos/docker/buildx/releases/latest" 2>/dev/null \
      | grep '"tag_name"' | head -1 | sed 's/.*"v\(.*\)",/\1/')
    BUILDX_VERSION="${BUILDX_VERSION:-0.20.0}"
    ARCH="$(uname -m)"
    BUILDX_ARCH="linux_${ARCH/arm64/arm64}"
    BUILDX_URL="https://github.com/docker/buildx/releases/download/v${BUILDX_VERSION}/buildx-v${BUILDX_VERSION}.${BUILDX_ARCH}"
    info "downloading buildx ${BUILDX_VERSION} for ${BUILDX_ARCH}..."
    mkdir -p "$HOME/.docker/cli-plugins"
    curl -fsSL "$BUILDX_URL" -o "$HOME/.docker/cli-plugins/docker-buildx" || {
      warn "download failed; trying via docker buildx install fallback"
      docker buildx install 2>/dev/null || true
    }
    chmod +x "$HOME/.docker/cli-plugins/docker-buildx" 2>/dev/null || true
  else
    warn "brew not found — install buildx manually: docker buildx install"
  fi
  if docker buildx version &>/dev/null; then
    ok "docker buildx ready"
  else
    warn "docker buildx not available — some features (image pinning) may fail"
  fi
}

# ---- start / restart colima --------------------------------------------------
start_colima() {
  local action="$1"  # "start" or "restart"

  # Check if colima is already running with the right config.
  if [ "$action" = "start" ] && colima status 2>/dev/null | grep -q "Running"; then
    info "colima is already running — checking resource config..."
    local current_cpus current_memory
    current_cpus=$(colima status 2>/dev/null | grep -i "cpus" | grep -oP '\d+')
    current_memory=$(colima status 2>/dev/null | grep -i "memory" | grep -oP '\d+')
    if [ "${current_cpus:-0}" -ge "$COLIMA_CPUS" ] && [ "${current_memory:-0}" -ge "$COLIMA_MEMORY" ]; then
      ok "colima already meets CPU=${current_cpus} Memory=${current_memory}GiB (req: ${COLIMA_CPUS} CPUs, ${COLIMA_MEMORY} GiB)"
      return 0
    fi
    warn "colima running with CPU=${current_cpus:-?} Memory=${current_memory:-?}GiB — recreating..."
    action="restart"
  fi

  if [ "$action" = "restart" ]; then
    info "stopping colima..."
    colima stop 2>/dev/null || true
    # Wait for full shutdown.
    while colima status 2>/dev/null | grep -q "Running"; do
      sleep 1
    done
    ok "colima stopped"
  fi

  info "starting colima with ${COLIMA_CPUS} CPUs, ${COLIMA_MEMORY} GiB RAM, ${COLIMA_DISK} GiB disk..."
  colima start \
    --cpu "$COLIMA_CPUS" \
    --memory "$COLIMA_MEMORY" \
    --disk "$COLIMA_DISK"

  ok "colima started"
}

# ---- smoke test -----------------------------------------------------------
smoke_test() {
  info "running docker smoke test..."
  local server_version
  server_version="$(docker info --format '{{.ServerVersion}}' 2>/dev/null)" || {
    fail "docker daemon not reachable after colima start"
  }
  ok "docker daemon v${server_version} reachable"

  if docker run --rm hello-world 2>/dev/null | grep -q "Hello from Docker"; then
    ok "hello-world container ran successfully"
  else
    warn "hello-world smoke test did not produce expected output — daemon may still be starting"
  fi
}

# ---- doctor gate ------------------------------------------------------------
run_doctor_gate() {
  info "running obench doctor --docker-env preflight..."
  if command -v obench &>/dev/null; then
    cd "$REPO_ROOT"
    if python3 -m obench.doctor --docker-env 2>&1; then
      ok "obench doctor --docker-env PASSED"
    else
      warn "obench doctor --docker-env FAILED — check output above"
    fi
  else
    warn "obench not on PATH — skipping doctor gate (run from repo: python3 -m obench.doctor --docker-env)"
  fi
}

# ============================================================================
# Main
# ============================================================================
main() {
  local action="start"

  case "${1:-}" in
    --restart|--recreate)
      action="restart"
      shift
      ;;
    --status)
      show_status
      exit 0
      ;;
    --help|-h)
      head -30 "$0"
      exit 0
      ;;
    *)
      ;;
  esac

  info "=== OpenBench colima benchmark environment setup ==="
  info "Pinned config: CPU=${COLIMA_CPUS}  Memory=${COLIMA_MEMORY}GiB  Disk=${COLIMA_DISK}GiB"
  echo ""

  start_colima "$action"
  echo ""

  enable_brew_autostart
  echo ""

  install_buildx
  echo ""

  smoke_test
  echo ""

  # Print final resource confirmation.
  show_status
  echo ""

  run_doctor_gate

  echo ""
  ok "colima benchmark environment ready"
  info "Next: obench run --task ... --harness ... --model ..."
}

main "$@"

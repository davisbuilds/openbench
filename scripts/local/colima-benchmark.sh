#!/usr/bin/env bash
# ============================================================================
# colima-benchmark.sh — Pinned Colima VM setup for OpenBench
# ============================================================================
#
# Usage:
#   bash scripts/local/colima-benchmark.sh                     # setup (idempotent)
#   bash scripts/local/colima-benchmark.sh --restart           # stop + start + setup
#   bash scripts/local/colima-benchmark.sh --status            # print VM resources
#   bash scripts/local/colima-benchmark.sh --profile openbench # dedicated VM
#   bash scripts/local/colima-benchmark.sh --autostart         # + brew autostart
#
# What this does (idempotently):
#   1. Leave a running VM alone when it meets the pinned floor; otherwise stop
#      it and start it with pinned CPU/memory/disk:
#        colima start --profile <profile> --cpu 4 --memory 12 --disk 100
#   2. With --autostart, enable the brew colima service so the VM survives
#      reboot (default profile only: the service starts no other profile)
#   3. Install the docker buildx plugin (via brew) if missing
#   4. Smoke-test: run docker info + a tiny hello-world container
#   5. Run `obench doctor --docker-env` against the same VM
#
# Profiles: --profile NAME (or OBENCH_COLIMA_PROFILE) runs benchmarks in their
# own VM, leaving the default VM, its size and the current Docker context to
# other workloads on the host. Either way, runs select the checked VM with
# DOCKER_CONTEXT (colima, or colima-NAME), which the script prints at the end.
#
# Requirements: colima, docker CLI, python3; brew for buildx and autostart
# ============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Pinned resource reservation — must match .openbench/env-requirements.toml.
COLIMA_CPUS=4
COLIMA_MEMORY=12   # GiB
COLIMA_DISK=100    # GiB

PROFILE="${OBENCH_COLIMA_PROFILE:-default}"
AUTOSTART=0

# ---- helpers ---------------------------------------------------------------
info()  { printf "\033[36m[colima-benchmark]\033[0m %s\n" "$*"; }
ok()    { printf "\033[32m  ✓\033[0m %s\n" "$*"; }
warn()  { printf "\033[33m  ⚠\033[0m %s\n" "$*" >&2; }
fail()  { printf "\033[31m  ✗\033[0m %s\n" "$*" >&2; exit 1; }

# ---- profile plumbing --------------------------------------------------------
# Colima names the Docker context of its default profile "colima" and of any
# other profile "colima-<profile>". Every docker call here targets that context,
# whatever the current one is.
docker_context() {
  if [ "$PROFILE" = default ]; then echo colima; else echo "colima-$PROFILE"; fi
}
dk() { DOCKER_CONTEXT="$(docker_context)" docker "$@"; }
colima_p() { local verb="$1"; shift; colima "$verb" --profile "$PROFILE" "$@"; }

# Prints "<cpus> <memory GiB> <disk GiB>" when the profile's VM is running,
# else nothing.
# `colima status` reports neither figure, so read `colima list --json`. List
# every profile and filter here: `colima list --profile X` fails until X exists.
running_resources() {
  { colima list --json 2>/dev/null || true; } | python3 -c '
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    vm = json.loads(line)
    if vm.get("name") == sys.argv[1] and vm.get("status") == "Running":
        print(int(vm.get("cpus", 0)), int(vm.get("memory", 0)) // 2**30,
              int(vm.get("disk", 0)) // 2**30)
' "$PROFILE"
}

# ---- status probe ----------------------------------------------------------
show_status() {
  if ! command -v colima &>/dev/null; then
    fail "colima not installed — run: brew install colima"
  fi
  local resources
  resources="$(running_resources)"
  if [ -z "$resources" ]; then
    info "colima profile '$PROFILE' is NOT running"
    return
  fi
  local cpus memory disk
  read -r cpus memory disk <<<"$resources"
  info "colima profile '$PROFILE' is running: CPUs=${cpus} Memory=${memory}GiB Disk=${disk}GiB"
  echo ""
  info "docker info resource summary (context $(docker_context)):"
  dk info --format '{{.ServerVersion}}' 2>/dev/null && \
    dk info --format 'CPUs: {{.NCPU}}  Memory: {{.MemTotal}}' 2>/dev/null || \
    warn "docker daemon not reachable inside colima"
}

# ---- brew services autostart -----------------------------------------------
# Opt-in: the brew service starts the default profile at every login, which
# reserves the pinned memory whether or not a benchmark is running.
enable_brew_autostart() {
  if ! command -v brew &>/dev/null; then
    warn "brew not installed — skipping brew services autostart"
    return
  fi
  if brew services list 2>/dev/null | grep -qE "^colima[[:space:]]+started"; then
    ok "colima brew service already started"
    return
  fi
  info "starting colima brew service (autostart on reboot)..."
  brew services start colima 2>&1 || warn "brew services start colima failed (may need sudo)"
  if brew services list 2>/dev/null | grep -qE "^colima[[:space:]]+started"; then
    ok "colima brew service started"
  else
    warn "could not verify colima brew service — check manually: brew services list"
  fi
}

# ---- buildx plugin -----------------------------------------------------------
install_buildx() {
  if dk buildx version &>/dev/null; then
    ok "docker buildx already installed: $(dk buildx version 2>/dev/null | head -1)"
    return
  fi
  if ! command -v brew &>/dev/null; then
    warn "docker buildx missing and brew not found — install the docker-buildx plugin manually"
    return
  fi
  info "installing docker buildx plugin (brew install docker-buildx)..."
  brew install docker-buildx 2>&1 || warn "brew install docker-buildx failed"
  if dk buildx version &>/dev/null; then
    ok "docker buildx ready"
  else
    warn "docker buildx still not found — add \"cliPluginsExtraDirs\": [\"$(brew --prefix)/lib/docker/cli-plugins\"] to ~/.docker/config.json"
  fi
}

# ---- start / restart colima --------------------------------------------------
start_colima() {
  local action="$1"  # "start" or "restart"
  local resources cpus memory disk

  resources="$(running_resources)"
  if [ "$action" = "start" ] && [ -n "$resources" ]; then
    read -r cpus memory disk <<<"$resources"
    if [ "$cpus" -ge "$COLIMA_CPUS" ] && [ "$memory" -ge "$COLIMA_MEMORY" ] &&
       [ "$disk" -ge "$COLIMA_DISK" ]; then
      ok "colima profile '$PROFILE' already meets CPU=${cpus} Memory=${memory}GiB Disk=${disk}GiB (req: ${COLIMA_CPUS} CPUs, ${COLIMA_MEMORY} GiB, ${COLIMA_DISK} GiB)"
      return 0
    fi
    warn "colima profile '$PROFILE' running with CPU=${cpus} Memory=${memory}GiB Disk=${disk}GiB — restarting with the pinned config..."
    action="restart"
  fi

  if [ "$action" = "restart" ] && [ -n "$resources" ]; then
    info "stopping colima profile '$PROFILE'..."
    colima_p stop 2>/dev/null || true
    local waited=0
    while [ -n "$(running_resources)" ]; do
      [ "$waited" -lt 120 ] || fail "colima profile '$PROFILE' still running after 120s"
      sleep 1
      waited=$((waited + 1))
    done
    ok "colima stopped"
  fi

  # A second profile must not take over the current Docker context: other
  # workloads on the host keep using theirs, and runs select this VM explicitly.
  local activate=()
  [ "$PROFILE" = default ] || activate=(--activate=false)

  info "starting colima profile '$PROFILE' with ${COLIMA_CPUS} CPUs, ${COLIMA_MEMORY} GiB RAM, ${COLIMA_DISK} GiB disk..."
  colima_p start \
    --cpu "$COLIMA_CPUS" \
    --memory "$COLIMA_MEMORY" \
    --disk "$COLIMA_DISK" \
    ${activate[@]+"${activate[@]}"}

  ok "colima started"
}

# ---- smoke test -----------------------------------------------------------
smoke_test() {
  info "running docker smoke test..."
  local server_version
  server_version="$(dk info --format '{{.ServerVersion}}' 2>/dev/null)" || {
    fail "docker daemon not reachable after colima start"
  }
  ok "docker daemon v${server_version} reachable"

  if dk run --rm hello-world 2>/dev/null | grep -q "Hello from Docker"; then
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
    if DOCKER_CONTEXT="$(docker_context)" python3 -m obench.doctor --docker-env 2>&1; then
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

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --restart|--recreate) action="restart" ;;
      --status) action="status" ;;
      --autostart) AUTOSTART=1 ;;
      --profile)
        [ "$#" -ge 2 ] || fail "--profile needs a name"
        PROFILE="$2"
        shift ;;
      --profile=*) PROFILE="${1#--profile=}" ;;
      --help|-h)
        head -32 "$0"
        exit 0 ;;
      *) fail "unknown argument: $1 (see --help)" ;;
    esac
    shift
  done
  # Resolve aliases as colima does: "colima" names the default profile, and a
  # leading "colima-" is dropped.
  case "$PROFILE" in
    colima) PROFILE=default ;;
    colima-*) PROFILE="${PROFILE#colima-}" ;;
  esac
  [[ "$PROFILE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail "invalid profile name: $PROFILE"
  if [ "$AUTOSTART" = 1 ] && [ "$PROFILE" != default ]; then
    fail "--autostart covers only the default profile; brew's colima service starts no other"
  fi

  if [ "$action" = "status" ]; then
    show_status
    exit 0
  fi

  info "=== OpenBench colima benchmark environment setup ==="
  info "Profile: ${PROFILE}  Pinned config: CPU=${COLIMA_CPUS}  Memory=${COLIMA_MEMORY}GiB  Disk=${COLIMA_DISK}GiB"
  echo ""

  start_colima "$action"
  echo ""

  if [ "$AUTOSTART" = 1 ]; then
    enable_brew_autostart
    echo ""
  fi

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
  # The checks above pinned this VM's context; a run that inherits another
  # active context would use a daemon they never saw.
  info "Runs must target this VM. Set this where the run starts (in a tmux launch"
  info "script, not only in the shell that opens tmux):"
  echo "  export DOCKER_CONTEXT=$(docker_context)"
  info "Next: obench run --task ... --harness ... --model ..."
}

main "$@"

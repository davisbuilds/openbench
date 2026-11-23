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
#   bash scripts/local/colima-benchmark.sh --provision-only  # VM checks; qualify later
#   bash scripts/local/colima-benchmark.sh --profile openbench --env-requirements .openbench/env-requirements-serial.toml
#   bash scripts/local/colima-benchmark.sh --autostart         # + brew autostart
#
# What this does (idempotently):
#   1. Leave a running VM alone when it meets the selected floor; otherwise stop
#      it and start it with selected CPU/memory and pinned disk (defaults):
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

# WHY: The reserved VM memory must exceed the usable daemon floor to leave room for the guest
# kernel.
ENV_REQUIREMENTS="$REPO_ROOT/.openbench/env-requirements.toml"
COLIMA_DISK=100

PROFILE="${OBENCH_COLIMA_PROFILE:-default}"
AUTOSTART=0
PROVISION_ONLY=0

info()  { printf "\033[36m[colima-benchmark]\033[0m %s\n" "$*"; }
ok()    { printf "\033[32m  ✓\033[0m %s\n" "$*"; }
warn()  { printf "\033[33m  ⚠\033[0m %s\n" "$*" >&2; }
fail()  { printf "\033[31m  ✗\033[0m %s\n" "$*" >&2; exit 1; }

load_resources() {
  local resources
  # WHY: Resolve the caller-relative configuration path before the gate changes directories.
  ENV_REQUIREMENTS="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve())' "$ENV_REQUIREMENTS")"
  resources="$(python3 -c '
import sys, tomllib
try:
    with open(sys.argv[1], "rb") as stream:
        data = tomllib.load(stream)
    cpus, floor = data.get("cpus", 4), data.get("memory_gib", 12)
    memory = data.get("colima_memory_gib", floor)
    if any(type(v) is not int or v <= 0 for v in (cpus, floor, memory)):
        raise ValueError("CPU and memory values must be positive integers")
    if memory < floor:
        raise ValueError("colima_memory_gib must be at least memory_gib")
    print(cpus, memory)
except (OSError, ValueError) as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)
' "$ENV_REQUIREMENTS")" || fail "cannot load resource requirements: $ENV_REQUIREMENTS"
  read -r COLIMA_CPUS COLIMA_MEMORY <<<"$resources"
}

# WHY: Address the selected profile’s context explicitly so the gate cannot inspect a
# different daemon.
docker_context() {
  if [ "$PROFILE" = default ]; then echo colima; else echo "colima-$PROFILE"; fi
}
dk() { DOCKER_CONTEXT="$(docker_context)" docker "$@"; }
# WHY: Colima removes its context during lifecycle operations. Preserve the user’s actual
# current selection instead of passing the temporary benchmark override.
colima_p() { local verb="$1"; shift; env -u DOCKER_CONTEXT -u DOCKER_HOST colima "$verb" --profile "$PROFILE" "$@"; }

# WHY: A named profile query fails before that profile exists, so inspect the complete profile
# listing for resources.
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

# WHY: Autostart reserves memory on every login, so it stays an explicit choice.
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

install_buildx() {
  if dk buildx version &>/dev/null; then
    ok "docker buildx already installed: $(dk buildx version 2>/dev/null | head -1)"
    return
  fi
  if ! command -v brew &>/dev/null; then
    fail "docker buildx missing and brew not found — install the docker-buildx plugin manually"
  fi
  info "installing docker buildx plugin (brew install docker-buildx)..."
  brew install docker-buildx 2>&1 || warn "brew install docker-buildx failed"
  if dk buildx version &>/dev/null; then
    ok "docker buildx ready"
  else
    fail "docker buildx still not found — add \"cliPluginsExtraDirs\": [\"$(brew --prefix)/lib/docker/cli-plugins\"] to ~/.docker/config.json"
  fi
}

start_colima() {
  local action="$1"
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

  # WHY: Other workloads may already use the current Docker context. Launch another profile
  # without taking it over.
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
    fail "hello-world smoke test failed — inspect the selected Docker context before retrying"
  fi
}

run_doctor_gate() {
  info "running obench doctor --docker-env preflight..."
  cd "$REPO_ROOT"
  if DOCKER_CONTEXT="$(docker_context)" python3 -m obench.doctor --docker-env --env-requirements "$ENV_REQUIREMENTS"; then
    ok "obench doctor --docker-env PASSED"
  else
    fail "obench doctor --docker-env FAILED — resolve the reported checks; use --provision-only only when campaign qualification will follow"
  fi
}

main() {
  local action="start"

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --restart|--recreate) action="restart" ;;
      --status) action="status" ;;
      --autostart) AUTOSTART=1 ;;
      --provision-only) PROVISION_ONLY=1 ;;
      --profile)
        [ "$#" -ge 2 ] || fail "--profile needs a name"
        PROFILE="$2"
        shift ;;
      --profile=*) PROFILE="${1#--profile=}" ;;
      --env-requirements)
        [ "$#" -ge 2 ] || fail "--env-requirements needs a TOML path"
        ENV_REQUIREMENTS="$2"
        shift ;;
      --env-requirements=*) ENV_REQUIREMENTS="${1#--env-requirements=}" ;;
      --help|-h)
        head -33 "$0"
        exit 0 ;;
      *) fail "unknown argument: $1 (see --help)" ;;
    esac
    shift
  done
  # WHY: Normalize the same aliases as Colima before selecting a Docker context.
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

  load_resources
  info "=== OpenBench colima benchmark environment setup ==="
  info "Profile: ${PROFILE}  Pinned config: CPU=${COLIMA_CPUS}  Memory=${COLIMA_MEMORY}GiB  Disk=${COLIMA_DISK}GiB"
  info "Doctor resource requirements: $ENV_REQUIREMENTS"
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

  show_status
  echo ""

  if [ "$PROVISION_ONLY" = 0 ]; then
    run_doctor_gate
  fi

  echo ""
  if [ "$PROVISION_ONLY" = 1 ]; then
    ok "colima VM provisioned; campaign readiness has not been checked"
  else
    ok "colima benchmark environment checks passed; campaign qualification is still required"
  fi
  # WHY: A later command must explicitly target the Docker context this gate inspected.
  info "Runs must target this VM. Set this where the run starts (in a tmux launch"
  info "script, not only in the shell that opens tmux):"
  echo "  export DOCKER_CONTEXT=$(docker_context)"
  info "Next: pin runtime images and run obench campaign qualify before campaign launch"
}

main "$@"

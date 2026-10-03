#!/usr/bin/env bash
# One Docker deployment entry point; never starts host-side executors.
set -euo pipefail

usage() {
  echo 'Usage: scripts/start_workers.sh --mode vpn|direct --context CONTEXT --project PROJECT COMMAND [ARG ...]'
  echo 'Commands: config, ps, logs, build, up, stop, restart, exec, images'
  echo 'Before up/restart: verify instance identities, execution ownership and migration backups (README).'
}

mode='' context='' project=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode|--context|--project)
      [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || { usage >&2; exit 2; }
      case "$1" in
        --mode) mode=$2 ;;
        --context) context=$2 ;;
        --project) project=$2 ;;
      esac
      shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) break ;;
  esac
done
[[ "$mode" == vpn || "$mode" == direct ]] || { usage >&2; exit 2; }
[[ -n "$context" && -n "$project" && $# -gt 0 ]] || { usage >&2; exit 2; }
case "$1" in
  config|ps|logs|build|up|stop|restart|exec|images) ;;
  *) usage >&2; exit 2 ;;
esac
# An inherited profile could silently re-enable VPN services in direct mode.
if [[ -n "${COMPOSE_PROFILES:-}" ]]; then
  echo 'Unset COMPOSE_PROFILES; this entry point uses only the selected deployment mode.' >&2
  exit 2
fi
# Also override a profile declared in .env, which the shell check cannot see.
export COMPOSE_PROFILES=''
if [[ "$mode" == direct ]]; then
  for arg in "$@"; do
    if [[ "$arg" == gluetun || "$arg" == vpn_sentinel ]]; then
      echo 'Use --mode vpn to operate VPN services explicitly.' >&2
      exit 2
    fi
  done
fi
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
compose=(docker --context "$context" compose --project-name "$project" --project-directory "$root"
  --env-file "$root/.env" -f "$root/docker-compose.yml" -f "$root/docker-compose.prod.yml")
if [[ "$mode" == direct ]]; then
  compose+=(-f "$root/docker-compose.direct.yml")
fi
exec "${compose[@]}" "$@"

#!/usr/bin/env bash
# Shared helpers for install.sh and scripts/*.sh deployment tooling. Source, do not execute.
# shellcheck shell=bash

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="$REPO/.env"

if [ -t 1 ]; then _b=$'\033[1m'; _r=$'\033[0m'; else _b=""; _r=""; fi
log()  { printf '%s==>%s %s\n' "$_b" "$_r" "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }

env_get() { # env_get KEY -> value (quotes stripped), empty if unset
  [ -f "$ENV_FILE" ] || return 0
  local v
  v=$(grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2-) || true
  v="${v%\"}"; v="${v#\"}"
  printf '%s' "$v"
}

env_set() { # env_set KEY VALUE  (creates the file mode 600 if needed)
  local k="$1" v="$2" tmp
  case "$v" in *[[:space:]]*) v="\"$v\"" ;; esac
  ( umask 077; touch "$ENV_FILE" )
  tmp=$(mktemp "$ENV_FILE.XXXXXX")
  K="$k" V="$v" awk 'BEGIN{FS="="; k=ENVIRON["K"]; v=ENVIRON["V"]}
    $1==k {print k "=" v; f=1; next} {print}
    END {if(!f) print k "=" v}' "$ENV_FILE" > "$tmp"
  chmod 600 "$tmp"; mv "$tmp" "$ENV_FILE"
}

dc() { docker compose --project-directory "$REPO" "$@"; }
# One-off command in the brain image against the mounted repo (no server, no caddy).
brain_run() { dc run --rm --no-deps -T brain "$@"; }

# Build the image, or (BRAIN_SKIP_BUILD=1) use one that already exists locally / can be pulled
# from the registry named by BRAIN_IMAGE. Useful offline or with a prebuilt image.
build_image() {
  if [ "${BRAIN_SKIP_BUILD:-0}" = "1" ]; then
    local img; img="$(env_get BRAIN_IMAGE)"; img="${img:-0xbrain/brain:local}"
    docker image inspect "$img" >/dev/null 2>&1 || dc pull brain || die "BRAIN_SKIP_BUILD=1 but image $img is neither local nor pullable"
  else
    dc build brain
  fi
}

wait_brain_healthy() { # wait_brain_healthy [seconds]
  local t="${1:-180}" i=0 cid st
  while [ "$i" -lt "$t" ]; do
    cid=$(dc ps -q brain 2>/dev/null || true)
    if [ -n "$cid" ]; then
      st=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || true)
      [ "$st" = "healthy" ] && return 0
      [ "$st" = "exited" ] && return 1
    fi
    sleep 2; i=$((i + 2))
  done
  return 1
}

public_health() { # public_health -> 0 if https://$BRAIN_DOMAIN/healthz answers through caddy (validated TLS unless internal)
  local dom port k=""
  dom=$(env_get BRAIN_DOMAIN); port=$(env_get BRAIN_HTTPS_PORT); port="${port:-443}"
  [ -n "$(env_get BRAIN_TLS_DIRECTIVE)" ] && k="-k"
  # --resolve pins to localhost so the check works before DNS is verified, while still validating the cert name.
  curl --noproxy "*" -fsS $k --max-time 8 --resolve "$dom:$port:127.0.0.1" "https://$dom:$port/healthz" >/dev/null 2>&1
}

wait_public_health() {
  local t="${1:-180}" i=0
  while [ "$i" -lt "$t" ]; do public_health && return 0; sleep 3; i=$((i + 3)); done
  return 1
}

run_validators() {
  brain_run python scripts/validate_repo.py --full || return 1
  brain_run python scripts/validate_content_release.py || return 2
}

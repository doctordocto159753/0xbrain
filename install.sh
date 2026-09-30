#!/usr/bin/env bash
# 0xBrain VPS installer. Run from the repository root of a fresh clone:
#   sudo ./install.sh
# Asks only for: brain domain, owner email, wiki name, record prefix. Everything else (including
# the owner passphrase for the OAuth consent page) is generated. Safe to re-run: existing .env
# values (identity, secret) and an already-instantiated wiki are preserved.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export REPO
# shellcheck source=deploy/lib.sh
. "$REPO/deploy/lib.sh"

usage() {
  cat <<USAGE
Usage: ./install.sh [options]
  --domain HOST          brain domain (DNS A/AAAA must point to this server)
  --email ADDR           owner email (ACME contact + owner identity)
  --name TEXT            wiki name
  --prefix ID            record-ID prefix, 2-8 chars [a-z][a-z0-9]
  --state-dir DIR        state root (default /var/lib/0xbrain)
  --yes                  non-interactive; fail instead of asking
  --skip-dns-check       do not compare DNS with this host's addresses
  --tls-internal         self-signed TLS, for disposable tests only
  --http-port N --https-port N   host ports (default 80/443; non-default only with --tls-internal)
  --python-base IMAGE    base image override (default python:3.12-slim-bookworm)
  --build-ca FILE        extra CA bundle for the image build behind a TLS-inspecting proxy
  --build-network NAME   network for the image build (e.g. host, to reach a loopback egress proxy)
  --stub                 run the health-only deployment STUB instead of the server (plumbing tests only)
USAGE
}

DOMAIN="" EMAIL="" NAME="" PREFIX="" STATE_DIR="" YES=0 SKIP_DNS=0
TLS_INTERNAL=0 HTTP_PORT="" HTTPS_PORT="" PY_BASE="" BUILD_CA="" BUILD_NET="" STUB=0
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2;; --email) EMAIL="$2"; shift 2;;
    --name) NAME="$2"; shift 2;; --prefix) PREFIX="$2"; shift 2;;
    --state-dir) STATE_DIR="$2"; shift 2;; --yes) YES=1; shift;;
    --skip-dns-check) SKIP_DNS=1; shift;; --tls-internal) TLS_INTERNAL=1; shift;;
    --http-port) HTTP_PORT="$2"; shift 2;; --https-port) HTTPS_PORT="$2"; shift 2;;
    --python-base) PY_BASE="$2"; shift 2;; --build-ca) BUILD_CA="$2"; shift 2;;
    --build-network) BUILD_NET="$2"; shift 2;;
    --stub) STUB=1; shift;; -h|--help) usage; exit 0;;
    *) usage >&2; die "unknown option: $1" 2;;
  esac
done

ask() { # ask VAR "prompt" [default]  -- keeps a preset value; prompts only on a tty
  local var="$1" prompt="$2" def="${3:-}" cur="${!1}" ans
  [ -n "$cur" ] && return 0
  cur="${def}"
  if [ "$YES" -eq 0 ] && [ -t 0 ]; then
    read -r -p "$prompt${def:+ [$def]}: " ans || true
    cur="${ans:-$def}"
  fi
  [ -n "$cur" ] || die "$var is required (use the matching option)" 2
  printf -v "$var" '%s' "$cur"
}

# ---- 1. prerequisites -------------------------------------------------------
log "1/10 prerequisites"
for c in docker git curl openssl awk; do command -v "$c" >/dev/null || die "missing prerequisite: $c"; done
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 plugin is required (docker compose)"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (run as root or add your user to the docker group)"
[ -f "$REPO/SYSTEM_DESIGN.md" ] && [ -f "$REPO/compose.yaml" ] || die "run install.sh from the repository root"
[ -d "$REPO/.git" ] || die "$REPO is not a Git checkout; clone the repository instead of downloading an archive"

# Inputs: preserve prior .env values so a re-run never rotates identity.
[ -n "$DOMAIN" ] || DOMAIN="$(env_get BRAIN_DOMAIN)"
[ -n "$EMAIL" ]  || EMAIL="$(env_get BRAIN_OWNER_EMAIL)"
[ -n "$NAME" ]   || NAME="$(env_get BRAIN_WIKI_NAME)"
[ -n "$PREFIX" ] || PREFIX="$(env_get BRAIN_PREFIX)"
[ -n "$STATE_DIR" ]   || STATE_DIR="$(env_get BRAIN_STATE_DIR)"
STATE_DIR="${STATE_DIR:-/var/lib/0xbrain}"
ask DOMAIN "Brain domain (e.g. brain.example.com)"
ask EMAIL  "Owner email"
ask NAME   "Wiki name" "My Brain"
ask PREFIX "Record prefix (2-8 lowercase letters/digits)" "zb"
[[ "$PREFIX" =~ ^[a-z][a-z0-9]{1,7}$ ]] || die "prefix must be 2-8 chars, lowercase, starting with a letter" 2
[[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die "invalid domain: $DOMAIN" 2
if [ "$TLS_INTERNAL" -eq 0 ] && { [ -n "$HTTP_PORT" ] || [ -n "$HTTPS_PORT" ]; }; then
  die "custom ports cannot obtain public certificates; use standard ports or --tls-internal" 2
fi
HTTP_PORT="${HTTP_PORT:-$(env_get BRAIN_HTTP_PORT)}"; HTTP_PORT="${HTTP_PORT:-80}"
HTTPS_PORT="${HTTPS_PORT:-$(env_get BRAIN_HTTPS_PORT)}"; HTTPS_PORT="${HTTPS_PORT:-443}"

if [ "$SKIP_DNS" -eq 0 ] && [ "$TLS_INTERNAL" -eq 0 ]; then
  resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')"
  local_ips="$(hostname -I 2>/dev/null || true)"
  match=0; for ip in $resolved; do case " $local_ips " in *" $ip "*) match=1;; esac; done
  if [ -z "$resolved" ]; then warn "$DOMAIN does not resolve yet; certificate issuance will fail until DNS is set"
  elif [ "$match" -eq 0 ]; then warn "$DOMAIN resolves to [$resolved] which is not a local address [$local_ips] (fine only behind NAT/floating IP)"; fi
  if { [ -z "$resolved" ] || [ "$match" -eq 0 ]; } && [ "$YES" -eq 0 ] && [ -t 0 ]; then
    read -r -p "Continue anyway? [y/N] " a; [[ "$a" =~ ^[Yy] ]] || die "aborted by user" 3
  fi
fi
if [ "$HTTP_PORT" = 80 ] && command -v ss >/dev/null; then
  if ss -ltn '( sport = :80 or sport = :443 )' 2>/dev/null | grep -q LISTEN && [ -z "$(dc ps -q caddy 2>/dev/null || true)" ]; then
    die "ports 80/443 are already in use by another service"
  fi
fi

# ---- 2. data dirs, identity, .env ------------------------------------------
log "2/10 data directories and configuration"
owner_uid="$(stat -c %u "$REPO")"; owner_gid="$(stat -c %g "$REPO")"
if [ "$owner_uid" -eq 0 ]; then
  if ! id brain >/dev/null 2>&1; then
    command -v useradd >/dev/null || die "repo is owned by root and useradd is unavailable; clone as a normal user"
    useradd --system --user-group --home-dir "$STATE_DIR/home" --shell /usr/sbin/nologin brain
  fi
  owner_uid="$(id -u brain)"; owner_gid="$(id -g brain)"
  log "repository was root-owned; handing it to system user 'brain' ($owner_uid)"
  chown -R "$owner_uid:$owner_gid" "$REPO"
fi
mkdir -p "$STATE_DIR"/{qmd,auth,home,caddy/data,caddy/config,backups}
chown -R "$owner_uid:$owner_gid" "$STATE_DIR"/{qmd,auth,home,backups}
chmod 700 "$STATE_DIR/auth"

env_set BRAIN_DOMAIN "$DOMAIN"; env_set BRAIN_OWNER_EMAIL "$EMAIL"
env_set BRAIN_WIKI_NAME "$NAME"; env_set BRAIN_PREFIX "$PREFIX"
env_set BRAIN_DATA_DIR "$REPO"; env_set BRAIN_STATE_DIR "$STATE_DIR"
env_set BRAIN_UID "$owner_uid"; env_set BRAIN_GID "$owner_gid"
env_set BRAIN_HTTP_PORT "$HTTP_PORT"; env_set BRAIN_HTTPS_PORT "$HTTPS_PORT"
env_set BRAIN_PUBLIC_URL "https://$DOMAIN$([ "$HTTPS_PORT" != 443 ] && echo ":$HTTPS_PORT" || true)"
env_set BRAIN_TLS_DIRECTIVE "$([ "$TLS_INTERNAL" -eq 1 ] && echo 'tls internal' || true)"
[ -n "$PY_BASE" ] && env_set BRAIN_PYTHON_BASE "$PY_BASE"
[ -n "$BUILD_NET" ] && env_set BRAIN_BUILD_NETWORK "$BUILD_NET"
[ "$STUB" -eq 1 ] && env_set BRAIN_ALLOW_STUB 1
# Owner passphrase for the OAuth consent page: generated once, never rotated by a re-run.
cur_secret="$(env_get BRAIN_OWNER_SECRET)"
if [ -z "$cur_secret" ] || [ "$cur_secret" = "generated-by-install.sh" ] || [ "${#cur_secret}" -lt 16 ]; then
  env_set BRAIN_OWNER_SECRET "$(openssl rand -hex 24)"
fi
unset cur_secret
chown "$owner_uid:$owner_gid" "$ENV_FILE"; chmod 600 "$ENV_FILE"

# ---- image ------------------------------------------------------------------
log "building the brain image (Python, Git, Node+QMD, conversion libs; no models)"
[ -z "$BUILD_CA" ] || { [ -f "$BUILD_CA" ] || die "--build-ca file not found"; export BRAIN_BUILD_CA="$BUILD_CA"; }
build_image

# ---- 3. instantiate if fresh ------------------------------------------------
log "3/10 instantiate (fresh wiki only)"
if [ -f "$REPO/00-system/registers/INSTANCE.json" ]; then
  log "already instantiated; leaving the wiki untouched"
else
  brain_run python scripts/instantiate.py --name "$NAME" --prefix "$PREFIX"
  brain_run git config core.hooksPath .githooks
  brain_run python scripts/validate_repo.py --full >/dev/null || die "validation failed after instantiate; nothing committed" 4
  brain_run git add -A
  brain_run git commit -q -m "bootstrap: instantiate $PREFIX ($NAME)" || die "bootstrap commit refused by the repository gate" 4
fi
brain_run git config core.hooksPath .githooks

# ---- 4. model-free QMD ------------------------------------------------------
log "4/10 QMD lexical search (no embeddings, no model downloads)"
brain_run bash scripts/configure-search.sh
brain_run bash scripts/refresh-search.sh

# ---- 5-8. HTTPS, start, health ---------------------------------------
log "5-7/10 starting services (caddy HTTPS -> brain remote MCP + owner OAuth)"
dc up -d
log "8/10 waiting for health"
wait_brain_healthy 240 || { dc logs --tail 40 brain >&2; die "brain did not become healthy" 5; }
wait_public_health 180 || { dc logs --tail 40 caddy >&2; die "HTTPS endpoint did not answer (DNS/ports/certificate?)" 5; }

# ---- 9. validators ----------------------------------------------------------
log "9/10 validators"
run_validators || die "validators failed" 6

# ---- 10. Claude steps -------------------------------------------------------
log "10/10 done"
cat <<DONE

0xBrain is running.
  URL:          $(env_get BRAIN_PUBLIC_URL)
  Health:       $(env_get BRAIN_PUBLIC_URL)/healthz
  MCP URL:      $(env_get BRAIN_PUBLIC_URL)/mcp
  Owner secret: sudo grep ^BRAIN_OWNER_SECRET= $ENV_FILE   (typed once on the consent page)
  State:        $STATE_DIR      Repo/data: $REPO

Connect Claude: follow docs/CONNECT_CLAUDE.md (custom connector URL above, owner
consent, standing instructions, first brain_status). Back up with: scripts/create-backup.sh
DONE
if [ "$STUB" -eq 1 ] || [ "$(env_get BRAIN_ALLOW_STUB)" = "1" ]; then
  warn "running the deployment STUB (BRAIN_ALLOW_STUB=1): no MCP endpoint; Claude cannot connect."
fi

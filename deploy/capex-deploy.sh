#!/usr/bin/env bash
# capex-deploy: pull-based, CI-gated deploys with automatic rollback.
# Installed as /usr/local/sbin/capex-deploy (root); capex-deploy.timer runs
# it every 10 minutes.
#
#   capex-deploy               deploy origin/main once its CI passed (or the pin)
#   capex-deploy --pin SHA     deploy SHA and stay on it
#   capex-deploy --unpin       follow origin/main again
#   capex-deploy --rollback    back to the previous release, pinned
#   capex-deploy --status      live release, pin, history
#   capex-deploy --force-gate  skip the CI check (emergencies only)
#
# One deploy:
#   fetch -> CI gate (lint-and-test passed for that exact commit)
#   -> build releases/<sha> as capex-deploy (git worktree + uv sync --frozen)
#   -> smoke-import -> drain the scheduler -> local DB backup
#   -> db migrate / sync-all / server init (once a DB exists)
#   -> install the release's systemd units -> switch /opt/capex/current
#   -> restart what was running -> health check.
# A failure after the switch restores the previous release and emails the
# operator. Migrations are additive, so the previous code still runs on
# the migrated DB.
set -euo pipefail

BASE=/opt/capex
REPO_DIR=$BASE/src
RELEASES=$BASE/releases
CURRENT=$BASE/current
PIN_FILE=/etc/capex/deploy-pin
HISTORY=$RELEASES/.history
GATE=/usr/local/lib/capex-deploy/ci_gate.py
KEEP=3
UV=/usr/local/bin/uv
PYTHON=/usr/bin/python3.12
EXTRAS=(--extra server --extra fetch --extra read --extra extract --extra export --extra charts)
SERVICES=(capex-scheduler capex-admin)
DB=/var/lib/capex/data/db/capex.db
HEARTBEAT=/var/lib/capex/run/scheduler.heartbeat
ADMIN_PORT=8081

log() { printf '[capex-deploy] %s\n' "$*"; }
as_deploy() { runuser -u capex-deploy -- env HOME=/home/capex-deploy "$@"; }

# Run a release's CLI as capex, with the services' environment.
capex_cli() {
  local release=$1
  shift
  # shellcheck disable=SC2016  # the inner script expands in the child shell
  runuser -u capex -- env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/home/capex bash -c '
    set -a
    . /etc/capex/capex.conf
    if [ -r /run/capex/capex.env ]; then . /run/capex/capex.env; fi
    set +a
    exec "$0" "$@"' "$release/.venv/bin/capex" "$@"
}

alert() {  # alert KEY SUBJECT BODY (best effort)
  local release
  release=$(readlink -f "$CURRENT" 2>/dev/null || true)
  if [ -n "$release" ] && [ -x "$release/.venv/bin/capex" ]; then
    capex_cli "$release" server alert "$1" "$2" "$3" || true
  fi
}

live_sha() {
  if [ -L "$CURRENT" ]; then basename "$(readlink -f "$CURRENT")"; fi
}

previous_sha() {  # the release deployed before the live one
  local live
  live=$(live_sha)
  [ -f "$HISTORY" ] || return 0
  grep -vx "$live" "$HISTORY" | tail -n 1 || true
}

resolve() {  # full SHA of a commit-ish, or fail
  as_deploy git -C "$REPO_DIR" rev-parse --verify --quiet "$1^{commit}" \
    || { log "unknown commit: $1"; exit 1; }
}

gate() {  # 0 = deploy, 1 = CI failed, 2 = wait
  local sha=$1 code=0
  if [ "${FORCE_GATE:-0}" = 1 ]; then
    log "CI gate skipped (--force-gate)"
    return 0
  fi
  python3 "$GATE" "$sha" || code=$?
  return "$code"
}

build() {
  local sha=$1 dir=$RELEASES/$1
  if [ -f "$dir/.capex-release" ] && [ -x "$dir/.venv/bin/capex" ]; then
    log "release ${sha:0:12} already built"
    return 0
  fi
  log "building release ${sha:0:12}"
  rm -rf "$dir"
  as_deploy git -C "$REPO_DIR" worktree prune
  as_deploy git -C "$REPO_DIR" worktree add --detach --force "$dir" "$sha" >/dev/null
  as_deploy "$UV" sync --quiet --frozen --project "$dir" --python "$PYTHON" "${EXTRAS[@]}"
  as_deploy "$dir/.venv/bin/python" -c \
    "import capex.cli.main, capex.server.scheduler, capex.server.jobs, capex.server.admin"
  echo "$sha" | as_deploy tee "$dir/.capex-release" >/dev/null   # marks the build complete
}

install_units() {  # from a release directory
  install -m 0644 "$1"/deploy/systemd/*.service "$1"/deploy/systemd/*.timer /etc/systemd/system/
  systemctl daemon-reload
}

switch_to() {
  ln -sfn "$RELEASES/$1" "$CURRENT.new"
  mv -Tf "$CURRENT.new" "$CURRENT"
}

healthy() {  # healthy SWITCH_EPOCH SERVICE...
  local since=$1 service
  shift
  for service in "$@"; do
    case $service in
      capex-scheduler)
        for _ in $(seq 45); do
          if [ -f "$HEARTBEAT" ] && [ "$(stat -c %Y "$HEARTBEAT")" -ge "$since" ]; then
            continue 2
          fi
          sleep 2
        done
        log "the scheduler wrote no heartbeat within 90 s"
        return 1 ;;
      capex-admin)
        for _ in $(seq 20); do
          if curl -fsS -o /dev/null -H "Host: localhost:$ADMIN_PORT" \
               "http://127.0.0.1:$ADMIN_PORT/"; then
            continue 2
          fi
          sleep 2
        done
        log "the admin panel does not answer"
        return 1 ;;
    esac
  done
}

prune() {
  local keep dir sha
  keep=$(tail -n "$KEEP" "$HISTORY" 2>/dev/null; live_sha)
  for dir in "$RELEASES"/*/; do
    [ -d "$dir" ] || continue
    sha=$(basename "$dir")
    if ! grep -qx "$sha" <<<"$keep"; then
      log "removing old release ${sha:0:12}"
      as_deploy git -C "$REPO_DIR" worktree remove --force "$dir" 2>/dev/null || rm -rf "$dir"
    fi
  done
  as_deploy git -C "$REPO_DIR" worktree prune
}

deploy() {
  local sha=$1 live prev_dir new_dir=$RELEASES/$1 running=() service since
  live=$(live_sha)
  if [ "$sha" = "$live" ]; then
    log "${sha:0:12} is already live"
    return 0
  fi
  build "$sha"

  for service in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "$service"; then running+=("$service"); fi
  done
  if systemctl is-active --quiet capex-scheduler; then
    log "draining the scheduler (it finishes its current job)"
    systemctl stop capex-scheduler
  fi
  prev_dir=${live:+$RELEASES/$live}

  if [ -f "$DB" ]; then
    log "backup, then migrate"
    if ! capex_cli "${prev_dir:-$new_dir}" server backup --no-upload >/dev/null \
       || ! capex_cli "$new_dir" db migrate \
       || ! capex_cli "$new_dir" db sync-all \
       || ! capex_cli "$new_dir" server init; then
      log "backup/migration failed: staying on ${live:0:12}"
      for service in "${running[@]}"; do systemctl start "$service" || true; done
      alert "deploy:${sha:0:12}" "deploy of ${sha:0:12} failed (migration)" \
        "The backup or migration step failed; ${live:0:12} stays live. journalctl -u capex-deploy"
      return 1
    fi
  else
    log "no DB at $DB yet: migration skipped (the data import creates it)"
  fi

  install_units "$new_dir"
  switch_to "$sha"
  since=$(date +%s)
  for service in "${running[@]}"; do systemctl restart "$service"; done
  if healthy "$since" "${running[@]}"; then
    [ "$(tail -n 1 "$HISTORY" 2>/dev/null)" = "$sha" ] || echo "$sha" >>"$HISTORY"
    install -m 0755 "$new_dir/deploy/capex-deploy.sh" /usr/local/sbin/capex-deploy
    install -m 0755 "$new_dir/deploy/capex-cli.sh" /usr/local/bin/capex
    install -D -m 0644 "$new_dir/deploy/ci_gate.py" "$GATE"
    log "live: ${sha:0:12}${live:+ (was ${live:0:12})}"
    prune
    return 0
  fi

  if [ -z "$live" ]; then
    log "first release is unhealthy and there is nothing to roll back to"
    alert "deploy:${sha:0:12}" "first deploy (${sha:0:12}) is unhealthy" \
      "No previous release to return to. journalctl -u capex-deploy -u capex-scheduler"
    return 1
  fi
  log "unhealthy: rolling back to ${live:0:12}"
  install_units "$prev_dir"
  switch_to "$live"
  for service in "${running[@]}"; do systemctl restart "$service" || true; done
  alert "deploy:${sha:0:12}" "deploy of ${sha:0:12} rolled back" \
    "The release did not become healthy and ${live:0:12} is live again. journalctl -u capex-deploy -u capex-scheduler -u capex-admin"
  return 1
}

status() {
  echo "live:     $(live_sha || true)"
  echo "pinned:   $(cat "$PIN_FILE" 2>/dev/null || echo "no (following origin/main)")"
  echo "history:  $(tail -n 5 "$HISTORY" 2>/dev/null | tr '\n' ' ')"
  echo "releases: $(find "$RELEASES" -mindepth 1 -maxdepth 1 -type d -printf '%f ' 2>/dev/null)"
  for service in capex-secrets "${SERVICES[@]}" capex-deploy.timer; do
    printf '%-16s %s\n' "$service" "$(systemctl is-active "$service" 2>/dev/null || true)"
  done
}

main() {
  [ "$(id -u)" -eq 0 ] || { echo "capex-deploy must run as root" >&2; exit 1; }
  if [ "${1:-}" = --force-gate ]; then FORCE_GATE=1; shift; fi
  local action=${1:-}
  case $action in
    --status) status; return 0 ;;
    --unpin) rm -f "$PIN_FILE"; log "unpinned: the next run follows origin/main"; return 0 ;;
  esac

  exec 9>/run/capex-deploy.lock
  flock -n 9 || { log "another deploy is running"; return 0; }
  install -d -m 0755 -o capex-deploy -g capex-deploy "$RELEASES"
  as_deploy git -C "$REPO_DIR" fetch --quiet --prune origin

  local sha code=0
  case $action in
    --pin)
      sha=$(resolve "${2:?usage: capex-deploy --pin SHA}")
      echo "$sha" >"$PIN_FILE"
      log "pinned to ${sha:0:12}" ;;
    --rollback)
      sha=$(previous_sha)
      [ -n "$sha" ] || { log "no previous release recorded"; return 1; }
      echo "$sha" >"$PIN_FILE"
      FORCE_GATE=1   # it passed the gate when it was first deployed
      log "rolling back to ${sha:0:12} (pinned; capex-deploy --unpin to follow main again)" ;;
    "")
      if [ -s "$PIN_FILE" ]; then sha=$(cat "$PIN_FILE"); else sha=$(resolve origin/main); fi ;;
    *) sed -n '2,12p' "$0"; return 2 ;;
  esac

  if [ "$sha" = "$(live_sha)" ]; then
    log "${sha:0:12} is already live"
    return 0
  fi
  gate "$sha" || code=$?
  case $code in
    0) ;;
    2) log "waiting for CI on ${sha:0:12}"; return 0 ;;
    *) log "CI did not pass for ${sha:0:12}: not deploying"
       alert "deploy:ci:${sha:0:12}" "CI failed for ${sha:0:12}: not deployed" \
         "The server stays on $(live_sha). Fix main, or pin a good commit: capex-deploy --pin SHA"
       return 0 ;;
  esac
  deploy "$sha"
}

main "$@"

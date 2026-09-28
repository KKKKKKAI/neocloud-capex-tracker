#!/usr/bin/env bash
# Bootstrap an Ubuntu 24.04 EC2 host for the capex tracker.
#
# Runs as root: first from cloud-init UserData (deploy/aws/capex-stack.yaml),
# then by hand whenever this file changes:
#     sudo bash /opt/capex/src/deploy/bootstrap.sh
# Every step is idempotent. Stack values arrive as CAPEX_* environment
# variables on the first run and are kept in /etc/capex/capex.conf.
# No secret ever passes through this script: capex-secrets.service reads
# them from SSM Parameter Store at boot.
set -euo pipefail

SRC_DIR=/opt/capex/src
VENV=/opt/capex/venv
DATA_DIR=/var/lib/capex
CONF=/etc/capex/capex.conf
UV_VERSION=0.12.19
CLAUDE_CODE_VERSION=2.1.232
CLAUDE_BIN=/home/capex/.local/bin/claude

log() { printf '[bootstrap] %s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "bootstrap.sh must run as root" >&2
  exit 1
fi

# ---- 1. Stack settings: environment first, then the saved conf --------
conf_get() {
  [ -f "$CONF" ] || return 0
  sed -n "s/^$1=//p" "$CONF" | tail -n 1 | tr -d "'\""
}
for key in CAPEX_DATA_VOLUME_ID CAPEX_SITE_BUCKET CAPEX_BACKUP_BUCKET \
           CAPEX_DISTRIBUTION_ID CAPEX_PUBLIC_BASE_URL; do
  if [ -z "${!key:-}" ]; then
    printf -v "$key" '%s' "$(conf_get "$key")"
  fi
done
imds_token=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
AWS_REGION_DETECTED=$(curl -fsS -H "X-aws-ec2-metadata-token: $imds_token" \
  http://169.254.169.254/latest/meta-data/placement/region)

# ---- 2. Packages, clock, automatic security updates -------------------
log "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  git sqlite3 jq rsync curl ca-certificates \
  python3.12 python3.12-venv unattended-upgrades
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
timedatectl set-timezone UTC

# ---- 3. Swap (1 GiB instances need headroom for claude + Excel) -------
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
  log "swap"
  [ -f /swapfile ] || fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
echo 'vm.swappiness=10' > /etc/sysctl.d/99-capex.conf
sysctl -q -p /etc/sysctl.d/99-capex.conf

# ---- 4. Persistent data volume at /var/lib/capex ----------------------
if [ -n "${CAPEX_DATA_VOLUME_ID:-}" ]; then
  dev="/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${CAPEX_DATA_VOLUME_ID//-/}"
  for _ in $(seq 60); do [ -e "$dev" ] && break; sleep 5; done
  if [ ! -e "$dev" ]; then
    echo "data volume $CAPEX_DATA_VOLUME_ID never appeared at $dev" >&2
    exit 1
  fi
  if [ -z "$(lsblk -no FSTYPE "$dev")" ]; then
    log "formatting new data volume"
    mkfs.ext4 -q -L capex-data "$dev"
  fi
  uuid=$(blkid -s UUID -o value "$dev")
  install -d -m 0755 "$DATA_DIR"
  grep -q "UUID=$uuid" /etc/fstab \
    || echo "UUID=$uuid $DATA_DIR ext4 defaults,nofail,x-systemd.device-timeout=30 0 2" >> /etc/fstab
  mountpoint -q "$DATA_DIR" || mount "$DATA_DIR"
fi

# ---- 5. Users and directories -----------------------------------------
id -u capex >/dev/null 2>&1 \
  || useradd --system --create-home --home-dir /home/capex --shell /bin/bash capex
id -u capex-deploy >/dev/null 2>&1 \
  || useradd --system --create-home --home-dir /home/capex-deploy --shell /usr/sbin/nologin capex-deploy
usermod -aG capex ubuntu   # the SSH user can read logs and data

install -d -m 0750 -o root -g capex /etc/capex
for d in "$DATA_DIR" "$DATA_DIR"/data "$DATA_DIR"/data/db "$DATA_DIR"/data/_sources \
         "$DATA_DIR"/workbook "$DATA_DIR"/charts "$DATA_DIR"/site "$DATA_DIR"/output \
         "$DATA_DIR"/logs "$DATA_DIR"/backups "$DATA_DIR"/run "$DATA_DIR"/llm-cwd; do
  install -d -m 0750 -o capex -g capex "$d"
done

# ---- 6. Non-secret runtime config -------------------------------------
tmp=$(mktemp)
cat >"$tmp" <<EOF
# Written by deploy/bootstrap.sh. Non-secret settings only; secrets are
# loaded from SSM into /run/capex/capex.env by capex-secrets.service.
CAPEX_HOME=$DATA_DIR
CAPEX_TZ=Europe/London
CAPEX_DB_JOURNAL_MODE=WAL
CAPEX_SECRETS_PATH=/capex/
CAPEX_CLAUDE_BIN=$CLAUDE_BIN
AWS_DEFAULT_REGION=$AWS_REGION_DETECTED
CAPEX_SITE_BUCKET=${CAPEX_SITE_BUCKET:-}
CAPEX_BACKUP_BUCKET=${CAPEX_BACKUP_BUCKET:-}
CAPEX_DISTRIBUTION_ID=${CAPEX_DISTRIBUTION_ID:-}
CAPEX_PUBLIC_BASE_URL=${CAPEX_PUBLIC_BASE_URL:-}
CAPEX_DATA_VOLUME_ID=${CAPEX_DATA_VOLUME_ID:-}
DISABLE_AUTOUPDATER=1
PYTHONUNBUFFERED=1
EOF
install -m 0640 -o root -g capex "$tmp" "$CONF"
rm -f "$tmp"

# ---- 7. uv + Python venv with the package -----------------------------
if ! /usr/local/bin/uv --version 2>/dev/null | grep -q " $UV_VERSION"; then
  log "uv $UV_VERSION"
  curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" \
    | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
[ -x "$VENV/bin/python" ] || /usr/local/bin/uv venv "$VENV" --python /usr/bin/python3.12
/usr/local/bin/uv pip install --python "$VENV/bin/python" \
  -e "${SRC_DIR}[server,fetch,read,extract,export,charts]"

# ---- 8. Claude Code (native build, pinned, no auto-update) ------------
if ! sudo -u capex -H "$CLAUDE_BIN" --version 2>/dev/null | grep -q "^$CLAUDE_CODE_VERSION "; then
  log "claude code $CLAUDE_CODE_VERSION"
  sudo -u capex -H bash -c "curl -fsSL https://claude.ai/install.sh | bash -s $CLAUDE_CODE_VERSION"
fi

# ---- 9. Journald cap and systemd units --------------------------------
install -d /etc/systemd/journald.conf.d
cat >/etc/systemd/journald.conf.d/capex.conf <<'EOF'
[Journal]
SystemMaxUse=200M
EOF
systemctl restart systemd-journald

install -m 0644 "$SRC_DIR"/deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable capex-secrets.service
if ! systemctl restart capex-secrets.service; then
  log "capex-secrets failed: check the /capex/ parameters (journalctl -u capex-secrets)"
fi

log "done"

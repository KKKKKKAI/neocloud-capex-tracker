#!/usr/bin/env bash
# Copy the server's newest DB backup (and optionally its raw filings) from
# the backup bucket into a local CAPEX_HOME, for development and audits.
#
#   scripts/pull_server_snapshot.sh [--raw] [DEST]      # DEST: ~/capex-snapshot
#   CAPEX_HOME=~/capex-snapshot capex export             # then work on it
#
# Uses your AWS CLI login (`aws login`). The server stays the only writer:
# nothing here is sent back.
set -euo pipefail

raw=0
if [ "${1:-}" = --raw ]; then raw=1; shift; fi
dest=${1:-$HOME/capex-snapshot}
stack=${CAPEX_STACK:-capex}

bucket=$(aws cloudformation describe-stacks --stack-name "$stack" \
  --query "Stacks[0].Outputs[?OutputKey=='BackupBucket'].OutputValue" --output text)
key=$(aws s3api list-objects-v2 --bucket "$bucket" --prefix db/ \
  --query "sort_by(Contents[?ends_with(Key, '.db.gz')], &Key)[-1].Key" --output text)
if [ -z "$key" ] || [ "$key" = None ]; then
  echo "no DB backup in s3://$bucket/db/ yet" >&2
  exit 1
fi

mkdir -p "$dest/data/db"
aws s3 cp --only-show-errors "s3://$bucket/$key" "$dest/data/db/capex.db.gz"
gunzip -f "$dest/data/db/capex.db.gz"
python3 - "$dest/data/db/capex.db" <<'EOF'
import sqlite3, sys
result = sqlite3.connect(sys.argv[1]).execute("PRAGMA integrity_check").fetchone()[0]
sys.exit(0 if result == "ok" else f"integrity_check: {result}")
EOF
if [ "$raw" = 1 ]; then
  aws s3 sync --only-show-errors "s3://$bucket/raw/" "$dest/data/_sources/"
fi
echo "snapshot of $key in $dest (integrity ok)"
echo "use it with: CAPEX_HOME=$dest capex ..."

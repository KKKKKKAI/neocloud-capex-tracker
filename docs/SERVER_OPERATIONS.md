# Server operations

How to run and repair the always-on tracker host (EC2 `capex`, `eu-north-1`).
How it was built: `deploy/README.md`. Migration log:
`docs/SERVER_MIGRATION_CHECKLIST.md`.

Commands marked **(host)** run on the server. Reach it with
`ssh capex` (see `scripts/ssh_config.example`), or without SSH from WSL:

```bash
python deploy/aws/run_on_host.py 'capex-deploy --status'
```

On the host, `capex …` runs the live release's CLI as the `capex` user with
the services' environment (`/usr/local/bin/capex`).

## Everyday

| To | Do |
|---|---|
| Open the admin panel | WSL: `scripts/admin_tunnel.sh`, then http://localhost:8081 |
| See what's running | panel Overview, or **(host)** `capex server jobs` / `capex server runs` |
| Run a job now | panel Run now, or **(host)** `capex server run JOB` |
| Read a run's log | panel Runs, or **(host)** `capex server log RUN_ID` |
| Follow the scheduler | **(host)** `journalctl -u capex-scheduler -f` |
| Health | **(host)** `capex server health` (the hourly job emails problems) |
| Pause everything scheduled | panel Schedule → Pause, or `capex settings set scheduler.paused true` |
| Pause Claude calls | panel Settings → Pause, or `capex settings set llm.paused_until 2026-11-01T00:00:00+00:00` |

## Deploys

Merging to `main` deploys by itself. Within about 10 minutes of CI going
green, `capex-deploy.timer` builds the new release, migrates the DB and
switches over. See `deploy/README.md` for the steps.

| To | **(host)** |
|---|---|
| See the live release, pin, history | `sudo capex-deploy --status` |
| Deploy now instead of waiting | `sudo systemctl start capex-deploy` |
| Go back to the previous release | `sudo capex-deploy --rollback` (pins it) |
| Stay on a specific commit | `sudo capex-deploy --pin SHA` |
| Follow `main` again | `sudo capex-deploy --unpin` |
| Deploy although CI hasn't passed | `sudo capex-deploy --force-gate --pin SHA` (emergencies only) |
| See a deploy's log | `journalctl -u capex-deploy -n 200` |

A failed deploy restores the previous release and emails the operator.
Migrations only ever add columns and tables, so older code keeps running on
a newer DB.

## Renew the Claude token (yearly; the health check warns at day 330)

1. In WSL: `claude setup-token`, then sign in. It prints a new token valid for one year.
2. AWS console → Systems Manager → Parameter Store →
   `/capex/CLAUDE_CODE_OAUTH_TOKEN` → Edit → paste it. Keep the type SecureString.
3. **(host)** `sudo systemctl restart capex-secrets capex-scheduler capex-admin`
4. **(host)** `capex settings set llm.token_created_at $(date +%F)` then `capex llm ping`.

Any other secret (Alpha Vantage, Gmail) changes the same way: edit the
parameter, then restart `capex-secrets` and the services.

## Restore the database

Backups: nightly to `s3://<BackupBucket>/db/` (verified), plus the newest 7
on the host in `/var/lib/capex/backups/db/`.

```bash
capex server backups                                  # (host) newest first
sudo systemctl stop capex-scheduler capex-admin       # (host)
capex server restore db/capex-20261102T031500Z.db.gz \
    --to /var/lib/capex/data/db/capex.db --force      # (host) verified, WAL cleared
sudo systemctl start capex-scheduler capex-admin      # (host)
```

To try it without touching the live DB, restore to another path, e.g.
`--to /tmp/restore-test.db`. A monthly drill: restore the newest S3 backup
to a scratch path and run `capex server health` against it with
`CAPEX_DB_PATH`.

For local work on production data: `scripts/pull_server_snapshot.sh [--raw]`
(WSL), then `CAPEX_HOME=~/capex-snapshot capex …`.

## Your IP changed (SSH times out)

```bash
deploy/aws/allow_my_ip.sh          # WSL: points the SSH allowlist at your current IP
```

`deploy/aws/run_on_host.py` keeps working meanwhile (it goes through SSM,
not SSH).

## Rotate the SSH key

1. WSL: `ssh-keygen -t ed25519 -f ~/.ssh/capex_ed25519_new`
2. `PUBKEY_FILE=~/.ssh/capex_ed25519_new.pub deploy/aws/deploy_stack.sh`.
   The key pair is replaced, and with it the instance. The data volume
   (DB, filings, site) is re-attached, and the new instance bootstraps
   itself. Then run "Go live" below.
3. Update `IdentityFile` in `~/.ssh/config`.

## Disk full

- Data volume: `df -h /var/lib/capex`. Run `capex server run prune`, or
  lower `prune.keep_workbooks` / `prune.run_log_days` in Settings. Raw
  filings are the bulk and are backed up to S3.
- Root disk: `sudo journalctl --vacuum-size=100M`. Old releases are pruned
  to 3 by the deployer.

## Add a company

Adding a company is a code change, because seeds live in git:
1. Add it to `data/_sources/_identity.yaml` and `data/seeds/coverage.yaml`, then open a PR.
2. After merge the deploy runs `db sync-all`, and the watcher adds its watchlist row on its next run.
3. In the panel, check its forms under Companies. Add dates under Calendar if Alpha Vantage lacks them, then click **Newest FORM** to fetch its latest filing.

Watching or unwatching a company, changing its forms, and every schedule
are runtime changes in the panel. No deploy is needed.

## Run a maintenance script on the server

```bash
# (host) as capex, with the service environment and the live release's code
sudo -u capex -H bash -c 'set -a; . /etc/capex/capex.conf; . /run/capex/capex.env; set +a;
  cd /var/lib/capex && /opt/capex/current/.venv/bin/python /opt/capex/current/scripts/SCRIPT.py --dry-run'
```

Stop the scheduler first if the script writes to the DB
(`sudo systemctl stop capex-scheduler`), then start it again.

## Go live, or start again after maintenance

```bash
sudo systemctl enable --now capex-scheduler capex-admin    # (host)
capex server health                                        # (host)
```

## Rebuild the host from scratch

`deploy/aws/deploy_stack.sh` with `NEW_AMI=1`. UserData clones the repo and
runs `bootstrap.sh`, which deploys the newest green `main` and re-attaches
the data volume. Then enable the services (above) and run
`sudo bash /opt/capex/current/deploy/smoke_test.sh`.

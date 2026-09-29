# deploy/

Everything that turns an empty AWS account into the always-on tracker
host, and keeps it on the latest green `main`. Day-to-day operations are
in `docs/SERVER_OPERATIONS.md`; the migration log is
`docs/SERVER_MIGRATION_CHECKLIST.md`.

| File | Runs where | What it does |
|---|---|---|
| `aws/capex-stack.yaml` | CloudFormation | EC2 host, persistent data volume, Elastic IP, SSH-only security group, private site bucket behind CloudFront, versioned backup bucket, least-privilege instance role |
| `aws/deploy_stack.sh` | WSL | Create or update the stack; keeps the current AMI unless `NEW_AMI=1` |
| `aws/allow_my_ip.sh` | WSL | Re-point the SSH allowlist at your current IP (changes nothing else) |
| `bootstrap.sh` | server (root) | Idempotent host setup: packages, swap, data volume, users, uv, Claude Code, deploy tooling, units, first release |
| `capex-deploy.sh` | server (root) | `/usr/local/sbin/capex-deploy`: CI-gated pull deploy with rollback (below) |
| `ci_gate.py` | server | Did the `lint-and-test` check pass for this exact commit? (GitHub's public API) |
| `capex-cli.sh` | server | `/usr/local/bin/capex`: the live release's CLI as the `capex` user with the service environment |
| `systemd/capex-secrets.service` | server | At boot, copies `/capex/*` from SSM into `/run/capex/capex.env` (tmpfs, `root:capex 0640`) |
| `systemd/capex-scheduler.service` | server | The scheduler (`capex server scheduler`); drains on stop |
| `systemd/capex-admin.service` | server | The admin panel on 127.0.0.1:8081 |
| `systemd/capex-deploy.{service,timer}` | server | Runs `capex-deploy` every 10 minutes |
| `systemd/capex-alert@.service` | server | Emails the operator when a unit fails (`OnFailure=`) |
| `smoke_test.sh` | server | Runs `capex.server.doctor` as the `capex` user with the service environment |
| `aws/run_on_host.py` | WSL | Runs a command on the host through SSM Run Command: no SSH key or open port needed |

## Releases and deploys

```
/opt/capex/src                git clone (owner capex-deploy); releases are its worktrees
/opt/capex/releases/<sha>/    one release: code + .venv (uv sync --frozen from uv.lock)
/opt/capex/current -> releases/<sha>   what the services run
/var/lib/capex/               data: DB, raw filings, site, workbooks, logs, backups
```

Every 10 minutes `capex-deploy`:
1. fetches, and deploys `origin/main` (or the pinned commit) only once its `lint-and-test` check passed;
2. builds the release as the unprivileged `capex-deploy` user;
3. drains the scheduler, which finishes its current job, and takes a local DB backup;
4. runs `db migrate`, `db sync-all` and `server init`;
5. installs the release's units and switches `current` atomically;
6. restarts whatever was running and checks the heartbeat and the admin panel.

If the new release doesn't come up, it goes back to the previous one and emails the operator. It keeps 3 releases. `capex-deploy --status | --pin SHA | --unpin | --rollback` (see `docs/SERVER_OPERATIONS.md`).

## Secrets

Only SSM Parameter Store holds secrets, in the stack's region, under
`/capex/`. Store the secret ones as **SecureString**:

| Parameter | Type |
|---|---|
| `/capex/CLAUDE_CODE_OAUTH_TOKEN` | SecureString (from `claude setup-token`; valid one year) |
| `/capex/ALPHA_VANTAGE_API_KEY` | SecureString |
| `/capex/GMAIL_APP_PASSWORD` | SecureString |
| `/capex/GMAIL_USERNAME` | String |

Check names and types without reading values (from WSL, via the dev venv):

```bash
python -m capex.server.secrets check
```

After changing a parameter on a running host:

```bash
python deploy/aws/run_on_host.py 'systemctl restart capex-secrets'
```

(`ssh capex sudo …` works too. With a passphrase-protected key, load
it once per WSL session: `eval "$(ssh-agent -s)" && ssh-add ~/.ssh/capex_ed25519`.)

## First deploy

```bash
deploy/aws/deploy_stack.sh                         # ~10 min (CloudFront is the slow part)
# add the printed PublicIp to ~/.ssh/config — see scripts/ssh_config.example
python deploy/aws/run_on_host.py 'tail -n 20 /var/log/capex-bootstrap.log'
python deploy/aws/run_on_host.py --timeout 600 'bash /opt/capex/current/deploy/smoke_test.sh'
```

The smoke test prints one line per check (claude, sec, alpha_vantage,
gmail, publish, memory, disk, data_mount) and seeds a placeholder
`index.html`, so the CloudFront URL answers before the first publish.

## Data safety

- The DB, raw filings and outputs live on the data volume
  (`/var/lib/capex`), not the root disk. Replacing the host (a new AMI,
  or a new SSH key) re-attaches the same volume.
- The volume is snapshotted if it is ever replaced or the stack is
  deleted. The backup bucket is retained on stack deletion.
- The host can write backups but cannot delete them. Old versions
  expire after 30 days, and DB backups after 180.

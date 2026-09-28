"""Run a shell command on the capex host through SSM Run Command.

Needs no SSH key or open port: it uses your AWS CLI credentials and the
host's Session Manager agent (the instance role allows it). The remote
command runs as root; stdout/stderr (up to 24 KB each) are printed and
the exit status mirrors the remote one.

    python deploy/aws/run_on_host.py 'systemctl status capex-secrets --no-pager'
    python deploy/aws/run_on_host.py --timeout 600 'bash /opt/capex/src/deploy/smoke_test.sh'
"""
from __future__ import annotations

import argparse
import sys
import time

import boto3

PENDING = ("Pending", "InProgress", "Delayed")


def stack_output(cloudformation, stack: str, key: str) -> str:
    outputs = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in outputs if o["OutputKey"] == key)


def run(ssm, instance_id: str, command: str, timeout: int) -> dict:
    command_id = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        TimeoutSeconds=60,
        Parameters={"commands": [command], "executionTimeout": [str(timeout)]},
    )["Command"]["CommandId"]
    deadline = time.monotonic() + timeout + 120
    while True:
        time.sleep(3)
        try:
            invocation = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if invocation["Status"] not in PENDING or time.monotonic() > deadline:
            return invocation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", help="shell command to run on the host (as root)")
    parser.add_argument("--stack", default="capex")
    parser.add_argument("--region", default=None, help="default: your AWS CLI region")
    parser.add_argument("--timeout", type=int, default=900, help="seconds")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    instance_id = stack_output(session.client("cloudformation"), args.stack, "InstanceId")
    invocation = run(session.client("ssm"), instance_id, args.command, args.timeout)

    sys.stdout.write(invocation["StandardOutputContent"])
    if invocation["StandardErrorContent"].strip():
        sys.stderr.write(invocation["StandardErrorContent"])
    if invocation["Status"] == "Success":
        return 0
    print(f"[{invocation['Status']}]", file=sys.stderr)
    return invocation["ResponseCode"] if invocation["ResponseCode"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env bash
# Create or update the capex CloudFormation stack. Run from WSL:
#     deploy/aws/deploy_stack.sh
#
# Env (all optional):
#   STACK_NAME     default capex
#   AWS_REGION     default: the CLI's configured region, else eu-north-1
#   PUBKEY_FILE    default ~/.ssh/capex_ed25519.pub
#   INSTANCE_TYPE  e.g. t3.small; omitted = keep the current / template default
#   NEW_AMI=1      move to the latest Ubuntu 24.04 AMI (REPLACES the host;
#                  the data volume survives and is re-attached)
set -euo pipefail
cd "$(dirname "$0")/../.."

STACK_NAME=${STACK_NAME:-capex}
REGION=${AWS_REGION:-$(aws configure get region || true)}
REGION=${REGION:-eu-north-1}
PUBKEY_FILE=${PUBKEY_FILE:-$HOME/.ssh/capex_ed25519.pub}
AMI_PARAM=/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id

stack_param() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
    --query "Stacks[0].Parameters[?ParameterKey=='$1'].ParameterValue" \
    --output text 2>/dev/null || true
}

# Keep the AMI the stack already runs unless asked: a new AMI replaces the host.
AMI_ID=$(stack_param ImageId)
if [ -z "$AMI_ID" ] || [ "$AMI_ID" = "None" ] || [ "${NEW_AMI:-0}" = 1 ]; then
  AMI_ID=$(aws ssm get-parameter --region "$REGION" --name "$AMI_PARAM" \
    --query Parameter.Value --output text)
fi
MY_IP=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')

overrides=("ImageId=$AMI_ID" "AdminCidr=$MY_IP/32" "SshPublicKey=$(cat "$PUBKEY_FILE")")
[ -n "${INSTANCE_TYPE:-}" ] && overrides+=("InstanceType=$INSTANCE_TYPE")

echo "stack=$STACK_NAME region=$REGION ami=$AMI_ID admin=$MY_IP/32"
aws cloudformation deploy --region "$REGION" --stack-name "$STACK_NAME" \
  --template-file deploy/aws/capex-stack.yaml \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --parameter-overrides "${overrides[@]}"

aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table

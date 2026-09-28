#!/usr/bin/env bash
# Point the SSH allowlist at your current public IP (home IPs change).
# Touches only the AdminCidr parameter; every other value — including
# the AMI — is kept, so the host is never replaced by this.
#     deploy/aws/allow_my_ip.sh
set -euo pipefail

STACK_NAME=${STACK_NAME:-capex}
REGION=${AWS_REGION:-$(aws configure get region || true)}
REGION=${REGION:-eu-north-1}
MY_IP=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')

current=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
  --query "Stacks[0].Parameters[?ParameterKey=='AdminCidr'].ParameterValue" --output text)
if [ "$current" = "$MY_IP/32" ]; then
  echo "SSH already allowed from $MY_IP"
  exit 0
fi

params=()
for key in $(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
               --query "Stacks[0].Parameters[].ParameterKey" --output text); do
  if [ "$key" = AdminCidr ]; then
    params+=("ParameterKey=AdminCidr,ParameterValue=$MY_IP/32")
  else
    params+=("ParameterKey=$key,UsePreviousValue=true")
  fi
done

aws cloudformation update-stack --region "$REGION" --stack-name "$STACK_NAME" \
  --use-previous-template --capabilities CAPABILITY_IAM --parameters "${params[@]}" >/dev/null
aws cloudformation wait stack-update-complete --region "$REGION" --stack-name "$STACK_NAME"
echo "SSH now allowed from $MY_IP (was ${current:-unset})"

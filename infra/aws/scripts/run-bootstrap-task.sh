#!/usr/bin/env bash
set -euo pipefail

aws_environment="${AWS_INFRA_ENVIRONMENT:-poc}"
wait_timeout_seconds="${BOOTSTRAP_WAIT_TIMEOUT_SECONDS:-43200}"
if [[ ! "${aws_environment}" =~ ^[a-z][a-z0-9-]{0,19}$ ]] || [[ -z "${AWS_PROFILE:-}" ]]; then
  echo "Valid AWS_INFRA_ENVIRONMENT and AWS_PROFILE are required" >&2
  exit 2
fi
if [[ ! "${wait_timeout_seconds}" =~ ^[1-9][0-9]*$ ]]; then
  echo "BOOTSTRAP_WAIT_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 2
fi

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd "${script_directory}/../../.." && pwd)"
config_path="${repository_root}/infra/aws/config/environments/${aws_environment}.json"
config_value() {
  node -e 'const fs=require("fs"); const c=JSON.parse(fs.readFileSync(process.argv[1],"utf8")); const path=process.argv[2].split("."); let v=c; for (const key of path) v=v[key]; process.stdout.write(String(v));' "${config_path}" "$1"
}

expected_account="$(config_value account)"
aws_region="$(config_value region)"
project_name="$(config_value projectName)"
environment_name="$(config_value environmentName)"
prefix="${project_name}-${environment_name}"
actual_account="$(aws --profile "${AWS_PROFILE}" --region "${aws_region}" sts get-caller-identity --query Account --output text)"
if [[ "${actual_account}" != "${expected_account}" ]]; then
  echo "AWS account ${actual_account} does not match config ${expected_account}" >&2
  exit 2
fi

stack_output() {
  aws --profile "${AWS_PROFILE}" --region "${aws_region}" cloudformation describe-stacks \
    --stack-name "${prefix}-management" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" \
    --output text
}

cluster="$(stack_output BootstrapClusterName)"
task_definition="$(stack_output BootstrapTaskDefinitionArn)"
security_group="$(stack_output BootstrapSecurityGroupId)"
subnets="$(stack_output BootstrapSubnetIds)"
network_configuration="awsvpcConfiguration={subnets=[${subnets}],securityGroups=[${security_group}],assignPublicIp=DISABLED}"

task_arn="$(aws --profile "${AWS_PROFILE}" --region "${aws_region}" ecs run-task \
  --cluster "${cluster}" \
  --task-definition "${task_definition}" \
  --launch-type FARGATE \
  --network-configuration "${network_configuration}" \
  --query 'tasks[0].taskArn' \
  --output text)"
if [[ -z "${task_arn}" || "${task_arn}" == "None" ]]; then
  echo "ECS did not start the bootstrap task" >&2
  exit 2
fi
echo "Started ${task_arn}"
deadline=$((SECONDS + wait_timeout_seconds))
while true; do
  task_status="$(aws --profile "${AWS_PROFILE}" --region "${aws_region}" ecs describe-tasks \
    --cluster "${cluster}" --tasks "${task_arn}" \
    --query 'tasks[0].lastStatus' --output text)"
  if [[ "${task_status}" == "STOPPED" ]]; then
    break
  fi
  if [[ -z "${task_status}" || "${task_status}" == "None" ]]; then
    echo "ECS task status is unavailable: ${task_arn}" >&2
    exit 2
  fi
  if (( SECONDS >= deadline )); then
    echo "Timed out waiting for ${task_arn}; the ECS task was not stopped" >&2
    exit 124
  fi
  sleep 30
done
exit_code="$(aws --profile "${AWS_PROFILE}" --region "${aws_region}" ecs describe-tasks \
  --cluster "${cluster}" --tasks "${task_arn}" \
  --query 'tasks[0].containers[0].exitCode' --output text)"
if [[ "${exit_code}" != "0" ]]; then
  aws --profile "${AWS_PROFILE}" --region "${aws_region}" ecs describe-tasks \
    --cluster "${cluster}" --tasks "${task_arn}" \
    --query 'tasks[0].{stopCode:stopCode,stoppedReason:stoppedReason,containers:containers[*].reason}'
  exit 1
fi
echo "Bootstrap task completed successfully"

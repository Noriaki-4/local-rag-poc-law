#!/usr/bin/env bash
set -euo pipefail

aws_environment="${AWS_INFRA_ENVIRONMENT:-poc}"
if [[ ! "${aws_environment}" =~ ^[a-z][a-z0-9-]{0,19}$ ]]; then
  echo "Invalid AWS_INFRA_ENVIRONMENT: ${aws_environment}" >&2
  exit 2
fi
if [[ -z "${AWS_PROFILE:-}" ]]; then
  echo "AWS_PROFILE is required" >&2
  exit 2
fi

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd "${script_directory}/../../.." && pwd)"
config_path="${repository_root}/infra/aws/config/environments/${aws_environment}.json"
if [[ ! -f "${config_path}" ]]; then
  echo "Environment config not found: ${config_path}" >&2
  exit 2
fi

config_value() {
  node -e 'const fs=require("fs"); const c=JSON.parse(fs.readFileSync(process.argv[1],"utf8")); const path=process.argv[2].split("."); let v=c; for (const key of path) v=v[key]; process.stdout.write(String(v));' "${config_path}" "$1"
}

expected_account="$(config_value account)"
aws_region="$(config_value region)"
project_name="$(config_value projectName)"
environment_name="$(config_value environmentName)"
repository_name="$(config_value agentCore.imageRepositoryName)"
image_tag="$(config_value agentCore.imageTag)"
actual_account="$(aws --profile "${AWS_PROFILE}" --region "${aws_region}" sts get-caller-identity --query Account --output text)"

if [[ "${actual_account}" != "${expected_account}" ]]; then
  echo "AWS account ${actual_account} does not match config ${expected_account}" >&2
  exit 2
fi

registry="${expected_account}.dkr.ecr.${aws_region}.amazonaws.com"
image_uri="${registry}/${project_name}-${environment_name}/${repository_name}:${image_tag}"

aws --profile "${AWS_PROFILE}" --region "${aws_region}" ecr get-login-password \
  | docker login --username AWS --password-stdin "${registry}"

docker buildx build \
  --platform linux/arm64 \
  --file "${repository_root}/infra/aws/agentcore/Dockerfile" \
  --tag "${image_uri}" \
  --push \
  "${repository_root}"

echo "Pushed ${image_uri}"

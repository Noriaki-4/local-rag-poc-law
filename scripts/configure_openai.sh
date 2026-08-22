#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
env_file="${OPENAI_ENV_FILE:-${repo_root}/.env}"

printf 'OpenAI API key（入力内容は表示されません）: ' >&2
IFS= read -r -s api_key
printf '\n' >&2

if [[ -z "${api_key}" || "${api_key}" =~ [[:space:]] ]]; then
  printf 'エラー: 空白を含まないAPI keyを入力してください。\n' >&2
  exit 1
fi

env_dir="$(dirname "${env_file}")"
mkdir -p "${env_dir}"
temp_file="$(mktemp "${env_file}.tmp.XXXXXX")"
trap 'rm -f "${temp_file}"' EXIT

provider_written=false
model_written=false
key_written=false
base_url_written=false
ceiling_written=false

if [[ -f "${env_file}" ]]; then
  while IFS= read -r line || [[ -n "${line}" ]]; do
    case "${line}" in
      LLM_PROVIDER=*)
        if [[ "${provider_written}" == false ]]; then
          printf 'LLM_PROVIDER=openai\n' >>"${temp_file}"
          provider_written=true
        fi
        ;;
      LLM_MODEL=*)
        if [[ "${model_written}" == false ]]; then
          printf 'LLM_MODEL=gpt-4o-mini\n' >>"${temp_file}"
          model_written=true
        fi
        ;;
      OPENAI_API_KEY=*)
        if [[ "${key_written}" == false ]]; then
          printf 'OPENAI_API_KEY=%s\n' "${api_key}" >>"${temp_file}"
          key_written=true
        fi
        ;;
      OPENAI_BASE_URL=*)
        if [[ "${base_url_written}" == false ]]; then
          printf 'OPENAI_BASE_URL=https://api.openai.com/v1\n' >>"${temp_file}"
          base_url_written=true
        fi
        ;;
      OPENAI_MAX_TOKENS_CEILING=*)
        if [[ "${ceiling_written}" == false ]]; then
          printf 'OPENAI_MAX_TOKENS_CEILING=16384\n' >>"${temp_file}"
          ceiling_written=true
        fi
        ;;
      *)
        printf '%s\n' "${line}" >>"${temp_file}"
        ;;
    esac
  done <"${env_file}"
fi

if [[ "${provider_written}" == false ]]; then
  printf 'LLM_PROVIDER=openai\n' >>"${temp_file}"
fi
if [[ "${model_written}" == false ]]; then
  printf 'LLM_MODEL=gpt-4o-mini\n' >>"${temp_file}"
fi
if [[ "${key_written}" == false ]]; then
  printf 'OPENAI_API_KEY=%s\n' "${api_key}" >>"${temp_file}"
fi
if [[ "${base_url_written}" == false ]]; then
  printf 'OPENAI_BASE_URL=https://api.openai.com/v1\n' >>"${temp_file}"
fi
if [[ "${ceiling_written}" == false ]]; then
  printf 'OPENAI_MAX_TOKENS_CEILING=16384\n' >>"${temp_file}"
fi

chmod 600 "${temp_file}"
mv "${temp_file}" "${env_file}"
trap - EXIT
unset api_key

printf '.envへOpenAI設定を保存しました。API keyは表示していません。\n'

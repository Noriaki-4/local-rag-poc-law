#!/usr/bin/env bash

set -euo pipefail

root_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
target_dir="${LAWQA_GUIDANCE_DIR:-$root_dir/datasets/lawqa_jp/external-guidance}"
documents_dir="$target_dir/documents"
template="$root_dir/docs/requirements/samples/external_guidance.manifest.sample.json"
manifest="$target_dir/manifest.json"

mkdir -p "$documents_dir"
cp "$template" "$manifest"

download() {
  local url="$1"
  local filename="$2"
  curl --fail --location --retry 3 --retry-delay 2 --output "$documents_dir/$filename" "$url"
}

download "https://www.fsa.go.jp/common/law/guide/kinyushohin.pdf" \
  "fsa-financial-instruments-business-supervisory-guidelines.pdf"
# The lawqa_jp reference URL was retired by FSA. This snapshot preserves the referenced version.
download "https://web.archive.org/web/20250319153435id_/https://www.fsa.go.jp/common/law/kaiji/250221_kaiji.pdf" \
  "fsa-corporate-disclosure-guidelines-250221.pdf"
download "https://www.fsa.go.jp/common/law/kaiji/koukaikaitsuke.pdf" \
  "fsa-tob-disclosure-guidelines.pdf"
download "https://www.mhlw.go.jp/content/11120000/000761110.pdf" \
  "mhlw-000761110.pdf"
download "https://www.mhlw.go.jp/file/06-Seisakujouhou-11120000-Iyakushokuhinkyoku/0000179264.pdf" \
  "mhlw-0000179264.pdf"
download "https://www.mlit.go.jp/common/001016469.pdf" \
  "mlit-restoration-guidelines.pdf"

while read -r checksum filename; do
  tmp_manifest="$manifest.tmp"
  jq --arg filename "$filename" --arg checksum "sha256:$checksum" \
    '(.documents[] | select(.file == ("documents/" + $filename))).sha256 = $checksum' \
    "$manifest" > "$tmp_manifest"
  mv "$tmp_manifest" "$manifest"
done < <(cd "$documents_dir" && shasum -a 256 *.pdf)

echo "Downloaded $(find "$documents_dir" -name '*.pdf' -type f | wc -l | tr -d ' ') documents to $target_dir"
echo "Manifest: $manifest"

"""LLM呼出しの固定指示・動的入力・出力契約を一組で保持する。"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from .prompt_assets import PromptAssetTrace
from .state import FrameworkModel

RUNTIME_INPUT_MARKER = "{{runtime_input}}"


class RenderedModelCall(FrameworkModel):
    """API送信と監査成果物が共有する、決定的なModel呼出し表現。"""

    stage: str = Field(min_length=1, max_length=160)
    instructions: str = Field(min_length=1)
    input_payload: dict[str, Any]
    input_tag: str = Field(min_length=1, max_length=160)
    output_schema: dict[str, Any]
    normalized_schema: dict[str, Any]
    request: str = Field(min_length=1)
    prompt_assets: tuple[PromptAssetTrace, ...] = ()

    @model_validator(mode="after")
    def request_must_match_parts(self) -> RenderedModelCall:
        expected = assemble_model_request(
            self.instructions,
            input_tag=self.input_tag,
            input_payload=self.input_payload,
        )
        if self.request != expected:
            raise ValueError("request does not match instructions and input_payload")
        return self

    @property
    def instructions_hash(self) -> str:
        return _text_sha256(self.instructions)

    @property
    def input_hash(self) -> str:
        return _json_sha256(self.input_payload)

    @property
    def output_schema_hash(self) -> str:
        return _json_sha256(self.output_schema)

    @property
    def normalized_schema_hash(self) -> str:
        return _json_sha256(self.normalized_schema)

    @property
    def request_hash(self) -> str:
        return _text_sha256(self.request)


def build_rendered_model_call(
    *,
    stage: str,
    instructions: str,
    input_tag: str,
    input_payload: dict[str, Any],
    output_schema: dict[str, Any],
    normalized_schema: dict[str, Any],
    prompt_assets: tuple[PromptAssetTrace, ...] = (),
) -> RenderedModelCall:
    """固定指示と動的入力から、実送信内容を一度だけ組み立てる。"""

    request = assemble_model_request(
        instructions,
        input_tag=input_tag,
        input_payload=input_payload,
    )
    return RenderedModelCall(
        stage=stage,
        instructions=instructions,
        input_tag=input_tag,
        input_payload=input_payload,
        output_schema=output_schema,
        normalized_schema=normalized_schema,
        request=request,
        prompt_assets=prompt_assets,
    )


def assemble_model_request(
    instructions: str,
    *,
    input_tag: str,
    input_payload: dict[str, Any],
) -> str:
    """固定位置へ実行時入力を挿入し、Providerへ渡す文字列を作る。"""

    if instructions.count(RUNTIME_INPUT_MARKER) != 1:
        raise ValueError("instructions must contain one runtime input marker")
    payload = json.dumps(
        input_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    input_block = f"<{input_tag}>{payload}</{input_tag}>"
    return instructions.replace(RUNTIME_INPUT_MARKER, input_block)


def write_model_call_artifacts(
    rendered: RenderedModelCall,
    output_dir: Path,
    *,
    provider: str,
    profile_name: str,
    profile_version: str,
    model: str,
) -> tuple[Path, ...]:
    """レビュー可能な成果物を出力する。生成物は手編集しない。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    files = model_call_artifact_contents(
        rendered,
        provider=provider,
        profile_name=profile_name,
        profile_version=profile_version,
        model=model,
    )
    written: list[Path] = []
    for name, content in files.items():
        path = output_dir / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return tuple(written)


def model_call_artifact_contents(
    rendered: RenderedModelCall,
    *,
    provider: str,
    profile_name: str,
    profile_version: str,
    model: str,
) -> dict[str, str]:
    """ファイル書込みと差分検査が共有する成果物本文。"""

    files = {
        "instructions.md": rendered.instructions,
        "input.json": _pretty_json(rendered.input_payload),
        "output_schema.json": _pretty_json(rendered.output_schema),
        "normalized_schema.json": _pretty_json(rendered.normalized_schema),
        "request.txt": rendered.request,
        "complete_request.json": _pretty_json(
            {
                "stage": rendered.stage,
                "provider": provider,
                "profileName": profile_name,
                "profileVersion": profile_version,
                "model": model,
                "prompt": rendered.request,
                "outputSchema": rendered.output_schema,
                "normalizedSchema": rendered.normalized_schema,
                "hashes": {
                    "instructions": rendered.instructions_hash,
                    "input": rendered.input_hash,
                    "outputSchema": rendered.output_schema_hash,
                    "normalizedSchema": rendered.normalized_schema_hash,
                    "prompt": rendered.request_hash,
                },
                "promptAssets": list(rendered.prompt_assets),
                "transportNote": (
                    "promptは実際に送信した結合後テキスト。outputSchemaは"
                    "Providerへ別項目として送信した構造化出力契約。"
                ),
            }
        ),
        "manifest.json": _pretty_json(
            {
                "stage": rendered.stage,
                "provider": provider,
                "profileName": profile_name,
                "profileVersion": profile_version,
                "model": model,
                "instructionsHash": rendered.instructions_hash,
                "inputHash": rendered.input_hash,
                "outputSchemaHash": rendered.output_schema_hash,
                "normalizedSchemaHash": rendered.normalized_schema_hash,
                "requestHash": rendered.request_hash,
                "promptAssets": list(rendered.prompt_assets),
            }
        ),
    }
    return {name: content.rstrip() + "\n" for name, content in files.items()}


def _pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _text_sha256(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _json_sha256(value: Any) -> str:
    return _text_sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


__all__ = [
    "RUNTIME_INPUT_MARKER",
    "RenderedModelCall",
    "assemble_model_request",
    "build_rendered_model_call",
    "model_call_artifact_contents",
    "write_model_call_artifacts",
]

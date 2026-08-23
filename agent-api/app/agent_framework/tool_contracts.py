"""Provider非依存のTool能力・入出力説明契約。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, Field

from .state import FrameworkModel


class ToolDefinition(FrameworkModel):
    name: str = Field(
        min_length=1,
        max_length=160,
        description="SolverDecision.tool_requestsで使う正規のTool名。",
    )
    description: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "Toolが何を行い、いつ使い、何を行わないかを説明するLLM向け契約。"
        ),
    )
    input_schema: dict[str, Any] = Field(
        description="Tool argumentsのProvider非依存JSON Schema。",
    )
    result_description: str = Field(
        min_length=1,
        max_length=2000,
        description="ToolResultとEvidenceとして返る情報および制約。",
    )
    read_only: bool = Field(
        default=True,
        description="外部状態を変更しないToolならtrue。",
    )
    parallel_safe: bool = Field(
        default=True,
        description="他のread-only Toolと安全に並列実行できるならtrue。",
    )


def model_input_schema(model_type: type[BaseModel]) -> dict[str, Any]:
    """Pydantic引数型から両Providerで使う小さいstrict schemaを作る。"""

    schema = deepcopy(model_type.model_json_schema())
    definitions = schema.pop("$defs", {})

    def normalize(value: Any) -> Any:
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            key = reference.rsplit("/", 1)[-1]
            target = normalize(deepcopy(definitions[key]))
            return {
                **target,
                **{
                    child_key: normalize(child_value)
                    for child_key, child_value in value.items()
                    if child_key != "$ref"
                },
            }
        normalized = {
            key: normalize(child)
            for key, child in value.items()
            if key not in {"title", "default"}
        }
        if normalized.get("type") == "object":
            properties = normalized.get("properties")
            if isinstance(properties, dict):
                normalized["required"] = list(properties)
                normalized["additionalProperties"] = False
        return normalized

    return normalize(schema)

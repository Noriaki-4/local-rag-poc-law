"""型付き契約からLLM向けの短い用語集を決定的に生成する。"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel

from .context import SolverContext
from .contracts import SolverDecision


def _llm_field_names(model_type: type[BaseModel]) -> tuple[str, ...]:
    return tuple(
        name
        for name, field in model_type.model_fields.items()
        if field.exclude is not True
    )


_LLM_VISIBLE_FIELDS: tuple[tuple[str, type[BaseModel], tuple[str, ...]], ...] = (
    ("SolverContext", SolverContext, _llm_field_names(SolverContext)),
    ("SolverDecision", SolverDecision, _llm_field_names(SolverDecision)),
)


@lru_cache(maxsize=1)
def render_solver_contract_glossary() -> str:
    """現在の入出力の入口だけを、Field.descriptionから生成する。"""

    lines = [
        "<contract_glossary>",
        "以下は正規契約の入口となる項目名と意味です。入れ子の出力項目はProvider schemaのdescriptionを正本とします。",
    ]
    for model_name, model_type, field_names in _LLM_VISIBLE_FIELDS:
        lines.append(f"### {model_name}")
        for field_name in field_names:
            field = model_type.model_fields[field_name]
            description = (field.description or "").strip()
            if not description:
                raise ValueError(
                    f"LLM-visible contract field lacks description: "
                    f"{model_name}.{field_name}"
                )
            lines.append(f"- `{model_name}.{field_name}`: {description}")
    lines.append("</contract_glossary>")
    return "\n".join(lines)


def contract_field_description(
    model_type: type[BaseModel],
    field_name: str,
) -> str:
    """輸送Schemaも同じField.descriptionを利用するための入口。"""

    description = (model_type.model_fields[field_name].description or "").strip()
    if not description:
        raise ValueError(
            f"contract field lacks description: {model_type.__name__}.{field_name}"
        )
    return description

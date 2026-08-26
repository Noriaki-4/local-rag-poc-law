"""型付き契約からLLM向けの短い用語集を決定的に生成する。"""

from __future__ import annotations

from functools import lru_cache
from typing import get_args, get_origin

from pydantic import BaseModel

from .context import ResearchStepInput, SolverContext
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


@lru_cache(maxsize=16)
def render_solver_contract_glossary(
    context_field_names: tuple[str, ...] | None = None,
    decision_field_names: tuple[str, ...] | None = None,
) -> str:
    """今回のProvider入出力にある入口だけをField.descriptionから生成する。"""

    lines = [
        "<contract_glossary>",
        "以下は正規契約の入口となる項目名と意味です。入れ子の出力項目はProvider schemaのdescriptionを正本とします。",
    ]
    requested_fields = {
        "SolverContext": context_field_names,
        "SolverDecision": decision_field_names,
    }
    for model_name, model_type, all_field_names in _LLM_VISIBLE_FIELDS:
        requested = requested_fields[model_name]
        field_names = (
            all_field_names
            if requested is None
            else tuple(name for name in all_field_names if name in requested)
        )
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


@lru_cache(maxsize=16)
def render_model_input_glossary(model_type: type[BaseModel]) -> str:
    """用途別read modelの入力契約をField.descriptionから生成する。"""

    lines = [
        "<input_contract>",
        "以下は今回の入力項目と意味です。",
    ]
    _append_model_fields(lines, model_type)
    lines.append("</input_contract>")
    return "\n".join(lines)


def _append_model_fields(
    lines: list[str],
    model_type: type[BaseModel],
    *,
    prefix: str = "",
    indent: str = "",
) -> None:
    for field_name, field in model_type.model_fields.items():
        description = (field.description or "").strip()
        if not description:
            raise ValueError(
                f"LLM-visible input field lacks description: "
                f"{model_type.__name__}.{field_name}"
            )
        path = f"{prefix}.{field_name}" if prefix else field_name
        is_collection = get_origin(field.annotation) in {list, tuple}
        item_type = _collection_item_model(field.annotation)
        display_path = f"{path}[]" if is_collection else path
        lines.append(f"{indent}- `{display_path}`: {description}")
        if item_type is not None:
            _append_model_fields(
                lines,
                item_type,
                prefix=display_path,
                indent=f"{indent}  ",
            )


@lru_cache(maxsize=8)
def render_research_step_input_glossary(
    field_names: tuple[str, ...],
    collection_item_fields: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> str:
    """初回Researchの投影入力にある項目だけを説明する。"""

    lines = [
        "<input_contract>",
        "以下は今回の入力項目と意味です。",
    ]
    selected_collection_fields = dict(collection_item_fields)
    for field_name in field_names:
        field = ResearchStepInput.model_fields.get(field_name)
        if field is None:
            raise ValueError(f"unknown ResearchStepInput field: {field_name}")
        description = (field.description or "").strip()
        if not description:
            raise ValueError(
                f"LLM-visible input field lacks description: {field_name}"
            )
        lines.append(f"- `{field_name}`: {description}")
        item_type = _collection_item_model(field.annotation)
        if item_type is not None:
            selected_names = selected_collection_fields.get(field_name)
            item_fields = (
                item_type.model_fields.items()
                if selected_names is None
                else (
                    (name, item_type.model_fields[name])
                    for name in selected_names
                )
            )
            for item_name, item_field in item_fields:
                item_description = (item_field.description or "").strip()
                if not item_description:
                    raise ValueError(
                        "LLM-visible input item field lacks description: "
                        f"{field_name}[].{item_name}"
                    )
                lines.append(
                    f"  - `{field_name}[].{item_name}`: {item_description}"
                )
    lines.append("</input_contract>")
    return "\n".join(lines)


def _collection_item_model(annotation: object) -> type[BaseModel] | None:
    if get_origin(annotation) not in {list, tuple}:
        return None
    arguments = get_args(annotation)
    if not arguments:
        return None
    item_type = arguments[0]
    if isinstance(item_type, type) and issubclass(item_type, BaseModel):
        return item_type
    return None

"""人間が読めるPrompt assetを検証付きで読み込む。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import cache
from hashlib import sha256
from pathlib import Path
from string import Template
from typing import TypedDict

_PROMPT_DIR = Path(__file__).with_name("prompts")
_SECTION_PATTERN = re.compile(
    r"^<!-- prompt-section:([a-z0-9_]+) -->\s*$\n"
    r"(.*?)"
    r"(?=^<!-- prompt-section:[a-z0-9_]+ -->\s*$|\Z)",
    re.MULTILINE | re.DOTALL,
)


class PromptAssetError(RuntimeError):
    """Prompt assetの欠落・構造不正・変数不正。"""


class PromptSectionTrace(TypedDict):
    """Prompt asset内で実際に参照したsectionの来歴。"""

    name: str
    sha256: str


class PromptAssetTrace(TypedDict):
    """LLM入力を構成したPrompt assetの来歴。"""

    asset: str
    sha256: str
    sections: list[PromptSectionTrace]


@cache
def _prompt_asset_content(name: str) -> str:
    path = _PROMPT_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptAssetError(f"prompt asset is unavailable: {path}") from exc


@cache
def prompt_sections(name: str) -> Mapping[str, str]:
    path = _PROMPT_DIR / name
    content = _prompt_asset_content(name)

    sections: dict[str, str] = {}
    for match in _SECTION_PATTERN.finditer(content):
        section_name = match.group(1)
        if section_name in sections:
            raise PromptAssetError(f"duplicate prompt section: {name}#{section_name}")
        sections[section_name] = match.group(2).strip()
    if not sections:
        raise PromptAssetError(f"prompt asset has no sections: {path}")
    return sections


def prompt_asset_trace(
    name: str,
    section_names: tuple[str, ...],
) -> PromptAssetTrace:
    """選択したsectionとasset原文のhashを、本文を複製せず返す。"""

    sections = prompt_sections(name)
    selected: list[PromptSectionTrace] = []
    for section_name in dict.fromkeys(section_names):
        try:
            source = sections[section_name]
        except KeyError as exc:
            raise PromptAssetError(
                f"prompt section is unavailable: {name}#{section_name}"
            ) from exc
        selected.append(
            {
                "name": section_name,
                "sha256": _text_sha256(source),
            }
        )
    return {
        "asset": f"agent_framework/prompts/{name}",
        "sha256": _text_sha256(_prompt_asset_content(name)),
        "sections": selected,
    }


def render_prompt_section(
    name: str,
    section_name: str,
    values: Mapping[str, object] | None = None,
) -> str:
    try:
        source = prompt_sections(name)[section_name]
    except KeyError as exc:
        raise PromptAssetError(
            f"prompt section is unavailable: {name}#{section_name}"
        ) from exc
    try:
        return Template(source).substitute(
            {key: str(value) for key, value in (values or {}).items()}
        )
    except (KeyError, ValueError) as exc:
        raise PromptAssetError(
            f"prompt section variables are invalid: {name}#{section_name}"
        ) from exc


def _text_sha256(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()

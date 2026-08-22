"""人間が読めるPrompt assetを検証付きで読み込む。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from string import Template

_PROMPT_DIR = Path(__file__).with_name("prompts")
_SECTION_PATTERN = re.compile(
    r"^<!-- prompt-section:([a-z0-9_]+) -->\s*$\n"
    r"(.*?)"
    r"(?=^<!-- prompt-section:[a-z0-9_]+ -->\s*$|\Z)",
    re.MULTILINE | re.DOTALL,
)


class PromptAssetError(RuntimeError):
    """Prompt assetの欠落・構造不正・変数不正。"""


@cache
def prompt_sections(name: str) -> Mapping[str, str]:
    path = _PROMPT_DIR / name
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptAssetError(f"prompt asset is unavailable: {path}") from exc

    sections: dict[str, str] = {}
    for match in _SECTION_PATTERN.finditer(content):
        section_name = match.group(1)
        if section_name in sections:
            raise PromptAssetError(
                f"duplicate prompt section: {name}#{section_name}"
            )
        sections[section_name] = match.group(2).strip()
    if not sections:
        raise PromptAssetError(f"prompt asset has no sections: {path}")
    return sections


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

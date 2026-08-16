"""Caseの正本から、LLM向けの再現可能な表示を決定的に作る。"""

from dataclasses import dataclass
from typing import Any

from .projection_policy import ProjectionPolicy


@dataclass(frozen=True)
class MaterialItem:
    item_id: str
    rendered: str


@dataclass(frozen=True)
class MaterialProjection:
    text: str
    manifest: dict[str, Any]


class Projector:
    """入力順とPolicyだけでMaterialを表示し、意味的な採否を行わない。"""

    def project_material(
        self,
        items: list[MaterialItem],
        policy: ProjectionPolicy,
        *,
        cursor: int = 0,
    ) -> MaterialProjection:
        if cursor < 0 or cursor > len(items):
            raise ValueError("cursor is outside material collection")
        page = items[cursor : cursor + policy.material_max_items]
        per_item_budget = max(1, policy.material_max_chars // max(1, len(page)))
        blocks: list[str] = []
        remaining = policy.material_max_chars
        truncated = 0
        truncated_item_ids: list[str] = []
        shown_item_ids: list[str] = []
        for item in page:
            separator_chars = 2 if blocks else 0
            allowed = min(per_item_budget, max(0, remaining - separator_chars))
            rendered = item.rendered[:allowed]
            if len(rendered) < len(item.rendered):
                truncated += 1
                truncated_item_ids.append(item.item_id)
            if not rendered:
                break
            if blocks:
                remaining -= 2
            blocks.append(rendered)
            shown_item_ids.append(item.item_id)
            remaining -= len(rendered)

        shown = len(blocks)
        next_cursor = cursor + shown
        omitted = len(items) - next_cursor
        text = "\n\n".join(blocks) if blocks else "引用候補なし"
        return MaterialProjection(
            text=text,
            manifest={
                "totalItems": len(items),
                "shownItems": shown,
                "omittedItems": max(0, omitted),
                "cursor": cursor,
                "nextCursor": next_cursor if omitted > 0 else None,
                "truncatedItemCount": truncated,
                "truncatedItemIds": truncated_item_ids,
                "shownItemIds": shown_item_ids,
                "omittedItemIds": [item.item_id for item in items[next_cursor:]],
                "originalChars": sum(len(item.rendered) for item in items),
                "includedChars": len(text) if blocks else 0,
                "complete": omitted <= 0 and truncated == 0,
            },
        )

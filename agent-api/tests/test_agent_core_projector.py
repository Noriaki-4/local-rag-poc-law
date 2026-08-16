from app.agent_core import MaterialItem, ProjectionPolicy, Projector


def test_projector_preserves_input_order_and_reports_omissions() -> None:
    items = [
        MaterialItem(item_id=f"evidence-{index}", rendered=f"evidence-{index}:本文")
        for index in range(1, 4)
    ]

    projection = Projector().project_material(
        items,
        ProjectionPolicy(material_max_items=2, material_max_chars=1000),
    )

    assert projection.text.index("evidence-1") < projection.text.index("evidence-2")
    assert "evidence-3" not in projection.text
    assert projection.manifest["totalItems"] == 3
    assert projection.manifest["shownItems"] == 2
    assert projection.manifest["omittedItems"] == 1
    assert projection.manifest["nextCursor"] == 2
    assert projection.manifest["complete"] is False
    assert projection.manifest["shownItemIds"] == [
        "evidence-1",
        "evidence-2",
    ]
    assert projection.manifest["omittedItemIds"] == ["evidence-3"]


def test_projector_pages_from_explicit_cursor_without_reranking() -> None:
    items = [
        MaterialItem(item_id=f"evidence-{index}", rendered=f"evidence-{index}")
        for index in range(1, 4)
    ]

    projection = Projector().project_material(
        items,
        ProjectionPolicy(material_max_items=2, material_max_chars=1000),
        cursor=2,
    )

    assert projection.text == "evidence-3"
    assert projection.manifest["cursor"] == 2
    assert projection.manifest["nextCursor"] is None
    assert projection.manifest["complete"] is True


def test_projector_reports_exact_truncated_item_ids() -> None:
    projection = Projector().project_material(
        [
            MaterialItem(item_id="evidence-1", rendered="本文" * 100),
            MaterialItem(item_id="evidence-2", rendered="短文"),
        ],
        ProjectionPolicy(material_max_items=2, material_max_chars=20),
    )

    assert projection.manifest["truncatedItemIds"] == ["evidence-1"]

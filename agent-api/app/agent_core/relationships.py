"""WorkItemの親子・実行依存を決定的に検証する。"""

from collections.abc import Iterable, Mapping

from .models import WorkItem, WorkItemDependency


class RelationshipError(ValueError):
    """参照不整合または循環を示す。"""


def validate_work_item_relationships(
    work_items: Mapping[str, WorkItem],
    dependencies: Iterable[WorkItemDependency],
) -> None:
    _validate_parents(work_items)
    _validate_dependencies(work_items, dependencies)


def _validate_parents(work_items: Mapping[str, WorkItem]) -> None:
    parents: dict[str, str] = {}
    for item in work_items.values():
        parent_id = item.parent_work_item_id
        if parent_id is None:
            continue
        parent = work_items.get(parent_id)
        if parent is None:
            raise RelationshipError(f"unknown parent work item: {parent_id}")
        if parent.case_id != item.case_id:
            raise RelationshipError("parent work item belongs to another case")
        parents[item.work_item_id] = parent_id

    _reject_cycles(parents, "parent-child")


def _validate_dependencies(
    work_items: Mapping[str, WorkItem],
    dependencies: Iterable[WorkItemDependency],
) -> None:
    edges: dict[str, list[str]] = {}
    seen_pairs: set[tuple[str, str]] = set()
    for dependency in dependencies:
        dependent = work_items.get(dependency.dependent_work_item_id)
        prerequisite = work_items.get(dependency.prerequisite_work_item_id)
        if dependent is None or prerequisite is None:
            raise RelationshipError("dependency references an unknown work item")
        if (
            dependent.case_id != dependency.case_id
            or prerequisite.case_id != dependency.case_id
        ):
            raise RelationshipError("dependency crosses case boundary")
        pair = (dependent.work_item_id, prerequisite.work_item_id)
        if pair in seen_pairs:
            raise RelationshipError("duplicate work item dependency")
        seen_pairs.add(pair)
        edges.setdefault(dependent.work_item_id, []).append(prerequisite.work_item_id)

    _reject_graph_cycles(edges, "work item dependency")


def _reject_cycles(parents: Mapping[str, str], label: str) -> None:
    edges = {node: [parent] for node, parent in parents.items()}
    _reject_graph_cycles(edges, label)


def _reject_graph_cycles(edges: Mapping[str, list[str]], label: str) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise RelationshipError(f"{label} cycle detected")
        if node in visited:
            return
        visiting.add(node)
        for target in edges.get(node, []):
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for node in edges:
        visit(node)

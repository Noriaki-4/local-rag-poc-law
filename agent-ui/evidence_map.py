import re
from collections import OrderedDict
from typing import Any


def citation_label(citation: dict[str, Any]) -> str:
    """引用の見出しラベルを組み立てる。

    ガイドラインPDFの heading は seed 時点で「タイトル p.N」形式になっているため、
    そのまま title と連結するとタイトルが二重に並ぶ。重複していれば heading を使う。
    """
    title = str(citation.get("title") or "").strip()
    heading = str(citation.get("heading") or "").strip()
    if heading and title and heading.startswith(title):
        return heading
    label = " ".join(part for part in (title, heading) if part)
    return label or str(citation.get("contentUnitId") or "引用")


def humanize_citation_ids(answer: str, citations: list[dict[str, Any]]) -> str:
    """回答本文中の contentUnitId を、人が読める条文名へ置き換える。

    引用一覧に無いIDはそのまま残す（回答が示した根拠を勝手に消さないため）。
    """
    if not answer:
        return answer
    labels = {
        str(citation.get("contentUnitId")): citation_label(citation)
        for citation in citations
        if citation.get("contentUnitId")
    }
    if not labels:
        return answer
    # 長いIDを先に並べないと、項付きIDが条レベルのIDに先食いされる。
    pattern = re.compile(
        "|".join(re.escape(content_unit_id) for content_unit_id in sorted(labels, key=len, reverse=True))
    )
    humanized = pattern.sub(lambda match: labels[match.group(0)], answer)
    return _normalize_citation_parentheses(humanized, labels.values())


def _normalize_citation_parentheses(text: str, labels: Any) -> str:
    """置換後に半角括弧で囲まれた引用ラベルを全角に揃え、日本語文中で読みやすくする。"""
    normalized = text
    for label in labels:
        normalized = normalized.replace(f"({label})", f"（{label}）")
    return normalized


def group_citations(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """引用を文書単位にまとめ、法令横断の表示に必要な最小情報を返す。"""
    groups: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for citation in citations:
        document_id = str(citation.get("documentId") or "")
        title = str(citation.get("title") or document_id or "名称不明の資料")
        key = document_id or title
        group = groups.setdefault(
            key,
            {
                "documentId": document_id,
                "title": title,
                "kind": _source_kind(document_id),
                "citationCount": 0,
                "headings": [],
            },
        )
        group["citationCount"] += 1
        heading = citation.get("heading")
        if heading and heading not in group["headings"]:
            group["headings"].append(str(heading))
    return list(groups.values())


def build_evidence_dot(
    question: str,
    citations: list[dict[str, Any]],
    graph_paths: list[dict[str, Any]] | None = None,
) -> str | None:
    """回答根拠の文書関係を示すGraphviz DOTを生成する。"""
    groups = group_citations(citations)
    if not groups:
        return None

    node_by_document_id = {
        group["documentId"]: f"source_{index}"
        for index, group in enumerate(groups)
        if group["documentId"]
    }
    question_label = _dot_escape(f"質問\n{_shorten(question, 46)}")
    lines = [
        "digraph evidence_map {",
        '  graph [rankdir="LR", bgcolor="transparent", pad="0.2", nodesep="0.35", ranksep="0.75"];',
        '  node [shape="box", style="rounded,filled", fontname="sans-serif", color="#64748b", margin="0.16"];',
        '  edge [fontname="sans-serif", fontsize="10", color="#64748b"];',
        f'  question [label="{question_label}", '
        'fillcolor="#fff7d6", color="#d4a72c"];',
    ]

    for index, group in enumerate(groups):
        node_id = f"source_{index}"
        kind_label = {"law": "法令", "guidance": "ガイドライン", "other": "資料"}[group["kind"]]
        label = f'{kind_label}: {group["title"]}\n引用 {group["citationCount"]}件'
        fill_color = {
            "law": "#e8f1ff",
            "guidance": "#eaf7ed",
            "other": "#f1f5f9",
        }[group["kind"]]
        lines.append(
            f'  {node_id} [label="{_dot_escape(label)}", fillcolor="{fill_color}"];'
        )
        lines.append(
            f'  question -> {node_id} [label="回答根拠", color="#94a3b8"];'
        )

    relations = _graph_relations(graph_paths or [], node_by_document_id)
    relation_pairs = {(source, target) for source, target, _, _ in relations}

    for left_index, left in enumerate(groups):
        if left["kind"] != "law":
            continue
        for right_index, right in enumerate(groups):
            if right_index <= left_index or right["kind"] != "law":
                continue
            relation = _title_relation(left["title"], right["title"])
            if relation:
                source_index, target_index, label = relation
                source = f"source_{left_index if source_index == 0 else right_index}"
                target = f"source_{right_index if target_index == 1 else left_index}"
                if (source, target) not in relation_pairs:
                    # グラフで確認した関係ではなく、法令名の命名規則からの推定。
                    relations.append((source, target, f"{label}（名称から推定）", "inferred"))
                    relation_pairs.add((source, target))

    law_nodes = [
        f"source_{index}"
        for index, group in enumerate(groups)
        if group["kind"] == "law"
    ]
    connected_undirected = {
        frozenset((source, target)) for source, target, _, _ in relations
    }
    for source, target in zip(law_nodes, law_nodes[1:]):
        pair = frozenset((source, target))
        if pair not in connected_undirected:
            relations.append((source, target, "この回答で併せて参照", "co_cited"))
            connected_undirected.add(pair)

    for source, target, label, relation_kind in relations:
        if relation_kind == "co_cited":
            lines.append(
                f'  {source} -> {target} [label="{_dot_escape(label)}", '
                'style="dashed", dir="none", color="#94a3b8", fontcolor="#64748b"];'
            )
        elif relation_kind == "inferred":
            lines.append(
                f'  {source} -> {target} [label="{_dot_escape(label)}", '
                'style="dotted", color="#2563eb", fontcolor="#1d4ed8"];'
            )
        else:
            lines.append(
                f'  {source} -> {target} [label="{_dot_escape(label)}", '
                'color="#2563eb", fontcolor="#1d4ed8"];'
            )

    lines.append("}")
    return "\n".join(lines)


def _graph_relations(
    graph_paths: list[dict[str, Any]],
    node_by_document_id: dict[str, str],
) -> list[tuple[str, str, str, str]]:
    labels = {
        "REFERENCES": "条文から参照",
        "EXPLAINS": "内容を解説",
    }
    relations: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in graph_paths:
        nodes = path.get("nodes") or []
        edges = path.get("edges") or []
        for index, edge in enumerate(edges):
            if index + 1 >= len(nodes):
                continue
            source_document_id = nodes[index].get("documentId")
            target_document_id = nodes[index + 1].get("documentId")
            edge_type = edge.get("edgeType")
            if (
                not source_document_id
                or not target_document_id
                or source_document_id == target_document_id
                or edge_type not in labels
                or source_document_id not in node_by_document_id
                or target_document_id not in node_by_document_id
            ):
                continue
            source = node_by_document_id[source_document_id]
            target = node_by_document_id[target_document_id]
            key = (source, target, edge_type)
            if key not in seen:
                relations.append((source, target, labels[edge_type], "formal"))
                seen.add(key)
    return relations


def _title_relation(left: str, right: str) -> tuple[int, int, str] | None:
    for parent_index, parent, child_index, child in (
        (0, left, 1, right),
        (1, right, 0, left),
    ):
        if child == f"{parent}施行令":
            return parent_index, child_index, "施行内容を具体化"
        if child == f"{parent}施行規則":
            return parent_index, child_index, "実施方法を具体化"
        if (
            child.startswith(parent)
            and child != parent
            and child.endswith(("内閣府令", "府令", "省令", "規則"))
        ):
            return parent_index, child_index, "委任事項を具体化"
    return None


def _source_kind(document_id: str) -> str:
    if document_id.startswith("law-"):
        return "law"
    if document_id.startswith("guidance-"):
        return "guidance"
    return "other"


def _shorten(value: str, max_chars: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[: max_chars - 1]}…"


def _dot_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

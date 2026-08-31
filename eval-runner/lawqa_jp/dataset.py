"""lawqa_jpを汎用Answer APIへ接続するリポジトリ固有adapter。"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests


CHOICE_LINE_PATTERN = re.compile(r"^([a-dA-D])[\s\u3000]+(.+)$")
EGOV_LAW_ID_PATTERN = re.compile(r"laws\.e-gov\.go\.jp/law/([^/?#]+)")
CONTEXT_HEADER_PATTERN = re.compile(r"^(#{2,5})\s+(.+?)\s*$")
ARTICLE_HEADER_PATTERN = re.compile(r"^第(\d+)条((?:の\d+)*)")
PARAGRAPH_HEADER_PATTERN = re.compile(r"^第(\d+)項$")
ITEM_HEADER_PATTERN = re.compile(r"^第(\d+)号$")
FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


class LawQADataset:
    """Dataset固有情報をAgent APIへ送らず、評価側だけで保持する。"""

    def __init__(
        self,
        *,
        samples_dir: Path,
        eval_path: str | None,
        eval_url: str | None,
        egov_api_base_url: str,
    ) -> None:
        self.samples_dir = samples_dir
        self.eval_path = eval_path
        self.eval_url = eval_url
        self.egov_api_base_url = egov_api_base_url.rstrip("/")
        registry_path = samples_dir / "eval" / "law_registry.json"
        self.registry = (
            json.loads(registry_path.read_text(encoding="utf-8"))
            if registry_path.exists()
            else {"laws": []}
        )
        issues_path = samples_dir / "eval" / "lawqa_known_issues.json"
        self.known_issues = (
            json.loads(issues_path.read_text(encoding="utf-8")).get("issues", {})
            if issues_path.exists()
            else {}
        )
        self.known_law_ids = tuple(
            str(item["lawId"]) for item in self.registry["laws"]
        )
        self.family_roots = {
            str(item["lawId"]): str(item.get("familyRoot") or item["lawId"])
            for item in self.registry["laws"]
        }
        self._egov_title_cache: dict[str, str | None] = {}

    def load(self, *, timeout_sec: int) -> tuple[list[dict[str, Any]], str]:
        if self.eval_url:
            response = requests.get(self.eval_url, timeout=timeout_sec)
            response.raise_for_status()
            return self.normalize_payload(response.json()), self.eval_url

        path = (
            Path(self.eval_path)
            if self.eval_path
            else self.samples_dir / "eval" / "lawqa_eval_item.sample.jsonl"
        )
        if not path.exists():
            raise FileNotFoundError(f"lawqa eval file not found: {path}")
        if path.suffix == ".jsonl":
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            return [
                self.normalize_internal_row(row, index)
                for index, row in enumerate(rows, start=1)
            ], str(path)
        return self.normalize_payload(
            json.loads(path.read_text(encoding="utf-8"))
        ), str(path)

    def normalize_payload(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict) and isinstance(payload.get("samples"), list):
            samples = payload["samples"]
        elif isinstance(payload, list):
            samples = payload
        else:
            raise ValueError(
                "Unsupported lawqa_jp JSON format. Expected list or object with samples list."
            )
        return [
            self.normalize_sample(sample, index)
            for index, sample in enumerate(samples, start=1)
        ]

    @staticmethod
    def normalize_internal_row(row: dict[str, Any], index: int) -> dict[str, Any]:
        return {
            **row,
            "questionId": row.get("questionId") or f"lawqa-{index:04d}",
            "choices": {
                label.upper(): text for label, text in row["choices"].items()
            },
            "goldAnswer": str(row["goldAnswer"]).upper(),
        }

    def normalize_sample(
        self,
        sample: dict[str, Any],
        index: int,
    ) -> dict[str, Any]:
        filename = str(sample.get("ファイル名") or f"lawqa-{index:04d}")
        order = sample.get("回答オーダーマップ番号")
        question_id = filename if order is None else f"{filename}-{order}"
        references = [
            self._reference_from_url(url) for url in sample.get("references", [])
        ]
        law_ids = {ref["lawId"] for ref in references if ref.get("lawId")}
        references.extend(
            self._context_expected_references(
                str(sample.get("コンテキスト") or ""),
                law_ids | set(self.known_law_ids),
            )
        )
        return {
            "questionId": question_id,
            "question": str(sample["問題文"]),
            "choices": self.parse_choices(str(sample["選択肢"])),
            "goldAnswer": str(sample["output"]).upper(),
            "expectedReferences": references,
            "notes": (
                "Converted from native lawqa_jp JSON. "
                "Gold and context are retained by the evaluator only."
            ),
        }

    @staticmethod
    def parse_choices(raw_choices: str) -> dict[str, str]:
        choices: dict[str, list[str]] = {}
        current_label: str | None = None
        for raw_line in raw_choices.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = CHOICE_LINE_PATTERN.match(line)
            if match:
                current_label = match.group(1).upper()
                choices[current_label] = [match.group(2).strip()]
            elif current_label:
                choices[current_label].append(line)
        normalized = {
            label: "\n".join(parts).strip() for label, parts in choices.items()
        }
        missing = set("ABCD") - set(normalized)
        if missing:
            raise ValueError(
                f"Missing choices {sorted(missing)} in lawqa_jp row: "
                f"{raw_choices[:120]}"
            )
        return {label: normalized[label] for label in sorted(normalized)}

    def family_of(self, document_id: str) -> str:
        law_id = str(document_id).removeprefix("law-")
        return f"law-{self.family_roots.get(law_id, law_id)}"

    @staticmethod
    def _reference_from_url(url: str) -> dict[str, str]:
        reference = {"url": url}
        match = EGOV_LAW_ID_PATTERN.search(url)
        if match:
            reference["lawId"] = match.group(1)
        return reference

    def _egov_title(self, law_id: str) -> str | None:
        if law_id in self._egov_title_cache:
            return self._egov_title_cache[law_id]
        title: str | None = None
        try:
            response = requests.get(
                f"{self.egov_api_base_url}/lawdata/{law_id}",
                timeout=60,
            )
            response.raise_for_status()
            title = ET.fromstring(response.content).findtext(".//LawTitle")
        except Exception:
            title = None
        self._egov_title_cache[law_id] = title
        return title

    @staticmethod
    def _article_suffix(header: str) -> str | None:
        match = ARTICLE_HEADER_PATTERN.match(header.translate(FULLWIDTH_DIGITS))
        if not match:
            return None
        parts = [match.group(1), *re.findall(r"の(\d+)", match.group(2))]
        return "_".join(parts)

    @staticmethod
    def _pure_num(header: str, pattern: re.Pattern[str]) -> int | None:
        match = pattern.match(header.translate(FULLWIDTH_DIGITS))
        return int(match.group(1)) if match else None

    def _context_expected_references(
        self,
        context: str,
        law_ids: set[str],
    ) -> list[dict[str, str]]:
        title_to_law_id: dict[str, str] = {}
        registry_by_id = {
            str(item["lawId"]): item for item in self.registry["laws"]
        }
        for law_id in law_ids:
            registry_item = registry_by_id.get(law_id)
            registered_titles = (
                [registry_item.get("title"), *registry_item.get("aliases", [])]
                if registry_item
                else []
            )
            for title in registered_titles:
                if title:
                    title_to_law_id.setdefault(str(title), law_id)
            if not registered_titles and (title := self._egov_title(law_id)):
                title_to_law_id.setdefault(title, law_id)

        references: list[dict[str, str]] = []
        seen: set[str] = set()
        current_law_id: str | None = None
        current_article: str | None = None
        current_paragraph: int | None = None
        for line in context.splitlines():
            header = CONTEXT_HEADER_PATTERN.match(line)
            if not header:
                continue
            level = len(header.group(1))
            text = header.group(2).strip()
            if level == 2:
                current_law_id = title_to_law_id.get(text)
                current_article = None
                current_paragraph = None
            elif level == 3 and current_law_id:
                current_article = self._article_suffix(text)
                current_paragraph = None
                if current_article:
                    self._add_reference(
                        references,
                        seen,
                        current_law_id,
                        f"law-{current_law_id}-article-{current_article}",
                    )
            elif level == 4 and current_law_id and current_article:
                current_paragraph = self._pure_num(text, PARAGRAPH_HEADER_PATTERN)
                if current_paragraph is not None:
                    self._add_reference(
                        references,
                        seen,
                        current_law_id,
                        f"law-{current_law_id}-article-{current_article}"
                        f"-paragraph-{current_paragraph}",
                    )
            elif (
                level == 5
                and current_law_id
                and current_article
                and current_paragraph is not None
            ):
                item_num = self._pure_num(text, ITEM_HEADER_PATTERN)
                if item_num is not None:
                    self._add_reference(
                        references,
                        seen,
                        current_law_id,
                        f"law-{current_law_id}-article-{current_article}"
                        f"-paragraph-{current_paragraph}-item-{item_num}",
                    )
        return references

    @staticmethod
    def _add_reference(
        references: list[dict[str, str]],
        seen: set[str],
        law_id: str,
        content_unit_id: str,
    ) -> None:
        if content_unit_id in seen:
            return
        seen.add(content_unit_id)
        references.append({"lawId": law_id, "contentUnitId": content_unit_id})

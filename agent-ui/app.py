import os

import requests
import streamlit as st

from evidence_map import (
    build_evidence_dot,
    citation_label,
    group_citations,
    humanize_citation_ids,
)
from example_questions import (
    LEVELS,
    evaluate_example,
    examples_by_level,
    find_example,
)


API_URL = os.getenv("AGENT_API_URL", "http://localhost:8000").rstrip("/")
DEFAULT_PATTERN = "pattern_4_deepsearch"

st.set_page_config(page_title="法令RAG 質問デモ", layout="wide")
st.title("法令RAG 質問デモ")
st.caption(
    "複数の法令・ガイドラインを横断検索して質問へ回答し、根拠のつながりを図で示します。"
    " 質問のコツや詳しい手順は docs/USER_GUIDE.md を参照してください。"
)

# 回答できる範囲はデフォルトで見えるよう、本文上部に常時表示する。
with st.expander("📚 回答できる範囲（対応している法令・ガイドライン）", expanded=True):
    st.markdown(
        "質問は**下記の投入済み範囲**に限られます。範囲外を聞くと引用条文が見つからないか、"
        "確度の低い回答になります。\n\n"
        "| 分野 | 対応している法令・資料 | 注意 |\n"
        "|---|---|---|\n"
        "| 借地借家・賃貸借 | 借地借家法、民法（賃貸借） | **民法は賃貸借（第601〜622条の2）のみ**。総則・物権・相続などは対象外 |\n"
        "| 金融商品取引法 | 金商法本体・施行令・関連府省令、監督指針/開示/公開買付けの各ガイドライン | 全条対応 |\n"
        "| 薬機法 | 医薬品医療機器等法・施行規則、法令遵守ガイドラインQ&A・適正広告基準 | 全条対応 |\n"
    )

if "question_text" not in st.session_state:
    st.session_state.question_text = ""

with st.expander(f"🧭 法令横断の質問例（Lv.1〜Lv.{LEVELS[-1].level}）", expanded=True):
    st.caption(
        "すべて複数の資料を横断する質問です。難易度は論点の多さではなく、"
        "**横断する資料の構造**（何階層の法令まで、ガイドラインまで見る必要があるか）で分けています。"
        " 質問文には**参照先の法令名をあえて書いていません**。"
        "「想定する参照先」は答え合わせ用で、そこへ自力でたどり着けるかを確認してください。"
    )
    for level, examples in examples_by_level():
        st.markdown(f"**Lv.{level.level} {level.name}** — {level.criteria}")
        st.markdown(
            "\n".join(
                [
                    "| テーマ | 質問例 | 想定する参照先 | 法令時点 |",
                    "|---|---|---|---|",
                    *[
                        f"| {example.title} | {example.question} | {example.expected} | "
                        f"{example.legal_as_of} |"
                        for example in examples
                    ],
                ]
            )
        )
    st.caption("試したい質問文を一覧からコピーし、下の入力欄へ貼り付けてください。")

with st.sidebar:
    with st.expander("環境チェック（開発者向け）", expanded=False):
        if st.button("Health check"):
            try:
                st.json(requests.get(f"{API_URL}/health", timeout=5).json())
            except requests.RequestException as exc:
                st.error(str(exc))

question = st.text_area(
    "質問を入力してください",
    key="question_text",
    height=110,
    help="自然な日本語で質問できます。「根拠条文も示して」と付けると引用条文が明確になります。",
)

with st.expander("詳細設定（開発者向け・通常は変更不要）", expanded=False):
    pattern = st.selectbox(
        "Pattern",
        [
            "pattern_1_baseline_rag",
            "pattern_2_rule_based_agentic_rag",
            "pattern_3_controlled_agentic_rag",
            "pattern_4_deepsearch_partial",
            "pattern_4_deepsearch",
        ],
        index=4,
        help="探索の深さ。既定は最終系の pattern_4_deepsearch。",
    )
    use_choices = st.checkbox(
        "選択式（lawqa_jp 4択スタイル）で質問する",
        help="デジタル庁データセットのような4択問題を試す場合にオン。",
    )
    choices = None
    if use_choices:
        cols = st.columns(4)
        choices = {
            "A": cols[0].text_input("A", value=""),
            "B": cols[1].text_input("B", value=""),
            "C": cols[2].text_input("C", value=""),
            "D": cols[3].text_input("D", value=""),
        }
    user_clearance = st.slider("User clearance level", min_value=1, max_value=3, value=2)
    st.caption(
        "検索は「候補を広く集める→絞る→LLMへ渡す→引用として見せる」の順で件数が絞られます: "
        "**Candidate Top K →（融合・再ランク）→ Rerank Top K → Top K**。"
    )
    top_k = st.slider(
        "Top K（回答の根拠として引用する条文・資料の最大件数）",
        min_value=1,
        max_value=20,
        value=5,
        help="回答の根拠として最終的に引用・表示する件数の上限。既定5件。"
        "増やすと根拠を広く出せるが関連の薄い条文も混ざりやすく、減らすと主要な根拠に絞られる。"
        "回答本文が挙げた条文は、この上限を超えても引用一覧に表示される。",
    )
    candidate_top_k = st.slider(
        "Candidate Top K（1クエリあたりの検索候補件数）",
        min_value=max(5, top_k),
        max_value=100,
        value=max(20, top_k),
        help="BM25＋ベクトル検索で1クエリあたり集める候補の数。質問は複数クエリに分解されるため"
        "候補プール全体はこれより大きくなる。大きいほど取りこぼしは減るが遅くなる。既定20。",
    )
    rerank_top_k = st.slider(
        "Rerank Top K（再ランク後にLLMへ渡す件数）",
        min_value=top_k,
        max_value=min(candidate_top_k, 50),
        value=min(max(16, top_k), candidate_top_k),
        help="RRF融合＋reranker で絞り込み、回答生成LLMへ渡す件数。既定16。"
        "LLMのコンテキスト上限（LLM_MAX_CONTEXT_CHARS）に収まる範囲で設定する。",
    )
    show_trace = st.checkbox("検索ルート・グラフ・trace を表示", value=False)

if st.button("質問する", type="primary"):
    if not (question or "").strip():
        st.warning("質問を入力してください。")
        st.stop()
    payload = {
        "question": question,
        "choices": {k: v for k, v in (choices or {}).items() if v.strip()} or None,
        "pattern": pattern,
        "userClearanceLevel": user_clearance,
        "topK": top_k,
        "candidateTopK": candidate_top_k,
        "rerankTopK": rerank_top_k,
    }
    try:
        with st.spinner("検索・回答生成中（最大2分程度かかる場合があります）"):
            response = requests.post(f"{API_URL}/answer", json=payload, timeout=240)
            response.raise_for_status()
        result = response.json()

        citations = result.get("citations", [])

        st.subheader("回答")
        # 回答本文はcontentUnitIdで引用するため、そのままでは読めない。条文名へ置き換える。
        st.write(humanize_citation_ids(result.get("answer") or "", citations))

        if result.get("predictedAnswer"):
            st.metric("選択式の判定", result["predictedAnswer"])
            st.json(result.get("choiceJudgements"))

        citation_groups = group_citations(citations)
        law_groups = [group for group in citation_groups if group["kind"] == "law"]

        st.subheader("引用した資料の内訳")
        count_columns = st.columns(2)
        count_columns[0].metric("引用した法令", f"{len(law_groups)}種類")
        count_columns[1].metric("法令・資料の合計", f"{len(citation_groups)}種類")
        if citation_groups:
            heading = "複数の資料を引用しました" if len(citation_groups) > 1 else "引用した資料"
            st.info(
                f"{heading}（**内容が質問に適合しているかは要確認**）: "
                + " ／ ".join(group["title"] for group in citation_groups)
            )
            st.caption(
                "引用の件数や法令の種類数は、検索精度そのものではありません。"
                "引用条文を開いて、質問に答える内容かを確認してください。"
            )
        else:
            st.warning("引用できる条文・資料を確認できませんでした。")

        # 例題は採点基準が分かっているため、文書・必要条文・回答要点を事後照合する。
        # この情報はAgent APIへ送らず、検索・回答生成には影響させない。
        matched_example = find_example(question)
        if matched_example:
            evaluation = evaluate_example(
                matched_example,
                citations,
                result.get("answer"),
            )
            all_statuses = (
                ("想定資料", evaluation.source_statuses),
                ("必要条文・資料", evaluation.evidence_statuses),
                ("回答要点", evaluation.answer_point_statuses),
            )
            passed_count = sum(
                status.reached for _, statuses in all_statuses for status in statuses
            )
            status_count = sum(len(statuses) for _, statuses in all_statuses)
            st.markdown(
                f"**例題「{matched_example.title}」の答え合わせ**"
                f"（採点項目 {passed_count}/{status_count}）"
            )
            st.markdown(
                "\n".join(
                    [
                        "| 確認段階 | 採点項目 | 結果 |",
                        "|---|---|---|",
                        *[
                            f"| {category} | {status.name} | "
                            f"{'確認' if status.reached else '**未確認**'} |"
                            for category, statuses in all_statuses
                            for status in statuses
                        ],
                    ]
                )
            )
            if not evaluation.passed:
                st.caption(
                    "文書名だけでなく、質問に必要な条文と回答要点まで確認しています。"
                    "これは検索・回答後の採点であり、採点基準はAgent APIへ送っていません。"
                    "検索とLLMには揺らぎがあるため、複数回の到達率も確認してください。"
                )

        evidence_dot = build_evidence_dot(
            question,
            citations,
            result.get("graphPaths", []),
        )
        if evidence_dot:
            st.graphviz_chart(evidence_dot, use_container_width=True)
            st.caption(
                "**実線**はグラフ上で確認できた条文参照・解説関係、"
                "**点線**は法令名の命名規則からの推定（条文同士の委任関係までは未確認）、"
                "**破線**は正式な上下関係ではなく、この回答で併せて根拠にした関係です。"
            )

        st.subheader(f"根拠として引用した条文・資料（{len(citations)}件）")
        if not citations:
            st.info("引用できる条文が見つかりませんでした。質問の範囲が投入済み法令の外かもしれません。")
        for citation in citations:
            with st.expander(citation_label(citation)):
                st.write(citation.get("text"))
                source = citation.get("sourceObjectUri")
                if source:
                    st.caption(f"出典: {source}")
                st.caption(f"ID: {citation.get('contentUnitId')}")

        if show_trace:
            st.subheader("検索ルート")
            st.write(" -> ".join(result.get("route", [])))
            st.subheader("Graph paths")
            st.json(result.get("graphPaths", []))
            st.subheader("Trace")
            st.json(result.get("trace", {}))
    except requests.RequestException as exc:
        st.error(f"リクエストに失敗しました: {exc}")

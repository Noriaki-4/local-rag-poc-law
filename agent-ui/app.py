import os

import requests
import streamlit as st


API_URL = os.getenv("AGENT_API_URL", "http://localhost:8000").rstrip("/")
DEFAULT_PATTERN = "pattern_4_deepsearch"

# 投入済み法令・ガイドラインに沿った質問例。利用者がそのまま試せるものを厳選する。
# 詳しい回答可能範囲は docs/USER_GUIDE.md を参照。
EXAMPLE_QUESTIONS = {
    "借地借家・賃貸借（民法/借地借家法）": [
        "借地権の存続期間は何年ですか。根拠条文も示してください。",
        "賃貸借が終了したとき、敷金はいつ返還されますか。",
        "借地権の存続期間が満了した場合、借地上の建物はどう扱われますか。",
    ],
    "金融商品取引法": [
        "有価証券の定義に国債証券は含まれますか。根拠条文も示してください。",
        "有価証券報告書は誰が、いつまでに提出する必要がありますか。",
        "株券等の公開買付けとは何ですか。",
    ],
    "薬機法（医薬品医療機器等法）": [
        "製造販売業者が整備すべき法令遵守体制とはどのようなものですか。根拠条文も示してください。",
        "総括製造販売責任者の役割は何ですか。",
    ],
}


def _flatten_examples() -> list[str]:
    return [q for questions in EXAMPLE_QUESTIONS.values() for q in questions]


st.set_page_config(page_title="法令RAG 質問デモ", layout="wide")
st.title("法令RAG 質問デモ")
st.caption(
    "投入済みの法令・ガイドラインに基づいて質問へ回答し、根拠条文を引用します。"
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
    st.session_state.question_text = _flatten_examples()[0]

with st.sidebar:
    st.subheader("質問例")
    st.caption("クリックすると入力欄にセットされます。")
    for law_family, questions in EXAMPLE_QUESTIONS.items():
        st.markdown(f"**{law_family}**")
        for example in questions:
            if st.button(example, key=f"ex-{example}", use_container_width=True):
                st.session_state.question_text = example

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
        "増やすと根拠を広く出せるが関連の薄い条文も混ざりやすく、減らすと主要な根拠に絞られる。",
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

        st.subheader("回答")
        st.write(result.get("answer"))

        if result.get("predictedAnswer"):
            st.metric("選択式の判定", result["predictedAnswer"])
            st.json(result.get("choiceJudgements"))

        citations = result.get("citations", [])
        st.subheader(f"根拠として引用した条文・資料（{len(citations)}件）")
        if not citations:
            st.info("引用できる条文が見つかりませんでした。質問の範囲が投入済み法令の外かもしれません。")
        for citation in citations:
            label = " ".join(filter(None, [citation.get("title"), citation.get("heading")]))
            with st.expander(label or citation.get("contentUnitId") or "引用"):
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

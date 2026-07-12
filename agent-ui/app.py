import os

import requests
import streamlit as st


API_URL = os.getenv("AGENT_API_URL", "http://localhost:8000").rstrip("/")

st.set_page_config(page_title="Local Agentic RAG POC", layout="wide")
st.title("Local Agentic RAG POC")

with st.sidebar:
    st.subheader("Environment")
    if st.button("Health check"):
        try:
            st.json(requests.get(f"{API_URL}/health", timeout=5).json())
        except requests.RequestException as exc:
            st.error(str(exc))

    if st.button("Seed sample data"):
        try:
            with st.spinner("Seeding OpenSearch / Neo4j / MinIO"):
                response = requests.post(f"{API_URL}/admin/seed", timeout=120)
                response.raise_for_status()
            st.success("Seed completed")
            st.json(response.json())
        except requests.RequestException as exc:
            st.error(str(exc))

pattern = st.selectbox(
    "Pattern",
    [
        "pattern_1_baseline_rag",
        "pattern_2_rule_based_agentic_rag",
        "pattern_3_controlled_agentic_rag",
        "pattern_4_deepsearch_partial",
        "pattern_4_deepsearch",
    ],
    index=1,
)

question = st.text_area(
    "Question",
    value="条例案が議会で可決された後、担当課は何をすべきか。根拠条文も示して。",
    height=110,
)

use_choices = st.checkbox("Multiple choice lawqa_jp style")
choices = None
if use_choices:
    cols = st.columns(4)
    choices = {
        "A": cols[0].text_input("A", value="選択肢A"),
        "B": cols[1].text_input("B", value="選択肢B"),
        "C": cols[2].text_input("C", value="選択肢C"),
        "D": cols[3].text_input("D", value="選択肢D"),
    }

user_clearance = st.slider("User clearance level", min_value=1, max_value=3, value=2)
top_k = st.slider("Top K", min_value=1, max_value=20, value=5)
candidate_top_k = st.slider(
    "Candidate Top K",
    min_value=max(5, top_k),
    max_value=100,
    value=max(20, top_k),
)
rerank_top_k = st.slider(
    "Rerank Top K",
    min_value=top_k,
    max_value=min(candidate_top_k, 50),
    value=min(max(10, top_k), candidate_top_k),
)

if st.button("Ask", type="primary"):
    payload = {
        "question": question,
        "choices": choices,
        "pattern": pattern,
        "userClearanceLevel": user_clearance,
        "topK": top_k,
        "candidateTopK": candidate_top_k,
        "rerankTopK": rerank_top_k,
    }
    try:
        with st.spinner("Searching"):
            response = requests.post(f"{API_URL}/answer", json=payload, timeout=120)
            response.raise_for_status()
        result = response.json()
        st.subheader("Answer")
        st.write(result["answer"])

        if result.get("predictedAnswer"):
            st.metric("Predicted answer", result["predictedAnswer"])
            st.json(result.get("choiceJudgements"))

        st.subheader("Route")
        st.write(" -> ".join(result.get("route", [])))

        st.subheader("Citations")
        for citation in result.get("citations", []):
            with st.expander(f"{citation.get('title')} {citation.get('heading')}"):
                st.write(citation.get("text"))
                st.code(citation.get("contentUnitId"))
                st.caption(citation.get("sourceObjectUri"))

        st.subheader("Graph paths")
        st.json(result.get("graphPaths", []))

        st.subheader("Trace")
        st.json(result.get("trace", {}))
    except requests.RequestException as exc:
        st.error(str(exc))

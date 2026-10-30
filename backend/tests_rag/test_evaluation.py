"""检索评分、标签分组和自动化门禁的可判定不变量。"""

import copy
import json
import numpy as np
import pytest
from evals.rag_evaluation import load_suite, score_case, selection_key


def sample():
    chunks = [
        {"source": "a.md", "content": "未发货；自动退款默认最多500元"},
        {"source": "b.md", "content": "金额超过500元需人工审批"},
        {"source": "c.md", "content": "会员等级不影响上限"},
    ]
    case = {
        "id": "multi",
        "query": "未发货600元能自动退吗",
        "evidence_groups": [
            {"id": "state", "any_of": [{"source": "a.md", "contains": "未发货"}]},
            {
                "id": "limit",
                "any_of": [
                    {"source": "b.md", "contains": "需人工审批"},
                    {"source": "a.md", "contains": "最多500元"},
                ],
            },
        ],
    }
    return case, chunks


def test_one_chunk_can_cover_multiple_requirements():
    case, chunks = sample()
    result = score_case(case, chunks, np.array([0.9, 0.7, 0.6]), 1, 0.5)
    assert result["recall"] == 1 and result["all_evidence_found"]


def test_partial_evidence_does_not_count_as_complete():
    case, chunks = sample()
    # 只有金额规则，缺少未发货条件。
    result = score_case(case, chunks, np.array([0.1, 0.9, 0.2]), 1, 0.5)
    assert result["recall"] == 0.5 and not result["all_evidence_found"]


def test_equivalent_source_is_accepted():
    case, chunks = sample()
    case["evidence_groups"] = [case["evidence_groups"][1]]
    assert score_case(case, chunks, np.array([0.1, 0.9, 0.2]), 1, 0.5)["recall"] == 1


def test_unknown_rejection_is_distinct_from_wrong_retrieval():
    case, chunks = sample()
    case["evidence_groups"] = []
    assert not score_case(case, chunks, np.array([0.4, 0.2, 0.1]), 4, 0.3)["rejected"]
    assert score_case(case, chunks, np.array([0.4, 0.2, 0.1]), 4, 0.5)["rejected"]


def test_same_intent_never_crosses_splits():
    cases, _, _ = load_suite()
    groups = {}
    for case in cases:
        groups.setdefault(case["group_id"], set()).add(case["split"])
    assert len(cases) == 140
    assert all(len(splits) == 1 for splits in groups.values())


def test_invalid_evidence_fails_instead_of_silently_scoring(tmp_path):
    cases, _, _ = load_suite()
    case = copy.deepcopy(next(c for c in cases if c["evidence_groups"]))
    case["evidence_groups"][0]["any_of"][0]["contains"] = "不存在的证据片段"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"cases": [case]}))
    with pytest.raises(ValueError, match="失效证据"):
        load_suite(path)


def test_cross_split_intent_is_rejected(tmp_path):
    cases, _, _ = load_suite()
    a, b = copy.deepcopy(cases[:2])
    b["split"] = "test" if a["split"] == "dev" else "dev"
    path = tmp_path / "leak.json"
    path.write_text(json.dumps({"cases": [a, b]}))
    with pytest.raises(ValueError, match="跨集"):
        load_suite(path)


def test_selection_does_not_depend_on_test_metrics():
    result = {
        "top_k": 4,
        "min_score": 0.5,
        "metrics": {
            "all_evidence_rate": 0.8,
            "unknown_rejection_rate": 0.8,
            "known_false_rejection_rate": 0.1,
            "recall_at_k": 0.9,
        },
    }
    before = selection_key(result)
    result["test_metrics"] = {"all_evidence_rate": 0}
    assert selection_key(result) == before


def test_remote_embedding_client_batches_requests(monkeypatch):
    import langchain_openai
    from app.rag.embeddings import OpenAICompatibleEmbeddings

    arguments = {}

    def client(**kwargs):
        arguments.update(kwargs)
        return object()

    monkeypatch.setattr(langchain_openai, "OpenAIEmbeddings", client)
    OpenAICompatibleEmbeddings(
        api_key="test", base_url="https://example.invalid", model="test"
    )
    assert arguments["chunk_size"] <= 20 and arguments["request_timeout"] == 30


def test_regression_gate_detects_metric_drop_and_changed_labels():
    from scripts.check_rag_regression import check

    fingerprint = {"corpus_sha256": {"a.md": "v1"}, "suite_sha256": "v1"}
    metrics = {
        "recall_at_k": 0.8,
        "all_evidence_rate": 0.8,
        "mrr_at_k": 0.8,
        "unknown_rejection_rate": 0.8,
        "known_false_rejection_rate": 0.1,
    }
    reference = {**fingerprint, "metrics": {"dev": metrics}}
    assert not check(reference, fingerprint, {"dev": {"metrics": metrics}})
    worse = {**metrics, "recall_at_k": 0.7}
    assert any(
        "退化" in error
        for error in check(reference, fingerprint, {"dev": {"metrics": worse}})
    )
    changed = {**fingerprint, "suite_sha256": "v2"}
    assert any(
        "指纹" in error
        for error in check(reference, changed, {"dev": {"metrics": metrics}})
    )


def test_answer_export_never_invents_responses_and_uses_real_context():
    from scripts.export_rag_answers import export

    cases, _, fingerprint = load_suite()
    case = next(c for c in cases if c["split"] == "test")
    report = {
        **fingerprint,
        "runs": {"local": {"selected_test": {"cases": [{**case, "hits": []}]}}},
    }
    pending, checks = export(report, "local")
    assert "response" not in pending[0] and not checks
    actual = {
        case["id"]: {
            "response": "测试实际回答",
            "retrieved_contexts": ["实际模型输入"],
            "behavior": case["expected_behavior"],
            "tool_calls": case["required_tools"],
        }
    }
    tasks, checks = export(report, "local", actual)
    assert tasks[0]["retrieved_contexts"] == ["实际模型输入"]
    assert checks[0]["behavior_matches"] and checks[0]["required_tools_present"]
    with pytest.raises(ValueError, match="ID完全一致"):
        export(report, "local", {})


def test_retrieval_uses_configured_threshold_and_top_k(monkeypatch):
    from app.rag import retriever
    from types import SimpleNamespace

    calls = []

    class Store:
        def search(self, vector, **kwargs):
            calls.append(kwargs)
            return [
                {"score": 0.7, "title": "规则", "source": "a.md"},
                {"score": 0.3, "title": "无关规则", "source": "b.md"},
            ]

    monkeypatch.setattr(retriever.settings, "rag_top_k", 6)
    monkeypatch.setattr(retriever.settings, "rag_min_score", 0.55)
    monkeypatch.setattr(
        retriever,
        "get_embeddings",
        lambda: SimpleNamespace(name="test", embed=lambda texts: [[1, 0]]),
    )
    monkeypatch.setattr(retriever, "get_vector_store", Store)
    assert [hit["score"] for hit in retriever.retrieve("查询规则")] == [0.7]
    assert calls[0]["top_k"] == 6


def test_invalid_rag_parameters_fail_at_configuration():
    from app.core.config import Settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="RAG_TOP_K"):
        Settings(_env_file=None, rag_top_k=0)

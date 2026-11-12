"""Workshop preflight: catches broken demo cases, CDN assets and misconfigured keys offline."""

import json

from src.config import load_policy, load_search_config
from src.investigations.baseline import run_deterministic
from src.investigations.replay import trace_dict
from src.investigations.state import InvestigationState
from src.preflight import run_checks
from src.wikidata.offline import SqlitePeopleGraphProvider

from .graph_helpers import build_people_db
from .test_investigation import A, B, ORGS, PEOPLE, TRIPLES


def _case_files(root, tmp_path):
    provider = SqlitePeopleGraphProvider(build_people_db(root, tmp_path, PEOPLE, ORGS, TRIPLES))
    pol, search = load_policy(), load_search_config()
    state = run_deterministic(InvestigationState(A, B, pol.default_groups, search.budgets, snapshot=provider.snapshot),
                              provider, pol, search)
    provider.close()
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps(trace_dict(state, "t", {})), encoding="utf-8")
    cases = tmp_path / "cases.yaml"

    def write(**over):
        case = {"id": "t", "title": "Ada ↔ Bea", "source_qid": A, "source_label": "Ada", "target_qid": B,
                "target_label": "Bea", "trace": str(trace), "verified_on": "20991231", **over}
        cases.write_text(json.dumps({"version": 1, "cases": [case]}), encoding="utf-8")  # JSON is valid YAML
        return cases
    return write, trace


def by_name(checks):
    return {c.name: c for c in checks}


def test_valid_case_passes_and_repo_has_no_failures(root, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    write, _ = _case_files(root, tmp_path)
    checks = by_name(run_checks(root, port=False, cases_path=write()))
    assert checks["case t"].level == "ok" and "shortest 2 hops" in checks["case t"].detail
    assert checks["offline assets"].level == "ok"
    assert checks["demo cases"].level == "warn"  # fewer than 3 cases
    assert checks["LLM"].level == "warn"


def test_broken_cases_fail(root, tmp_path):
    write, trace = _case_files(root, tmp_path)
    assert by_name(run_checks(root, port=False, cases_path=write(target_qid="Q900003")))["case t"].level == "fail"
    assert by_name(run_checks(root, port=False, cases_path=write(verified_on="20000101")))["case t"].level == "fail"
    data = json.loads(trace.read_text(encoding="utf-8"))
    data["events"] = data["events"][:-1]  # truncated trace: no terminal event
    trace.write_text(json.dumps(data), encoding="utf-8")
    assert by_name(run_checks(root, port=False, cases_path=write()))["case t"].level == "fail"


def test_non_openrouter_key_in_openrouter_variable_fails(root, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-0123456789abcdef0123456789abcdef")
    checks = by_name(run_checks(root, port=False))
    assert checks["LLM"].level == "fail" and "DEEPSEEK_API_KEY" in checks["LLM"].detail

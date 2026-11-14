"""Save as demo replay: POST /api/investigations/<id>/record and the shared recording module.

Everything runs on a synthetic people graph in a temp dir: config/demo_cases.yaml and the committed
fixtures are never touched.
"""

import json

import pytest
import yaml

from src.config import load_policy, load_search_config
from src.investigations.recording import RecordError, entry_yaml, prepare_recording, save_demo_case
from src.investigations.service import DemoCase, InvestigationService, load_demo_cases
from src.wikidata.offline import SqlitePeopleGraphProvider

from .test_agent import FakeChat, adaptive, run
from .test_investigation import A, B, F, ORGS, PEOPLE, TRIPLES, build_people_db
from .test_investigation_api import wait_done

CASES_YAML = """# test cases
version: 1
cases:
  - id: ada-bea
    title: "Ada and Bea"
    source_qid: Q900001
    source_label: "Ada"
    target_qid: Q900002
    target_label: "Bea"
    notes: "existing"
"""


@pytest.fixture(scope="module")
def db_path(tmp_path_factory, request):
    return build_people_db(request.config.rootpath, tmp_path_factory.mktemp("rec"), PEOPLE, ORGS, TRIPLES)


@pytest.fixture
def service(db_path, tmp_path):
    provider = SqlitePeopleGraphProvider(db_path)
    cases_path = tmp_path / "demo_cases.yaml"
    cases_path.write_text(CASES_YAML, encoding="utf-8")
    svc = InvestigationService(provider, load_policy(), load_search_config(), demo_cases=load_demo_cases(cases_path),
                               root=tmp_path, chat_factory=lambda: None, agent_available=lambda: False)
    svc.cases_path = cases_path
    svc.cases_dir = tmp_path / "data" / "fixtures" / "cases"
    svc.traces_dir = tmp_path / "data" / "fixtures" / "event_traces"
    yield svc
    provider.close()


@pytest.fixture
def api(app, service):
    app.config["INVESTIGATIONS"] = service
    yield app.test_client()
    app.config.pop("INVESTIGATIONS", None)


def live_run(api, source=A, target=B):
    inv = api.post("/api/investigations", json={"source_qid": source, "target_qid": target}).get_json()
    wait_done(api, inv["investigation_id"])
    return inv["investigation_id"]


def test_a_saved_live_run_becomes_a_demo_replay_that_reproduces_it(api, service):
    inv_id = live_run(api)
    sug = api.get(f"/api/investigations/{inv_id}/record").get_json()
    assert sug == {"case_id": "ada-bea-2", "title": "Ada and Bea"}  # "ada-bea" is taken: never overwritten

    res = api.post(f"/api/investigations/{inv_id}/record", json={"case_id": "ada-bea-live", "title": "Ada and Bea (live)"})
    assert res.status_code == 201, res.get_json()
    body = res.get_json()
    assert body["verification"] == "fixture reproduces the run exactly"
    assert body["case"]["trace"] == "data/fixtures/event_traces/ada-bea-live.json"
    assert (service.cases_dir / "ada-bea-live.json").is_file() and (service.traces_dir / "ada-bea-live.json").is_file()

    # The YAML keeps its comments and the old case, gains a valid entry, and the service reloads it.
    text = service.cases_path.read_text(encoding="utf-8")
    assert text.startswith(CASES_YAML)
    cases = {c["id"]: c for c in yaml.safe_load(text)["cases"]}
    assert cases["ada-bea-live"]["title"] == "Ada and Bea (live)" and cases["ada-bea-live"]["source_qid"] == A
    assert cases["ada-bea-live"]["notes"] == "Recorded deterministic exploration run (saved from the workbench)."
    assert "ada-bea-live" in [c["id"] for c in api.get("/api/demo-cases").get_json()["cases"]]

    # It replays at once and ends with exactly the live run's paths.
    rep = api.post("/api/investigations", json={"mode": "replay", "case_id": "ada-bea-live", "replay_delay_seconds": 0}).get_json()
    wait_done(api, rep["investigation_id"])
    live_paths = api.get(f"/api/investigations/{inv_id}/paths").get_json()
    replay_paths = api.get(f"/api/investigations/{rep['investigation_id']}/paths").get_json()
    assert replay_paths["comparison"] == live_paths["comparison"]


def test_recording_refusals_leave_every_file_untouched(api, service):
    inv_id = live_run(api)
    before = service.cases_path.read_text(encoding="utf-8")
    post = lambda i, cid: api.post(f"/api/investigations/{i}/record", json={"case_id": cid})
    for cid, status, code in [("Bad Name", 400, "invalid_case_id"), ("ab", 400, "invalid_case_id"),
                              ("ada-bea", 409, "case_exists")]:
        res = post(inv_id, cid)
        assert (res.status_code, res.get_json()["error"]["code"]) == (status, code)
    assert api.post(f"/api/investigations/{inv_id}/record", json={}).status_code == 400

    no_path = live_run(api, A, F)  # F is in another component: no connection
    assert post(no_path, "ada-fay").get_json()["error"]["code"] == "no_path"

    assert post(inv_id, "ada-bea-x").status_code == 201
    rep = api.post("/api/investigations", json={"mode": "replay", "case_id": "ada-bea-x", "replay_delay_seconds": 0}).get_json()
    wait_done(api, rep["investigation_id"])
    assert post(rep["investigation_id"], "ada-bea-y").get_json()["error"]["code"] == "not_live"  # a replay is already a recording

    after = service.cases_path.read_text(encoding="utf-8")
    assert after.startswith(before) and after.count("- id:") == before.count("- id:") + 1
    assert sorted(p.name for p in service.cases_dir.iterdir()) == ["ada-bea-x.json"]
    assert api.post("/api/investigations/nope/record", json={"case_id": "zzz"}).status_code == 404


def test_save_rolls_back_when_the_updated_yaml_does_not_validate(db_path, tmp_path):
    provider = SqlitePeopleGraphProvider(db_path)
    pol, search = load_policy(), load_search_config()
    from src.investigations.baseline import run_deterministic
    from src.investigations.state import InvestigationState

    state = run_deterministic(InvestigationState(A, B, pol.default_groups, search.budgets, snapshot=provider.snapshot),
                              provider, pol, search)
    rec = prepare_recording(state, provider, pol, search, "ada-bea-rb")
    cases_path = tmp_path / "demo_cases.yaml"
    cases_path.write_text(CASES_YAML, encoding="utf-8")

    def broken(_path):
        raise ValueError("invalid")

    with pytest.raises(ValueError):
        save_demo_case(rec, cases_dir=tmp_path / "c", traces_dir=tmp_path / "t", cases_path=cases_path, validate=broken)
    assert cases_path.read_text(encoding="utf-8") == CASES_YAML
    assert not list((tmp_path / "c").glob("*.json")) and not list((tmp_path / "t").glob("*.json"))
    provider.close()


def test_agent_runs_are_verified_by_rebuilding_the_trace(db_path):
    provider = SqlitePeopleGraphProvider(db_path)
    pol, search = load_policy(), load_search_config()
    state = run(provider, FakeChat(adaptive))
    assert state.strategy == "agent" and state.comparison and state.comparison.shortest.result
    rec = prepare_recording(state, provider, pol, search, "ada-bea-agent")
    assert rec.verification == "trace rebuilds the same graph and paths"
    assert rec.entry["title"] == "Ada and Bea (AI agent)"
    # The YAML lines parse back into a valid demo case.
    case = DemoCase.model_validate(yaml.safe_load(entry_yaml(rec.entry))[0])
    assert case.id == "ada-bea-agent" and case.trace == "data/fixtures/event_traces/ada-bea-agent.json"
    with pytest.raises(RecordError) as err:
        prepare_recording(state, provider, pol, search, "Not Valid")
    assert err.value.code == "invalid_case_id"
    provider.close()

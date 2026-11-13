"""Relationship policy (config/relationships.yaml) loading and invariants."""

import pytest
from pydantic import ValidationError

from src.config import RelationshipPolicy, load_policy


def test_policy_loads_with_expected_groups():
    p = load_policy()
    assert p.default_groups == {"family", "mentorship", "institutional", "works", "birthplace"}
    assert "P19" in p.active_pids()  # birthplace is on by default since policy v4
    for weak, pid in (("position", "P39"), ("awards", "P166"), ("party", "P102"), ("influence", "P737")):
        assert weak in p.groups and not p.groups[weak].enabled_by_default
        assert pid not in p.active_pids()
        assert pid in p.active_pids(p.default_groups | {weak})


def test_all_costs_nonnegative_and_ordered():
    p = load_policy()
    costs = {pid: p.strength_cost(pid) for pid in p.properties}
    assert all(c >= 0 for c in costs.values())
    # Interpersonal relationships must be cheaper than shared affiliations.
    for strong in ("P26", "P22", "P25", "P3373", "P40", "P451", "P1038", "P1066", "P802", "P184", "P185"):
        for weak in ("P108", "P69", "P463", "P54", "P112", "P161", "P57", "P58", "P86", "P175",
                     "P39", "P19", "P166", "P102"):
            assert costs[strong] < costs[weak], (strong, weak)
    # A shared film/music credit beats a shared institution; every weak group is weaker still.
    for work in ("P161", "P57", "P58", "P86", "P175"):
        assert all(costs[work] < costs[x] for x in ("P108", "P69", "P463", "P54", "P112"))
    for weak in ("P39", "P19", "P166", "P102"):
        assert all(costs[weak] > costs[x] for x in ("P108", "P69", "P463", "P54"))
    # A generic "relative" (kinship type lost with its qualifier) is weaker than explicit family.
    for explicit in ("P26", "P22", "P25", "P40", "P3373", "P451"):
        assert costs[explicit] < costs["P1038"]
    assert p.hub_penalty(0) == 0 and p.hub_penalty(1000) > p.hub_penalty(10) > 0


def test_v2_v3_properties_are_in_the_right_groups():
    p = load_policy()
    assert p.version >= 3
    groups = {pid: prop.group for pid, prop in p.properties.items()}
    assert {groups[x] for x in ("P40", "P451", "P1038")} == {"family"}
    assert {groups[x] for x in ("P802", "P184", "P185")} == {"mentorship"}
    assert {groups[x] for x in ("P54", "P112")} == {"institutional"}
    assert {groups[x] for x in ("P161", "P57", "P58", "P86", "P175")} == {"works"}
    assert "P50" not in groups  # author: scholarly-article co-authorship would swamp the graph
    assert {"P40", "P451", "P1038", "P54", "P802", "P184", "P185", "P112", "P161", "P175"} <= p.active_pids()


def test_every_property_has_bilingual_labels():
    for pid, prop in load_policy().properties.items():
        assert prop.label and prop.label_es and prop.inverse_label and prop.inverse_label_es, pid


def _raw():
    import yaml

    from src.config import DEFAULT_POLICY_PATH

    return yaml.safe_load(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["properties"]["P26"].update(group="nope"),
        lambda d: d["properties"]["P26"].update(strength=1.5),
        lambda d: d["properties"]["P26"].update(strength=0),
        lambda d: d["properties"].update({"X1": d["properties"]["P26"]}),
        lambda d: d["ranking"].update(hop_penalty=-0.1),
        lambda d: d["pathfinding"].update(unexpected=1),
    ],
)
def test_invalid_policies_rejected(mutate):
    data = _raw()
    mutate(data)
    with pytest.raises(ValidationError):
        RelationshipPolicy.model_validate(data)


def test_places_have_a_lower_hub_cap_than_other_kinds():
    pf = load_policy().pathfinding
    assert pf.hub_cap("place") is not None and pf.hub_cap("place") < pf.max_intermediate_degree
    assert pf.hub_cap("organization") == pf.max_intermediate_degree  # not overridden
    place_cap = pf.hub_cap("place")
    assert pf.is_hub("place", place_cap + 1) and not pf.is_hub("place", place_cap)
    assert not pf.is_hub("organization", place_cap + 1)
    assert not pf.is_hub("person", 10**9)  # people are never hubs


def test_per_kind_cap_override_and_none():
    pf = load_policy().pathfinding.model_copy(
        update={"max_intermediate_degree": 10, "max_intermediate_degree_by_kind": {"place": 3, "work": None}})
    assert pf.is_hub("place", 4) and not pf.is_hub("place", 3)
    assert not pf.is_hub("work", 10**6)  # explicit None: no cap for works
    assert pf.is_hub("position", 11)     # falls back to the global cap


@pytest.mark.parametrize("bad", [{"person": 5}, {"planet": 5}, {"place": 0}])
def test_per_kind_cap_validation(bad):
    data = load_policy().model_dump()
    data["pathfinding"]["max_intermediate_degree_by_kind"] = bad
    with pytest.raises(ValidationError):
        RelationshipPolicy.model_validate(data)

"""Relationship policy (config/relationships.yaml) loading and invariants."""

import pytest
from pydantic import ValidationError

from src.config import RelationshipPolicy, load_policy


def test_policy_loads_with_expected_groups():
    p = load_policy()
    assert p.default_groups == {"family", "mentorship", "institutional"}
    assert "P39" not in p.active_pids()
    assert "P39" in p.active_pids(p.default_groups | {"position"})


def test_all_costs_nonnegative_and_ordered():
    p = load_policy()
    costs = {pid: p.strength_cost(pid) for pid in p.properties}
    assert all(c >= 0 for c in costs.values())
    # Interpersonal relationships must be cheaper than shared affiliations.
    for strong in ("P26", "P22", "P25", "P3373", "P1066"):
        for weak in ("P108", "P69", "P463", "P39"):
            assert costs[strong] < costs[weak], (strong, weak)
    assert p.hub_penalty(0) == 0 and p.hub_penalty(1000) > p.hub_penalty(10) > 0


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

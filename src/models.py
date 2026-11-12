"""Pydantic DTOs shared by the path engine, the API and replay fixtures."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Scope = Literal["explored_graph", "complete_configured_scope"]
SearchStatus = Literal["found", "no_path_within_bounds", "budget_exhausted"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class PathNode(_Model):
    qid: str
    label_en: str | None
    label_es: str | None
    kind: str
    url: str


class CostBreakdown(_Model):
    strength: float
    strength_cost: float
    hop_penalty: float
    hub_penalty: float
    total: float


class PathHop(_Model):
    """One traversed edge. ``subject``/``object`` are the ORIGINAL Wikidata direction."""

    edge_id: str
    from_qid: str
    to_qid: str
    subject: str
    pid: str
    object: str
    direction: Literal["forward", "reverse"]
    relation_group: str
    label: str
    label_es: str
    property_url: str
    source: str
    origin: str
    cost: CostBreakdown


class PathResult(_Model):
    path_id: str
    mode: Literal["shortest", "strongest"]
    source_qid: str
    target_qid: str
    nodes: list[PathNode]
    hops: list[PathHop]
    hop_count: int
    total_cost: float
    scope: Scope
    policy_version: int
    enabled_groups: list[str]
    snapshot: str | None

    @property
    def edge_ids(self) -> list[str]:
        return [h.edge_id for h in self.hops]


class SearchStats(_Model):
    visited_nodes: int
    expanded_states: int
    truncated_reason: str | None = None


class PathOutcome(_Model):
    mode: Literal["shortest", "strongest"]
    status: SearchStatus
    result: PathResult | None
    scope: Scope
    stats: SearchStats


class PathComparison(_Model):
    source_qid: str
    target_qid: str
    shortest: PathOutcome
    strongest: PathOutcome
    same_path: bool
    scope: Scope

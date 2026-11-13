"""Load and validate versioned configuration: relationship policy and search budgets."""

from __future__ import annotations

import math
from pathlib import Path
from typing import get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.graph.model import NodeKind

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY_PATH = ROOT / "config" / "relationships.yaml"
DEFAULT_SEARCH_PATH = ROOT / "config" / "search.yaml"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RelationGroup(_Frozen):
    label: str
    label_es: str
    description: str
    enabled_by_default: bool


class PropertyPolicy(_Frozen):
    label: str
    label_es: str
    inverse_label: str
    inverse_label_es: str
    group: str
    strength: float = Field(gt=0, le=1)
    symmetric: bool = False


class RankingPolicy(_Frozen):
    hop_penalty: float = Field(ge=0)
    minimum_strength: float = Field(gt=0, le=1)
    hub_penalty_weight: float = Field(ge=0)


class PathfindingPolicy(_Frozen):
    max_hops: int = Field(ge=1, le=12)
    max_intermediate_degree: int | None = Field(default=None, ge=1)
    # Per-kind overrides of the hub cap (e.g. a lower cap for birthplaces). Kinds not listed use
    # max_intermediate_degree; a listed None means "no cap" for that kind. Persons are never hubs.
    max_intermediate_degree_by_kind: dict[str, int | None] = Field(default_factory=dict)
    max_visited_nodes: int = Field(ge=1)
    max_expanded_states: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_kind_caps(self) -> "PathfindingPolicy":
        kinds = set(get_args(NodeKind)) - {"person"}
        for kind, cap in self.max_intermediate_degree_by_kind.items():
            if kind not in kinds:
                raise ValueError(f"max_intermediate_degree_by_kind: unknown or person kind {kind!r}")
            if cap is not None and cap < 1:
                raise ValueError(f"max_intermediate_degree_by_kind[{kind!r}] must be >= 1")
        return self

    def hub_cap(self, kind: str) -> int | None:
        """The hub cap that applies to a non-person node of this kind (None = no cap)."""
        return self.max_intermediate_degree_by_kind.get(kind, self.max_intermediate_degree)

    def is_hub(self, kind: str, degree: int) -> bool:
        """True if a node may not be an INTERMEDIATE path node (endpoints are always allowed)."""
        if kind == "person":
            return False
        cap = self.hub_cap(kind)
        return cap is not None and degree > cap


class RelationshipPolicy(_Frozen):
    version: int
    groups: dict[str, RelationGroup]
    properties: dict[str, PropertyPolicy]
    ranking: RankingPolicy
    pathfinding: PathfindingPolicy

    @model_validator(mode="after")
    def _check_refs(self) -> "RelationshipPolicy":
        for pid, prop in self.properties.items():
            if not (pid.startswith("P") and pid[1:].isdigit()):
                raise ValueError(f"invalid property id {pid!r}")
            if prop.group not in self.groups:
                raise ValueError(f"{pid}: unknown group {prop.group!r}")
        return self

    # ---- derived helpers -------------------------------------------------------------------

    @property
    def default_groups(self) -> frozenset[str]:
        return frozenset(g for g, spec in self.groups.items() if spec.enabled_by_default)

    def active_pids(self, groups: frozenset[str] | set[str] | None = None) -> frozenset[str]:
        """Properties eligible for traversal given the enabled relation groups."""
        enabled = self.default_groups if groups is None else frozenset(groups)
        unknown = enabled - self.groups.keys()
        if unknown:
            raise ValueError(f"unknown relation groups: {sorted(unknown)}")
        return frozenset(pid for pid, p in self.properties.items() if p.group in enabled)

    def strength_cost(self, pid: str) -> float:
        """-ln(max(minimum_strength, strength)); always >= 0 because strength <= 1."""
        s = max(self.ranking.minimum_strength, self.properties[pid].strength)
        return -math.log(s)

    def hub_penalty(self, degree: int) -> float:
        return self.ranking.hub_penalty_weight * math.log1p(max(0, degree))


def load_policy(path: Path | str = DEFAULT_POLICY_PATH) -> RelationshipPolicy:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return RelationshipPolicy.model_validate(data)


# ---- search budgets (config/search.yaml) ----------------------------------------------------


class SearchBudgets(_Frozen):
    max_explored_nodes: int = Field(ge=2)
    max_explored_edges: int = Field(ge=1)
    max_edges_per_expansion: int = Field(ge=1)
    max_provider_requests: int = Field(ge=1)
    max_agent_tool_calls: int = Field(ge=1)
    wall_time_seconds: float = Field(gt=0)

    def lowered(self, overrides: dict | None) -> "SearchBudgets":
        """Apply per-investigation overrides, which may only LOWER configured values."""
        if not overrides:
            return self
        unknown = set(overrides) - set(type(self).model_fields)
        if unknown:
            raise ValueError(f"unknown budget fields: {sorted(unknown)}")
        merged = {k: min(getattr(self, k), v) for k, v in overrides.items()}
        return type(self).model_validate({**self.model_dump(), **merged})


class FrontierPolicy(_Frozen):
    # Negative = reward well-connected nodes (search v3: -0.5). Bounded so a typo cannot flip the search.
    degree_weight: float = Field(ge=-2, le=2)
    expansions_after_first_path: int = Field(ge=0)


class AgentPolicy(_Frozen):
    max_turns: int = Field(ge=1)
    no_progress_limit: int = Field(ge=1)
    max_result_nodes: int = Field(ge=1)
    status_frontier_size: int = Field(ge=1)
    max_summary_chars: int = Field(ge=50)


class UiPolicy(_Frozen):
    max_visible_nodes: int = Field(ge=10)


class SearchConfig(_Frozen):
    version: int
    budgets: SearchBudgets
    frontier: FrontierPolicy
    agent: AgentPolicy
    ui: UiPolicy


def load_search_config(path: Path | str = DEFAULT_SEARCH_PATH) -> SearchConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return SearchConfig.model_validate(data)

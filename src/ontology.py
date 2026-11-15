"""The people-connections teaching ontology (`ontology/people_connections.ttl`), read with RDFLib.

The ontology holds MEANING: classes, relation groups (direct tie or shared affiliation) and the
Wikidata properties with their domain and range in the people graph. The ranking policy
(`config/relationships.yaml`) holds WEIGHTS: strengths, which groups are on by default, hub caps.
`load_people_ontology` reads the first, checks it against the second, and returns one JSON-ready
view of both for the workbench's Ontology view. A mismatch raises `OntologyError`.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from .config import ROOT, RelationshipPolicy

DEFAULT_ONTOLOGY_PATH = ROOT / "ontology" / "people_connections.ttl"
EX = Namespace("https://example.org/six-degrees/ontology#")
WDT = "http://www.wikidata.org/prop/direct/"


class OntologyError(ValueError):
    """The ontology file is invalid or disagrees with the relationship policy."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class OntologyClass(_Frozen):
    id: str            # local name, e.g. "Person"
    label: str
    label_es: str
    comment: str
    kind: str          # the graph's node kind (person, organization, work, place, position, other)


class OntologyGroup(_Frozen):
    id: str
    label: str
    label_es: str
    comment: str
    link_type: str     # "DirectTie" | "SharedAffiliation"
    link_label: str
    enabled_by_default: bool   # policy


class OntologyProperty(_Frozen):
    pid: str
    label: str
    label_es: str
    comment: str
    inverse_label: str  # policy: how a reverse hop reads
    domain: str
    range: str
    group: str
    symmetric: bool
    inverse_of: str | None
    strength: float     # policy


class OntologyView(_Frozen):
    text: str
    version: str
    policy_version: int
    classes: list[OntologyClass]
    groups: list[OntologyGroup]
    properties: list[OntologyProperty]


def _local(node, ns: str) -> str | None:
    s = str(node)
    return s[len(ns):] if s.startswith(ns) else None


def _labels(g: Graph, subject) -> tuple[str, str]:
    en = es = ""
    for o in g.objects(subject, RDFS.label):
        if isinstance(o, Literal):
            if o.language == "en":
                en = str(o)
            elif o.language == "es":
                es = str(o)
    return en, es


def _one(g: Graph, subject, pred, what: str):
    values = list(g.objects(subject, pred))
    if len(values) != 1:
        raise OntologyError(f"{what}: expected exactly one {pred.n3(g.namespace_manager)}, found {len(values)}")
    return values[0]


def load_people_ontology(policy: RelationshipPolicy, path: Path | str = DEFAULT_ONTOLOGY_PATH) -> OntologyView:
    text = Path(path).read_text(encoding="utf-8")
    g = Graph()
    try:
        g.parse(data=text, format="turtle")
    except Exception as exc:  # RDFLib raises several parser exception types
        raise OntologyError(f"Turtle does not parse: {exc}") from exc

    onto = next(iter(g.subjects(RDF.type, OWL.Ontology)), None)
    if onto is None:
        raise OntologyError("no owl:Ontology header")
    version = str(_one(g, onto, OWL.versionInfo, "ontology header"))

    classes: list[OntologyClass] = []
    for c in sorted(g.subjects(RDF.type, OWL.Class), key=str):
        cid = _local(c, str(EX))
        if cid is None:
            raise OntologyError(f"class {c} is outside the ex: namespace")
        en, es = _labels(g, c)
        classes.append(OntologyClass(id=cid, label=en, label_es=es, comment=str(g.value(c, RDFS.comment) or ""),
                                     kind=str(_one(g, c, EX.nodeKind, f"class {cid}"))))
    class_ids = {c.id for c in classes}

    groups: list[OntologyGroup] = []
    for gr in g.subjects(RDF.type, EX.RelationGroup):
        gid = _local(gr, str(EX))
        if gid not in policy.groups:
            raise OntologyError(f"relation group {gid!r} is not in the policy")
        link = _one(g, gr, EX.linkType, f"group {gid}")
        en, es = _labels(g, gr)
        if en != policy.groups[gid].label:
            raise OntologyError(f"group {gid}: label {en!r} differs from the policy's {policy.groups[gid].label!r}")
        groups.append(OntologyGroup(id=gid, label=en, label_es=es, comment=str(g.value(gr, RDFS.comment) or ""),
                                    link_type=_local(link, str(EX)) or "", link_label=_labels(g, link)[0],
                                    enabled_by_default=policy.groups[gid].enabled_by_default))
    missing_groups = set(policy.groups) - {gr.id for gr in groups}
    if missing_groups:
        raise OntologyError(f"policy groups missing from the ontology: {sorted(missing_groups)}")
    order = list(policy.groups)
    groups.sort(key=lambda gr: order.index(gr.id))

    properties: list[OntologyProperty] = []
    for p in g.subjects(RDF.type, OWL.ObjectProperty):
        pid = _local(p, WDT)
        if pid is None:
            raise OntologyError(f"property {p} is not a wdt: property")
        if pid not in policy.properties:
            raise OntologyError(f"{pid} is in the ontology but not in the policy")
        pol = policy.properties[pid]
        domain = _local(_one(g, p, RDFS.domain, pid), str(EX))
        rng = _local(_one(g, p, RDFS.range, pid), str(EX))
        if domain not in class_ids or rng not in class_ids:
            raise OntologyError(f"{pid}: domain/range must be ontology classes, got {domain!r} -> {rng!r}")
        group = _local(_one(g, p, EX.relationGroup, pid), str(EX))
        if group != pol.group:
            raise OntologyError(f"{pid}: group {group!r} differs from the policy's {pol.group!r}")
        symmetric = (p, RDF.type, OWL.SymmetricProperty) in g
        if symmetric != pol.symmetric:
            raise OntologyError(f"{pid}: symmetric={symmetric} differs from the policy")
        en, es = _labels(g, p)
        if en != pol.label:
            raise OntologyError(f"{pid}: label {en!r} differs from the policy's {pol.label!r}")
        inv = g.value(p, OWL.inverseOf)
        properties.append(OntologyProperty(
            pid=pid, label=en, label_es=es, comment=str(g.value(p, RDFS.comment) or ""),
            inverse_label=pol.inverse_label, domain=domain, range=rng, group=group, symmetric=symmetric,
            inverse_of=_local(inv, WDT) if inv is not None else None, strength=pol.strength))
    missing = set(policy.properties) - {p.pid for p in properties}
    if missing:
        raise OntologyError(f"policy properties missing from the ontology: {sorted(missing)}")
    for p in properties:
        if p.inverse_of and next((q.inverse_of for q in properties if q.pid == p.inverse_of), None) != p.pid:
            raise OntologyError(f"{p.pid}: owl:inverseOf {p.inverse_of} is not declared both ways")
    porder = list(policy.properties)
    properties.sort(key=lambda p: porder.index(p.pid))

    return OntologyView(text=text, version=version, policy_version=policy.version,
                        classes=classes, groups=groups, properties=properties)

"""One tool-calling orchestrator (plan §6). The LLM chooses what to explore; the backend does the rest.

Loop: the model gets a small system prompt and the task, then calls typed tools. Each turn's text
is emitted as ``agent.decision`` (a short decision summary, not chain of thought). Expansions,
path updates and budgets reuse the deterministic building blocks in investigations/steps.py.

Stops on: ``finish`` / a turn without tool calls ("agent_finished", completed), or budget, cancel,
max turns, no progress, or a model/API error (partial). The final answer is built from the
backend's PathResult objects; the model's summary is kept only if every QID it mentions is on a
returned path, otherwise it is replaced by a backend-generated summary and flagged.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..config import RelationshipPolicy, SearchConfig
from ..investigations.state import InvestigationState
from ..investigations.steps import begin, budget_stop, fail, finish
from ..models import PathComparison
from ..wikidata.provider import KnowledgeProvider
from .client import ChatClient
from .prompts import build_system_prompt, build_task_message
from .tools import Toolbox, tool_definitions

log = logging.getLogger(__name__)

QID_RE = re.compile(r"\bQ\d+\b")
MAX_DECISION_CHARS = 300


def backend_summary(cmp: PathComparison | None) -> str:
    """Plain summary generated from verified results only."""
    # The demo shows the strongest path only, so the summary describes that one.
    w = cmp.strongest.result if cmp is not None else None
    if w is None:
        return "No connection was found among the explored relationships within the budget."
    labels = {n.qid: n.label_en or n.label_es or n.qid for n in w.nodes}
    route = labels[w.source_qid] + "".join(f" —{h.label}→ {labels[h.to_qid]}" for h in w.hops)
    return (f"Strongest connection ({w.hop_count} edge{'s' if w.hop_count != 1 else ''}, cost {w.total_cost:.2f}): "
            f"{route}. (Among explored relationships only.)")


def clip_summary(text: str, max_chars: int) -> str:
    """Shorten to ``max_chars`` at the last full sentence; else at a word boundary with an ellipsis."""
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    end = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if head.endswith((".", "!", "?")):
        end = len(head) - 1
    if end >= max_chars // 3:
        return head[:end + 1]
    cut = head[:max_chars - 1].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:—-") + "…"


def verify_summary(summary: str | None, cmp: PathComparison | None, max_chars: int) -> dict[str, Any]:
    allowed: set[str] = set()
    path_ids = []
    if cmp is not None:
        for r in (cmp.shortest.result, cmp.strongest.result):
            if r is not None:
                allowed |= {n.qid for n in r.nodes}
                path_ids.append(r.path_id)
    text = clip_summary(" ".join((summary or "").split()), max_chars)
    unverified = sorted(set(QID_RE.findall(text)) - allowed)
    # With no path, ``allowed`` is empty, so any QID the model names is unverified.
    if text and not unverified:
        return {"summary": text, "summary_source": "agent", "summary_verified": True,
                "unverified_mentions": [], "cited_path_ids": sorted(set(path_ids)),
                "backend_summary": backend_summary(cmp)}
    return {"summary": backend_summary(cmp), "summary_source": "backend", "summary_verified": False,
            "unverified_mentions": unverified, "rejected_agent_summary": text or None,
            "cited_path_ids": sorted(set(path_ids)), "backend_summary": backend_summary(cmp)}


def _wrap_up(chat: ChatClient, messages: list[dict[str, Any]], definitions: list[dict[str, Any]],
             state: InvestigationState, tools: Toolbox, stop: str) -> str | None:
    """One tool-free turn so a budget-stopped run still gets the agent's (verified) summary."""
    if messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
        return None  # unanswered tool calls would make the conversation invalid
    prompt = (f"The investigation stopped: {stop}. Do not call any tools. In 1-3 sentences, summarize "
              f"the verified result of compute_paths for the audience, mentioning only people on the strongest path "
              f"(say plainly if there is no path, and that the search was cut short): "
              f"{json.dumps(tools._paths(), ensure_ascii=False)}")
    try:
        reply = chat.complete(messages + [{"role": "user", "content": prompt}], definitions)
    except Exception as exc:
        log.warning("agent wrap-up call failed: %s", exc)
        return None
    text = " ".join((reply.content or "").split())
    if text:
        state.events.emit("agent.decision", {"turn": "wrap-up", "summary": clip_summary(text, MAX_DECISION_CHARS), "tool_calls": []})
    return text or None


def _model_unreachable_answer(state: InvestigationState, search: SearchConfig, message: str) -> dict[str, Any]:
    """The final answer when the language model call failed (no network, bad key, provider down).

    Without this the answer was the generic "no connection ... within the budget", which reads as a
    search result; at a venue without internet the presenter could not tell the AI was unreachable.
    """
    answer = verify_summary(None, state.comparison, search.agent.max_summary_chars)
    note = (f"The AI agent's language model could not be reached ({message}), so the run stopped early. "
            "Switch to Deterministic (no AI) or use a replay.")
    has_path = bool(state.comparison and state.comparison.shortest.result)
    answer["summary"] = f"{note} So far: {answer['summary']}" if has_path else note
    answer["model_unreachable"] = True
    return answer


def run_agent(
    state: InvestigationState,
    provider: KnowledgeProvider,
    policy: RelationshipPolicy,
    search: SearchConfig,
    chat: ChatClient,
) -> InvestigationState:
    state.strategy = "agent"
    try:
        if not begin(state, provider, policy, search, extra={"model": getattr(chat, "model", None), "max_turns": search.agent.max_turns}):
            return state
        tools = Toolbox(state, provider, policy, search)
        source, target = state.graph.node(state.source_qid), state.graph.node(state.target_qid)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt(policy, state.budgets, state.groups)},
            {"role": "user", "content": build_task_message(source, target, state.groups)},  # type: ignore[arg-type]
        ]
        definitions = tool_definitions()
        step = 0
        stop: str | None = None
        for turn in range(1, search.agent.max_turns + 1):
            stop = budget_stop(state)
            if stop:
                break
            try:
                reply = chat.complete(messages, definitions)
            except Exception as exc:  # network, auth, rate limit, malformed response
                log.warning("agent model call failed: %s", exc)
                message = f"{type(exc).__name__}: {str(exc)[:300]}"
                return finish(state, "agent_error", completed=False, extra={
                    "error": {"code": "agent_error", "message": message},
                    "answer": _model_unreachable_answer(state, search, message)})
            # Shown to the audience: cut at a sentence or word boundary, never mid-word.
            decision = clip_summary(" ".join((reply.content or "").split()), MAX_DECISION_CHARS)
            if decision:
                state.events.emit("agent.decision", {"turn": turn, "summary": decision,
                                                     "tool_calls": [tc.name for tc in reply.tool_calls]})
            if not reply.tool_calls:
                tools.finished_summary = reply.content or ""
                stop = "agent_finished"
                break
            messages.append(reply.as_message())
            for tc in reply.tool_calls:
                if state.tool_calls >= state.budgets.max_agent_tool_calls:
                    result: dict[str, Any] = {"ok": False, "error": {"code": "budget_exhausted",
                                                                     "message": "max_agent_tool_calls reached"}}
                    stop = "max_agent_tool_calls"
                else:
                    step += 1
                    result = tools.call(tc.name, tc.arguments, step, decision)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False)})
            if tools.finished_summary is not None:
                stop = "agent_finished"
            elif stop is None and tools.consecutive_no_progress >= search.agent.no_progress_limit:
                stop = "no_progress"
            if stop:
                break
        else:
            stop = "max_turns"

        if tools.finished_summary is None and stop not in ("agent_finished", "cancelled"):
            tools.finished_summary = _wrap_up(chat, messages, definitions, state, tools, stop or "max_turns")
        answer = verify_summary(tools.finished_summary, state.comparison, search.agent.max_summary_chars)
        return finish(state, stop or "max_turns", completed=stop == "agent_finished",
                      extra={"answer": answer, "tool_calls": state.tool_calls})
    except Exception as exc:
        log.exception("agent investigation %s crashed", state.investigation_id)
        return fail(state, "internal_error", f"{type(exc).__name__}: {exc}")

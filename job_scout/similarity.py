"""Rootstock similarity scorer for MissionaryX Job Scout v0.1.

All scoring is deterministic keyword-based. No LLM required.
Weights are explicit and configurable. Scoring remains fully inspectable.

Dimensions score the ROLE, not the candidate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from job_application.opportunity import JobOpportunity


_DIMENSION_WEIGHTS: dict[str, int] = {
    "agent_building": 15,
    "tool_use_and_integrations": 12,
    "ai_assisted_development": 12,
    "workflow_orchestration": 10,
    "evals_and_verification": 10,
    "reliability_and_recovery": 8,
    "rapid_prototyping": 8,
    "applied_product_orientation": 8,
    "portfolio_friendliness": 7,
    "nontraditional_background_friendliness": 4,
    "human_in_the_loop": 3,
    "linux_git_python_relevance": 3,
}

assert sum(_DIMENSION_WEIGHTS.values()) == 100, "Weights must sum to 100"

_DIMENSION_KEYWORDS: dict[str, list[str]] = {
    "agent_building": [
        "agent", "agentic", "ai agent", "build agent", "agent engineer",
        "agentic systems", "autonomous agent", "coding agent", "agent-based",
        "multi-agent", "agent workflow", "agent loop",
    ],
    "tool_use_and_integrations": [
        "tool calling", "tool use", "function calling", "mcp",
        "model context protocol", "api integration", "sdk", "tool-calling",
        "function call", "tool_calling", "plugin", "integration",
    ],
    "ai_assisted_development": [
        "claude code", "codex", "openai codex", "chatgpt", "ai-assisted",
        "ai coding", "coding agent", "ai-powered", "ai tools", "copilot",
        "github copilot", "ai pair programming",
    ],
    "workflow_orchestration": [
        "workflow", "orchestration", "pipeline", "automation", "task automation",
        "agentic workflow", "agent orchestration", "orchestrate", "coordinate",
        "multi-step", "chain", "sequence",
    ],
    "evals_and_verification": [
        "evaluation", "evals", "llm evals", "agent evaluation", "verification",
        "grounding", "llm evaluation", "eval", "benchmark", "quality assurance",
        "test generation", "automated testing", "ai testing",
    ],
    "reliability_and_recovery": [
        "reliability", "failure recovery", "fallback", "retry", "resilience",
        "fault tolerance", "error recovery", "guardrails", "safety", "robust",
        "indeterminate", "reconciliation", "recovery",
    ],
    "rapid_prototyping": [
        "prototype", "rapid prototyping", "proof of concept", "poc",
        "ship quickly", "fast iteration", "demo", "mvp", "iterate",
        "move fast", "quickly", "experimental",
    ],
    "applied_product_orientation": [
        "ai product", "ai-native", "applied ai", "production", "real-world",
        "production system", "product engineering", "ship", "end-to-end",
        "user-facing", "customer", "business impact",
    ],
    "portfolio_friendliness": [
        "portfolio", "github", "open source", "show your work",
        "demonstrated", "working system", "project", "side project",
        "personal project", "take home", "code sample", "showcase",
    ],
    "nontraditional_background_friendliness": [
        "self-taught", "bootcamp", "non-traditional", "unconventional",
        "experience over degree", "no degree required", "equivalent experience",
        "demonstrated ability", "alternative background", "diverse background",
        "skills over credentials",
    ],
    "human_in_the_loop": [
        "human in the loop", "approval", "human oversight", "human review",
        "human feedback", "hitl", "human-in-the-loop", "user approval",
        "review queue", "human validation",
    ],
    "linux_git_python_relevance": [
        "python", "linux", "git", "unix", "bash", "command line", "cli",
        "terminal", "version control",
    ],
}


@dataclass(frozen=True, slots=True)
class SimilarityDimension:
    name: str
    score: int  # 0-100
    matched_signals: tuple[str, ...]
    weight: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": self.score,
            "matched_signals": list(self.matched_signals),
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SimilarityDimension:
        return cls(
            name=data["name"],
            score=data["score"],
            matched_signals=tuple(data.get("matched_signals", [])),
            weight=data["weight"],
        )


@dataclass(frozen=True, slots=True)
class RootstockSimilarityScore:
    agent_building: SimilarityDimension
    tool_use_and_integrations: SimilarityDimension
    ai_assisted_development: SimilarityDimension
    workflow_orchestration: SimilarityDimension
    evals_and_verification: SimilarityDimension
    reliability_and_recovery: SimilarityDimension
    rapid_prototyping: SimilarityDimension
    applied_product_orientation: SimilarityDimension
    portfolio_friendliness: SimilarityDimension
    nontraditional_background_friendliness: SimilarityDimension
    human_in_the_loop: SimilarityDimension
    linux_git_python_relevance: SimilarityDimension
    total: int  # 0-100, weighted aggregate

    def dimensions(self) -> tuple[SimilarityDimension, ...]:
        return (
            self.agent_building,
            self.tool_use_and_integrations,
            self.ai_assisted_development,
            self.workflow_orchestration,
            self.evals_and_verification,
            self.reliability_and_recovery,
            self.rapid_prototyping,
            self.applied_product_orientation,
            self.portfolio_friendliness,
            self.nontraditional_background_friendliness,
            self.human_in_the_loop,
            self.linux_git_python_relevance,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_building": self.agent_building.to_dict(),
            "tool_use_and_integrations": self.tool_use_and_integrations.to_dict(),
            "ai_assisted_development": self.ai_assisted_development.to_dict(),
            "workflow_orchestration": self.workflow_orchestration.to_dict(),
            "evals_and_verification": self.evals_and_verification.to_dict(),
            "reliability_and_recovery": self.reliability_and_recovery.to_dict(),
            "rapid_prototyping": self.rapid_prototyping.to_dict(),
            "applied_product_orientation": self.applied_product_orientation.to_dict(),
            "portfolio_friendliness": self.portfolio_friendliness.to_dict(),
            "nontraditional_background_friendliness": self.nontraditional_background_friendliness.to_dict(),
            "human_in_the_loop": self.human_in_the_loop.to_dict(),
            "linux_git_python_relevance": self.linux_git_python_relevance.to_dict(),
            "total": self.total,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RootstockSimilarityScore:
        return cls(
            agent_building=SimilarityDimension.from_dict(data["agent_building"]),
            tool_use_and_integrations=SimilarityDimension.from_dict(data["tool_use_and_integrations"]),
            ai_assisted_development=SimilarityDimension.from_dict(data["ai_assisted_development"]),
            workflow_orchestration=SimilarityDimension.from_dict(data["workflow_orchestration"]),
            evals_and_verification=SimilarityDimension.from_dict(data["evals_and_verification"]),
            reliability_and_recovery=SimilarityDimension.from_dict(data["reliability_and_recovery"]),
            rapid_prototyping=SimilarityDimension.from_dict(data["rapid_prototyping"]),
            applied_product_orientation=SimilarityDimension.from_dict(data["applied_product_orientation"]),
            portfolio_friendliness=SimilarityDimension.from_dict(data["portfolio_friendliness"]),
            nontraditional_background_friendliness=SimilarityDimension.from_dict(
                data["nontraditional_background_friendliness"]
            ),
            human_in_the_loop=SimilarityDimension.from_dict(data["human_in_the_loop"]),
            linux_git_python_relevance=SimilarityDimension.from_dict(data["linux_git_python_relevance"]),
            total=data["total"],
        )


def _score_dimension(text: str, keywords: list[str], name: str) -> SimilarityDimension:
    """Score a single dimension by counting keyword hits in posting text."""
    matched = [kw for kw in keywords if kw in text]
    unique_hits = len(set(matched))
    # Each unique keyword match contributes to score; first hit is worth more
    score = min(100, unique_hits * 25 + (10 if unique_hits >= 1 else 0))
    return SimilarityDimension(
        name=name,
        score=score,
        matched_signals=tuple(dict.fromkeys(matched)),  # preserve order, dedupe
        weight=_DIMENSION_WEIGHTS[name],
    )


class RootstockSimilarityScorer:
    """Deterministic Rootstock similarity scorer. No LLM required."""

    def score(self, opportunity: JobOpportunity) -> RootstockSimilarityScore:
        text = opportunity.posting_text.lower()

        dims: dict[str, SimilarityDimension] = {}
        for dim_name, keywords in _DIMENSION_KEYWORDS.items():
            dims[dim_name] = _score_dimension(text, keywords, dim_name)

        total = int(
            sum(dims[k].score * _DIMENSION_WEIGHTS[k] for k in _DIMENSION_WEIGHTS) / 100
        )
        total = max(0, min(100, total))

        return RootstockSimilarityScore(
            agent_building=dims["agent_building"],
            tool_use_and_integrations=dims["tool_use_and_integrations"],
            ai_assisted_development=dims["ai_assisted_development"],
            workflow_orchestration=dims["workflow_orchestration"],
            evals_and_verification=dims["evals_and_verification"],
            reliability_and_recovery=dims["reliability_and_recovery"],
            rapid_prototyping=dims["rapid_prototyping"],
            applied_product_orientation=dims["applied_product_orientation"],
            portfolio_friendliness=dims["portfolio_friendliness"],
            nontraditional_background_friendliness=dims["nontraditional_background_friendliness"],
            human_in_the_loop=dims["human_in_the_loop"],
            linux_git_python_relevance=dims["linux_git_python_relevance"],
            total=total,
        )

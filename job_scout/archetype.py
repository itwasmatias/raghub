"""Canonical archetype preference model for MissionaryX Job Scout v0.1.

Represents the creator's preferred work: Agent / Applied AI Engineer (Rootstock-style).
Match is made on nature of the work, not on title alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class PreferenceClass(str, Enum):
    CAN_DO = "can_do"
    LIKES_DOING = "likes_doing"
    STRONG_PORTFOLIO_EVIDENCE = "strong_portfolio_evidence"
    INTERESTING_STRETCH = "interesting_stretch"


@dataclass(frozen=True, slots=True)
class ArchetypeSignal:
    """Named signal with preference class and detection keywords."""

    name: str
    preference_class: PreferenceClass
    keywords: tuple[str, ...]
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "preference_class": self.preference_class.value,
            "keywords": list(self.keywords),
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchetypeSignal:
        return cls(
            name=data["name"],
            preference_class=PreferenceClass(data["preference_class"]),
            keywords=tuple(data["keywords"]),
            weight=data.get("weight", 1.0),
        )


@dataclass(frozen=True, slots=True)
class ArchetypeProfile:
    """Complete preference/archetype model for opportunity discovery."""

    archetype_id: str
    description: str
    signals: tuple[ArchetypeSignal, ...]
    negative_indicators: tuple[str, ...]
    target_role_families: tuple[str, ...]

    def get_signals_by_class(self, cls: PreferenceClass) -> tuple[ArchetypeSignal, ...]:
        return tuple(s for s in self.signals if s.preference_class == cls)

    def all_positive_keywords(self) -> frozenset[str]:
        return frozenset(kw for s in self.signals for kw in s.keywords)

    def to_dict(self) -> dict[str, Any]:
        return {
            "archetype_id": self.archetype_id,
            "description": self.description,
            "signals": [s.to_dict() for s in self.signals],
            "negative_indicators": list(self.negative_indicators),
            "target_role_families": list(self.target_role_families),
        }


def rootstock_archetype() -> ArchetypeProfile:
    """Canonical archetype: Agent / Applied AI Engineer (Rootstock-style)."""
    signals = (
        ArchetypeSignal(
            "agent_building",
            PreferenceClass.LIKES_DOING,
            (
                "agent", "agentic", "ai agent", "build agent", "agent engineer",
                "agentic systems", "autonomous agent", "coding agent",
            ),
            weight=2.0,
        ),
        ArchetypeSignal(
            "tool_calling",
            PreferenceClass.LIKES_DOING,
            (
                "tool calling", "tool use", "function calling", "mcp",
                "model context protocol", "tool-calling", "tool_calling",
            ),
            weight=1.8,
        ),
        ArchetypeSignal(
            "workflow_orchestration",
            PreferenceClass.LIKES_DOING,
            (
                "workflow", "orchestration", "multi-agent", "agent orchestration",
                "pipeline automation", "task automation", "agentic workflow",
            ),
            weight=1.5,
        ),
        ArchetypeSignal(
            "ai_assisted_development",
            PreferenceClass.STRONG_PORTFOLIO_EVIDENCE,
            (
                "claude code", "codex", "openai codex", "chatgpt", "ai-assisted",
                "ai coding", "coding agent", "ai-powered development",
            ),
            weight=1.8,
        ),
        ArchetypeSignal(
            "evals_and_verification",
            PreferenceClass.LIKES_DOING,
            (
                "evaluation", "evals", "llm evals", "agent evaluation",
                "verification", "reliability", "grounding", "llm evaluation",
            ),
            weight=1.6,
        ),
        ArchetypeSignal(
            "llm_application_engineering",
            PreferenceClass.LIKES_DOING,
            (
                "llm", "large language model", "language model", "gpt-4",
                "claude", "anthropic", "openai", "prompt engineering",
            ),
            weight=1.4,
        ),
        ArchetypeSignal(
            "rag_and_retrieval",
            PreferenceClass.CAN_DO,
            (
                "rag", "retrieval augmented", "vector", "embedding",
                "semantic search", "vector database", "retrieval",
            ),
            weight=1.2,
        ),
        ArchetypeSignal(
            "rapid_prototyping",
            PreferenceClass.LIKES_DOING,
            (
                "prototype", "rapid prototyping", "proof of concept", "poc",
                "ship quickly", "fast iteration", "demo", "mvp",
            ),
            weight=1.4,
        ),
        ArchetypeSignal(
            "applied_product_orientation",
            PreferenceClass.LIKES_DOING,
            (
                "ai product", "ai-native", "applied ai", "product engineering",
                "production ai", "real-world", "production system",
            ),
            weight=1.5,
        ),
        ArchetypeSignal(
            "human_in_the_loop",
            PreferenceClass.STRONG_PORTFOLIO_EVIDENCE,
            (
                "human in the loop", "approval", "human oversight",
                "human review", "human feedback", "hitl", "human-in-the-loop",
            ),
            weight=1.3,
        ),
        ArchetypeSignal(
            "portfolio_based_hiring",
            PreferenceClass.LIKES_DOING,
            (
                "portfolio", "github", "open source", "show your work",
                "demonstrated projects", "working system", "project evidence",
            ),
            weight=1.6,
        ),
        ArchetypeSignal(
            "python_api_integration",
            PreferenceClass.CAN_DO,
            ("python", "api integration", "rest api", "sdk integration"),
            weight=1.2,
        ),
        ArchetypeSignal(
            "linux_git_workflow",
            PreferenceClass.CAN_DO,
            ("linux", "git", "unix", "bash", "command line", "cli"),
            weight=1.0,
        ),
        ArchetypeSignal(
            "local_models",
            PreferenceClass.INTERESTING_STRETCH,
            (
                "local model", "ollama", "open source model", "self-hosted model",
                "on-premise", "fine-tuning", "fine tuning", "lora",
            ),
            weight=1.1,
        ),
        ArchetypeSignal(
            "agent_observability",
            PreferenceClass.LIKES_DOING,
            (
                "observability", "monitoring", "logging", "tracing",
                "agent monitoring", "telemetry", "agent observability",
            ),
            weight=1.2,
        ),
        ArchetypeSignal(
            "failure_recovery",
            PreferenceClass.STRONG_PORTFOLIO_EVIDENCE,
            (
                "failure recovery", "fallback", "retry", "resilience",
                "fault tolerance", "error recovery", "indeterminate",
            ),
            weight=1.3,
        ),
    )

    negative_indicators = (
        "traditional backend with no ai",
        "pure frontend development",
        "pure mobile development",
        "pure data analyst",
        "traditional devops no ai",
        "ml research requiring phd",
        "commission-based sales",
        "sales representative",
        "annotation only",
        "content moderation",
        "generic it support",
        "senior distributed systems 8+ years",
    )

    target_role_families = (
        "Agent Engineer",
        "AI Agent Engineer",
        "Applied AI Engineer",
        "Agentic Systems Engineer",
        "AI Product Engineer",
        "AI Automation Engineer",
        "AI Workflow Engineer",
        "AgentOps Engineer",
        "AI Reliability Engineer",
        "AI Evaluation Engineer",
        "LLM Evaluation Engineer",
        "AI Evals Engineer",
        "AI Quality Engineer",
        "AI Operations Engineer",
        "LLM Operations Engineer",
        "AI Tooling Engineer",
        "AI Developer Experience",
        "AI Integration Engineer",
        "AI Implementation Engineer",
        "AI Solutions Engineer",
        "Technical AI Operations",
        "Prompt / Agent Workflow Engineer",
        "AI Prototyping Engineer",
        "AI Research Engineer",
        "Applied LLM Engineer",
        "AI Systems Specialist",
    )

    return ArchetypeProfile(
        archetype_id="rootstock_agent_applied_ai_engineer_v1",
        description=(
            "Agent / Applied AI Engineer archetype. "
            "Builds production AI agents, agentic workflows, evaluation systems, "
            "and AI-native products. Portfolio and demonstrated systems matter more "
            "than traditional pedigree."
        ),
        signals=signals,
        negative_indicators=negative_indicators,
        target_role_families=target_role_families,
    )

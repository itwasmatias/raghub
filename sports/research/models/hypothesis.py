from dataclasses import dataclass


@dataclass
class Hypothesis:
    title: str
    statement: str
    rationale: str
    confidence: float
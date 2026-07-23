from dataclasses import dataclass


@dataclass
class Experiment:
    name: str
    hypothesis: str
    methodology: str
    metrics: list[str]
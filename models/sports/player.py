from dataclasses import dataclass, field


@dataclass(slots=True)
class Player:
    name: str
    team: str
    performance_history: list[float] = field(
        default_factory=list
    )
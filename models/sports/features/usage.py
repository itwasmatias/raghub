from dataclasses import dataclass


@dataclass(slots=True)
class UsageFeatures:

    minutes_change: float = 0.0

    usage_change: float = 0.0

    role_change: float = 0.0
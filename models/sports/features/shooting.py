from dataclasses import dataclass


@dataclass(slots=True)
class ShootingFeatures:

    effective_field_goal: float = 0.0

    true_shooting: float = 0.0

    shot_quality: float = 0.0
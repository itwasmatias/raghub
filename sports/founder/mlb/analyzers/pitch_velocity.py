from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


ZERO = Decimal("0")
ONE = Decimal("1")
NEUTRAL = Decimal("0.50")
THREE_PLACES = Decimal("0.001")


def _clamp(value: Decimal) -> Decimal:
    return max(ZERO, min(ONE, value))


def _rounded(value: Decimal) -> Decimal:
    return value.quantize(
        THREE_PLACES,
        rounding=ROUND_HALF_UP,
    )


def _validate_nonnegative_decimal(
    name: str,
    value: Decimal | None,
) -> None:
    if value is not None and value < ZERO:
        raise ValueError(f"{name} cannot be negative")


def _validate_nonnegative_integer(
    name: str,
    value: int,
) -> None:
    if value < 0:
        raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True, slots=True)
class MlbPitchVelocitySample:
    pitcher_id: str
    pitcher_name: str
    baseline_velocity: Decimal | None
    recent_velocity: Decimal | None
    maximum_velocity: Decimal | None
    early_inning_velocity: Decimal | None
    late_inning_velocity: Decimal | None
    pitches_observed: int
    games_observed: int

    def __post_init__(self) -> None:
        if not self.pitcher_id.strip():
            raise ValueError("pitcher_id is required")

        if not self.pitcher_name.strip():
            raise ValueError("pitcher_name is required")

        for name, value in (
            ("baseline_velocity", self.baseline_velocity),
            ("recent_velocity", self.recent_velocity),
            ("maximum_velocity", self.maximum_velocity),
            ("early_inning_velocity", self.early_inning_velocity),
            ("late_inning_velocity", self.late_inning_velocity),
        ):
            _validate_nonnegative_decimal(name, value)

        _validate_nonnegative_integer(
            "pitches_observed",
            self.pitches_observed,
        )
        _validate_nonnegative_integer(
            "games_observed",
            self.games_observed,
        )


@dataclass(frozen=True, slots=True)
class MlbPitchVelocityAssessment:
    pitcher_id: str
    pitcher_name: str
    velocity_change: Decimal | None
    late_inning_velocity_change: Decimal | None
    velocity_score: Decimal
    velocity_retention_score: Decimal
    reliability_score: Decimal
    raw_score: Decimal
    overall_score: Decimal
    data_quality: str
    evidence: tuple[str, ...]
    warnings: tuple[str, ...]


class MlbPitchVelocityAnalyzer:
    VELOCITY_WEIGHT = Decimal("0.65")
    RETENTION_WEIGHT = Decimal("0.35")

    def analyze(
        self,
        sample: MlbPitchVelocitySample,
    ) -> MlbPitchVelocityAssessment:
        evidence: list[str] = []
        warnings: list[str] = []

        velocity_change = self._velocity_change(sample)
        late_inning_change = self._late_inning_change(sample)

        velocity_score = self._velocity_score(
            velocity_change,
            sample.maximum_velocity,
            evidence,
            warnings,
        )
        retention_score = self._retention_score(
            late_inning_change,
            evidence,
            warnings,
        )
        reliability = self._reliability_score(
            sample,
            warnings,
        )

        raw_score = (
            velocity_score * self.VELOCITY_WEIGHT
            + retention_score * self.RETENTION_WEIGHT
        )

        overall_score = (
            NEUTRAL
            + (raw_score - NEUTRAL) * reliability
        )

        data_quality = self._data_quality(reliability)

        if not evidence:
            evidence.append(
                "Velocity assessment remains neutral because usable "
                "velocity evidence is unavailable."
            )

        return MlbPitchVelocityAssessment(
            pitcher_id=sample.pitcher_id,
            pitcher_name=sample.pitcher_name,
            velocity_change=(
                _rounded(velocity_change)
                if velocity_change is not None
                else None
            ),
            late_inning_velocity_change=(
                _rounded(late_inning_change)
                if late_inning_change is not None
                else None
            ),
            velocity_score=_rounded(velocity_score),
            velocity_retention_score=_rounded(retention_score),
            reliability_score=_rounded(reliability),
            raw_score=_rounded(raw_score),
            overall_score=_rounded(_clamp(overall_score)),
            data_quality=data_quality,
            evidence=tuple(evidence),
            warnings=tuple(warnings),
        )

    def _velocity_change(
        self,
        sample: MlbPitchVelocitySample,
    ) -> Decimal | None:
        if (
            sample.baseline_velocity is None
            or sample.recent_velocity is None
        ):
            return None

        return (
            sample.recent_velocity
            - sample.baseline_velocity
        )

    def _late_inning_change(
        self,
        sample: MlbPitchVelocitySample,
    ) -> Decimal | None:
        if (
            sample.early_inning_velocity is None
            or sample.late_inning_velocity is None
        ):
            return None

        return (
            sample.late_inning_velocity
            - sample.early_inning_velocity
        )

    def _velocity_score(
        self,
        velocity_change: Decimal | None,
        maximum_velocity: Decimal | None,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        if velocity_change is None:
            warnings.append(
                "Baseline or recent velocity is missing."
            )
            return NEUTRAL

        score = _clamp(
            NEUTRAL
            + velocity_change / Decimal("6")
        )

        if maximum_velocity is not None:
            if maximum_velocity >= Decimal("98"):
                score = _clamp(score + Decimal("0.05"))
                evidence.append(
                    "Maximum velocity shows premium raw velocity."
                )
            elif maximum_velocity < Decimal("90"):
                warnings.append(
                    "Maximum velocity is below the expected major-league "
                    "starting-pitcher range."
                )

        if velocity_change >= Decimal("1"):
            evidence.append(
                "Recent velocity is meaningfully above the pitcher's "
                "own baseline."
            )
        elif velocity_change <= Decimal("-1.5"):
            warnings.append(
                "Recent velocity shows a meaningful decline from baseline."
            )
        else:
            evidence.append(
                "Recent velocity is near the pitcher's established baseline."
            )

        return score

    def _retention_score(
        self,
        late_inning_change: Decimal | None,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        if late_inning_change is None:
            warnings.append(
                "Early- or late-inning velocity is missing."
            )
            return NEUTRAL

        score = _clamp(
            ONE
            + late_inning_change / Decimal("4")
        )

        if late_inning_change >= Decimal("-0.75"):
            evidence.append(
                "The pitcher retains velocity effectively into later innings."
            )
        elif late_inning_change <= Decimal("-2"):
            warnings.append(
                "Significant late-inning velocity fade was observed."
            )
        else:
            warnings.append(
                "Moderate late-inning velocity fade was observed."
            )

        return score

    def _reliability_score(
        self,
        sample: MlbPitchVelocitySample,
        warnings: list[str],
    ) -> Decimal:
        available_fields = sum(
            value is not None
            for value in (
                sample.baseline_velocity,
                sample.recent_velocity,
                sample.maximum_velocity,
                sample.early_inning_velocity,
                sample.late_inning_velocity,
            )
        )
        field_coverage = (
            Decimal(available_fields)
            / Decimal("5")
        )
        pitch_support = _clamp(
            Decimal(sample.pitches_observed)
            / Decimal("180")
        )
        game_support = _clamp(
            Decimal(sample.games_observed)
            / Decimal("5")
        )

        reliability = (
            field_coverage * Decimal("0.40")
            + pitch_support * Decimal("0.35")
            + game_support * Decimal("0.25")
        )

        if (
            sample.pitches_observed < 60
            or sample.games_observed < 3
        ):
            warnings.append(
                "Velocity assessment is based on a limited sample."
            )

        return _clamp(reliability)

    def _data_quality(
        self,
        reliability: Decimal,
    ) -> str:
        if reliability >= Decimal("0.80"):
            return "HIGH"

        if reliability >= Decimal("0.50"):
            return "MEDIUM"

        return "LOW"

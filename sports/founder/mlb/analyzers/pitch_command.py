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


def _validate_probability(
    name: str,
    value: Decimal | None,
) -> None:
    if value is None:
        return

    if value < ZERO or value > ONE:
        raise ValueError(f"{name} must be between 0 and 1")


def _validate_nonnegative_integer(
    name: str,
    value: int,
) -> None:
    if value < 0:
        raise ValueError(f"{name} cannot be negative")


def _weighted_average(
    components: tuple[
        tuple[Decimal | None, Decimal],
        ...,
    ],
) -> Decimal:
    weighted_total = ZERO
    weight_total = ZERO

    for value, weight in components:
        if value is None:
            continue

        weighted_total += value * weight
        weight_total += weight

    if weight_total == ZERO:
        return NEUTRAL

    return _clamp(weighted_total / weight_total)


@dataclass(frozen=True, slots=True)
class MlbPitchCommandSample:
    pitcher_id: str
    pitcher_name: str
    strike_rate: Decimal | None
    walk_rate: Decimal | None
    first_pitch_strike_rate: Decimal | None
    zone_rate: Decimal | None
    edge_rate: Decimal | None
    heart_rate: Decimal | None
    mistake_rate: Decimal | None
    location_quality: Decimal | None
    pitches_observed: int
    games_observed: int

    def __post_init__(self) -> None:
        if not self.pitcher_id.strip():
            raise ValueError("pitcher_id is required")

        if not self.pitcher_name.strip():
            raise ValueError("pitcher_name is required")

        for name, value in (
            ("strike_rate", self.strike_rate),
            ("walk_rate", self.walk_rate),
            (
                "first_pitch_strike_rate",
                self.first_pitch_strike_rate,
            ),
            ("zone_rate", self.zone_rate),
            ("edge_rate", self.edge_rate),
            ("heart_rate", self.heart_rate),
            ("mistake_rate", self.mistake_rate),
            ("location_quality", self.location_quality),
        ):
            _validate_probability(name, value)

        _validate_nonnegative_integer(
            "pitches_observed",
            self.pitches_observed,
        )
        _validate_nonnegative_integer(
            "games_observed",
            self.games_observed,
        )


@dataclass(frozen=True, slots=True)
class MlbPitchCommandAssessment:
    pitcher_id: str
    pitcher_name: str
    control_score: Decimal
    command_score: Decimal
    reliability_score: Decimal
    raw_score: Decimal
    overall_score: Decimal
    data_quality: str
    evidence: tuple[str, ...]
    warnings: tuple[str, ...]


class MlbPitchCommandAnalyzer:
    CONTROL_WEIGHT = Decimal("0.45")
    COMMAND_WEIGHT = Decimal("0.55")

    def analyze(
        self,
        sample: MlbPitchCommandSample,
    ) -> MlbPitchCommandAssessment:
        evidence: list[str] = []
        warnings: list[str] = []

        control_score = self._control_score(
            sample,
            evidence,
            warnings,
        )
        command_score = self._command_score(
            sample,
            evidence,
            warnings,
        )
        reliability = self._reliability_score(
            sample,
            warnings,
        )

        raw_score = (
            control_score * self.CONTROL_WEIGHT
            + command_score * self.COMMAND_WEIGHT
        )

        overall_score = (
            NEUTRAL
            + (raw_score - NEUTRAL) * reliability
        )

        if (
            control_score >= Decimal("0.70")
            and command_score < Decimal("0.50")
        ):
            warnings.append(
                "Strong strike-throwing control does not translate "
                "to strong command because location quality is weak."
            )

        if not evidence:
            evidence.append(
                "Command and control remain neutral because usable "
                "location evidence is unavailable."
            )

        return MlbPitchCommandAssessment(
            pitcher_id=sample.pitcher_id,
            pitcher_name=sample.pitcher_name,
            control_score=_rounded(control_score),
            command_score=_rounded(command_score),
            reliability_score=_rounded(reliability),
            raw_score=_rounded(raw_score),
            overall_score=_rounded(_clamp(overall_score)),
            data_quality=self._data_quality(reliability),
            evidence=tuple(evidence),
            warnings=tuple(warnings),
        )

    def _control_score(
        self,
        sample: MlbPitchCommandSample,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        strike_score = (
            _clamp(
                (
                    sample.strike_rate
                    - Decimal("0.55")
                )
                / Decimal("0.20")
            )
            if sample.strike_rate is not None
            else None
        )

        walk_score = (
            _clamp(
                (
                    Decimal("0.15")
                    - sample.walk_rate
                )
                / Decimal("0.13")
            )
            if sample.walk_rate is not None
            else None
        )

        first_pitch_score = (
            _clamp(
                (
                    sample.first_pitch_strike_rate
                    - Decimal("0.48")
                )
                / Decimal("0.24")
            )
            if sample.first_pitch_strike_rate is not None
            else None
        )

        zone_score = (
            _clamp(
                ONE
                - abs(
                    sample.zone_rate
                    - Decimal("0.48")
                )
                / Decimal("0.20")
            )
            if sample.zone_rate is not None
            else None
        )

        score = _weighted_average(
            (
                (strike_score, Decimal("0.35")),
                (walk_score, Decimal("0.35")),
                (first_pitch_score, Decimal("0.20")),
                (zone_score, Decimal("0.10")),
            )
        )

        if all(
            value is None
            for value in (
                sample.strike_rate,
                sample.walk_rate,
                sample.first_pitch_strike_rate,
                sample.zone_rate,
            )
        ):
            warnings.append(
                "Strike-throwing and walk-control data are missing."
            )
        elif score >= Decimal("0.70"):
            evidence.append(
                "Strike rate, walk suppression, and early-count "
                "performance indicate strong control."
            )
        elif score <= Decimal("0.35"):
            warnings.append(
                "Strike-throwing and walk results indicate weak control."
            )
        else:
            evidence.append(
                "Strike-throwing control is near the expected range."
            )

        return score

    def _command_score(
        self,
        sample: MlbPitchCommandSample,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        edge_score = (
            _clamp(
                (
                    sample.edge_rate
                    - Decimal("0.25")
                )
                / Decimal("0.25")
            )
            if sample.edge_rate is not None
            else None
        )

        heart_avoidance_score = (
            _clamp(
                (
                    Decimal("0.42")
                    - sample.heart_rate
                )
                / Decimal("0.25")
            )
            if sample.heart_rate is not None
            else None
        )

        mistake_avoidance_score = (
            _clamp(
                (
                    Decimal("0.20")
                    - sample.mistake_rate
                )
                / Decimal("0.17")
            )
            if sample.mistake_rate is not None
            else None
        )

        location_score = (
            sample.location_quality
            if sample.location_quality is not None
            else None
        )

        score = _weighted_average(
            (
                (edge_score, Decimal("0.30")),
                (
                    heart_avoidance_score,
                    Decimal("0.25"),
                ),
                (
                    mistake_avoidance_score,
                    Decimal("0.25"),
                ),
                (location_score, Decimal("0.20")),
            )
        )

        if all(
            value is None
            for value in (
                sample.edge_rate,
                sample.heart_rate,
                sample.mistake_rate,
                sample.location_quality,
            )
        ):
            warnings.append(
                "Pitch-location and command data are missing."
            )
            return score

        if sample.edge_rate is not None:
            if sample.edge_rate >= Decimal("0.43"):
                evidence.append(
                    "The pitcher consistently works near the edges."
                )
            elif sample.edge_rate <= Decimal("0.30"):
                warnings.append(
                    "Low edge usage limits demonstrated command."
                )

        if sample.heart_rate is not None:
            if sample.heart_rate <= Decimal("0.22"):
                evidence.append(
                    "The pitcher avoids the heart of the strike zone."
                )
            elif sample.heart_rate >= Decimal("0.33"):
                warnings.append(
                    "Too many pitches are located over the heart "
                    "of the strike zone."
                )

        if sample.mistake_rate is not None:
            if sample.mistake_rate <= Decimal("0.06"):
                evidence.append(
                    "The pitcher limits mistake pitches."
                )
            elif sample.mistake_rate >= Decimal("0.12"):
                warnings.append(
                    "High mistake-pitch rate reduces command quality."
                )

        if sample.location_quality is not None:
            if sample.location_quality >= Decimal("0.70"):
                evidence.append(
                    "Overall pitch-location quality is strong."
                )
            elif sample.location_quality < Decimal("0.45"):
                warnings.append(
                    "Overall pitch-location quality is weak."
                )

        if score <= Decimal("0.40"):
            warnings.append(
                "Command quality is below the expected range."
            )

        return score

    def _reliability_score(
        self,
        sample: MlbPitchCommandSample,
        warnings: list[str],
    ) -> Decimal:
        available_fields = sum(
            value is not None
            for value in (
                sample.strike_rate,
                sample.walk_rate,
                sample.first_pitch_strike_rate,
                sample.zone_rate,
                sample.edge_rate,
                sample.heart_rate,
                sample.mistake_rate,
                sample.location_quality,
            )
        )

        field_coverage = (
            Decimal(available_fields)
            / Decimal("8")
        )
        pitch_support = _clamp(
            Decimal(sample.pitches_observed)
            / Decimal("420")
        )
        game_support = _clamp(
            Decimal(sample.games_observed)
            / Decimal("7")
        )

        reliability = (
            field_coverage * Decimal("0.45")
            + pitch_support * Decimal("0.35")
            + game_support * Decimal("0.20")
        )

        if (
            sample.pitches_observed < 100
            or sample.games_observed < 3
        ):
            warnings.append(
                "Command assessment is based on a limited sample."
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

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


ZERO = Decimal("0")
ONE = Decimal("1")
NEUTRAL = Decimal("0.50")
THREE_PLACES = Decimal("0.001")


PITCH_TYPE_ALIASES = {
    "FF": "FOUR_SEAM",
    "FOUR_SEAM": "FOUR_SEAM",
    "FOUR-SEAM": "FOUR_SEAM",
    "FOUR SEAM": "FOUR_SEAM",
    "4-SEAM": "FOUR_SEAM",
    "4 SEAM": "FOUR_SEAM",
    "FT": "TWO_SEAM",
    "TWO_SEAM": "TWO_SEAM",
    "TWO-SEAM": "TWO_SEAM",
    "TWO SEAM": "TWO_SEAM",
    "2-SEAM": "TWO_SEAM",
    "2 SEAM": "TWO_SEAM",
    "SI": "SINKER",
    "SINKER": "SINKER",
    "FC": "CUTTER",
    "CUTTER": "CUTTER",
    "SL": "SLIDER",
    "SLIDER": "SLIDER",
    "ST": "SWEEPER",
    "SWEEPER": "SWEEPER",
    "CU": "CURVEBALL",
    "CURVE": "CURVEBALL",
    "CURVEBALL": "CURVEBALL",
    "KC": "KNUCKLE_CURVE",
    "KNUCKLE_CURVE": "KNUCKLE_CURVE",
    "KNUCKLE CURVE": "KNUCKLE_CURVE",
    "SLURVE": "SLURVE",
    "CH": "CHANGEUP",
    "CHANGE": "CHANGEUP",
    "CHANGEUP": "CHANGEUP",
    "FS": "SPLITTER",
    "SPLITTER": "SPLITTER",
    "FO": "FORKBALL",
    "FORKBALL": "FORKBALL",
    "KN": "KNUCKLEBALL",
    "KNUCKLEBALL": "KNUCKLEBALL",
    "SCREWBALL": "SCREWBALL",
    "EP": "EEPHUS",
    "EEPHUS": "EEPHUS",
    "UNKNOWN": "UNKNOWN",
}


def _clamp(value: Decimal) -> Decimal:
    return max(ZERO, min(ONE, value))


def _rounded(value: Decimal) -> Decimal:
    return value.quantize(
        THREE_PLACES,
        rounding=ROUND_HALF_UP,
    )


def _normalize_pitch_type(value: str) -> str:
    normalized = value.strip().upper().replace("/", "_")
    return PITCH_TYPE_ALIASES.get(normalized, "UNKNOWN")


def _validate_probability(
    name: str,
    value: Decimal | None,
) -> None:
    if value is None:
        return

    if value < ZERO or value > ONE:
        raise ValueError(f"{name} must be between 0 and 1")


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
class MlbPitchProfile:
    pitch_type: str
    usage_rate: Decimal
    previous_usage_rate: Decimal | None
    average_velocity: Decimal | None
    maximum_velocity: Decimal | None
    spin_rate: Decimal | None
    horizontal_movement: Decimal | None
    induced_vertical_break: Decimal | None
    whiff_rate: Decimal | None
    chase_rate: Decimal | None
    csw_rate: Decimal | None
    ground_ball_rate: Decimal | None
    hard_hit_rate: Decimal | None
    barrel_rate: Decimal | None
    batting_average_allowed: Decimal | None
    slugging_allowed: Decimal | None
    xwoba: Decimal | None
    run_value: Decimal | None
    pitches_observed: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "pitch_type",
            _normalize_pitch_type(self.pitch_type),
        )

        for name, value in (
            ("usage_rate", self.usage_rate),
            ("previous_usage_rate", self.previous_usage_rate),
            ("whiff_rate", self.whiff_rate),
            ("chase_rate", self.chase_rate),
            ("csw_rate", self.csw_rate),
            ("ground_ball_rate", self.ground_ball_rate),
            ("hard_hit_rate", self.hard_hit_rate),
            ("barrel_rate", self.barrel_rate),
            (
                "batting_average_allowed",
                self.batting_average_allowed,
            ),
            ("xwoba", self.xwoba),
        ):
            _validate_probability(name, value)

        for name, value in (
            ("average_velocity", self.average_velocity),
            ("maximum_velocity", self.maximum_velocity),
            ("spin_rate", self.spin_rate),
            ("slugging_allowed", self.slugging_allowed),
        ):
            _validate_nonnegative_decimal(name, value)

        _validate_nonnegative_integer(
            "pitches_observed",
            self.pitches_observed,
        )


@dataclass(frozen=True, slots=True)
class MlbPitchArsenalSample:
    pitcher_id: str
    pitcher_name: str
    pitches: tuple[MlbPitchProfile, ...]
    total_pitches_observed: int
    games_observed: int

    def __post_init__(self) -> None:
        if not self.pitcher_id.strip():
            raise ValueError("pitcher_id is required")

        if not self.pitcher_name.strip():
            raise ValueError("pitcher_name is required")

        _validate_nonnegative_integer(
            "total_pitches_observed",
            self.total_pitches_observed,
        )
        _validate_nonnegative_integer(
            "games_observed",
            self.games_observed,
        )


@dataclass(frozen=True, slots=True)
class MlbPitchArsenalAssessment:
    pitcher_id: str
    pitcher_name: str
    normalized_pitch_types: tuple[str, ...]
    effective_pitch_count: int
    arsenal_depth_score: Decimal
    pitch_quality_score: Decimal
    pitch_mix_stability_score: Decimal
    reliability_score: Decimal
    raw_score: Decimal
    overall_score: Decimal
    data_quality: str
    evidence: tuple[str, ...]
    warnings: tuple[str, ...]


class MlbPitchArsenalAnalyzer:
    DEPTH_WEIGHT = Decimal("0.25")
    QUALITY_WEIGHT = Decimal("0.55")
    STABILITY_WEIGHT = Decimal("0.20")

    def analyze(
        self,
        sample: MlbPitchArsenalSample,
    ) -> MlbPitchArsenalAssessment:
        evidence: list[str] = []
        warnings: list[str] = []

        normalized_types = tuple(
            dict.fromkeys(
                pitch.pitch_type
                for pitch in sample.pitches
            )
        )

        effective_pitch_count = self._effective_pitch_count(
            sample.pitches
        )
        depth_score = self._depth_score(
            effective_pitch_count,
            evidence,
            warnings,
        )
        quality_score = self._arsenal_quality_score(
            sample.pitches,
            evidence,
            warnings,
        )
        stability_score = self._stability_score(
            sample.pitches,
            evidence,
            warnings,
        )
        reliability = self._reliability_score(
            sample,
            warnings,
        )

        raw_score = (
            depth_score * self.DEPTH_WEIGHT
            + quality_score * self.QUALITY_WEIGHT
            + stability_score * self.STABILITY_WEIGHT
        )

        overall_score = (
            NEUTRAL
            + (raw_score - NEUTRAL) * reliability
        )

        if not evidence:
            evidence.append(
                "Arsenal assessment remains neutral because usable "
                "pitch evidence is unavailable."
            )

        return MlbPitchArsenalAssessment(
            pitcher_id=sample.pitcher_id,
            pitcher_name=sample.pitcher_name,
            normalized_pitch_types=normalized_types,
            effective_pitch_count=effective_pitch_count,
            arsenal_depth_score=_rounded(depth_score),
            pitch_quality_score=_rounded(quality_score),
            pitch_mix_stability_score=_rounded(stability_score),
            reliability_score=_rounded(reliability),
            raw_score=_rounded(raw_score),
            overall_score=_rounded(_clamp(overall_score)),
            data_quality=self._data_quality(reliability),
            evidence=tuple(evidence),
            warnings=tuple(warnings),
        )

    def _effective_pitch_count(
        self,
        pitches: tuple[MlbPitchProfile, ...],
    ) -> int:
        return sum(
            pitch.usage_rate >= Decimal("0.10")
            and pitch.pitches_observed >= 20
            for pitch in pitches
        )

    def _depth_score(
        self,
        effective_pitch_count: int,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        score = _clamp(
            Decimal(effective_pitch_count)
            / Decimal("4")
        )

        if effective_pitch_count >= 4:
            evidence.append(
                "The pitcher has a deep four-or-more-pitch arsenal."
            )
        elif effective_pitch_count >= 3:
            evidence.append(
                "The pitcher has three established usable pitches."
            )
        elif effective_pitch_count <= 1:
            warnings.append(
                "The pitcher has limited demonstrated arsenal depth."
            )

        return score

    def _arsenal_quality_score(
        self,
        pitches: tuple[MlbPitchProfile, ...],
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        weighted_total = ZERO
        usage_total = ZERO

        for pitch in pitches:
            pitch_score = self._pitch_quality_score(pitch)

            if pitch_score is None or pitch.usage_rate <= ZERO:
                continue

            weighted_total += pitch_score * pitch.usage_rate
            usage_total += pitch.usage_rate

        if usage_total == ZERO:
            warnings.append(
                "Pitch-quality metrics are unavailable."
            )
            return NEUTRAL

        score = _clamp(weighted_total / usage_total)

        if score >= Decimal("0.70"):
            evidence.append(
                "Usage-weighted pitch quality is strong."
            )
        elif score <= Decimal("0.35"):
            warnings.append(
                "Usage-weighted pitch quality is weak."
            )
        else:
            evidence.append(
                "Usage-weighted pitch quality is near league-average."
            )

        return score

    def _pitch_quality_score(
        self,
        pitch: MlbPitchProfile,
    ) -> Decimal | None:
        components: list[Decimal] = []

        if pitch.whiff_rate is not None:
            components.append(
                _clamp(
                    (
                        pitch.whiff_rate
                        - Decimal("0.15")
                    )
                    / Decimal("0.30")
                )
            )

        if pitch.chase_rate is not None:
            components.append(
                _clamp(
                    (
                        pitch.chase_rate
                        - Decimal("0.20")
                    )
                    / Decimal("0.25")
                )
            )

        if pitch.csw_rate is not None:
            components.append(
                _clamp(
                    (
                        pitch.csw_rate
                        - Decimal("0.20")
                    )
                    / Decimal("0.20")
                )
            )

        if pitch.hard_hit_rate is not None:
            components.append(
                _clamp(
                    (
                        Decimal("0.50")
                        - pitch.hard_hit_rate
                    )
                    / Decimal("0.30")
                )
            )

        if pitch.barrel_rate is not None:
            components.append(
                _clamp(
                    (
                        Decimal("0.15")
                        - pitch.barrel_rate
                    )
                    / Decimal("0.12")
                )
            )

        if pitch.batting_average_allowed is not None:
            components.append(
                _clamp(
                    (
                        Decimal("0.350")
                        - pitch.batting_average_allowed
                    )
                    / Decimal("0.200")
                )
            )

        if pitch.slugging_allowed is not None:
            components.append(
                _clamp(
                    (
                        Decimal("0.600")
                        - pitch.slugging_allowed
                    )
                    / Decimal("0.350")
                )
            )

        if pitch.xwoba is not None:
            components.append(
                _clamp(
                    (
                        Decimal("0.420")
                        - pitch.xwoba
                    )
                    / Decimal("0.220")
                )
            )

        if pitch.run_value is not None:
            components.append(
                _clamp(
                    NEUTRAL
                    - pitch.run_value / Decimal("20")
                )
            )

        if not components:
            return None

        return sum(components, ZERO) / Decimal(len(components))

    def _stability_score(
        self,
        pitches: tuple[MlbPitchProfile, ...],
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        changes = [
            abs(
                pitch.usage_rate
                - pitch.previous_usage_rate
            )
            for pitch in pitches
            if pitch.previous_usage_rate is not None
        ]

        if not changes:
            warnings.append(
                "Previous pitch-mix usage data are unavailable."
            )
            return NEUTRAL

        average_change = (
            sum(changes, ZERO)
            / Decimal(len(changes))
        )
        score = _clamp(
            ONE
            - average_change / Decimal("0.50")
        )

        if average_change <= Decimal("0.05"):
            evidence.append(
                "Pitch mix is stable relative to the prior sample."
            )
        elif average_change >= Decimal("0.15"):
            warnings.append(
                "Pitch mix has changed substantially from the prior sample."
            )

        return score

    def _reliability_score(
        self,
        sample: MlbPitchArsenalSample,
        warnings: list[str],
    ) -> Decimal:
        measurable_fields = 0
        available_fields = 0

        for pitch in sample.pitches:
            quality_values = (
                pitch.average_velocity,
                pitch.maximum_velocity,
                pitch.spin_rate,
                pitch.horizontal_movement,
                pitch.induced_vertical_break,
                pitch.whiff_rate,
                pitch.chase_rate,
                pitch.csw_rate,
                pitch.ground_ball_rate,
                pitch.hard_hit_rate,
                pitch.barrel_rate,
                pitch.batting_average_allowed,
                pitch.slugging_allowed,
                pitch.xwoba,
                pitch.run_value,
            )
            measurable_fields += len(quality_values)
            available_fields += sum(
                value is not None
                for value in quality_values
            )

        if measurable_fields == 0:
            field_coverage = ZERO
        else:
            field_coverage = (
                Decimal(available_fields)
                / Decimal(measurable_fields)
            )

        pitch_support = _clamp(
            Decimal(sample.total_pitches_observed)
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
            sample.total_pitches_observed < 100
            or sample.games_observed < 3
        ):
            warnings.append(
                "Arsenal assessment is based on a limited sample."
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

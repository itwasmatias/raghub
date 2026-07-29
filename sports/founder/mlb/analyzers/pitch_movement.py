from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


ZERO = Decimal("0")
ONE = Decimal("1")
NEUTRAL = Decimal("0.50")
THREE_PLACES = Decimal("0.001")

SUPPORTED_PITCH_TYPES = {
    "FOUR_SEAM",
    "SINKER",
    "CUTTER",
    "SLIDER",
    "SWEEPER",
    "CURVEBALL",
    "CHANGEUP",
    "SPLITTER",
}


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
class MlbPitchMovementSample:
    pitcher_id: str
    pitcher_name: str
    pitch_type: str
    baseline_spin_rate: Decimal | None
    recent_spin_rate: Decimal | None
    horizontal_movement: Decimal | None
    induced_vertical_break: Decimal | None
    release_point_variance: Decimal | None
    extension: Decimal | None
    pitches_observed: int
    games_observed: int

    def __post_init__(self) -> None:
        if not self.pitcher_id.strip():
            raise ValueError("pitcher_id is required")

        if not self.pitcher_name.strip():
            raise ValueError("pitcher_name is required")

        if self.pitch_type not in SUPPORTED_PITCH_TYPES:
            raise ValueError("pitch_type is unsupported")

        for name, value in (
            ("baseline_spin_rate", self.baseline_spin_rate),
            ("recent_spin_rate", self.recent_spin_rate),
            ("release_point_variance", self.release_point_variance),
            ("extension", self.extension),
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
class MlbPitchMovementAssessment:
    pitcher_id: str
    pitcher_name: str
    pitch_type: str
    spin_change: Decimal | None
    spin_score: Decimal
    movement_score: Decimal
    release_consistency_score: Decimal
    extension_score: Decimal
    reliability_score: Decimal
    raw_score: Decimal
    overall_score: Decimal
    data_quality: str
    evidence: tuple[str, ...]
    warnings: tuple[str, ...]


class MlbPitchMovementAnalyzer:
    SPIN_WEIGHT = Decimal("0.25")
    MOVEMENT_WEIGHT = Decimal("0.40")
    RELEASE_WEIGHT = Decimal("0.20")
    EXTENSION_WEIGHT = Decimal("0.15")

    def analyze(
        self,
        sample: MlbPitchMovementSample,
    ) -> MlbPitchMovementAssessment:
        evidence: list[str] = []
        warnings: list[str] = []

        spin_change = self._spin_change(sample)
        spin_score = self._spin_score(
            spin_change,
            evidence,
            warnings,
        )
        movement_score = self._movement_score(
            sample,
            evidence,
            warnings,
        )
        release_score = self._release_score(
            sample.release_point_variance,
            evidence,
            warnings,
        )
        extension_score = self._extension_score(
            sample.extension,
            evidence,
            warnings,
        )
        reliability = self._reliability_score(
            sample,
            warnings,
        )

        raw_score = (
            spin_score * self.SPIN_WEIGHT
            + movement_score * self.MOVEMENT_WEIGHT
            + release_score * self.RELEASE_WEIGHT
            + extension_score * self.EXTENSION_WEIGHT
        )

        overall_score = (
            NEUTRAL
            + (raw_score - NEUTRAL) * reliability
        )

        if not evidence:
            evidence.append(
                "Movement assessment remains neutral because usable "
                "movement evidence is unavailable."
            )

        return MlbPitchMovementAssessment(
            pitcher_id=sample.pitcher_id,
            pitcher_name=sample.pitcher_name,
            pitch_type=sample.pitch_type,
            spin_change=(
                _rounded(spin_change)
                if spin_change is not None
                else None
            ),
            spin_score=_rounded(spin_score),
            movement_score=_rounded(movement_score),
            release_consistency_score=_rounded(release_score),
            extension_score=_rounded(extension_score),
            reliability_score=_rounded(reliability),
            raw_score=_rounded(raw_score),
            overall_score=_rounded(_clamp(overall_score)),
            data_quality=self._data_quality(reliability),
            evidence=tuple(evidence),
            warnings=tuple(warnings),
        )

    def _spin_change(
        self,
        sample: MlbPitchMovementSample,
    ) -> Decimal | None:
        if (
            sample.baseline_spin_rate is None
            or sample.recent_spin_rate is None
        ):
            return None

        return sample.recent_spin_rate - sample.baseline_spin_rate

    def _spin_score(
        self,
        spin_change: Decimal | None,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        if spin_change is None:
            warnings.append(
                "Baseline or recent spin rate is missing."
            )
            return NEUTRAL

        score = _clamp(
            NEUTRAL
            + spin_change / Decimal("800")
        )

        if spin_change >= Decimal("100"):
            evidence.append(
                "Recent spin rate is meaningfully above baseline."
            )
        elif spin_change <= Decimal("-150"):
            warnings.append(
                "Recent spin rate shows a meaningful decline."
            )
        else:
            evidence.append(
                "Recent spin rate is near the established baseline."
            )

        return score

    def _movement_score(
        self,
        sample: MlbPitchMovementSample,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        if (
            sample.horizontal_movement is None
            and sample.induced_vertical_break is None
        ):
            warnings.append(
                "Horizontal and vertical movement data are missing."
            )
            return NEUTRAL

        horizontal_score = self._horizontal_score(sample)
        vertical_score = self._vertical_score(sample)

        score = (
            horizontal_score * Decimal("0.40")
            + vertical_score * Decimal("0.60")
        )

        if score >= Decimal("0.70"):
            evidence.append(
                "Pitch movement is strong for the pitch type."
            )
        elif score <= Decimal("0.35"):
            warnings.append(
                "Pitch movement is weak for the pitch type."
            )
        else:
            evidence.append(
                "Pitch movement is within the expected range."
            )

        return _clamp(score)

    def _horizontal_score(
        self,
        sample: MlbPitchMovementSample,
    ) -> Decimal:
        value = sample.horizontal_movement
        if value is None:
            return NEUTRAL

        magnitude = abs(value)

        target = {
            "FOUR_SEAM": Decimal("8"),
            "SINKER": Decimal("15"),
            "CUTTER": Decimal("5"),
            "SLIDER": Decimal("8"),
            "SWEEPER": Decimal("15"),
            "CURVEBALL": Decimal("10"),
            "CHANGEUP": Decimal("14"),
            "SPLITTER": Decimal("8"),
        }[sample.pitch_type]

        return _clamp(magnitude / target)

    def _vertical_score(
        self,
        sample: MlbPitchMovementSample,
    ) -> Decimal:
        value = sample.induced_vertical_break
        if value is None:
            return NEUTRAL

        if sample.pitch_type == "FOUR_SEAM":
            return _clamp(
                (value - Decimal("8"))
                / Decimal("12")
            )

        if sample.pitch_type in {"SINKER", "CHANGEUP", "SPLITTER"}:
            drop = abs(value)
            return _clamp(drop / Decimal("14"))

        if sample.pitch_type in {"SLIDER", "SWEEPER", "CURVEBALL"}:
            drop = abs(value)
            return _clamp(drop / Decimal("12"))

        if sample.pitch_type == "CUTTER":
            return _clamp(
                (value - Decimal("5"))
                / Decimal("10")
            )

        return NEUTRAL

    def _release_score(
        self,
        variance: Decimal | None,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        if variance is None:
            warnings.append(
                "Release-point variance is missing."
            )
            return NEUTRAL

        score = _clamp(
            ONE - variance / Decimal("0.60")
        )

        if variance <= Decimal("0.10"):
            evidence.append(
                "Release point is highly consistent."
            )
        elif variance >= Decimal("0.40"):
            warnings.append(
                "Release-point inconsistency may reduce command support."
            )

        return score

    def _extension_score(
        self,
        extension: Decimal | None,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        if extension is None:
            warnings.append(
                "Extension data are missing."
            )
            return NEUTRAL

        score = _clamp(
            (extension - Decimal("5.0"))
            / Decimal("2.5")
        )

        if extension >= Decimal("6.5"):
            evidence.append(
                "Extension improves perceived velocity and pitch shape."
            )
        elif extension < Decimal("5.5"):
            warnings.append(
                "Limited extension may reduce perceived velocity."
            )

        return score

    def _reliability_score(
        self,
        sample: MlbPitchMovementSample,
        warnings: list[str],
    ) -> Decimal:
        available_fields = sum(
            value is not None
            for value in (
                sample.baseline_spin_rate,
                sample.recent_spin_rate,
                sample.horizontal_movement,
                sample.induced_vertical_break,
                sample.release_point_variance,
                sample.extension,
            )
        )

        field_coverage = (
            Decimal(available_fields)
            / Decimal("6")
        )
        pitch_support = _clamp(
            Decimal(sample.pitches_observed)
            / Decimal("120")
        )
        game_support = _clamp(
            Decimal(sample.games_observed)
            / Decimal("5")
        )

        reliability = (
            field_coverage * Decimal("0.45")
            + pitch_support * Decimal("0.35")
            + game_support * Decimal("0.20")
        )

        if (
            sample.pitches_observed < 40
            or sample.games_observed < 3
        ):
            warnings.append(
                "Movement assessment is based on a limited sample."
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

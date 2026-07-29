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


@dataclass(frozen=True, slots=True)
class MlbPitchPowerSample:
    pitcher_id: str
    pitcher_name: str
    average_fastball_velocity: Decimal | None
    maximum_fastball_velocity: Decimal | None
    strikeout_rate: Decimal | None
    swinging_strike_rate: Decimal | None
    hard_hit_rate_allowed: Decimal | None
    barrel_rate_allowed: Decimal | None
    pitches_observed: int
    games_observed: int

    def __post_init__(self) -> None:
        if not self.pitcher_id.strip():
            raise ValueError("pitcher_id is required")

        if not self.pitcher_name.strip():
            raise ValueError("pitcher_name is required")

        _validate_nonnegative_decimal(
            "average_fastball_velocity",
            self.average_fastball_velocity,
        )
        _validate_nonnegative_decimal(
            "maximum_fastball_velocity",
            self.maximum_fastball_velocity,
        )

        for name, value in (
            ("strikeout_rate", self.strikeout_rate),
            (
                "swinging_strike_rate",
                self.swinging_strike_rate,
            ),
            (
                "hard_hit_rate_allowed",
                self.hard_hit_rate_allowed,
            ),
            (
                "barrel_rate_allowed",
                self.barrel_rate_allowed,
            ),
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
class MlbPitchPowerAssessment:
    pitcher_id: str
    pitcher_name: str
    velocity_power_score: Decimal
    bat_miss_score: Decimal
    contact_suppression_score: Decimal
    reliability_score: Decimal
    raw_score: Decimal
    overall_score: Decimal
    data_quality: str
    evidence: tuple[str, ...]
    warnings: tuple[str, ...]


class MlbPitchPowerAnalyzer:
    VELOCITY_WEIGHT = Decimal("0.35")
    BAT_MISS_WEIGHT = Decimal("0.40")
    CONTACT_SUPPRESSION_WEIGHT = Decimal("0.25")

    def analyze(
        self,
        sample: MlbPitchPowerSample,
    ) -> MlbPitchPowerAssessment:
        evidence: list[str] = []
        warnings: list[str] = []

        velocity_power = self._velocity_power_score(
            sample,
            evidence,
            warnings,
        )
        bat_miss = self._bat_miss_score(
            sample,
            evidence,
            warnings,
        )
        contact_suppression = self._contact_suppression_score(
            sample,
            evidence,
            warnings,
        )
        reliability = self._reliability_score(
            sample,
            warnings,
        )

        raw_score = (
            velocity_power * self.VELOCITY_WEIGHT
            + bat_miss * self.BAT_MISS_WEIGHT
            + contact_suppression
            * self.CONTACT_SUPPRESSION_WEIGHT
        )

        overall_score = (
            NEUTRAL
            + (raw_score - NEUTRAL) * reliability
        )

        if not evidence:
            evidence.append(
                "Pitch-power assessment remains neutral because "
                "strong supporting evidence is unavailable."
            )

        return MlbPitchPowerAssessment(
            pitcher_id=sample.pitcher_id,
            pitcher_name=sample.pitcher_name,
            velocity_power_score=_rounded(velocity_power),
            bat_miss_score=_rounded(bat_miss),
            contact_suppression_score=_rounded(
                contact_suppression
            ),
            reliability_score=_rounded(reliability),
            raw_score=_rounded(raw_score),
            overall_score=_rounded(
                _clamp(overall_score)
            ),
            data_quality=self._data_quality(reliability),
            evidence=tuple(evidence),
            warnings=tuple(warnings),
        )

    def _velocity_power_score(
        self,
        sample: MlbPitchPowerSample,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        components: list[Decimal] = []

        if sample.average_fastball_velocity is not None:
            average_score = _clamp(
                NEUTRAL
                + (
                    sample.average_fastball_velocity
                    - Decimal("94")
                )
                / Decimal("10")
            )
            components.append(average_score)

        if sample.maximum_fastball_velocity is not None:
            maximum_score = _clamp(
                NEUTRAL
                + (
                    sample.maximum_fastball_velocity
                    - Decimal("97")
                )
                / Decimal("10")
            )
            components.append(maximum_score)

        if not components:
            warnings.append(
                "Average and maximum fastball velocity are missing."
            )
            return NEUTRAL

        if (
            sample.average_fastball_velocity is not None
            and sample.average_fastball_velocity
            >= Decimal("96")
        ) or (
            sample.maximum_fastball_velocity is not None
            and sample.maximum_fastball_velocity
            >= Decimal("99")
        ):
            evidence.append(
                "Fastball velocity indicates premium raw pitch power."
            )

        if (
            sample.average_fastball_velocity is not None
            and sample.average_fastball_velocity
            < Decimal("92")
        ) and (
            sample.maximum_fastball_velocity is not None
            and sample.maximum_fastball_velocity
            < Decimal("95")
        ):
            warnings.append(
                "Fastball velocity provides limited raw power."
            )

        return sum(components, ZERO) / Decimal(len(components))

    def _bat_miss_score(
        self,
        sample: MlbPitchPowerSample,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        components: list[Decimal] = []

        if sample.strikeout_rate is not None:
            strikeout_score = _clamp(
                NEUTRAL
                + (
                    sample.strikeout_rate
                    - Decimal("0.230")
                )
                / Decimal("0.300")
            )
            components.append(strikeout_score)

        if sample.swinging_strike_rate is not None:
            swinging_strike_score = _clamp(
                NEUTRAL
                + (
                    sample.swinging_strike_rate
                    - Decimal("0.110")
                )
                / Decimal("0.160")
            )
            components.append(swinging_strike_score)

        if not components:
            warnings.append(
                "Strikeout and swinging-strike rates are missing."
            )
            return NEUTRAL

        if (
            sample.strikeout_rate is not None
            and sample.strikeout_rate >= Decimal("0.280")
        ) or (
            sample.swinging_strike_rate is not None
            and sample.swinging_strike_rate
            >= Decimal("0.130")
        ):
            evidence.append(
                "Strikeout and bat-missing results support strong "
                "pitch power."
            )

        if (
            sample.strikeout_rate is not None
            and sample.strikeout_rate < Decimal("0.180")
        ) or (
            sample.swinging_strike_rate is not None
            and sample.swinging_strike_rate
            < Decimal("0.080")
        ):
            warnings.append(
                "Bat-missing performance is below the expected "
                "starting-pitcher range."
            )

        return sum(components, ZERO) / Decimal(len(components))

    def _contact_suppression_score(
        self,
        sample: MlbPitchPowerSample,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        components: list[Decimal] = []

        if sample.hard_hit_rate_allowed is not None:
            hard_hit_score = _clamp(
                NEUTRAL
                + (
                    Decimal("0.360")
                    - sample.hard_hit_rate_allowed
                )
                / Decimal("0.300")
            )
            components.append(hard_hit_score)

        if sample.barrel_rate_allowed is not None:
            barrel_score = _clamp(
                NEUTRAL
                + (
                    Decimal("0.075")
                    - sample.barrel_rate_allowed
                )
                / Decimal("0.150")
            )
            components.append(barrel_score)

        if not components:
            warnings.append(
                "Hard-hit and barrel rates are missing."
            )
            return NEUTRAL

        if (
            sample.hard_hit_rate_allowed is not None
            and sample.hard_hit_rate_allowed
            <= Decimal("0.320")
        ) or (
            sample.barrel_rate_allowed is not None
            and sample.barrel_rate_allowed
            <= Decimal("0.055")
        ):
            evidence.append(
                "Contact quality allowed indicates effective damage "
                "suppression."
            )

        if (
            sample.hard_hit_rate_allowed is not None
            and sample.hard_hit_rate_allowed
            >= Decimal("0.420")
        ):
            warnings.append(
                "Hard contact allowed weakens the pitch-power profile."
            )

        if (
            sample.barrel_rate_allowed is not None
            and sample.barrel_rate_allowed
            >= Decimal("0.100")
        ):
            warnings.append(
                "Elevated barrel contact weakens the pitch-power "
                "profile."
            )

        return sum(components, ZERO) / Decimal(len(components))

    def _reliability_score(
        self,
        sample: MlbPitchPowerSample,
        warnings: list[str],
    ) -> Decimal:
        available_fields = sum(
            value is not None
            for value in (
                sample.average_fastball_velocity,
                sample.maximum_fastball_velocity,
                sample.strikeout_rate,
                sample.swinging_strike_rate,
                sample.hard_hit_rate_allowed,
                sample.barrel_rate_allowed,
            )
        )

        field_coverage = (
            Decimal(available_fields)
            / Decimal("6")
        )
        pitch_support = _clamp(
            Decimal(sample.pitches_observed)
            / Decimal("240")
        )
        game_support = _clamp(
            Decimal(sample.games_observed)
            / Decimal("6")
        )

        reliability = (
            field_coverage * Decimal("0.40")
            + pitch_support * Decimal("0.35")
            + game_support * Decimal("0.25")
        )

        if (
            sample.pitches_observed < 80
            or sample.games_observed < 3
        ):
            warnings.append(
                "Pitch-power assessment is based on a limited sample."
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

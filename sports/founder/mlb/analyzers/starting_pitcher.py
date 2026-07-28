from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


ZERO = Decimal("0")
ONE = Decimal("1")
TWO_PLACES = Decimal("0.01")
THREE_PLACES = Decimal("0.001")


def _clamp(value: Decimal) -> Decimal:
    return max(ZERO, min(ONE, value))


def _rounded(
    value: Decimal,
    precision: Decimal = THREE_PLACES,
) -> Decimal:
    return value.quantize(
        precision,
        rounding=ROUND_HALF_UP,
    )


def _validate_probability(
    name: str,
    value: Decimal | None,
) -> None:
    if value is None:
        return

    if value < ZERO or value > ONE:
        raise ValueError(
            f"{name} must be between 0 and 1"
        )


def _validate_nonnegative(
    name: str,
    value: Decimal | None,
) -> None:
    if value is not None and value < ZERO:
        raise ValueError(
            f"{name} cannot be negative"
        )


@dataclass(frozen=True, slots=True)
class MlbStartingPitcherSample:
    pitcher_id: str
    pitcher_name: str
    throws: str
    starts_in_sample: int
    innings_in_sample: Decimal
    recent_era: Decimal | None
    recent_fip: Decimal | None
    recent_whip: Decimal | None
    strikeout_rate: Decimal | None
    walk_rate: Decimal | None
    ground_ball_rate: Decimal | None
    hard_hit_rate: Decimal | None
    average_fastball_velocity: Decimal | None
    fastball_velocity_change: Decimal | None
    average_pitch_count: Decimal | None
    pitch_count_change: Decimal | None
    days_rest: int | None
    opponent_woba_vs_hand: Decimal | None
    opponent_strikeout_rate_vs_hand: Decimal | None
    confirmed_starter: bool

    def __post_init__(self) -> None:
        if not self.pitcher_id.strip():
            raise ValueError("pitcher_id is required")

        if not self.pitcher_name.strip():
            raise ValueError("pitcher_name is required")

        if self.throws not in {"L", "R"}:
            raise ValueError("throws must be L or R")

        if self.starts_in_sample < 0:
            raise ValueError(
                "starts_in_sample cannot be negative"
            )

        _validate_nonnegative(
            "innings_in_sample",
            self.innings_in_sample,
        )
        _validate_nonnegative(
            "recent_era",
            self.recent_era,
        )
        _validate_nonnegative(
            "recent_fip",
            self.recent_fip,
        )
        _validate_nonnegative(
            "recent_whip",
            self.recent_whip,
        )
        _validate_nonnegative(
            "average_fastball_velocity",
            self.average_fastball_velocity,
        )
        _validate_nonnegative(
            "average_pitch_count",
            self.average_pitch_count,
        )

        for name, value in (
            ("strikeout_rate", self.strikeout_rate),
            ("walk_rate", self.walk_rate),
            ("ground_ball_rate", self.ground_ball_rate),
            ("hard_hit_rate", self.hard_hit_rate),
            (
                "opponent_woba_vs_hand",
                self.opponent_woba_vs_hand,
            ),
            (
                "opponent_strikeout_rate_vs_hand",
                self.opponent_strikeout_rate_vs_hand,
            ),
        ):
            _validate_probability(name, value)

        if self.days_rest is not None and self.days_rest < 0:
            raise ValueError("days_rest cannot be negative")


@dataclass(frozen=True, slots=True)
class MlbStartingPitcherAssessment:
    pitcher_id: str
    pitcher_name: str
    performance_score: Decimal
    command_score: Decimal
    matchup_score: Decimal
    workload_score: Decimal
    reliability_score: Decimal
    raw_score: Decimal
    overall_score: Decimal
    data_quality: str
    evidence: tuple[str, ...]
    warnings: tuple[str, ...]


class MlbStartingPitcherAnalyzer:
    """
    Conservative and explainable starting-pitcher assessment.

    The analyzer does not predict a game result by itself. It produces
    normalized pitcher features for a later matchup and pricing model.
    """

    PERFORMANCE_WEIGHT = Decimal("0.30")
    COMMAND_WEIGHT = Decimal("0.25")
    MATCHUP_WEIGHT = Decimal("0.25")
    WORKLOAD_WEIGHT = Decimal("0.20")

    def analyze(
        self,
        sample: MlbStartingPitcherSample,
    ) -> MlbStartingPitcherAssessment:
        evidence: list[str] = []
        warnings: list[str] = []

        performance = self._performance_score(
            sample,
            evidence,
            warnings,
        )
        command = self._command_score(
            sample,
            evidence,
            warnings,
        )
        matchup = self._matchup_score(
            sample,
            evidence,
            warnings,
        )
        workload = self._workload_score(
            sample,
            evidence,
            warnings,
        )
        reliability = self._reliability_score(
            sample,
            evidence,
            warnings,
        )

        raw_score = (
            performance * self.PERFORMANCE_WEIGHT
            + command * self.COMMAND_WEIGHT
            + matchup * self.MATCHUP_WEIGHT
            + workload * self.WORKLOAD_WEIGHT
        )

        # Reliability pulls uncertain assessments toward neutral rather
        # than allowing incomplete data to create an extreme score.
        overall = (
            Decimal("0.50")
            + (
                raw_score - Decimal("0.50")
            ) * reliability
        )

        data_quality = self._data_quality(
            sample,
            reliability,
        )

        return MlbStartingPitcherAssessment(
            pitcher_id=sample.pitcher_id,
            pitcher_name=sample.pitcher_name,
            performance_score=_rounded(performance),
            command_score=_rounded(command),
            matchup_score=_rounded(matchup),
            workload_score=_rounded(workload),
            reliability_score=_rounded(reliability),
            raw_score=_rounded(raw_score),
            overall_score=_rounded(_clamp(overall)),
            data_quality=data_quality,
            evidence=tuple(evidence),
            warnings=tuple(warnings),
        )

    def _performance_score(
        self,
        sample: MlbStartingPitcherSample,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        components: list[Decimal] = []

        if sample.recent_era is not None:
            era_score = _clamp(
                (
                    Decimal("6.50")
                    - sample.recent_era
                )
                / Decimal("5.00")
            )
            components.append(era_score)

            if sample.recent_era <= Decimal("3.25"):
                evidence.append(
                    "Recent ERA indicates strong run prevention."
                )
            elif sample.recent_era >= Decimal("5.25"):
                warnings.append(
                    "Recent ERA indicates weak run prevention."
                )

        if sample.recent_fip is not None:
            fip_score = _clamp(
                (
                    Decimal("6.00")
                    - sample.recent_fip
                )
                / Decimal("4.50")
            )
            components.append(fip_score)

            if sample.recent_fip <= Decimal("3.40"):
                evidence.append(
                    "Recent FIP supports the run-prevention results."
                )
            elif sample.recent_fip >= Decimal("5.00"):
                warnings.append(
                    "Recent FIP indicates underlying performance risk."
                )

        if sample.recent_whip is not None:
            whip_score = _clamp(
                (
                    Decimal("1.70")
                    - sample.recent_whip
                )
                / Decimal("0.90")
            )
            components.append(whip_score)

            if sample.recent_whip <= Decimal("1.15"):
                evidence.append(
                    "Recent WHIP shows effective baserunner control."
                )
            elif sample.recent_whip >= Decimal("1.45"):
                warnings.append(
                    "Recent WHIP shows elevated baserunner traffic."
                )

        if not components:
            warnings.append(
                "No recent ERA, FIP, or WHIP data is available."
            )
            return Decimal("0.50")

        return _clamp(
            sum(components, ZERO)
            / Decimal(len(components))
        )

    def _command_score(
        self,
        sample: MlbStartingPitcherSample,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        if (
            sample.strikeout_rate is None
            or sample.walk_rate is None
        ):
            warnings.append(
                "Strikeout or walk data is incomplete."
            )
            return Decimal("0.50")

        strikeout_minus_walk = (
            sample.strikeout_rate
            - sample.walk_rate
        )

        score = _clamp(
            (
                strikeout_minus_walk
                + Decimal("0.02")
            )
            / Decimal("0.28")
        )

        if strikeout_minus_walk >= Decimal("0.20"):
            evidence.append(
                "Strikeout-minus-walk rate shows strong command."
            )
        elif strikeout_minus_walk <= Decimal("0.08"):
            warnings.append(
                "Strikeout-minus-walk rate shows limited command."
            )

        return score

    def _matchup_score(
        self,
        sample: MlbStartingPitcherSample,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        components: list[Decimal] = []

        if sample.opponent_woba_vs_hand is not None:
            woba_score = _clamp(
                (
                    Decimal("0.370")
                    - sample.opponent_woba_vs_hand
                )
                / Decimal("0.090")
            )
            components.append(woba_score)

            if (
                sample.opponent_woba_vs_hand
                <= Decimal("0.305")
            ):
                evidence.append(
                    "Opponent has a weak wOBA against this "
                    "pitcher's handedness."
                )
            elif (
                sample.opponent_woba_vs_hand
                >= Decimal("0.345")
            ):
                warnings.append(
                    "Opponent has a strong wOBA against this "
                    "pitcher's handedness."
                )

        if (
            sample.opponent_strikeout_rate_vs_hand
            is not None
        ):
            opponent_k_score = _clamp(
                (
                    sample.opponent_strikeout_rate_vs_hand
                    - Decimal("0.16")
                )
                / Decimal("0.14")
            )
            components.append(opponent_k_score)

            if (
                sample.opponent_strikeout_rate_vs_hand
                >= Decimal("0.25")
            ):
                evidence.append(
                    "Opponent strikeout tendency favors the pitcher."
                )
            elif (
                sample.opponent_strikeout_rate_vs_hand
                <= Decimal("0.19")
            ):
                warnings.append(
                    "Opponent is difficult to strike out."
                )

        if not components:
            warnings.append(
                "Opponent handedness matchup data is unavailable."
            )
            return Decimal("0.50")

        return _clamp(
            sum(components, ZERO)
            / Decimal(len(components))
        )

    def _workload_score(
        self,
        sample: MlbStartingPitcherSample,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        score = Decimal("0.70")

        if sample.days_rest is None:
            score -= Decimal("0.10")
            warnings.append(
                "Rest-day information is unavailable."
            )
        elif sample.days_rest <= 3:
            score -= Decimal("0.35")
            warnings.append(
                "Pitcher is working on short rest."
            )
        elif sample.days_rest == 4:
            score -= Decimal("0.08")
        elif sample.days_rest >= 5:
            score += Decimal("0.10")
            evidence.append(
                "Pitcher has normal or extended rest."
            )

        if sample.fastball_velocity_change is None:
            score -= Decimal("0.05")
        elif sample.fastball_velocity_change <= Decimal("-2.0"):
            score -= Decimal("0.35")
            warnings.append(
                "Fastball velocity has declined materially."
            )
        elif sample.fastball_velocity_change <= Decimal("-1.0"):
            score -= Decimal("0.18")
            warnings.append(
                "Fastball velocity is trending downward."
            )
        elif sample.fastball_velocity_change >= Decimal("0.5"):
            score += Decimal("0.08")
            evidence.append(
                "Fastball velocity is stable or improving."
            )

        if (
            sample.average_pitch_count is not None
            and sample.average_pitch_count >= Decimal("105")
        ):
            score -= Decimal("0.08")
            warnings.append(
                "Recent pitch workload is elevated."
            )

        if (
            sample.pitch_count_change is not None
            and sample.pitch_count_change >= Decimal("12")
        ):
            score -= Decimal("0.08")
            warnings.append(
                "Recent pitch count has increased materially."
            )

        return _clamp(score)

    def _reliability_score(
        self,
        sample: MlbStartingPitcherSample,
        evidence: list[str],
        warnings: list[str],
    ) -> Decimal:
        score = Decimal("0.20")

        score += min(
            Decimal(sample.starts_in_sample)
            / Decimal("6")
            * Decimal("0.35"),
            Decimal("0.35"),
        )

        score += min(
            sample.innings_in_sample
            / Decimal("36")
            * Decimal("0.25"),
            Decimal("0.25"),
        )

        completeness_fields = (
            sample.recent_era,
            sample.recent_fip,
            sample.recent_whip,
            sample.strikeout_rate,
            sample.walk_rate,
            sample.fastball_velocity_change,
            sample.days_rest,
            sample.opponent_woba_vs_hand,
            sample.opponent_strikeout_rate_vs_hand,
        )
        available = sum(
            value is not None
            for value in completeness_fields
        )

        score += (
            Decimal(available)
            / Decimal(len(completeness_fields))
            * Decimal("0.20")
        )

        if sample.starts_in_sample < 3:
            score -= Decimal("0.15")
            warnings.append(
                "Recent sample contains fewer than three starts."
            )

        if sample.innings_in_sample < Decimal("12"):
            score -= Decimal("0.10")
            warnings.append(
                "Recent innings sample is limited."
            )

        if not sample.confirmed_starter:
            score = min(score, Decimal("0.45"))
            warnings.append(
                "Starting pitcher is not confirmed."
            )
        else:
            evidence.append(
                "Starting pitcher is confirmed."
            )

        return _clamp(score)

    def _data_quality(
        self,
        sample: MlbStartingPitcherSample,
        reliability: Decimal,
    ) -> str:
        if (
            sample.confirmed_starter
            and reliability >= Decimal("0.78")
            and sample.starts_in_sample >= 5
            and sample.innings_in_sample >= Decimal("25")
        ):
            return "HIGH"

        if (
            reliability >= Decimal("0.55")
            and sample.starts_in_sample >= 3
            and sample.innings_in_sample >= Decimal("15")
        ):
            return "MEDIUM"

        return "LOW"

"""Pydantic request/response models shared by the API."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PeriodIn(Strict):
    start: date = Field(description="First survey date (inclusive), local to the study timezone.")
    end: date = Field(description="Last survey date (inclusive).")


class RecruitmentRowIn(Strict):
    household_id: str = Field(min_length=1, description="Household identifier used for clustering.")
    person_id: str = Field(min_length=1, description="Person identifier, unique within a batch.")
    stratum: str = Field(min_length=1, description="Sampling stratum declared at recruitment.")
    age_band: str = Field(min_length=1)
    region: str = Field(min_length=1)
    sex: str = Field(min_length=1)
    inclusion_probability: Decimal | None = Field(
        default=None,
        gt=0,
        le=1,
        description="Known first-order inclusion probability. Unknown must be null.",
    )
    household_size: int | None = Field(default=None, ge=1)
    notes: str | None = None


class RecruitmentBatchIn(Strict):
    batch_ref: str | None = Field(default=None, description="Optional client idempotency key.")
    survey_period: PeriodIn
    rows: list[RecruitmentRowIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _period_order(self):
        if self.survey_period.end < self.survey_period.start:
            raise ValueError("survey_period.end must not precede survey_period.start")
        return self


class TargetBatchIn(Strict):
    batch_ref: str | None = None
    population_total: int = Field(gt=0)
    margins: dict[str, dict[str, float]] = Field(
        description="dimension -> category -> known population total.",
        examples=[
            {
                "age_band": {"18-24": 1800, "25-44": 4200},
                "region": {"north": 4500, "south": 3300},
                "sex": {"F": 6120, "M": 5880},
            }
        ],
    )

    @model_validator(mode="after")
    def _margins_sum_to_total(self):
        totals = {dim: round(sum(cats.values()), 6) for dim, cats in self.margins.items()}
        bad = {d: v for d, v in totals.items() if abs(v - self.population_total) > 1e-6}
        if bad:
            raise ValueError(
                "every dimension must sum to population_total "
                f"({self.population_total}); got {totals}"
            )
        return self


class AiringIn(Strict):
    program_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    station: str = Field(min_length=1)
    air_date: date
    start_local: str = Field(pattern=r"^\d{1,2}:\d{2}$")
    end_local: str = Field(pattern=r"^\d{1,2}:\d{2}$", description="May exceed 24:00 for cross-midnight slots.")


class ProgramBatchIn(Strict):
    batch_ref: str | None = None
    timezone: str = Field(default="Asia/Shanghai")
    airings: list[AiringIn] = Field(min_length=1)


class CoverageWindowIn(Strict):
    start: str
    end: str


class DiaryEntryIn(Strict):
    start: str
    end: str
    station: str = Field(min_length=1)
    source_serial: str = Field(min_length=1, description="Original diary line identifier, preserved verbatim.")
    revision: int | None = Field(default=None, ge=1)


class ReplacementScopeIn(Strict):
    type: Literal["append", "replace"] = "append"
    start: str | None = None
    end: str | None = None


class DiarySubmissionIn(Strict):
    person_id: str = Field(min_length=1)
    household_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    submitted_at: str
    replacement_scope: ReplacementScopeIn = Field(default_factory=ReplacementScopeIn)
    coverage: list[CoverageWindowIn] = Field(default_factory=list)
    entries: list[DiaryEntryIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_scope(self):
        if self.replacement_scope.type == "replace" and not (
            self.replacement_scope.start and self.replacement_scope.end
        ):
            raise ValueError("replace scope requires start and end")
        return self


class RakingIn(Strict):
    tolerance: float = Field(default=1e-4, gt=0, le=1)
    max_iterations: int = Field(default=100, ge=1, le=5000)
    lower_factor: float = Field(
        default=0.3, gt=0, lt=1, description="Lower clamp as a fraction of each person's design weight."
    )
    upper_factor: float = Field(
        default=4.0, gt=1, description="Upper clamp as a multiple of each person's design weight."
    )
    dimensions: list[str] = Field(default_factory=lambda: ["age_band", "region", "sex"])
    stratum_merges: list[list[str]] = Field(default_factory=list)


class CompletenessIn(Strict):
    type: Literal["full_coverage", "min_complete_days"] = "full_coverage"
    max_uncovered_minutes: float = Field(default=0.0, ge=0)
    min_days: int = Field(default=0, ge=0)


class BootstrapIn(Strict):
    replicates: int = Field(default=200, ge=0, le=5000)
    seed: int = Field(default=20260901)
    max_calibration_failure_rate: float = Field(default=0.10, ge=0, le=1)


class ThresholdsIn(Strict):
    reach_minutes: float = Field(default=5.0, gt=0)
    coverage_gap_share_threshold: float = Field(
        default=0.03, ge=0, le=1, description="Reserved weighted-share shortfall threshold."
    )
    under_representation_ratio: float = Field(
        default=0.70,
        gt=0,
        le=1,
        description=(
            "Flag a group when (sample share / target population share) falls below this ratio, "
            "i.e. the achieved diary sample represents much less of that group than the population does."
        ),
    )
    min_cell_persons: int = Field(default=5, ge=0)
    min_effective_sample_share: float = Field(default=0.25, ge=0, le=1)


class RetentionIn(Strict):
    """Segment-level retention configuration.

    ``model`` controls the optional interval-censored estimate. It is *never*
    presented as a measurement; ``none`` publishes hard bounds only.
    """

    enabled: bool = Field(
        default=True,
        description="Compute segment retention when a segment batch is pinned.",
    )
    model: Literal["interval_censored", "none"] = Field(
        default="interval_censored",
        description=(
            "interval_censored: declare a within-granularity endpoint distribution and publish a "
            "model estimate with its sensitivity band. none: hard bounds only, no model estimate."
        ),
    )
    require_monotone: bool = Field(
        default=True,
        description="Refuse publication when the conservative retention curve rises within an airing.",
    )
    monotonicity_tolerance: float = Field(default=0.0, ge=0)


class SegmentSpecIn(Strict):
    start_offset_min: Decimal = Field(ge=0, description="Minutes from the effective airing start.")
    end_offset_min: Decimal = Field(gt=0, description="Minutes from the effective airing start.")
    label: str | None = None

    @model_validator(mode="after")
    def _order(self):
        if self.end_offset_min <= self.start_offset_min:
            raise ValueError("segment end_offset_min must exceed start_offset_min")
        return self


class SegmentAiringIn(Strict):
    program_key: str = Field(min_length=1)
    air_date: date
    station: str = Field(min_length=1)
    segments: list[SegmentSpecIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _contiguous_and_ordered(self):
        ordered = sorted(self.segments, key=lambda s: s.start_offset_min)
        prev = Decimal(0)
        for idx, seg in enumerate(ordered):
            if seg.start_offset_min != prev:
                raise ValueError(
                    f"segments must be contiguous from 0; segment #{idx} starts at "
                    f"{seg.start_offset_min} but {prev} was expected"
                )
            prev = seg.end_offset_min
        return self


class SegmentBatchIn(Strict):
    batch_ref: str | None = None
    unit_minutes: float = Field(
        gt=0,
        description="Recording granularity in minutes (e.g. 5). A record's endpoints are known only to this unit.",
    )
    granularity_note: str | None = None
    airings: list[SegmentAiringIn] = Field(min_length=1)


class BroadcastConfirmationIn(Strict):
    program_key: str = Field(min_length=1)
    air_date: date
    station: str = Field(min_length=1)
    confirmed_start: str = Field(description="Confirmed as-aired start instant (ISO 8601 with offset).")
    confirmed_end: str = Field(description="Confirmed as-aired end instant (ISO 8601 with offset).")
    basis: Literal["as_aired_log", "encoder_log", "analyst_confirmed"] = "analyst_confirmed"
    note: str | None = None


class RunConfigIn(Strict):
    timezone: str | None = Field(default=None, description="Study timezone; must match the program batch timezone.")
    completeness: CompletenessIn = Field(default_factory=CompletenessIn)
    raking: RakingIn = Field(default_factory=RakingIn)
    bootstrap: BootstrapIn = Field(default_factory=BootstrapIn)
    thresholds: ThresholdsIn = Field(default_factory=ThresholdsIn)
    retention: RetentionIn = Field(default_factory=RetentionIn)
    allow_program_comparison: bool = Field(
        default=False,
        description="Leave false: coverage gaps make cross-program performance comparison invalid.",
    )


class RunInputsIn(Strict):
    recruitment_batch_seq: int = Field(ge=1)
    target_batch_seq: int = Field(ge=1)
    program_batch_seq: int = Field(ge=1)
    diary_seq_cutoff: int = Field(ge=0, description="Pinned diary submission serial; 0 means no diary rows.")
    segment_batch_seq: int | None = Field(
        default=None, ge=1, description="Pinned segment-boundary batch; omit to skip retention."
    )
    confirmation_seqs: list[int] = Field(
        default_factory=list,
        description="Pinned broadcast-confirmation serials used as the effective version for their airings.",
    )


class RunCreateIn(Strict):
    note: str | None = None
    inputs: RunInputsIn
    config: RunConfigIn = Field(default_factory=RunConfigIn)
    parent_run_id: str | None = None
    seed: int | None = Field(default=None, description="Overrides config.bootstrap.seed.")


class ExecuteIn(Strict):
    max_steps: Annotated[int, Field(ge=1, le=100)] = 20

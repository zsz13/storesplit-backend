"""Optional AI product judge. Disabled by default; the MVP never calls it.

The judge is only ever consulted for matches the deterministic matcher marks as
unresolved, and only when AI_JUDGE_ENABLED=true. There is no provider implementation in
this repository; enabling the flag without one raises at startup of the scrape service.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from app.config import get_settings

Verdict = Literal["same", "different", "unsure"]


@dataclass(frozen=True)
class JudgeInput:
    category: str
    title_a: str
    brand_a: str | None
    size_a: str | None
    title_b: str
    brand_b: str | None
    size_b: str | None


class AIProductJudge(Protocol):
    def judge(self, candidate: JudgeInput) -> Verdict: ...


class DisabledJudge:
    """The default: never merges anything."""

    def judge(self, candidate: JudgeInput) -> Verdict:
        return "unsure"


def get_judge() -> AIProductJudge:
    if get_settings().ai_judge_enabled:
        raise RuntimeError(
            "AI_JUDGE_ENABLED=true but no AIProductJudge implementation is installed. "
            "The MVP runs deterministic matching only."
        )
    return DisabledJudge()

"""Error taxonomy (DESIGN.md §8).

Agent failures (wrong answer, limits, timeout, crash) are *results*: the trial is
graded with score 0. Only harness failures raise InfraError; those trials are
retried and excluded from accuracy.
"""

from typing import Literal

InfraKind = Literal["image", "docker", "prepare", "verifier", "adapter", "protocol", "timeout_harness"]


class InfraError(Exception):
    def __init__(self, kind: InfraKind, message: str):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "message": self.message}

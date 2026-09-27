# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""What a build run did: every image built, failed or skipped."""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Outcome:
    """Accumulated results, in the order builds were attempted.

    ``built`` holds local tags; ``failures`` holds tags, or the literal
    ``build_command`` when the escape hatch fails. ``results`` is the
    per-image record: name, tag and status (built, failed, skipped).
    """

    built: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    results: list[dict[str, str]] = field(default_factory=list)
    pushed: list[dict[str, str]] = field(default_factory=list)

    def record(self, name: str, tag: str, status: str) -> None:
        """Note one image's outcome."""
        self.results.append({"name": name, "tag": tag, "status": status})
        {"built": self.built, "failed": self.failures, "skipped": self.skipped}[
            status
        ].append(tag)


def compact(value: object) -> str:
    """Single-line JSON, as the lanes' jq -c writes these values."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)

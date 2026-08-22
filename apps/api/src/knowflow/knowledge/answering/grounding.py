from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Never

from knowflow.knowledge.answering.chat import ChatError

GROUNDING_CONTROL_PREFIX = "[[KNOWFLOW_EVIDENCE:"
GROUNDING_CONTROL_SUFFIX = "]]"
_MAX_CONTROL_LENGTH = 512


class GroundedAnswerParser:
    def __init__(
        self, labels_by_subquestion: Mapping[str, Sequence[str]]
    ) -> None:
        self._allowed = {
            subquestion_id: tuple(labels)
            for subquestion_id, labels in labels_by_subquestion.items()
        }
        self._buffer = ""
        self._current_subquestion_id: str | None = None
        self._current_labels: tuple[str, ...] = ()
        self._current_has_text = False
        self._seen: set[str] = set()
        self._associations: list[tuple[str, tuple[str, ...]]] = []

    def feed(self, delta: str) -> str:
        self._buffer += delta
        visible: list[str] = []
        while self._buffer:
            if self._current_subquestion_id is None:
                if not self._buffer.startswith(GROUNDING_CONTROL_PREFIX):
                    if GROUNDING_CONTROL_PREFIX.startswith(self._buffer):
                        break
                    self._fail("Grounded answer did not begin with an evidence association")
                marker_end = self._buffer.find(
                    GROUNDING_CONTROL_SUFFIX, len(GROUNDING_CONTROL_PREFIX)
                )
                if marker_end < 0:
                    if len(self._buffer) > _MAX_CONTROL_LENGTH:
                        self._fail("Grounded answer evidence association was too long")
                    break
                header = self._buffer[len(GROUNDING_CONTROL_PREFIX) : marker_end]
                self._start_section(header)
                self._buffer = self._buffer[
                    marker_end + len(GROUNDING_CONTROL_SUFFIX) :
                ]
                continue

            next_marker = self._buffer.find(GROUNDING_CONTROL_PREFIX)
            if next_marker >= 0:
                text = self._buffer[:next_marker]
                self._record_text(text)
                visible.append(text)
                self._buffer = self._buffer[next_marker:]
                self._finish_section()
                continue

            held = self._partial_prefix_length(self._buffer)
            emit_end = len(self._buffer) - held
            text = self._buffer[:emit_end]
            self._record_text(text)
            visible.append(text)
            self._buffer = self._buffer[emit_end:]
            break
        return "".join(visible)

    def finish(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        if self._current_subquestion_id is None:
            if self._buffer:
                self._fail("Grounded answer ended inside an evidence association")
        else:
            if self._buffer and GROUNDING_CONTROL_PREFIX.startswith(self._buffer):
                self._fail("Grounded answer ended inside an evidence association")
            self._record_text(self._buffer)
            self._buffer = ""
            self._finish_section()
        if self._seen != set(self._allowed):
            self._fail("Grounded answer omitted an answered subquestion")
        return tuple(self._associations)

    def _start_section(self, header: str) -> None:
        parts = header.split("|")
        if len(parts) != 2:
            self._fail("Grounded answer evidence association was malformed")
        subquestion_id = parts[0].strip()
        labels = tuple(
            dict.fromkeys(label.strip() for label in parts[1].split(",") if label.strip())
        )
        if subquestion_id not in self._allowed or subquestion_id in self._seen:
            self._fail("Grounded answer referenced an unknown subquestion")
        if not labels or not set(labels) <= set(self._allowed[subquestion_id]):
            self._fail("Grounded answer referenced evidence from another subquestion")
        self._current_subquestion_id = subquestion_id
        self._current_labels = labels
        self._current_has_text = False

    def _record_text(self, text: str) -> None:
        if text.strip():
            self._current_has_text = True

    def _finish_section(self) -> None:
        if self._current_subquestion_id is None or not self._current_has_text:
            self._fail("Grounded answer subquestion had no visible answer")
        self._seen.add(self._current_subquestion_id)
        self._associations.append(
            (self._current_subquestion_id, self._current_labels)
        )
        self._current_subquestion_id = None
        self._current_labels = ()
        self._current_has_text = False

    @staticmethod
    def _partial_prefix_length(value: str) -> int:
        limit = min(len(value), len(GROUNDING_CONTROL_PREFIX) - 1)
        for size in range(limit, 0, -1):
            if GROUNDING_CONTROL_PREFIX.startswith(value[-size:]):
                return size
        return 0

    @staticmethod
    def _fail(message: str) -> Never:
        raise ChatError(message)

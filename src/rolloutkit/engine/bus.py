"""Event collection.

Deliberately not a pub/sub: contracts run after the experiment, over the full
recorded stream. Live subscribers would invite contracts that influence what they
measure.
"""

from __future__ import annotations

from collections.abc import Iterator

from rolloutkit.engine.events import Event, Kind


class EventBus:
    def __init__(self) -> None:
        self._events: list[Event] = []

    def record(self, event: Event) -> Event:
        self._events.append(event)
        return event

    def __iter__(self) -> Iterator[Event]:
        return iter(self.ordered())

    def __len__(self) -> int:
        return len(self._events)

    def ordered(self) -> list[Event]:
        """Chronological. Concurrent producers append out of order."""
        return sorted(self._events, key=lambda e: e.timestamp_ns)

    def of(self, *kinds: Kind) -> list[Event]:
        wanted = {str(k) for k in kinds}
        return [e for e in self.ordered() if e.kind in wanted]

    def first(self, kind: Kind) -> Event | None:
        for e in self.ordered():
            if e.kind == str(kind):
                return e
        return None

    def last(self, kind: Kind) -> Event | None:
        for e in reversed(self.ordered()):
            if e.kind == str(kind):
                return e
        return None

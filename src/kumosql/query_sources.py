"""Registry of query sources that feed the graph.

A source reports the assets it knows about: table references or repository
file paths. It is enabled only when every reported asset reconciles with an
existing graph identity. Otherwise it stays ``not_enabled`` and the unmatched
count is kept so the gap is visible instead of silently ignored.

The registry performs no network access; each source is built from records or
listings the caller already holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from .graph import ObservedRead
from .identity import NodeIdentity

if TYPE_CHECKING:
    from .pipeline import Pipeline

STATE_ENABLED = "connected"
STATE_NOT_ENABLED = "not_enabled"
STATE_ERROR = "error"

_SAMPLE_LIMIT = 10


@runtime_checkable
class QuerySource(Protocol):
    """Interface a query source implements to be registered."""

    name: str
    kind: str

    def assets(self) -> Iterable[object]:
        """Return every asset the source reports.

        Each item is a table reference accepted by ``Pipeline.resolve_reference``
        or a ``NodeIdentity`` for a repository file path.
        """
        ...


@dataclass(frozen=True)
class SourceStatus:
    name: str
    kind: str
    state: str
    assets_total: int = 0
    assets_matched: int = 0
    unmatched: tuple[str, ...] = ()
    reason: str = ""

    @property
    def assets_unmatched(self) -> int:
        return self.assets_total - self.assets_matched

    @property
    def matched(self) -> float | None:
        """Fraction of reported assets that map to a graph identity."""

        if self.assets_total == 0:
            return None
        return self.assets_matched / self.assets_total

    @property
    def enabled(self) -> bool:
        return self.state == STATE_ENABLED

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "name": self.name,
            "kind": self.kind,
            "state": self.state,
            "matched": self.matched,
            "assets_total": self.assets_total,
            "assets_matched": self.assets_matched,
            "assets_unmatched": self.assets_unmatched,
        }
        if self.unmatched:
            result["unmatched_samples"] = list(self.unmatched)
        if self.reason:
            result["reason"] = self.reason
        return result


class ObservedReadsSource:
    """Observed-read records of the kind the graph builder ingests.

    Destinations and referenced tables are the reported assets. Reads without
    a destination contribute only their references.
    """

    kind = "observed reads"

    def __init__(self, records: Iterable[ObservedRead | Mapping[str, object]], name: str = "Observed reads") -> None:
        self.name = name
        self._records = tuple(
            record if isinstance(record, ObservedRead) else ObservedRead.from_record(record)
            for record in records
        )

    def assets(self) -> list[object]:
        seen: set[str] = set()
        result: list[object] = []
        for record in self._records:
            for reference in (record.destination, *record.referenced_tables):
                if reference is None:
                    continue
                label = _label(reference)
                if label and label not in seen:
                    seen.add(label)
                    result.append(reference)
        return result


class GitHubRepoSource:
    """A repository file listing, as returned by ``github_repo.connect``.

    Each listed path is an asset and must match a pipeline model loaded from
    the same path.
    """

    kind = "repository files"

    def __init__(self, files: Sequence[str], name: str = "GitHub repository") -> None:
        self.name = name
        self._files = tuple(files)

    @classmethod
    def from_connection(cls, connection: Mapping[str, object]) -> "GitHubRepoSource":
        files = connection.get("files")
        if not isinstance(files, list):
            raise ValueError("connection result has no file list")
        return cls([str(item) for item in files], name=str(connection.get("repository") or "GitHub repository"))

    def assets(self) -> list[object]:
        return [NodeIdentity.for_asset(path) for path in dict.fromkeys(self._files)]


def _label(reference: object) -> str:
    if isinstance(reference, NodeIdentity):
        return reference.key
    if isinstance(reference, str):
        return reference
    if isinstance(reference, Mapping):
        return repr(sorted((str(k), str(v)) for k, v in reference.items()))
    return str(reference)


@dataclass
class SourceRegistry:
    """Named sources, evaluated against a pipeline's graph identities."""

    _sources: dict[str, QuerySource] = field(default_factory=dict)

    def register(self, source: QuerySource) -> None:
        if not source.name or not source.kind:
            raise ValueError("a source needs a name and a kind")
        if source.name in self._sources:
            raise ValueError(f"source already registered: {source.name}")
        self._sources[source.name] = source

    def names(self) -> list[str]:
        return list(self._sources)

    def evaluate(self, pipeline: "Pipeline") -> list[SourceStatus]:
        return [self._evaluate_one(source, pipeline) for source in self._sources.values()]

    def to_json(self, pipeline: "Pipeline") -> list[dict[str, object]]:
        return [status.to_json() for status in self.evaluate(pipeline)]

    @staticmethod
    def _evaluate_one(source: QuerySource, pipeline: "Pipeline") -> SourceStatus:
        try:
            assets = list(source.assets())
        except Exception as exc:  # a broken source must not hide the others
            return SourceStatus(source.name, source.kind, STATE_ERROR, reason=f"source failed: {exc}")
        matched = 0
        unmatched: list[str] = []
        for asset in assets:
            resolution = pipeline.resolve_reference(asset, use_defaults=True)
            if resolution.matched:
                matched += 1
            else:
                unmatched.append(resolution.reference)
        total = len(assets)
        if total == 0:
            state, reason = STATE_NOT_ENABLED, "source reported no assets"
        elif unmatched:
            state, reason = STATE_NOT_ENABLED, f"{len(unmatched)} of {total} assets do not map to a graph identity"
        else:
            state, reason = STATE_ENABLED, ""
        return SourceStatus(
            source.name, source.kind, state, total, matched, tuple(unmatched[:_SAMPLE_LIMIT]), reason
        )


__all__ = [
    "GitHubRepoSource",
    "ObservedReadsSource",
    "QuerySource",
    "STATE_ENABLED",
    "STATE_ERROR",
    "STATE_NOT_ENABLED",
    "SourceRegistry",
    "SourceStatus",
]

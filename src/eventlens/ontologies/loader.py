"""Strict YAML loading and deterministic ontology resolution."""

from __future__ import annotations

from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Iterable, Iterator

import yaml

from eventlens.artifacts.models import sha256_hex

from .schema import OntologyComponentVintage, OntologyLayer, OntologySlot, ResolvedOntology


class OntologyError(ValueError):
    """A layer or inheritance graph is invalid."""


def load_layer(path: Path) -> OntologyLayer:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return OntologyLayer.model_validate(payload)
    except (OSError, yaml.YAMLError, ValueError, TypeError) as error:
        raise OntologyError(f"invalid ontology layer {path.name}: {error}") from error


class OntologyCatalog:
    def __init__(self, layers: Iterable[OntologyLayer]):
        # Re-validate copied/programmatically constructed models so callers
        # cannot bypass graph invariants through ``model_copy(update=...)``.
        try:
            values = tuple(
                OntologyLayer.model_validate(item.model_dump(mode="json"))
                for item in layers
            )
        except (ValueError, TypeError) as error:
            raise OntologyError(f"catalog contains an invalid ontology layer: {error}") from error
        identities = {(item.ontology_id, item.version) for item in values}
        if len(identities) != len(values):
            raise OntologyError("catalog contains duplicate ontology ID/version pairs")
        grouped: dict[str, list[OntologyLayer]] = {}
        for item in values:
            grouped.setdefault(item.ontology_id, []).append(item)
        self._versions = {
            ontology_id: tuple(sorted(items, key=_layer_sort_key))
            for ontology_id, items in grouped.items()
        }
        # Compatibility/readability view of the current latest layers.
        self._layers = {key: items[-1] for key, items in self._versions.items()}

    @classmethod
    def from_paths(cls, paths: Iterable[Path]) -> "OntologyCatalog":
        return cls(load_layer(path) for path in paths)

    @classmethod
    def packaged(cls) -> "OntologyCatalog":
        root = resources.files("eventlens.ontologies") / "vintages"
        paths = tuple(_walk_yaml(root))
        if not paths:
            raise OntologyError("packaged ontology vintage catalog is empty")
        return cls(
            OntologyLayer.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
            for path in paths
        )

    def resolve(
        self,
        ontology_id: str,
        *,
        as_of: datetime | None = None,
        version: str | None = None,
    ) -> ResolvedOntology:
        """Resolve current, vintage-as-of, or explicitly pinned ontology layers.

        ``as_of`` is an exclusive knowledge boundary: every component must be
        released and effective strictly before it. Parent versions are always
        selected by immutable version-and-hash pins stored in the child layer.
        """

        if ontology_id not in self._versions:
            raise OntologyError(f"unknown ontology {ontology_id!r}")
        if as_of is not None and version is not None:
            raise OntologyError("resolve accepts either as_of or version, not both")
        cutoff = _strict_utc(as_of) if as_of is not None else None
        if version is not None:
            matches = [item for item in self._versions[ontology_id] if item.version == version]
            if not matches:
                raise OntologyError(f"unknown ontology version {ontology_id!r}@{version!r}")
            target = matches[0]
        else:
            target = self._select_layer(ontology_id, cutoff=cutoff, inclusive=False)
        ordered: list[OntologyLayer] = []
        visiting: set[tuple[str, str]] = set()
        visited: set[tuple[str, str]] = set()

        def visit(layer: OntologyLayer) -> None:
            identity = (layer.ontology_id, layer.version)
            if identity in visiting:
                raise OntologyError("ontology inheritance contains a cycle")
            if identity in visited:
                return
            visiting.add(identity)
            for pin in layer.parent_pins:
                parent = self._pinned_layer(pin.ontology_id, pin.version)
                actual_hash = sha256_hex(parent.model_dump(mode="json"))
                if actual_hash != pin.layer_sha256:
                    raise OntologyError(
                        f"pinned parent checksum mismatch for {pin.ontology_id!r}@{pin.version!r}"
                    )
                if cutoff is not None and (
                    parent.released_at >= cutoff or parent.effective_from >= cutoff
                ):
                    raise OntologyError(
                        f"pinned parent {pin.ontology_id!r}@{pin.version!r} is not "
                        f"available before {cutoff.isoformat()}"
                    )
                visit(parent)
            visiting.remove(identity)
            visited.add(identity)
            ordered.append(layer)

        visit(target)
        merged: dict[str, OntologySlot] = {}
        for layer in ordered:
            for slot in layer.slots:
                if slot.slot_id in merged:
                    raise OntologyError(
                        f"slot {slot.slot_id!r} is defined by multiple inherited layers"
                    )
                merged[slot.slot_id] = slot
            for override in layer.overrides:
                base = merged.get(override.slot_id)
                if base is None:
                    raise OntologyError(
                        f"override references unknown slot {override.slot_id!r}"
                    )
                updates = override.model_dump(exclude={"slot_id"}, exclude_none=True)
                merged[override.slot_id] = base.model_copy(update=updates)
        slots = tuple(merged[key] for key in sorted(merged))
        try:
            return ResolvedOntology.create(
                ontology_id=target.ontology_id,
                version=target.version,
                component_versions=tuple(
                    (layer.ontology_id, layer.version) for layer in ordered
                ),
                component_vintages=tuple(
                    OntologyComponentVintage(
                        ontology_id=layer.ontology_id,
                        version=layer.version,
                        released_at=layer.released_at,
                        effective_from=layer.effective_from,
                        layer_sha256=sha256_hex(layer.model_dump(mode="json")),
                    )
                    for layer in ordered
                ),
                layer_ids=tuple(layer.ontology_id for layer in ordered),
                slots=slots,
            )
        except ValueError as error:
            raise OntologyError(f"resolved ontology is invalid: {error}") from error

    def _pinned_layer(self, ontology_id: str, version: str) -> OntologyLayer:
        matches = [
            layer for layer in self._versions.get(ontology_id, ())
            if layer.version == version
        ]
        if not matches:
            raise OntologyError(
                f"unknown pinned ontology {ontology_id!r}@{version!r}"
            )
        return matches[0]

    def _select_layer(
        self,
        ontology_id: str,
        *,
        cutoff: datetime | None,
        inclusive: bool,
    ) -> OntologyLayer:
        versions = self._versions.get(ontology_id)
        if not versions:
            raise OntologyError(f"unknown inherited ontology {ontology_id!r}")
        if cutoff is None:
            return versions[-1]
        eligible = [
            layer for layer in versions
            if (
                layer.released_at <= cutoff and layer.effective_from <= cutoff
                if inclusive
                else layer.released_at < cutoff and layer.effective_from < cutoff
            )
        ]
        if not eligible:
            raise OntologyError(
                f"no cutoff-eligible ontology version for {ontology_id!r} before {cutoff.isoformat()}"
            )
        return eligible[-1]


def _layer_sort_key(layer: OntologyLayer) -> tuple[datetime, datetime, tuple[int, int, int]]:
    return (
        layer.released_at,
        layer.effective_from,
        tuple(int(item) for item in layer.version.split(".")),
    )


def _strict_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise OntologyError("ontology as_of must be timezone-aware UTC")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise OntologyError("ontology as_of must use UTC")
    return value.astimezone(timezone.utc)


def _walk_yaml(root: object) -> Iterator[object]:
    """Yield all immutable packaged vintage assets in deterministic order."""

    children = sorted(root.iterdir(), key=lambda item: item.name)  # type: ignore[attr-defined]
    for child in children:
        if child.is_dir():
            yield from _walk_yaml(child)
        elif child.name.endswith((".yaml", ".yml")):
            yield child


__all__ = ["OntologyCatalog", "OntologyError", "load_layer"]

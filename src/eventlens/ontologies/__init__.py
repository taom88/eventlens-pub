"""Company-context ontology public API."""

from .loader import OntologyCatalog, OntologyError, load_layer
from .schema import (
    CitationPolicy, ExtractionMode, FreshnessPolicy, MissingValuePolicy,
    OntologyComponentVintage, OntologyLayer, OntologyLayerType, OntologyParentPin, OntologySlot, RequiredUse,
    ResolvedOntology, SlotCadence, SlotDataType, SlotMateriality, SlotOverride,
    SlotUnit, SlotValidator, SourceKind,
)

__all__ = [
    "CitationPolicy", "ExtractionMode", "FreshnessPolicy", "MissingValuePolicy",
    "OntologyCatalog", "OntologyComponentVintage", "OntologyError", "OntologyLayer", "OntologyLayerType", "OntologyParentPin",
    "OntologySlot", "RequiredUse", "ResolvedOntology", "SlotCadence",
    "SlotDataType", "SlotMateriality", "SlotOverride", "SlotUnit",
    "SlotValidator", "SourceKind", "load_layer",
]

"""Fault templates: a mechanism plus the slots that bind it to a concrete target.

A template is the reusable half of a SREGym problem. Binding it to a workload from the
inventory produces a variant whose mechanism is known-good but whose instance is novel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


@dataclass
class FaultTemplate:
    id: str
    mechanism: str
    category: str          # SREGym taxonomy, e.g. "Misconfiguration Failure"
    fault_level: str       # Container | App | Cluster Management | OS | Hardware
    primitive: str         # the generators/fault inject_<name> this maps to
    symptom_class: str
    root_cause_template: str
    engine: str = "spec"   # "spec" = our JSON-patch ops; "sregym" = drive the primitive itself
    checks: dict = field(default_factory=dict)  # kubectl assertions proving applied/symptom
    invoke: dict = field(default_factory=dict)  # extra args a primitive needs, per action
    requires: dict = field(default_factory=dict)   # what a target must have to be bindable
    params: dict = field(default_factory=dict)     # how to derive the fault payload
    mutation: dict = field(default_factory=dict)   # declarative kubectl patch spec
    public_instances: list[str] = field(default_factory=list)  # ids already in public SREGym

    @property
    def difficulty_hint(self) -> str:
        return self.params.get("difficulty", "unknown")


def load_templates(directory: Path | None = None) -> list[FaultTemplate]:
    directory = directory or TEMPLATE_DIR
    out: list[FaultTemplate] = []
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text()) or {}
        out.append(FaultTemplate(**raw))
    return out


def templates_by_id(templates: list[FaultTemplate]) -> dict[str, FaultTemplate]:
    return {t.id: t for t in templates}

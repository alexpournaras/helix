"""Model probes and pre-flight checks (PLAN B6 "Model probes", B13 POST, B16).

A probe sends a tiny structured request and records whether structured output works, which effort values
are accepted, and the latency. Pre-flight refuses role assignments that were not probed or failed, an
OpenAI project that shares data (unless the case is synthetic), and a non-loopback bind.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from . import strict
from .providers.base import KNOWN_MODELS, ModelError, StructuredRequest
from .settings import ModelRef, ResearchSettings, resolve_role, strongest_other

PROBE_EFFORTS = {
    "openai": ["none", "minimal", "low", "medium", "high", "xhigh"],
    "anthropic": ["low", "medium", "high", "xhigh", "max"],
}


class ProbeOut(BaseModel):
    ok: bool
    word: str


PROBE_SYSTEM = "This is a connectivity probe. Return ok=true and word=\"helix\"."
PROBE_USER = "Probe."


class ProbeStore:
    """data/research_probes.json: model → last probe record."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def all(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, model: str) -> dict | None:
        return self.all().get(model)

    def put(self, model: str, record: dict) -> None:
        with self._lock:
            data = self.all()
            data[model] = record
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), "utf-8")
            tmp.replace(self.path)

    def accepts(self, ref: ModelRef) -> bool:
        record = self.get(ref.model)
        if not record or not record.get("ok"):
            return False
        if ref.effort and ref.effort in (record.get("efforts_rejected") or []):
            return False
        return True


async def probe(provider: Any, ref: ModelRef, *, efforts: list[str] | None = None) -> dict:
    """Probe one model. `efforts` defaults to the provider's candidate list; the configured effort goes first."""
    schema = strict.to_provider_schema(ProbeOut)
    candidates = efforts if efforts is not None else list(PROBE_EFFORTS.get(ref.family, []))
    if ref.effort and ref.effort not in candidates:
        candidates.insert(0, ref.effort)
    record: dict[str, Any] = {
        "model": ref.model, "provider": ref.family, "ok": False, "structured": False,
        "efforts_accepted": [], "efforts_rejected": [], "latency_ms": None, "error": None,
        "context_window": KNOWN_MODELS.get(ref.model, {}).get("context_window"),
        "probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    plan: list[str | None] = list(candidates) or [None]
    for effort in plan:
        request = StructuredRequest(model=ref.model, system=PROBE_SYSTEM, user=PROBE_USER, schema=schema,
                                    schema_name="ProbeOut", effort=effort, max_output_tokens=2000,
                                    idle_timeout=60, deadline=180)
        started = time.monotonic()
        try:
            result = await provider.complete(request)
        except ModelError as exc:
            if exc.kind == "config" and effort is not None and exc.status == 400:
                record["efforts_rejected"].append(effort)
                continue
            record["error"] = f"{exc.kind}: {exc.message}"
            if exc.kind == "config":
                break
            continue
        latency = int((time.monotonic() - started) * 1000)
        valid = False
        for text in reversed(result.texts):
            try:
                ProbeOut.model_validate_json(text)
                valid = True
                break
            except ValueError:
                pass
        if valid:
            record["structured"] = True
            record["ok"] = True
            if effort is not None:
                record["efforts_accepted"].append(effort)
            record["latency_ms"] = record["latency_ms"] or latency
        else:
            record["error"] = "Structured output did not validate."
    return record


# ---------- pre-flight ----------

@dataclass
class Preflight:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    independence: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings, "providers": self.providers,
                "independence": self.independence}


def is_loopback_bind() -> bool:
    host = os.environ.get("HELIX_HOST", "127.0.0.1").strip().lower()
    return host in ("127.0.0.1", "localhost", "::1", "[::1]") or host.startswith("127.")


def remote_allowed() -> bool:
    return os.environ.get("HELIX_ALLOW_REMOTE", "") == "1"


def loopback_guard() -> str | None:
    """Error text when the research API must refuse requests (B16.1)."""
    if is_loopback_bind() or remote_allowed():
        return None
    return ("The research API only runs when Helix is bound to this machine (HELIX_HOST=127.0.0.1). "
            "Set HELIX_ALLOW_REMOTE=1 only behind authentication.")


def privacy_gate(settings: ResearchSettings, *, synthetic: bool, uses_openai: bool) -> str | None:
    shares = settings.privacy.openai_project_shares_data
    if not uses_openai:
        return None
    if shares is None:
        return ("Answer the privacy question first: does your OpenAI project share data with OpenAI? "
                "(Settings → Research → Privacy.)")
    if shares and not synthetic:
        return ("Your OpenAI project shares inputs and outputs with OpenAI. Real health text is blocked; "
                "use a project without data sharing, or mark the case as synthetic.")
    return None


def run(settings: ResearchSettings, providers: dict[str, Any], probes: ProbeStore, *, synthetic: bool,
        require_probes: bool = True) -> Preflight:
    errors: list[str] = []
    warnings: list[str] = []
    guard = loopback_guard()
    if guard:
        errors.append(guard)

    def available(ref: ModelRef) -> bool:
        provider = providers.get(ref.family)
        if provider is None or not provider.has_key():
            return False
        return probes.accepts(ref) if require_probes else True

    used: set[str] = set()
    independence: dict[str, str] = {}
    for role in settings.roles:
        if role == "figure_reader" and not settings.figures.get("enabled"):
            continue
        try:
            resolved = resolve_role(settings, role, available)
        except LookupError:
            errors.append(f"Role {role}: no configured model is available"
                          + (" and probed" if require_probes else "") + ".")
            continue
        independence[role] = resolved.independence
        used.update(m.family for m in resolved.members)
        for sub in resolved.substitutions:
            warnings.append(f"Role {role}: {sub}.")
        if resolved.independence == "same_family":
            warnings.append(f"Role {role}: both sides of the check are from one model family.")
        elif resolved.independence == "reduced":
            warnings.append(f"Role {role}: runs with fewer models than configured.")
    for family in ("openai", "anthropic"):
        try:
            strongest_other(settings, family, available)
        except LookupError:
            errors.append("No strongest model is available for adjudication and confirmation.")
            break
    gate = privacy_gate(settings, synthetic=synthetic, uses_openai="openai" in used)
    if gate:
        errors.append(gate)
    return Preflight(ok=not errors, errors=errors, warnings=sorted(set(warnings)), providers=sorted(used),
                     independence=independence)

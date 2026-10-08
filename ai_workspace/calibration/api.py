"""Э1 Ф7: resolver калибровки — pure, без I/O. Режимы Б (паритет) / П (opt-in профиль)."""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PARAMS = ("retries", "max_iterations", "shaping", "context_mode")
# скаляр -> ключ узла (retry ОТЛИЧАЕТСЯ от имени скаляра)
NODE_KEY = {"retries": "retry", "max_iterations": "max_iterations",
            "shaping": None, "context_mode": "context"}
DEFAULT = {"retries": 0, "max_iterations": 1, "shaping": "full-context",
           "context_mode": "full"}

@dataclass(frozen=True)
class ResolvedScalars:
    retries: int
    max_iterations: int
    shaping: str
    context_mode: str
    sources: dict
    stale_marks: tuple
    profile_id: str | None
    drift: str | None

def _node_get(node, key):
    return node.get(key) if hasattr(node, "get") else None

def _node_id(node, fallback):
    nid = getattr(node, "id", None)
    return nid if nid is not None else fallback

def _calibrated_for_matches(cal, facts):
    if facts is None:
        return True
    if not isinstance(cal, Mapping):
        return False
    return cal.get("model_id") == facts.get("model_id") and cal.get("digest") == facts.get("digest")

def resolve(mode_nodes, registry, model_facts=None, *, profile=None):
    now = str(profile.get("profile_id")) if isinstance(profile, Mapping) and profile.get("profile_id") else None
    # схема профиля: status, calibrated_for{model_id,digest}, scalars{...}
    reg_classes = {}
    if registry is not None:
        try:
            reg_classes = registry.get("model_classes") or {}
        except Exception:
            reg_classes = {}
    out = {}
    for key, node in (mode_nodes.items() if isinstance(mode_nodes, Mapping) else []):
        nid = _node_id(node, key)
        cls = str(_node_get(node, "model_class") or "fast")
        spec = reg_classes.get(cls) or {}
        pstatus = profile.get("status") if isinstance(profile, Mapping) else None
        active = spec.get("active_profile")
        # --- гейт П (конъюнкция, fail-closed) ---
        mode_p = (profile is not None and isinstance(profile, Mapping)
                  and spec.get("calibration_status") == "calibrated"
                  and bool(active)
                  and pstatus == "calibrated"
                  and _calibrated_for_matches(profile.get("calibrated_for"), model_facts))
        drift = None
        pid = None
        if profile is not None and isinstance(profile, Mapping):
            if spec.get("calibration_status") == "calibrated" and active and pstatus == "calibrated" \
               and not _calibrated_for_matches(profile.get("calibrated_for"), model_facts):
                drift = "digest_mismatch"
            elif spec.get("calibration_status") != pstatus:
                drift = "status_divergence"
        if mode_p:
            pid = now
        prof_scalars = (profile.get("scalars") or {}) if (mode_p and isinstance(profile, Mapping)) else {}
        pins = _node_get(node, "calibration_pin") or []
        vals, srcs = {}, {}
        for p in PARAMS:
            nkey = NODE_KEY[p]
            has_node = nkey is not None and hasattr(node, "get") and (nkey in node)
            if p in pins and has_node:              # P
                vals[p], srcs[p] = node.get(nkey), "pin"
            elif p in prof_scalars:                 # C
                vals[p], srcs[p] = prof_scalars[p], "profile"
            elif has_node:                          # N
                vals[p], srcs[p] = node.get(nkey), "node"
            elif mode_p and (p in spec):            # R (только режим П)
                vals[p], srcs[p] = spec[p], "class"
            else:                                   # E
                vals[p], srcs[p] = DEFAULT[p], "default"
        out[nid] = ResolvedScalars(
            retries=int(vals["retries"]), max_iterations=int(vals["max_iterations"]),
            shaping=str(vals["shaping"]), context_mode=str(vals["context_mode"]),
            sources=srcs, stale_marks=(), profile_id=pid, drift=drift)
    return out

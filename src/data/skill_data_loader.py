"""Active skill data loader with fallback chain and schema adapter.

Replaces the removed `data/pob_complete_skills.json` path that two handlers
(`_handle_inspect_spell_gem`, `_handle_list_all_spells`) previously read
directly. Picks data from the first available source:

    1. data/pob2/pob2_active_skills.json  (PoB2 v0.15+, 469 skills with
       per-skill `constant_stats`; produced by scripts/pob2_to_json.py).
    2. data/complete_models/active_skills.json  (upstream snapshot,
       6454 entries with tags/damage_types but mostly empty stats).

When the PoB2 file is the primary source, this loader still consults
complete_models to back-fill `skillTypes` (tags + damage_types) on
entries that share an id — PoB2's per-skill records don't include
type tags but the handler's element-detection and filter_tags depend
on them.

The loader normalizes both sources to a single shape understood by
the existing handler rendering code:

    {
      "metadata": {"source": str, "extraction_date": str, "count": int},
      "skills": {
          skill_id: {
              "name": str,
              "description": str,
              "skillTypes": list[str],
              "weaponTypes": list[str],
              "castTime": float,
              "levels": dict,             # empty for both sources today
              "statSets": list[dict],     # PoB2 only; one synthetic set
              "qualityStats": list,       # empty for both sources today
              "hidden": bool,
          }, ...
      }
    }

Per-level numerics (`levels`) require a join with PoB2's `Gems.lua`
which is not currently parsed; entries return `levels: {}` and the
handlers' existing `if levels:` guards skip that rendering block.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Module-level cache; skill data is immutable at runtime.
_CACHE: dict[str, Any] | None = None


def _find_sources(module_dir: Path) -> tuple[Path | None, Path | None]:
    """Locate (preferred, fallback) data files relative to the installed package.

    The two candidate roots match both layouts FreshDataProvider already
    handles: installed wheel (site-packages) and working-tree checkout.
    """
    candidates = [
        # site-packages layout: <prefix>/site-packages/src/data/<this>.py
        # → data/ lives at <prefix>/site-packages/data/
        module_dir.parent.parent.parent / "data",
        # working tree: src/data/<this>.py → data/
        module_dir.parent.parent / "data",
    ]
    pob2: Path | None = None
    full: Path | None = None
    for root in candidates:
        if pob2 is None:
            cand = root / "pob2" / "pob2_active_skills.json"
            if cand.is_file():
                pob2 = cand
        if full is None:
            cand = root / "complete_models" / "active_skills.json"
            if cand.is_file():
                full = cand
    return pob2, full


def _constant_stats_to_pairs(constant_stats: list[dict]) -> list[list]:
    """Convert PoB2 `[{stat, value}, ...]` to handler's `[[stat_id, value], ...]`."""
    pairs = []
    for item in constant_stats:
        stat = item.get("stat")
        if stat is None:
            continue
        pairs.append([stat, item.get("value")])
    return pairs


def _adapt_pob2(skill_id: str, raw: dict, tags_lookup: dict[str, list[str]]) -> dict:
    """Adapt a PoB2 active_skills record to the handler-consumed shape."""
    const_stats = _constant_stats_to_pairs(raw.get("constant_stats", []) or [])
    stat_sets = []
    if const_stats:
        stat_sets.append({
            "label": "Constant Stats",
            "constantStats": const_stats,
        })
    return {
        "name": raw.get("name") or skill_id,
        "description": raw.get("description") or "",
        "skillTypes": tags_lookup.get(skill_id, []),
        "weaponTypes": [],
        "castTime": 0,
        "levels": {},
        "statSets": stat_sets,
        "qualityStats": [],
        "hidden": False,
    }


def _adapt_complete_models(skill_id: str, raw: dict) -> dict:
    """Adapt a complete_models active_skills record to the handler shape."""
    tags = list(raw.get("tags", []) or [])
    damage_types = list(raw.get("damage_types", []) or [])
    # Element-detection in _handle_list_all_spells looks for Fire/Cold/
    # Lightning/Chaos in skillTypes; normalize damage_types to that casing.
    type_aliases = []
    for dt in damage_types:
        if not isinstance(dt, str):
            continue
        type_aliases.append(dt.capitalize())
    return {
        "name": raw.get("display_name") or skill_id,
        "description": raw.get("description") or "",
        "skillTypes": tags + type_aliases,
        "weaponTypes": [],
        "castTime": 0,
        "levels": {},
        "statSets": [],
        "qualityStats": [],
        "hidden": bool(raw.get("is_support")),  # support gems shouldn't show up in spell list
    }


def _build_tags_lookup(full_path: Path | None) -> dict[str, list[str]]:
    """Read complete_models and extract id → (tags + damage_types) mapping."""
    if full_path is None:
        return {}
    try:
        data = json.loads(full_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"[skill_data_loader] cannot read {full_path}: {exc}")
        return {}
    lookup: dict[str, list[str]] = {}
    for skill_id, raw in (data.get("active_skills") or {}).items():
        if not isinstance(raw, dict):
            continue
        tags = list(raw.get("tags", []) or [])
        damage_types = [dt.capitalize() for dt in (raw.get("damage_types") or [])
                        if isinstance(dt, str)]
        if tags or damage_types:
            lookup[skill_id] = tags + damage_types
    return lookup


def load_active_skills() -> dict[str, Any]:
    """Return adapted active-skill data. Cached at module level."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    module_dir = Path(__file__).parent
    pob2_path, full_path = _find_sources(module_dir)

    if pob2_path is None and full_path is None:
        logger.error("[skill_data_loader] no active-skill data files found")
        _CACHE = {"metadata": {"source": "none", "count": 0}, "skills": {}}
        return _CACHE

    skills: dict[str, dict] = {}
    source_tag = ""
    extraction_date = "unknown"

    if pob2_path is not None:
        try:
            pob2_data = json.loads(pob2_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"[skill_data_loader] cannot read {pob2_path}: {exc}")
            pob2_data = {}
        tags_lookup = _build_tags_lookup(full_path)
        for skill_id, raw in (pob2_data.get("active_skills") or {}).items():
            if not isinstance(raw, dict):
                continue
            skills[skill_id] = _adapt_pob2(skill_id, raw, tags_lookup)
        meta = pob2_data.get("metadata", {})
        git_meta = meta.get("pob2_git", {}) if isinstance(meta, dict) else {}
        source_tag = f"pob2 {git_meta.get('describe', '?')}"
        extraction_date = meta.get("parsed_at", "unknown") if isinstance(meta, dict) else "unknown"

    # Fill gaps from complete_models — only IDs not already supplied by PoB2.
    if full_path is not None:
        try:
            full_data = json.loads(full_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"[skill_data_loader] cannot read {full_path}: {exc}")
            full_data = {}
        added = 0
        for skill_id, raw in (full_data.get("active_skills") or {}).items():
            if skill_id in skills or not isinstance(raw, dict):
                continue
            skills[skill_id] = _adapt_complete_models(skill_id, raw)
            added += 1
        if not source_tag:
            source_tag = "complete_models"
            extraction_date = full_data.get("metadata", {}).get("extraction_date", "unknown") \
                if isinstance(full_data.get("metadata"), dict) else "unknown"
        elif added:
            source_tag += f" + complete_models ({added} extra)"

    _CACHE = {
        "metadata": {
            "source": source_tag or "unknown",
            "extraction_date": extraction_date,
            "count": len(skills),
        },
        "skills": skills,
    }
    logger.info(f"[skill_data_loader] loaded {len(skills)} active skills from {source_tag}")
    return _CACHE


def reset_cache() -> None:
    """Clear the module-level cache. Useful in tests."""
    global _CACHE
    _CACHE = None

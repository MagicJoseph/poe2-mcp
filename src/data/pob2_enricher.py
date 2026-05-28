"""Optional PoB2 enrichment for FreshDataProvider.

Reads data/pob2/pob2_supports.json (produced by scripts/pob2_to_json.py)
and merges its values into FreshDataProvider._support_gems. Entries that
already exist in complete_models are upgraded with real PoB2 multipliers,
spirit cost, and gem family; new entries are added.

This module is best-effort: if the JSON file is absent, the enrichment
is silently skipped and the provider keeps whatever was loaded from
complete_models or extracted snapshots.

Entry point: `enrich_with_pob2(provider)`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _find_pob2_data(provider_module_dir: Path) -> Path | None:
    """Search for pob2_supports.json in known locations relative to the package."""
    candidates = [
        # Installed package layout: <prefix>/.../site-packages/src/data/<this>.py
        # → data/pob2 lives at <prefix>/.../site-packages/data/pob2/
        provider_module_dir.parent.parent.parent / "data" / "pob2" / "pob2_supports.json",
        # Working-tree layout: src/data/<this>.py → data/pob2/
        provider_module_dir.parent.parent / "data" / "pob2" / "pob2_supports.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _format_display_name(name: str) -> str:
    """PoB2 has `name = 'Heft'`; MCP convention uses `display_name = 'Heft Support'`."""
    if name.endswith(" Support"):
        return name
    return f"{name} Support"


def _convert_constant_stats(constant_stats: list[dict]) -> dict[str, Any]:
    """Convert [{'stat':'x','value':30}, ...] to {'x': 30, ...}."""
    return {item["stat"]: item["value"] for item in constant_stats if "stat" in item}


def enrich_with_pob2(provider) -> dict[str, int]:
    """Merge PoB2 support-gem data into the provider.

    Returns a dict of statistics (matched, added, total_after). Safe to
    call repeatedly; subsequent calls just re-merge.
    """
    module_dir = Path(__file__).parent
    pob2_file = _find_pob2_data(module_dir)
    if pob2_file is None:
        logger.info("[pob2_enricher] pob2_supports.json not found — skipping")
        return {"matched": 0, "added": 0, "total_after": len(provider._support_gems)}

    try:
        data = json.loads(pob2_file.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"[pob2_enricher] failed to read {pob2_file}: {exc}")
        return {"matched": 0, "added": 0, "total_after": len(provider._support_gems)}

    pob2_supports: dict[str, dict] = data.get("supports", {})
    if not pob2_supports:
        return {"matched": 0, "added": 0, "total_after": len(provider._support_gems)}

    existing = provider._support_gems
    matched = 0
    added = 0

    for pob2_id, pob2_data in pob2_supports.items():
        effects = _convert_constant_stats(pob2_data.get("constant_stats", []))

        enriched = {
            "id": pob2_id,
            "display_name": _format_display_name(pob2_data.get("name", pob2_id)),
            "name": pob2_data.get("name"),
            "description": pob2_data.get("description"),
            "effects": effects,
            "stat_effects": [{"stat": s, "value": v} for s, v in effects.items()],
            "mana_multiplier": pob2_data.get("mana_multiplier"),
            "spirit_cost": pob2_data.get("spirit_reservation", 0) or 0,
            "is_lineage": pob2_data.get("is_lineage", False),
            "gem_family": pob2_data.get("gem_family", []),
            "tags": pob2_data.get("require_skill_types", []),
            "require_skill_types": pob2_data.get("require_skill_types", []),
            "exclude_skill_types": pob2_data.get("exclude_skill_types", []),
            "add_skill_types": pob2_data.get("add_skill_types", []),
            "data_source": "pob2",
        }

        if pob2_id in existing:
            old = existing[pob2_id]
            for k, v in enriched.items():
                if v in (None, "", [], {}):
                    continue
                old[k] = v
            matched += 1
        else:
            existing[pob2_id] = enriched
            added += 1

    total_after = len(existing)
    logger.info(
        f"[pob2_enricher] PoB2 enrichment: matched={matched}, "
        f"added={added}, total={total_after}"
    )

    meta = data.get("metadata", {})
    if isinstance(meta, dict):
        provider._pob2_meta = {
            "version": meta.get("pob2_git", {}).get("describe", "?"),
            "head_short": meta.get("pob2_git", {}).get("head_short", "?"),
            "parsed_at": meta.get("parsed_at", "?"),
            "counts": meta.get("counts", {}),
        }

    return {"matched": matched, "added": added, "total_after": total_after}

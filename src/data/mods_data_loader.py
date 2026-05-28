"""Item modifier data loader with fallback chain.

Five mod handlers in `mcp_server.py` (`list_all_mods`, `search_mods_by_stat`,
`inspect_mod`, `get_mod_tiers`, `get_available_mods`) previously read
`data/poe2_mods_extracted.json` directly — that filename no longer ships in
the package. The current bundled file is `data/poe2_mods_corrected.json`
with an identical schema (`{metadata, mods: [{mod_id, generation_type_name,
level_requirement, min_value, max_value, domain_flag, stats}, ...]}`).

This loader picks the first available file from a fallback chain and
caches the result at module level.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CACHE: dict[str, Any] | None = None

_CANDIDATE_NAMES = (
    "poe2_mods_corrected.json",
    "poe2_mods_extracted.json",
)


def _find_mods_file(module_dir: Path) -> Path | None:
    """Search candidate names under both site-packages and working-tree layouts."""
    roots = [
        module_dir.parent.parent.parent / "data",  # site-packages layout
        module_dir.parent.parent / "data",         # working-tree layout
    ]
    for root in roots:
        for name in _CANDIDATE_NAMES:
            cand = root / name
            if cand.is_file():
                return cand
    return None


def load_mods_data() -> dict[str, Any]:
    """Return the mods file content. Cached. `{"mods": [...], "metadata": {...}}` on hit;
    `{"mods": [], "metadata": {"source": "missing"}}` if no file is found."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    path = _find_mods_file(Path(__file__).parent)
    if path is None:
        logger.error("[mods_data_loader] no mods file found in expected locations")
        _CACHE = {"metadata": {"source": "missing"}, "mods": []}
        return _CACHE

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"[mods_data_loader] cannot read {path}: {exc}")
        _CACHE = {"metadata": {"source": "error"}, "mods": []}
        return _CACHE

    meta = data.get("metadata", {}) if isinstance(data.get("metadata"), dict) else {}
    meta = {**meta, "source_file": path.name}
    data["metadata"] = meta
    _CACHE = data
    logger.info(f"[mods_data_loader] loaded {len(data.get('mods', []))} mods from {path.name}")
    return _CACHE


def reset_cache() -> None:
    """Clear the module-level cache. Useful in tests."""
    global _CACHE
    _CACHE = None

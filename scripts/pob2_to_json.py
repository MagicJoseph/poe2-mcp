#!/usr/bin/env python3
"""PoB2 Lua → canonical JSON parser.

PoB2 (PathOfBuilding-PoE2) stores gem data in Lua files with a predictable
structure. This parser extracts selected fields without a Lua interpreter,
using balanced-brace traversal + targeted regexes.

Output: JSON compatible with FreshDataProvider, with additional fields:
    - display_name (from PoB2.name + " Support" if support)
    - effects (from constantStats)
    - mana_multiplier (from levels[1].manaMultiplier)
    - spirit_reservation (from levels[1].spiritReservationFlat)
    - require_skill_types, exclude_skill_types
    - gem_family
    - is_lineage
    - description

Usage:
    python pob2_parser.py <pob2-dir> <output-dir>

Reads from:
    <pob2-dir>/src/Data/Skills/sup_*.lua → supports
    <pob2-dir>/src/Data/Skills/act_*.lua → active skills
    <pob2-dir>/src/Data/Gems.lua         → gem metadata (tags, requirements)

Writes:
    <output-dir>/pob2_supports.json
    <output-dir>/pob2_active_skills.json
    <output-dir>/pob2_gems.json
    <output-dir>/pob2_metadata.json (version, HEAD SHA, parser version)
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

PARSER_VERSION = "1.0.0"


# ─── Generic Lua block extraction ───────────────────────────────────────────

def find_balanced(text: str, start: int) -> int:
    """Return the index of the CLOSING brace for the opener at position `start`.

    Handles "" / '' strings and -- comments. `start` MUST point at '{'.
    """
    if text[start] != "{":
        raise ValueError(f"Expected '{{' at position {start}, found '{text[start]}'")
    depth = 0
    i = start
    in_string = False
    string_quote = ""
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == string_quote:
                in_string = False
        else:
            if c == '"' or c == "'":
                in_string = True
                string_quote = c
            elif c == "-" and i + 1 < len(text) and text[i + 1] == "-":
                # Line comment — skip to end of line
                nl = text.find("\n", i)
                i = (nl if nl != -1 else len(text)) + 1
                continue
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    raise ValueError(f"Unclosed brace starting at position {start}")


# ─── Per-skill block field extraction ────────────────────────────────────────

RE_STRING = r'"([^"\\]*(?:\\.[^"\\]*)*)"'
RE_NUM = r"-?\d+(?:\.\d+)?"


def _str(field: str, block: str) -> str | None:
    m = re.search(rf'^\s*{field}\s*=\s*{RE_STRING}', block, re.MULTILINE)
    return m.group(1) if m else None


def _bool(field: str, block: str) -> bool | None:
    m = re.search(rf'^\s*{field}\s*=\s*(true|false)', block, re.MULTILINE)
    if not m:
        return None
    return m.group(1) == "true"


def _str_list(field: str, block: str) -> list[str]:
    m = re.search(rf'^\s*{field}\s*=\s*\{{([^}}]*)\}}', block, re.MULTILINE)
    if not m:
        return []
    return re.findall(RE_STRING, m.group(1))


def _skill_type_list(field: str, block: str) -> list[str]:
    m = re.search(rf'^\s*{field}\s*=\s*\{{([^}}]*)\}}', block, re.MULTILINE)
    if not m:
        return []
    return re.findall(r"SkillType\.(\w+)", m.group(1))


def _level1_field(name: str, block: str) -> int | None:
    """Extract a number from `[1] = { ..., name = X, ...}` inside levels."""
    # Look at the first `[1] = {` that contains our field
    m = re.search(
        rf"\[1\]\s*=\s*\{{[^}}]*\b{name}\s*=\s*({RE_NUM})[^}}]*\}}",
        block,
    )
    if not m:
        return None
    val = m.group(1)
    return int(float(val)) if "." not in val else float(val)


def _constant_stats(block: str) -> list[tuple[str, float]]:
    """Extract constantStats = { {"name", value}, ... } from statSets."""
    # Find the first `constantStats = {` — may contain nested {} as pairs
    m = re.search(r"constantStats\s*=\s*\{", block)
    if not m:
        return []
    try:
        end = find_balanced(block, m.end() - 1)
    except ValueError:
        return []
    body = block[m.end() : end]
    # Each stat: { "name", value }
    pairs = re.findall(rf'\{{\s*"([^"]+)"\s*,\s*({RE_NUM})\s*\}}', body)
    return [(name, int(float(val)) if "." not in val else float(val)) for name, val in pairs]


# ─── High-level skill block parsing ──────────────────────────────────────────

SKILL_BLOCK_HEADER = re.compile(r"^skills\[\"([^\"]+)\"\]\s*=\s*\{", re.MULTILINE)


def parse_skills_file(text: str) -> dict[str, dict[str, Any]]:
    """Parse a `act_*.lua` / `sup_*.lua` file. Returns {skill_id: parsed_dict}."""
    out: dict[str, dict[str, Any]] = {}
    for header in SKILL_BLOCK_HEADER.finditer(text):
        skill_id = header.group(1)
        open_brace = header.end() - 1
        close_brace = find_balanced(text, open_brace)
        block = text[open_brace + 1 : close_brace]

        rec: dict[str, Any] = {"id": skill_id}
        if (v := _str("name", block)) is not None:
            rec["name"] = v
        if (v := _str("description", block)) is not None:
            rec["description"] = v
        if (v := _bool("support", block)) is not None:
            rec["support"] = v
        if (v := _bool("isLineage", block)) is not None:
            rec["is_lineage"] = v
        if fam := _str_list("gemFamily", block):
            rec["gem_family"] = fam
        if req := _skill_type_list("requireSkillTypes", block):
            rec["require_skill_types"] = req
        if exc := _skill_type_list("excludeSkillTypes", block):
            rec["exclude_skill_types"] = exc
        if add := _skill_type_list("addSkillTypes", block):
            rec["add_skill_types"] = add

        mm = _level1_field("manaMultiplier", block)
        if mm is not None:
            rec["mana_multiplier"] = mm
        sr = _level1_field("spiritReservationFlat", block)
        if sr is not None:
            rec["spirit_reservation"] = sr

        cs = _constant_stats(block)
        if cs:
            rec["constant_stats"] = [{"stat": s, "value": v} for s, v in cs]

        out[skill_id] = rec
    return out


# ─── Gems.lua parsing (light) ────────────────────────────────────────────────

GEM_BLOCK_HEADER = re.compile(r'\["(Metadata/[^"]+)"\]\s*=\s*\{', re.MULTILINE)


def parse_gems_file(text: str) -> dict[str, dict[str, Any]]:
    """Parse Gems.lua — gem metadata (tags, requirements, tier)."""
    out: dict[str, dict[str, Any]] = {}
    for header in GEM_BLOCK_HEADER.finditer(text):
        gem_id = header.group(1)
        open_brace = header.end() - 1
        close_brace = find_balanced(text, open_brace)
        block = text[open_brace + 1 : close_brace]

        rec: dict[str, Any] = {"metadata_id": gem_id}
        for f in ("name", "baseTypeName", "gameId", "variantId",
                  "grantedEffectId", "gemType", "tagString",
                  "weaponRequirements"):
            v = _str(f, block)
            if v is not None:
                rec[f] = v
        # Numeric requirements
        for f in ("reqStr", "reqDex", "reqInt", "Tier", "naturalMaxLevel"):
            m = re.search(rf'^\s*{f}\s*=\s*({RE_NUM})', block, re.MULTILINE)
            if m:
                rec[f] = int(float(m.group(1)))
        # tags = { foo = true, bar = true }
        m = re.search(r"^\s*tags\s*=\s*\{([^}]*)\}", block, re.MULTILINE)
        if m:
            rec["tags"] = re.findall(r"(\w+)\s*=\s*true", m.group(1))

        out[gem_id] = rec
    return out


# ─── CLI orchestration ──────────────────────────────────────────────────────

def get_git_meta(pob2_dir: Path) -> dict[str, str]:
    def _git(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", str(pob2_dir), *args],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except subprocess.CalledProcessError:
            return ""

    return {
        "head_sha": _git("rev-parse", "HEAD"),
        "head_short": _git("rev-parse", "--short", "HEAD"),
        "head_subject": _git("log", "-1", "--format=%s"),
        "head_date": _git("log", "-1", "--format=%aI"),
        "describe": _git("describe", "--tags", "--always"),
    }


def main(pob2_dir_arg: str, output_dir_arg: str) -> int:
    pob2 = Path(pob2_dir_arg)
    out = Path(output_dir_arg)
    out.mkdir(parents=True, exist_ok=True)

    skills_dir = pob2 / "src" / "Data" / "Skills"
    if not skills_dir.is_dir():
        print(f"ERROR: missing {skills_dir}", file=sys.stderr)
        return 1

    # Supports
    supports: dict[str, dict[str, Any]] = {}
    for fname in ("sup_str.lua", "sup_dex.lua", "sup_int.lua"):
        f = skills_dir / fname
        if not f.exists():
            print(f"  WARNING: missing {f}", file=sys.stderr)
            continue
        parsed = parse_skills_file(f.read_text(encoding="utf-8"))
        supports.update(parsed)
        print(f"  {fname}: {len(parsed)} entries")

    # Active skills
    active: dict[str, dict[str, Any]] = {}
    for fname in ("act_str.lua", "act_dex.lua", "act_int.lua",
                  "minion.lua", "other.lua"):
        f = skills_dir / fname
        if not f.exists():
            continue
        parsed = parse_skills_file(f.read_text(encoding="utf-8"))
        active.update(parsed)
        print(f"  {fname}: {len(parsed)} entries")

    # Gems metadata
    gems_file = pob2 / "src" / "Data" / "Gems.lua"
    gems: dict[str, dict[str, Any]] = {}
    if gems_file.exists():
        gems = parse_gems_file(gems_file.read_text(encoding="utf-8"))
        print(f"  Gems.lua: {len(gems)} entries")

    git_meta = get_git_meta(pob2)
    meta = {
        "source": "PathOfBuildingCommunity/PathOfBuilding-PoE2",
        "parser_version": PARSER_VERSION,
        "parsed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "pob2_git": git_meta,
        "counts": {
            "supports": len(supports),
            "active_skills": len(active),
            "gems": len(gems),
        },
    }

    (out / "pob2_supports.json").write_text(
        json.dumps({"metadata": meta, "supports": supports}, indent=2),
        encoding="utf-8",
    )
    (out / "pob2_active_skills.json").write_text(
        json.dumps({"metadata": meta, "active_skills": active}, indent=2),
        encoding="utf-8",
    )
    (out / "pob2_gems.json").write_text(
        json.dumps({"metadata": meta, "gems": gems}, indent=2),
        encoding="utf-8",
    )
    (out / "pob2_metadata.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8",
    )

    print(f"\n[parser] OK — output: {out}")
    print(f"  supports:      {len(supports)}")
    print(f"  active_skills: {len(active)}")
    print(f"  gems:          {len(gems)}")
    print(f"  git: {git_meta.get('describe', '?')} ({git_meta.get('head_short', '?')})")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: pob2_parser.py <pob2-dir> <output-dir>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))

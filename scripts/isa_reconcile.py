#!/usr/bin/env python3
"""isa_reconcile — deterministic ID-keyed merge of _ephemeral slices (ISA-SPEC §7)."""
from __future__ import annotations
import argparse, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import isa_common

# ---------------------------------------------------------------------------
# Verification block parser
# ---------------------------------------------------------------------------

# A verification block starts with a line whose leading non-whitespace content
# begins with an ISC id (possibly prefixed by dashes, stars, or ** markers).
_VBLOCK_RE = re.compile(r"^\s*(?:[-*]+\s*)?(?:\*\*\s*)?(ISC-[0-9][0-9.]*)\b")


def _isc_sort_key(isc_id: str) -> tuple:
    """Convert 'ISC-7.1' → (7, 1) for numeric comparison."""
    suffix = isc_id.split("-", 1)[1]  # "7.1"
    return tuple(int(p) for p in suffix.split("."))


def _parse_verification_body(body: str) -> tuple[str, dict]:
    """Split a Verification section body into (preamble, blocks).

    Returns:
        preamble: text before the first ISC block header (may be empty)
        blocks: ordered dict {isc_id: block_text} where block_text includes
                the header line and all continuation lines up to the next block.
    """
    lines = body.splitlines(keepends=True)
    preamble_lines: list[str] = []
    blocks: dict[str, str] = {}
    current_id: str | None = None
    current_lines: list[str] = []

    for line in lines:
        m = _VBLOCK_RE.match(line)
        if m:
            # Flush previous block
            if current_id is not None:
                blocks[current_id] = "".join(current_lines)
            else:
                # Preamble lines collected so far — but a match already reset
                # current_id, so preamble is what we collected before any match.
                pass
            current_id = m.group(1)
            current_lines = [line]
        else:
            if current_id is None:
                preamble_lines.append(line)
            else:
                current_lines.append(line)

    # Flush the last block
    if current_id is not None:
        blocks[current_id] = "".join(current_lines)

    preamble = "".join(preamble_lines)
    return preamble, blocks


def _build_verification_body(preamble: str, merged_blocks: dict) -> str:
    """Reassemble a Verification body from preamble + sorted merged blocks."""
    sorted_ids = sorted(merged_blocks.keys(), key=_isc_sort_key)
    parts = [preamble] if preamble else []
    for isc_id in sorted_ids:
        parts.append(merged_blocks[isc_id])
    return "".join(parts)


# ---------------------------------------------------------------------------
# Surgical string edits on the raw master text
# ---------------------------------------------------------------------------

# Matches a full ISC checkbox line so we can swap its state character.
_ISC_LINE_RE = re.compile(
    r"^(-\s*\[)([ xX\-])(\]\s*ISC-[0-9][0-9.]*\s*:.*)$",
    re.MULTILINE,
)

# Matches the frontmatter progress line.
_PROGRESS_LINE_RE = re.compile(
    r"^(progress:\s*)(\d+)(/\d+)(\s*)$",
    re.MULTILINE,
)

# Matches a ## Verification section — captures the section body (until next ##
# heading or end of file).
_VERIFICATION_SECTION_RE = re.compile(
    r"(^##\s+Verification\s*$\n)(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _swap_isc_states(raw: str, state_changes: dict) -> str:
    """For each {isc_id: new_state} in state_changes, rewrite its checkbox in raw.

    Only the state character is changed; everything else on the line is
    preserved byte-for-byte. Raises if an id in state_changes is not found.
    """
    # Build a mapping from isc_id to new state for quick lookup.
    remaining = dict(state_changes)  # will pop as we match

    def replacer(m: re.Match) -> str:
        # The full ISC line is prefix + state + suffix where suffix contains
        # the ISC id. We need to extract the id from the suffix.
        prefix = m.group(1)   # "- ["
        state = m.group(2)    # " " or "x" etc.
        suffix = m.group(3)   # "] ISC-N: text"
        # Extract ISC id from suffix
        id_m = re.search(r"ISC-[0-9][0-9.]*", suffix)
        if id_m is None:
            return m.group(0)
        isc_id = id_m.group(0)
        if isc_id in remaining:
            new_state = remaining.pop(isc_id)
            return prefix + new_state + suffix
        return m.group(0)

    result = _ISC_LINE_RE.sub(replacer, raw)
    return result


def _rewrite_progress(raw: str, new_n: int) -> str:
    """Rewrite the frontmatter progress N/M line with new_n, keeping M unchanged."""
    def replacer(m: re.Match) -> str:
        return m.group(1) + str(new_n) + m.group(3) + m.group(4)
    return _PROGRESS_LINE_RE.sub(replacer, raw, count=1)


def _rewrite_verification_section(raw: str, new_body: str) -> str:
    """Replace the ## Verification section body with new_body."""
    def replacer(m: re.Match) -> str:
        heading_line = m.group(1)  # "## Verification\n"
        return heading_line + new_body
    result = _VERIFICATION_SECTION_RE.sub(replacer, raw, count=1)
    return result


# ---------------------------------------------------------------------------
# Core reconcile function
# ---------------------------------------------------------------------------

def reconcile(
    master_path: Path,
    slice_paths: list[Path],
    dry_run: bool = False,
) -> int:
    """Merge slices into master by ISC ID.

    Returns:
        0 — success (or dry_run preview)
        1 — drift detected (a slice ISC id absent from master)
        2 — file not found
    """
    # --- Parse master ---
    master_raw = master_path.read_text(encoding="utf-8")
    master_isa = isa_common.parse_isa_text(master_raw, path=master_path)
    master_ids = {isc.id for isc in master_isa.iscs}
    master_state_by_id = {isc.id: isc.state for isc in master_isa.iscs}

    # --- Parse slices, detect drift ---
    drift: list[tuple[Path, str]] = []
    desired_states: dict[str, str] = {}  # isc_id → new state (last slice wins)
    slice_vblocks: dict[str, str] = {}   # isc_id → block text (last slice wins)

    for sp in slice_paths:
        slice_isa = isa_common.parse_isa_text(
            sp.read_text(encoding="utf-8"), path=sp
        )
        for isc in slice_isa.iscs:
            if isc.id not in master_ids:
                drift.append((sp, isc.id))
            else:
                desired_states[isc.id] = isc.state

        # Parse slice Verification blocks
        v_body = slice_isa.section("Verification") or ""
        _, s_blocks = _parse_verification_body(v_body)
        for isc_id, block_text in s_blocks.items():
            slice_vblocks[isc_id] = block_text

    if drift:
        for sp, isc_id in drift:
            print(
                f"DRIFT: slice '{sp}' contains {isc_id} which is absent from master",
                file=sys.stderr,
            )
        print("ABORT: master not modified.", file=sys.stderr)
        return 1

    # --- Compute state changes ---
    state_changes: dict[str, str] = {}
    for isc_id, new_state in desired_states.items():
        if master_state_by_id.get(isc_id) != new_state:
            state_changes[isc_id] = new_state

    # --- Merge Verification blocks ---
    master_v_body = master_isa.section("Verification") or ""
    master_preamble, master_vblocks = _parse_verification_body(master_v_body)
    merged_vblocks: dict[str, str] = dict(master_vblocks)  # start with master
    merged_vblocks.update(slice_vblocks)                    # slice wins on overlap

    new_v_body = _build_verification_body(master_preamble, merged_vblocks)

    # --- Compute new progress ---
    # Apply desired states to get final per-id state map
    final_states = dict(master_state_by_id)
    final_states.update(desired_states)
    new_checked = sum(1 for s in final_states.values() if s == "x")
    total_iscs = len(master_isa.iscs)
    prog = master_isa.progress_pair()
    old_n = prog[0] if prog else 0

    # --- Dry run ---
    if dry_run:
        print("=== isa-reconcile dry-run ===")
        print(f"Master: {master_path}")
        for sp in slice_paths:
            print(f"Slice:  {sp}")
        print()
        for isc in master_isa.iscs:
            new_state = desired_states.get(isc.id, isc.state)
            vblock_action = (
                "copied" if isc.id in slice_vblocks else "unchanged"
            )
            old_display = f"[{isc.state}]"
            new_display = f"[{new_state}]"
            changed = " (changed)" if new_state != isc.state else ""
            print(
                f"  {isc.id}: {old_display} → {new_display}{changed}  verification: {vblock_action}"
            )
        print(f"\nProgress: {old_n}/{total_iscs} → {new_checked}/{total_iscs}")
        print("(dry-run: master not modified)")
        return 0

    # --- Apply surgical edits ---
    new_raw = master_raw

    # 1. ISC checkbox states
    if state_changes:
        new_raw = _swap_isc_states(new_raw, state_changes)

    # 2. Verification section body
    # Always rewrite the section to ensure idempotence (ensures consistent format)
    new_raw = _rewrite_verification_section(new_raw, new_v_body)

    # 3. Progress
    new_raw = _rewrite_progress(new_raw, new_checked)

    master_path.write_text(new_raw, encoding="utf-8")

    changed_ids = list(state_changes.keys())
    print(
        f"isa-reconcile: merged {len(slice_paths)} slice(s) into {master_path.name}. "
        f"States changed: {changed_ids if changed_ids else 'none'}. "
        f"Progress: {new_checked}/{total_iscs}."
    )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="isa_reconcile",
        description="Deterministic ID-keyed merge of _ephemeral slices (ISA-SPEC §7).",
    )
    parser.add_argument("master", help="Path to the master ISA.md")
    parser.add_argument("slices", nargs="+", help="One or more slice ISA paths")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the merge plan without modifying any file",
    )
    args = parser.parse_args(argv)

    master_path = Path(args.master)
    if not master_path.exists():
        print(f"ERROR: master file not found: {master_path}", file=sys.stderr)
        return 2

    slice_paths: list[Path] = []
    for s in args.slices:
        sp = Path(s)
        if not sp.exists():
            print(f"ERROR: slice file not found: {sp}", file=sys.stderr)
            return 2
        slice_paths.append(sp)

    return reconcile(master_path, slice_paths, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

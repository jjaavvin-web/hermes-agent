"""Image-managed install refusal contract (#91277 Phase 3).

One shared admission gate for every surface that can start an in-place
``hermes update`` mutation (CLI apply, CLI --check, dashboard update
endpoint). The decision layers:

0. **Immutable deployment markers** (``.deployed-commit`` / ``.install_method``
   == ``immutable-deployment``, written by ``scripts/build_deployment.sh``):
   a `git archive` export of one verified commit, with no ``.git`` directory.
   This fork's fleet runs from these builds; upstream is absorbed as a merge
   project + GATE-B cutover, never an in-place update against a deployment
   (ledger row 135). Checked first, before any other admit branch.
1. **Baked provenance marker** (``/etc/hermes/image-provenance.json``,
   written by the image build — see :mod:`hermes_cli.image_provenance`):
   authoritative ground truth that this filesystem came from an immutable
   image. Fail-closed: a present-but-malformed marker still refuses.
2. **Filesystem heuristics** (``detect_install_method()``): the pre-existing
   docker/nix/apt detection, kept as the fallback for images built before
   the marker existed and for package-managed installs that have no image
   marker at all.

A refusal prints the real update command for the deployment kind, records a
``refused`` receipt (so fleet tooling sees "this install cannot self-update,
use <command>" instead of a silent non-update), and exits 2 on CLI surfaces.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpdateRefusal:
    """Why an in-place update is refused, and what to run instead."""

    code: str              # immutable-deployment | image-marker | image-marker-invalid | docker | nix | apt
    message: str           # full user-facing text (multi-line ok)
    update_command: str    # the one-line remediation command


def evaluate_update_admission(project_root: Path) -> Optional[UpdateRefusal]:
    """Return an :class:`UpdateRefusal` when in-place update must not run.

    ``None`` means the install is eligible for in-place update (git checkout
    or unknown-but-mutable). Never raises; on any internal error it falls
    back to the heuristic layer only.
    """
    # Layer 0: immutable deployment markers (ledger row 135) — fail closed
    # BEFORE any other admit branch. scripts/build_deployment.sh exports a
    # verified commit with `git archive` (no `.git`) and stamps a
    # `.deployed-commit` file at the deployment root, plus an
    # `.install_method` = "immutable-deployment" stamp. Neither the image-
    # provenance layer below nor the docker/nix/apt heuristics know about
    # this deployment shape, so without this layer an unknown marker falls
    # through as "legacy in-place-updatable" and gets admitted — exactly the
    # gap that would let `hermes update` / the dashboard Update button take
    # a live-state.db backup and reach a fleet restart against a build this
    # fork treats as read-only. Upstream changes are absorbed as merge
    # projects (candidate branch + build_deployment.sh + GATE-B cutover),
    # never applied in place here.
    try:
        deployed_commit = project_root / ".deployed-commit"
        git_marker = project_root / ".git"
        install_method_marker = project_root / ".install_method"
        immutable = deployed_commit.exists() and not git_marker.exists()
        if not immutable and install_method_marker.exists():
            try:
                stamped = install_method_marker.read_text(encoding="utf-8").strip().lower()
            except OSError:
                stamped = ""
            immutable = stamped == "immutable-deployment"
        if immutable:
            return UpdateRefusal(
                code="immutable-deployment",
                message=(
                    "✗ This is an immutable deployment (built by "
                    "scripts/build_deployment.sh): in-place update is "
                    "disabled; absorb upstream via a merge candidate + "
                    "GATE-B cutover."
                ),
                update_command=(
                    "absorb upstream via a merge candidate, then "
                    "scripts/build_deployment.sh <verified-head> --activate "
                    "(GATE-B cutover)"
                ),
            )
    except Exception as exc:
        logger.debug("Immutable-deployment admission check failed: %s", exc)

    # Layer 1: baked provenance marker — authoritative when present.
    try:
        from hermes_cli.image_provenance import read_image_provenance

        provenance = read_image_provenance()
        if provenance is not None:
            from hermes_cli.config import (
                format_docker_update_message,
                recommended_update_command_for_method,
            )

            if not provenance.valid:
                # Present but malformed: still image-managed — an integrity
                # defect is never permission to mutate the image in place.
                command = recommended_update_command_for_method("docker")
                return UpdateRefusal(
                    code="image-marker-invalid",
                    message=(
                        "✗ This install is image-managed, but its provenance "
                        f"marker is invalid ({provenance.error}).\n"
                        "  In-place update is disabled. Update by pulling a "
                        f"new image:\n    {command}"
                    ),
                    update_command=command,
                )
            manager = provenance.manager
            if manager == "docker":
                return UpdateRefusal(
                    code="image-marker",
                    message=format_docker_update_message(),
                    update_command=recommended_update_command_for_method("docker"),
                )
            command = recommended_update_command_for_method(manager)
            return UpdateRefusal(
                code="image-marker",
                message=command,
                update_command=command,
            )
    except Exception as exc:
        logger.debug("Image provenance check failed (using heuristics): %s", exc)

    # Layer 2: pre-existing filesystem heuristics, verbatim semantics.
    try:
        from hermes_cli.config import (
            detect_install_method,
            format_docker_update_message,
            is_nix_install_method,
            recommended_update_command_for_method,
        )

        method = detect_install_method(project_root)
        if method == "docker":
            return UpdateRefusal(
                code="docker",
                message=format_docker_update_message(),
                update_command=recommended_update_command_for_method("docker"),
            )
        if is_nix_install_method(method) or method == "apt":
            command = recommended_update_command_for_method(method)
            return UpdateRefusal(
                code=method if method == "apt" else "nix",
                message=command,
                update_command=command,
            )
    except Exception as exc:
        logger.debug("Install-method admission check failed: %s", exc)
    return None


def record_refusal_receipt(refusal: UpdateRefusal) -> None:
    """Write a minimal ``refused`` receipt for a blocked update attempt.

    Gives fleet tooling a durable record that an update was ATTEMPTED and
    refused ("not updatable in place, use <command>") instead of a silent
    nothing. Best-effort; never raises.
    """
    try:
        from hermes_cli.update_receipt import (
            begin_update_receipt,
            finalize_update_receipt,
            record_step,
        )

        begin_update_receipt()
        record_step(
            "admission",
            False,
            f"not updatable in place ({refusal.code}); use: {refusal.update_command}",
        )
        finalize_update_receipt("refused", stop_reason=refusal.code)
    except Exception as exc:
        logger.debug("Could not record refusal receipt: %s", exc)

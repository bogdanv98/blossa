# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Deterministic schema analysis — no LLM involved."""

from .deterministic import run_checks

__all__ = ["run_checks"]

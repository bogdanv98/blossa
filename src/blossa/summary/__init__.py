# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Builds the compact, PII-safe per-table summaries that are fed to the LLM."""

from .builder import build_summaries

__all__ = ["build_summaries"]

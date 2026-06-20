# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""The semantic pass: run the LLM provider over PII-safe summaries to infer meaning."""

from .runner import run_semantic_pass

__all__ = ["run_semantic_pass"]

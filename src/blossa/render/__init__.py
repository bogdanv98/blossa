# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Render a ScanReport to a human-readable Markdown map and a machine-readable JSON artifact."""

from .json_export import render_json, write_json
from .markdown import render_markdown, write_markdown

__all__ = ["render_markdown", "write_markdown", "render_json", "write_json"]

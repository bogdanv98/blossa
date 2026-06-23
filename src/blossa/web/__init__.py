# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Optional local web UI for Blossa (the `blossa serve` command).

Lives behind the `blossa[web]` extra so the core CLI stays dependency-light. The app reuses the
same deterministic pipeline and the same read-only / PII boundaries as the CLI.
"""

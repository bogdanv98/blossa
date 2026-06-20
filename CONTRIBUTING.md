# Contributing to Blossa

Thanks for your interest in improving Blossa! A few things to know before you open a pull request.

## License & CLA

- Blossa is licensed under **AGPL-3.0-only** (see [LICENSE](LICENSE)).
- By contributing, you agree to the **Contributor License Agreement** ([CLA.md](CLA.md)). This lets
  the project stay open source *and* keeps open the option of a future commercial license. You keep
  ownership of your work — the CLA is a license, not an assignment.

## How to accept the CLA (DCO sign-off)

We use the [Developer Certificate of Origin](https://developercertificate.org/). Sign off every
commit, which certifies you wrote the code and agree to the CLA:

```bash
git commit -s -m "Your message"
```

This appends a `Signed-off-by: Your Name <you@example.com>` line to the commit.

## Development setup

```bash
python -m pip install -e ".[dev]"
ruff check src tests    # lint
pytest                  # tests
```

## Ground rules

- Keep the **deterministic core** doing the heavy lifting; the LLM only ever sees PII-safe summaries.
- **Never** send raw row values to an LLM — only aggregates, value patterns, and masked samples.
- The database connection is and stays **read-only**.
- Do **not** develop or test against real/production data. Use the bundled synthetic schema
  (`blossa scan --demo` or the Docker setup under `docker/`).
- Add or update tests for behavioural changes; keep `ruff` and `pytest` green.

## Adding copyright headers

New source files should start with:

```python
# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only
```

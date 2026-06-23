# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Generate the SQL a DBA runs to create Blossa's read-only account.

Blossa never creates the account or grants itself — it only *emits* the SQL. A privileged DBA
reviews and runs it. Two profiles:

  * scoped (default) — READ on just the chosen schemas' tables/views. Oracle then limits both the
    data and the ALL_* catalog views to exactly those schemas. Safest; easiest for a DBA to approve.
  * full              — READ ANY TABLE + SELECT_CATALOG_ROLE: read the whole database, and answer
    catalog questions over the DBA_* views. Opt-in, for organisations that want the full picture.
"""

from __future__ import annotations

import re

_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")
_PASSWORD_PLACEHOLDER = "REPLACE_WITH_A_STRONG_PASSWORD"  # noqa: S105 - placeholder, not a secret


def _ident(name: str, kind: str) -> str:
    """Validate and upper-case an Oracle identifier (guards the generated SQL against injection)."""
    n = name.strip().upper()
    if not _IDENT.match(n):
        raise ValueError(f"Invalid {kind} name: {name!r} (letters, digits and _ only).")
    return n


def build_grants_sql(
    username: str = "BLOSSA_ASSISTANT",
    scope: str = "scoped",
    schemas: list[str] | None = None,
) -> str:
    """Return a ready-to-review SQL script that creates `username` with read-only access.

    scope="scoped" needs `schemas`; scope="full" ignores it.
    """
    user = _ident(username, "account")
    header = [
        "-- ---------------------------------------------------------------------------",
        "-- Blossa read-only account. Review, set a real password, then run as a DBA.",
        f"-- Profile: {scope}.  Blossa connects as this account and only ever reads.",
        "-- ---------------------------------------------------------------------------",
        "",
        f'CREATE USER {user} IDENTIFIED BY "{_PASSWORD_PLACEHOLDER}";',
        f"GRANT CREATE SESSION TO {user};",
        "",
    ]

    if scope == "full":
        body = [
            "-- Full profile: read every table, and the whole data dictionary (DBA_* views).",
            f"GRANT READ ANY TABLE TO {user};",
            f"GRANT SELECT_CATALOG_ROLE TO {user};",
        ]
        return "\n".join(header + body) + "\n"

    if scope != "scoped":
        raise ValueError(f"Unknown profile {scope!r}; expected 'scoped' or 'full'.")

    owners = [_ident(s, "schema") for s in (schemas or []) if s.strip()]
    if not owners:
        raise ValueError("The scoped profile needs at least one schema.")
    owner_list = ", ".join(f"'{o}'" for o in owners)
    body = [
        f"-- Scoped profile: READ on every table/view in {', '.join(owners)}.",
        "-- Oracle limits both the data and the ALL_* catalog views to exactly these schemas.",
        "-- (Re-run after new tables are added, or switch to the full profile.)",
        "BEGIN",
        "   FOR o IN (",
        "      SELECT owner, object_name FROM dba_objects",
        f"       WHERE owner IN ({owner_list})",
        "         AND object_type IN ('TABLE', 'VIEW')",
        "   ) LOOP",
        "      EXECUTE IMMEDIATE",
        f"         'GRANT READ ON \"' || o.owner || '\".\"' || o.object_name || '\" TO {user}';",
        "   END LOOP;",
        "END;",
        "/",
    ]
    return "\n".join(header + body) + "\n"

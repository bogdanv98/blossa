"""The DBA grant-script generator and the catalog_scope config flag."""

import pytest

from blossa.config import OracleConfig
from blossa.grants import build_grants_sql


def test_scoped_grants_read_on_listed_schemas():
    sql = build_grants_sql("BLOSSA_ASSISTANT", "scoped", ["HR", "Sales"])
    assert 'CREATE USER BLOSSA_ASSISTANT IDENTIFIED BY' in sql
    assert "GRANT CREATE SESSION TO BLOSSA_ASSISTANT;" in sql
    # Per-object READ grants, driven by a loop over the chosen owners.
    assert "GRANT READ ON" in sql
    assert "'HR'" in sql and "'SALES'" in sql  # upper-cased
    # Scoped must NOT hand out database-wide privileges.
    assert "READ ANY TABLE" not in sql
    assert "SELECT_CATALOG_ROLE" not in sql


def test_full_grants_read_any_table_and_catalog_role():
    sql = build_grants_sql("BLOSSA_ASSISTANT", "full")
    assert "GRANT READ ANY TABLE TO BLOSSA_ASSISTANT;" in sql
    assert "GRANT SELECT_CATALOG_ROLE TO BLOSSA_ASSISTANT;" in sql


def test_scoped_requires_at_least_one_schema():
    with pytest.raises(ValueError, match="at least one schema"):
        build_grants_sql("BLOSSA_ASSISTANT", "scoped", [])


def test_rejects_unknown_profile():
    with pytest.raises(ValueError, match="profile"):
        build_grants_sql("BLOSSA_ASSISTANT", "sideways", ["HR"])


@pytest.mark.parametrize("bad", ["robert'); DROP", "has space", "1leading", "semi;colon"])
def test_rejects_injection_in_identifiers(bad):
    with pytest.raises(ValueError):
        build_grants_sql(bad, "full")
    with pytest.raises(ValueError):
        build_grants_sql("BLOSSA_ASSISTANT", "scoped", [bad])


def test_catalog_scope_config_default_and_full():
    assert OracleConfig().catalog_scope == "scoped"
    assert OracleConfig().use_dba_catalog is False
    assert OracleConfig(catalog_scope="full").use_dba_catalog is True

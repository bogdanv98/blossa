# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Pydantic data model for Blossa.

The model is layered so each pipeline stage produces a well-typed artifact:

  introspection  -> SchemaInfo (TableInfo / ColumnInfo / ConstraintInfo / IndexInfo)
  profiling      -> ColumnProfile attached to columns
  checks         -> Finding list + inferred Relationship list
  summary        -> TableSummary (the PII-safe object fed to the LLM)
  semantic pass  -> TableSemantics / ColumnSemantics
  render         -> ScanReport (the whole thing, serialised to JSON / Markdown)

Hard rule: nothing that flows toward the LLM (TableSummary and below) may carry raw row
values. Only aggregates, value *patterns*, and *masked* samples are allowed.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- enums


class ConfidenceLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ConstraintType(StrEnum):
    PRIMARY_KEY = "P"
    FOREIGN_KEY = "R"
    UNIQUE = "U"
    CHECK = "C"


class KeyRole(StrEnum):
    NONE = "none"
    PRIMARY_KEY = "primary_key"
    FOREIGN_KEY = "foreign_key"
    PK_AND_FK = "pk_and_fk"
    UNIQUE = "unique"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    NOTICE = "notice"


class FindingKind(StrEnum):
    UNDECLARED_FK_CANDIDATE = "undeclared_fk_candidate"
    ORPHAN_ROWS = "orphan_rows"
    TYPE_INCONSISTENCY = "type_inconsistency"
    NAMING_INCONSISTENCY = "naming_inconsistency"
    MISSING_TABLE_COMMENT = "missing_table_comment"
    MISSING_COLUMN_COMMENT = "missing_column_comment"


# ----------------------------------------------------------------- introspection


class ColumnInfo(BaseModel):
    """A column as read from ALL_TAB_COLUMNS / ALL_COL_COMMENTS."""

    name: str
    column_id: int = 0
    data_type: str
    data_length: int | None = None
    data_precision: int | None = None
    data_scale: int | None = None
    nullable: bool = True
    data_default: str | None = None
    comment: str | None = None

    @property
    def type_signature(self) -> str:
        """Normalised type string, e.g. NUMBER(10,2), VARCHAR2(64), DATE."""
        t = self.data_type.upper()
        if t in {"NUMBER"} and (self.data_precision is not None or self.data_scale):
            p = self.data_precision if self.data_precision is not None else "*"
            s = self.data_scale or 0
            return f"NUMBER({p},{s})"
        if t in {"VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "RAW"} and self.data_length:
            return f"{t}({self.data_length})"
        return t


class ConstraintInfo(BaseModel):
    """A constraint from ALL_CONSTRAINTS / ALL_CONS_COLUMNS."""

    name: str
    type: ConstraintType
    columns: list[str] = Field(default_factory=list)
    # For foreign keys:
    referenced_table: str | None = None
    referenced_columns: list[str] = Field(default_factory=list)
    # For check constraints:
    search_condition: str | None = None
    status: str | None = None


class IndexInfo(BaseModel):
    """An index from ALL_INDEXES / ALL_IND_COLUMNS."""

    name: str
    unique: bool = False
    columns: list[str] = Field(default_factory=list)


class ColumnProfile(BaseModel):
    """PII-safe aggregate profile of a column's *data*. No raw values, ever."""

    total_rows: int = 0
    null_count: int = 0
    distinct_count: int = 0
    min_length: int | None = None
    max_length: int | None = None
    # Detected structural patterns, e.g. "9999-AA", "email", "ISO-date". Never literal values.
    value_patterns: list[str] = Field(default_factory=list)
    # Masked example values, e.g. "j***@***.com", "AB-####". Safe to show / send to an LLM.
    masked_samples: list[str] = Field(default_factory=list)
    # For low-cardinality code columns, the *number* of categories (not the categories themselves).
    is_low_cardinality: bool = False

    @property
    def null_fraction(self) -> float:
        return (self.null_count / self.total_rows) if self.total_rows else 0.0


class TableInfo(BaseModel):
    """A table with its columns, constraints and (optional) data profiles."""

    name: str
    comment: str | None = None
    num_rows: int | None = None  # optimizer stat from ALL_TABLES (may be stale / None)
    columns: list[ColumnInfo] = Field(default_factory=list)
    constraints: list[ConstraintInfo] = Field(default_factory=list)
    indexes: list[IndexInfo] = Field(default_factory=list)
    # Filled by the profiling step: column name -> profile.
    profiles: dict[str, ColumnProfile] = Field(default_factory=dict)

    def column(self, name: str) -> ColumnInfo | None:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def primary_key(self) -> ConstraintInfo | None:
        return next((c for c in self.constraints if c.type == ConstraintType.PRIMARY_KEY), None)

    @property
    def foreign_keys(self) -> list[ConstraintInfo]:
        return [c for c in self.constraints if c.type == ConstraintType.FOREIGN_KEY]


class SchemaInfo(BaseModel):
    """The whole introspected schema."""

    name: str
    tables: list[TableInfo] = Field(default_factory=list)

    def table(self, name: str) -> TableInfo | None:
        return next((t for t in self.tables if t.name == name), None)


# ------------------------------------------------------------------- analysis


class Relationship(BaseModel):
    """A relationship between two tables — either declared (FK) or inferred (candidate)."""

    from_table: str
    from_columns: list[str]
    to_table: str
    to_columns: list[str]
    declared: bool
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH
    evidence: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    """A deterministic observation about the schema (no LLM involved)."""

    kind: FindingKind
    severity: Severity = Severity.NOTICE
    table: str | None = None
    columns: list[str] = Field(default_factory=list)
    message: str
    details: dict[str, str | int | float] = Field(default_factory=dict)


# -------------------------------------------------------- PII-safe LLM summary


class ColumnSummary(BaseModel):
    """Compact, PII-safe description of one column, as sent to the LLM."""

    name: str
    type: str
    nullable: bool
    key_role: KeyRole = KeyRole.NONE
    comment: str | None = None
    references: str | None = None  # "OTHER_TABLE.OTHER_COL" if this column is an FK
    # Aggregate profile facts (all PII-safe):
    distinct_count: int | None = None
    null_fraction: float | None = None
    value_patterns: list[str] = Field(default_factory=list)
    masked_samples: list[str] = Field(default_factory=list)


class TableSummary(BaseModel):
    """The single object handed to the LLM per table. Contains no raw row values."""

    name: str
    comment: str | None = None
    row_count: int | None = None
    columns: list[ColumnSummary] = Field(default_factory=list)
    outbound: list[Relationship] = Field(default_factory=list)  # this table -> others
    inbound: list[Relationship] = Field(default_factory=list)  # others -> this table


# ----------------------------------------------------------------- semantics


class ColumnSemantics(BaseModel):
    column: str
    meaning: str
    confidence: ConfidenceLevel
    evidence: list[str] = Field(default_factory=list)


class TableSemantics(BaseModel):
    table: str
    purpose: str
    confidence: ConfidenceLevel
    evidence: list[str] = Field(default_factory=list)
    columns: list[ColumnSemantics] = Field(default_factory=list)


# -------------------------------------------------------------- final report


class ScanMetadata(BaseModel):
    blossa_version: str
    schema_name: str
    generated_at: datetime
    llm_provider: str
    llm_model: str | None = None
    profiling_enabled: bool = True
    table_count: int = 0


class ScanReport(BaseModel):
    """The complete result of `blossa scan` — serialised to JSON and rendered to Markdown."""

    metadata: ScanMetadata
    schema_info: SchemaInfo
    relationships: list[Relationship] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    semantics: list[TableSemantics] = Field(default_factory=list)

    def semantics_for(self, table: str) -> TableSemantics | None:
        return next((s for s in self.semantics if s.table == table), None)

    def findings_for(self, table: str) -> list[Finding]:
        return [f for f in self.findings if f.table == table]

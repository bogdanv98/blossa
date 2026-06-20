# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Offline demo fixture — the synthetic BLOSSA_DEMO schema as a fully-built SchemaInfo.

Lets `blossa scan --demo` exercise the whole pipeline (checks → summaries → semantic pass →
render) with no Oracle and no GPU. It mirrors docker/init/01_create_schema.sql, including
pre-computed PII-safe profiles. Data-overlap / orphan checks need a live DB, so on the demo
path undeclared FKs are inferred by name only (clearly flagged low confidence).
"""

from __future__ import annotations

from .models import (
    ColumnInfo,
    ColumnProfile,
    ConstraintInfo,
    ConstraintType,
    IndexInfo,
    SchemaInfo,
    TableInfo,
)


def _col(name, dtype, length=None, prec=None, scale=None, nullable=True, comment=None):
    return ColumnInfo(
        name=name,
        data_type=dtype,
        data_length=length,
        data_precision=prec,
        data_scale=scale,
        nullable=nullable,
        comment=comment,
    )


def _pk(name, *cols):
    return ConstraintInfo(name=name, type=ConstraintType.PRIMARY_KEY, columns=list(cols))


def _fk(name, cols, ref_table, ref_cols):
    return ConstraintInfo(
        name=name,
        type=ConstraintType.FOREIGN_KEY,
        columns=list(cols),
        referenced_table=ref_table,
        referenced_columns=list(ref_cols),
    )


def build_demo_schema() -> SchemaInfo:
    status_ref = TableInfo(
        name="STATUS_REF",
        num_rows=4,
        columns=[
            _col("STATUS_CD", "NUMBER", prec=2, nullable=False),
            _col("DESCR", "VARCHAR2", length=40, nullable=False),
        ],
        constraints=[_pk("PK_STATUS_REF", "STATUS_CD")],
        profiles={
            "STATUS_CD": ColumnProfile(
                total_rows=4, null_count=0, distinct_count=4, is_low_cardinality=True,
                value_patterns=["9"], masked_samples=["*"],
            ),
            "DESCR": ColumnProfile(
                total_rows=4, null_count=0, distinct_count=4, is_low_cardinality=True,
                min_length=3, max_length=9, value_patterns=["A{3}", "A{6}"],
                masked_samples=["N**", "S*****D"],
            ),
        },
    )

    customers = TableInfo(
        name="CUSTOMERS",
        comment="Master record of customers placing orders.",
        num_rows=4,
        columns=[
            _col("CUST_ID", "NUMBER", prec=10, nullable=False,
                 comment="Surrogate primary key for a customer."),
            _col("NAME", "VARCHAR2", length=120, nullable=False),
            _col("EMAIL", "VARCHAR2", length=120, comment="Primary contact email address."),
            _col("PHONE", "VARCHAR2", length=30),
            _col("CREATED_DT", "DATE", nullable=False),
            _col("STATUS_CD", "NUMBER", prec=2, nullable=False),
        ],
        constraints=[_pk("PK_CUSTOMERS", "CUST_ID")],
        profiles={
            "CUST_ID": ColumnProfile(total_rows=4, distinct_count=4, value_patterns=["9{4}"],
                                     masked_samples=["1**1", "1**2"]),
            "NAME": ColumnProfile(total_rows=4, distinct_count=4, min_length=14, max_length=19,
                                  value_patterns=["A{4} A{7} A{3}"],
                                  masked_samples=["A*************L", "C************u"]),
            "EMAIL": ColumnProfile(total_rows=4, null_count=0, distinct_count=4,
                                   value_patterns=["email"],
                                   masked_samples=["o***@a***.example", "c***@m***.example"]),
            "PHONE": ColumnProfile(total_rows=4, null_count=1, distinct_count=3,
                                   value_patterns=["+99 99 999 9999", "+99 999 999 999"],
                                   masked_samples=["+*************1", "+************3"]),
            "CREATED_DT": ColumnProfile(total_rows=4, distinct_count=1, value_patterns=["date"]),
            "STATUS_CD": ColumnProfile(total_rows=4, distinct_count=2, is_low_cardinality=True,
                                       value_patterns=["9"], masked_samples=["*"]),
        },
    )

    products = TableInfo(
        name="PRODUCTS",
        num_rows=4,
        columns=[
            _col("PROD_ID", "NUMBER", prec=10, nullable=False),
            _col("SKU", "VARCHAR2", length=20, nullable=False),
            _col("DESCR", "VARCHAR2", length=200),
            _col("PRICE", "NUMBER", prec=10, scale=2),
            _col("CAT_CD", "VARCHAR2", length=4),
        ],
        constraints=[_pk("PK_PRODUCTS", "PROD_ID"),
                     ConstraintInfo(name="UQ_PRODUCTS_SKU", type=ConstraintType.UNIQUE,
                                    columns=["SKU"])],
        profiles={
            "PROD_ID": ColumnProfile(total_rows=4, distinct_count=4, value_patterns=["9{2}"],
                                     masked_samples=["*"]),
            "SKU": ColumnProfile(total_rows=4, distinct_count=4, min_length=11, max_length=11,
                                 value_patterns=["A{3}-A{3}-9{3}"],
                                 masked_samples=["S*********1", "S*********2"]),
            "DESCR": ColumnProfile(total_rows=4, distinct_count=4,
                                   value_patterns=["A{6}, A{8}", "A{5}, A{4}"],
                                   masked_samples=["W*************d", "G********e"]),
            "PRICE": ColumnProfile(total_rows=4, distinct_count=3, value_patterns=["9.99", "99.99"],
                                   masked_samples=["1***9", "3***9"]),
            "CAT_CD": ColumnProfile(total_rows=4, distinct_count=2, is_low_cardinality=True,
                                    value_patterns=["A{3}"], masked_samples=["W*D", "G*Z"]),
        },
    )

    orders = TableInfo(
        name="ORDERS",
        num_rows=3,
        columns=[
            _col("ORDER_ID", "NUMBER", prec=12, nullable=False),
            _col("CUST_ID", "NUMBER", prec=10, nullable=False),
            _col("ORDER_DATE", "DATE", nullable=False),
            _col("TOTAL_AMT", "NUMBER", prec=12, scale=2,
                 comment="Order gross total in account currency."),
            _col("STATUS_CD", "NUMBER", prec=2, nullable=False),
        ],
        constraints=[_pk("PK_ORDERS", "ORDER_ID"),
                     _fk("FK_ORDERS_CUST", ["CUST_ID"], "CUSTOMERS", ["CUST_ID"])],
        profiles={
            "ORDER_ID": ColumnProfile(total_rows=3, distinct_count=3, value_patterns=["9{6}"],
                                      masked_samples=["9****1"]),
            "CUST_ID": ColumnProfile(total_rows=3, distinct_count=3, value_patterns=["9{4}"],
                                     masked_samples=["1**1"]),
            "ORDER_DATE": ColumnProfile(total_rows=3, distinct_count=3, value_patterns=["date"]),
            "TOTAL_AMT": ColumnProfile(total_rows=3, distinct_count=3,
                                       value_patterns=["99.99", "99.99"], masked_samples=["5***8"]),
            "STATUS_CD": ColumnProfile(total_rows=3, distinct_count=3, is_low_cardinality=True,
                                       value_patterns=["9"], masked_samples=["*"]),
        },
    )

    order_items = TableInfo(
        name="ORDER_ITEMS",
        num_rows=5,
        columns=[
            _col("ITEM_ID", "NUMBER", prec=12, nullable=False),
            _col("ORDER_ID", "NUMBER", prec=12, nullable=False),
            _col("PROD_ID", "NUMBER", prec=10, nullable=False),
            _col("QTY", "NUMBER", prec=6, nullable=False),
            _col("UNIT_PRICE", "NUMBER", prec=10, scale=2),
        ],
        constraints=[_pk("PK_ORDER_ITEMS", "ITEM_ID"),
                     _fk("FK_ITEMS_ORDER", ["ORDER_ID"], "ORDERS", ["ORDER_ID"])],
        profiles={
            "ITEM_ID": ColumnProfile(total_rows=5, distinct_count=5, value_patterns=["9"]),
            "ORDER_ID": ColumnProfile(total_rows=5, distinct_count=3, value_patterns=["9{6}"]),
            "PROD_ID": ColumnProfile(total_rows=5, distinct_count=4, value_patterns=["9{2}"]),
            "QTY": ColumnProfile(total_rows=5, distinct_count=2, is_low_cardinality=True,
                                 value_patterns=["9"]),
            "UNIT_PRICE": ColumnProfile(total_rows=5, distinct_count=4, value_patterns=["99.99"]),
        },
        indexes=[IndexInfo(name="IX_ITEMS_PROD", columns=["PROD_ID"])],
    )

    cust_notes = TableInfo(
        name="CUST_NOTES",
        num_rows=2,
        columns=[
            _col("NOTE_ID", "NUMBER", prec=12, nullable=False),
            _col("CUST_ID", "VARCHAR2", length=20, nullable=False),  # type inconsistency vs others
            _col("NOTE_TXT", "VARCHAR2", length=400),
        ],
        constraints=[_pk("PK_CUST_NOTES", "NOTE_ID")],
        profiles={
            "NOTE_ID": ColumnProfile(total_rows=2, distinct_count=2, value_patterns=["9"]),
            "CUST_ID": ColumnProfile(total_rows=2, distinct_count=2, value_patterns=["9{4}"],
                                     masked_samples=["1**1", "1**3"]),
            "NOTE_TXT": ColumnProfile(total_rows=2, distinct_count=2,
                                      value_patterns=["A{3}-99 A{5} A{6}."],
                                      masked_samples=["N*****************d"]),
        },
    )

    return SchemaInfo(
        name="BLOSSA_DEMO",
        tables=[status_ref, customers, products, orders, order_items, cust_notes],
    )

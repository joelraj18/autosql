import json

import pandas as pd
import pytest

from autosql import infer_schema, link_roles, profile_catalog, to_dot, to_mermaid
from autosql.loading import snake_case
from conftest import NORTHWIND, catalog_from


@pytest.fixture(scope="module")
def nw_schema(northwind_catalog):
    profile = profile_catalog(northwind_catalog)
    return profile, infer_schema(northwind_catalog, profile)


def test_northwind_relationships_match_the_source_constraints(nw_schema):
    profile, schema = nw_schema
    accepted = {r.key for r in schema.accepted}
    assert len(accepted) == 7
    assert {r.key for r in schema.suggested} == {"orders.ship_via → shippers.shipper_id"}
    assert next(r for r in schema.accepted if r.child_col == "reports_to").kind == "self"
    expected = {f"{snake_case(f['child_table'])}.{snake_case(f['child_col'])} → {snake_case(f['parent_table'])}.{snake_case(f['parent_col'])}"
                for f in json.loads((NORTHWIND / "expected_fks.json").read_text())}
    ticked = schema.with_accepted(["orders.ship_via → shippers.shipper_id"])
    assert {r.key for r in ticked.accepted} == expected  # all 8 FOREIGN KEY constraints of the original script


def test_foreign_key_columns_become_identifiers(nw_schema):
    profile, schema = nw_schema
    assert profile.col("orders", "ship_via").role == "measure"                # before the schema: just a small integer
    linked = link_roles(profile, schema)
    for table, col, parent in (("orders", "ship_via", "shippers.shipper_id"), ("employees", "reports_to", "employees.employee_id")):
        c = linked.col(table, col)
        assert (c.role, c.kind, c.reason) == ("identifier", "", f"links to {parent}")
    assert linked.col("orders", "freight").role == "measure"                  # real measures are untouched


def test_cardinality_and_fanout(nw_schema):
    _, schema = nw_schema
    lines = next(r for r in schema.accepted if r.key == "order_details.order_id → orders.order_id")
    assert lines.cardinality == "1:N" and lines.max_children == 25 and abs(lines.avg_children - 2.596) < 0.01
    customers = next(r for r in schema.accepted if r.parent_table == "customers")
    assert customers.childless_parents == 2  # FISSA, PARIS
    freight = schema.fanouts[0]
    assert (freight.parent_table, freight.column) == ("orders", "freight")
    assert round(freight.total, 2) == 64942.69 and round(freight.joined_total, 2) == 207306.10
    assert round(freight.factor, 2) == 3.19


def test_low_containment_and_type_mismatch():
    parents = pd.DataFrame({"customer_id": [str(i) for i in range(1, 41)], "name": [f"n{i}" for i in range(40)]})
    children = pd.DataFrame({"id": range(100), "customer_id": [i % 80 + 1 for i in range(100)]})
    catalog = catalog_from({"customers": parents, "orders": children})
    schema = infer_schema(catalog, profile_catalog(catalog))
    rel = schema.relationships[0]
    assert not rel.accepted and rel.containment == pytest.approx(0.5)  # half the codes don't exist: only suggested
    assert rel.type_mismatch is False  # both load as integers


def test_diagrams(nw_schema):
    profile, schema = nw_schema
    dot = to_dot(profile, schema)
    assert dot.startswith("digraph") and '"order_details" -> "orders"' in dot
    mermaid = to_mermaid(profile, schema)
    assert "ORDERS ||--o{ ORDER_DETAILS" in mermaid


def test_composite_keys_link_on_both_columns():
    from autosql.dialect import Dialect
    from test_codegen_runs import composite_key_frames, composite_key_with_orphans
    catalog = catalog_from(composite_key_frames())
    profile = profile_catalog(catalog)
    schema = infer_schema(catalog, profile)
    r = schema.accepted[0]
    assert (r.key, r.composite, r.containment, r.cardinality) == ("sales.(region, store_no) → stores.(region, store_no)", True, 1.0, "1:N")
    assert r.childless_parents == 1 and r.on(Dialect("postgres"), "s", "st") == "st.region = s.region AND st.store_no = s.store_no"
    with pytest.raises(ValueError, match="use child_cols"):
        r.child_col                                       # no code may join a composite link on one column by mistake
    assert {(f.parent_table, f.column, f.via) for f in schema.fanouts} == {("stores", "monthly_rent", ("region", "store_no"))}
    assert '"sales" -> "stores" [label="region, store_no (N:1)"]' in to_dot(profile, schema)
    assert 'STORES ||--o{ SALES : "region, store_no"' in to_mermaid(profile, schema)
    linked = link_roles(profile, schema)
    assert linked.col("sales", "store_no").role == "identifier"          # a store number is not a measure to add up
    loose = catalog_from(composite_key_with_orphans())
    weak = infer_schema(loose, profile_catalog(loose))
    assert weak.accepted == [] and weak.suggested[0].containment == 0.75 and "only 75% of its pairs" in weak.suggested[0].reason

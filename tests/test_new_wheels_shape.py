"""A New Wheels-shaped table (synthetic) must produce the same query shape as the hand-written MySQL 8 analysis."""
from autosql import build_cells, run_cells
from conftest import analyse


def test_generated_sql_matches_the_hand_written_query(new_wheels_catalog):
    d = analyse(new_wheels_catalog)
    cells = {c.id: c for c in build_cells(d)}
    view_mysql = cells["base"].statements[0].sql["mysql"]
    assert "DATEDIFF(ship_date, order_date) AS ship_days" in view_mysql
    assert "QUARTER(order_date) AS quarter_number" in view_mysql and "YEAR(order_date) AS order_year" in view_mysql
    assert "WHEN 'Very Bad' THEN 1" in view_mysql and "WHEN 'Very Good' THEN 5" in view_mysql
    assert "ship_date - order_date AS ship_days" in cells["base"].statements[0].sql["postgres"]
    per_quarter = cells["kpi_by_period"].statements[0].sql["mysql"]
    for piece in ("WITH quarterly AS", "with_prev AS", "order_year * 4 + quarter_number AS period_index",
                  "THEN LAG(p.revenue) OVER (ORDER BY p.period_index)",  # LAG, kept only for the calendar neighbour
                  "ROUND(100.0 * (revenue - prev_revenue) / NULLIF(prev_revenue, 0), 1) AS revenue_qoq_pct",
                  "AVG(ship_days) AS avg_ship_days", "AVG(rating_score) AS avg_rating", "COUNT(*) AS orders"):
        assert piece in per_quarter, piece


def test_the_planted_slowdown_is_found(new_wheels_catalog):
    d = analyse(new_wheels_catalog)
    results, m = run_cells(build_cells(d), new_wheels_catalog)
    assert not [r.error for r in results if r.error]
    assert m["qoq_last_complete"]["pct"] < 0                       # revenue falls ...
    assert m["driver_vs_symptom"]["driver"] == "avg_ship_days"
    assert m["driver_vs_symptom"]["driver_change_pct"] > 10          # ... while shipping time rises
    notes = " ".join(n for r in results if r.cell_id == "drilldown" for n in r.notes)
    assert "controlled test" in notes
    assert m["driver_split"]["volume"] < 0                          # fewer orders explain most of the fall
    tree = " ".join(n for r in results if r.cell_id == "driver_tree" for n in r.notes)
    assert "S07** moved most (+382,476.51, against the overall fall of -1,707,042.10)" in tree   # it grew while the total fell
    assert "-22% of the change" not in tree                          # a negative share of a change reads as nonsense

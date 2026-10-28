from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from datawright.config import FIXTURES_DIR
from datawright.connectors.powerbi import tmdl
from datawright.core.paths import make_path, split_path

DEF = FIXTURES_DIR / "pbip" / "retail-issues" / "Retail.SemanticModel" / "definition"
CLEAN_DEF = FIXTURES_DIR / "pbip" / "retail-clean" / "Retail.SemanticModel" / "definition"
SALES = DEF / "tables" / "Sales.tmdl"
GOLDEN = Path(__file__).parent / "golden"
BOM = chr(0xFEFF)


def sales_text() -> str:
    return tmdl.read_text(SALES)


def test_parses_tables_measures_and_columns() -> None:
    model = tmdl.load_model("Retail", DEF)
    assert model.source == "pbip"
    assert set(model.tables) == {"Sales", "Product", "Customer", "Calendar", "Targets"}
    sales = model.tables["Sales"]
    assert [m.name for m in sales.measures] == [
        "Total Sales", "Total Cost", "Profit", "Margin %", "Order Lines", "Sales YoY %",
        "Avg Order Value", "Units ", "Revenue", "Gross Sales",
    ]  # fmt: skip
    assert [c.name for c in sales.columns] == [
        "OrderID", "OrderDate", "ShipDate", "ProductKey", "CustomerKey", "Quantity",
        "UnitPrice", "Amount", "Cost", "LoadBatch",
    ]  # fmt: skip
    order_lines, total_sales = sales.measure("Order Lines"), sales.measure("Total Sales")
    assert order_lines is not None and order_lines.description == "Number of sales order lines."
    assert total_sales is not None and total_sales.expression == "SUM(Sales[Amount])"
    aov = sales.measure("Avg Order Value")
    assert aov is not None and aov.format_string is None
    unit_price = sales.column("UnitPrice")
    assert unit_price is not None and unit_price.data_type == "double"
    assert unit_price.summarize_by == "sum" and unit_price.kind == "data"
    load_batch = sales.column("LoadBatch")
    assert load_batch is not None and load_batch.is_hidden
    assert model.tables["Product"].columns[1].name == "Product Name"


def test_clean_model_properties() -> None:
    model = tmdl.load_model("Retail", CLEAN_DEF)
    cal = model.tables["Calendar"]
    assert cal.data_category == "Time" and cal.is_calculated
    date, month = cal.column("Date"), cal.column("Month")
    assert date is not None and date.is_key and date.kind == "calculatedTableColumn"
    assert month is not None and month.sort_by_column == "MonthNumber"
    city = model.tables["Customer"].column("City")
    assert city is not None and city.data_category == "City"
    assert model.annotations["__PBI_TimeIntelligenceEnabled"] == "0"
    assert model.expressions["DataFolder"].startswith('"C:')
    sales_partition = model.tables["Sales"].partitions[0]
    assert sales_partition.kind == "m" and sales_partition.mode == "import"
    assert "Table.TransformColumnTypes" in (sales_partition.source or "")


def test_multiline_and_fenced_expressions() -> None:
    sales = tmdl.load_model("Retail", DEF).tables["Sales"]
    yoy, aov = sales.measure("Sales YoY %"), sales.measure("Avg Order Value")
    assert yoy is not None and aov is not None
    lines = yoy.expression.splitlines()
    assert lines[0] == "VAR CurrentSales = [Total Sales]"
    assert lines[2] == "RETURN"
    assert lines[3].startswith("\tIFERROR(")
    assert aov.expression == "DIVIDE([Total Sales], DISTINCTCOUNT(Sales[OrderID]))"


def test_nested_objects_and_properties_stay_separate() -> None:
    doc = tmdl.parse(sales_text())
    order_id = doc.find("column", "OrderID", ("table", "Sales"))
    assert order_id is not None
    assert "annotation" not in order_id.properties
    partition = doc.find("partition", "Sales", ("table", "Sales"))
    assert partition is not None and partition.expression == "m"
    assert "Csv.Document" in partition.properties["source"]
    cal = tmdl.parse(tmdl.read_text(DEF / "tables" / "Calendar.tmdl"))
    part = cal.find("partition", "Calendar", ("table", "Calendar"))
    assert part is not None
    assert part.properties["source"].startswith("ADDCOLUMNS(")


def test_relationships() -> None:
    rels = tmdl.load_model("Retail", DEF).relationships
    assert len(rels) == 4
    both = [r for r in rels if r.cross_filtering == "bothDirections"]
    assert [(r.from_table, r.from_column, r.to_table, r.to_column) for r in both] == [
        ("Sales", "CustomerKey", "Customer", "CustomerKey")
    ]
    inactive = [r for r in rels if not r.is_active]
    assert [r.label for r in inactive] == ["Sales[ShipDate] -> Calendar[Date]"]
    assert all(r.from_cardinality == "many" and r.to_cardinality == "one" for r in rels)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("Sales.ProductKey", ("Sales", "ProductKey")),
        ("'Sales Table'.'Product Key'", ("Sales Table", "Product Key")),
        ("Sales.'Unit Price'", ("Sales", "Unit Price")),
        ("'It''s'.Col", ("It's", "Col")),
    ],
)
def test_column_refs(ref: str, expected: tuple[str, str]) -> None:
    assert tmdl.parse_column_ref(ref) == expected


@pytest.mark.parametrize(
    ("kind", "table", "name", "description", "golden"),
    [
        ("measure", "Sales", "Total Sales", "Total sales amount, before returns.",
         "Sales.total-sales-described.tmdl"),
        ("measure", "Sales", "Order Lines", "Count of order lines.\nOne row per product per order.",
         "Sales.order-lines-replaced.tmdl"),
        ("measure", "Sales", "Order Lines", None, "Sales.order-lines-removed.tmdl"),
        ("table", None, "Sales", "One row per order line.", "Sales.table-described.tmdl"),
        ("column", "Sales", "UnitPrice", "Price per unit.", "Sales.column-described.tmdl"),
    ],
)  # fmt: skip
def test_set_description_matches_golden_bytes(
    kind: str, table: str | None, name: str, description: str | None, golden: str
) -> None:
    out = tmdl.set_description(sales_text(), kind, name, description, table=table)
    assert out.encode("utf-8") == (GOLDEN / golden).read_bytes()


def test_unchanged_description_is_byte_identical() -> None:
    text = sales_text()
    out = tmdl.set_description(
        text, "measure", "Order Lines", "Number of sales order lines.", table="Sales"
    )
    assert out == text


def test_crlf_and_bom_are_preserved() -> None:
    crlf = BOM + sales_text().replace("\n", "\r\n")
    out = tmdl.set_description(
        crlf, "measure", "Total Sales", "Total sales amount, before returns.", table="Sales"
    )
    golden = (GOLDEN / "Sales.total-sales-described.tmdl").read_text(encoding="utf-8")
    assert out == BOM + golden.replace("\n", "\r\n")
    assert "\n" not in out.replace("\r\n", "")


def test_edits_on_a_file_power_bi_desktop_saved() -> None:
    """golden/desktop-saved/Sales.tmdl is a sample as Desktop 2.159 rewrote it (CRLF, its own
    property order, unfenced single-line measures). Each edit adds or replaces exactly one line."""
    path = GOLDEN / "desktop-saved" / "Sales.tmdl"
    before = tmdl.read_text(path)
    assert before.encode("utf-8") == path.read_bytes()
    intents = [
        {"op": "tmdl.set_description", "kind": "column", "name": "OrderDate", "table": "Sales",
         "value": "Date the order was placed."},
        {"op": "tmdl.set_property", "kind": "column", "name": "OrderID", "table": "Sales",
         "key": "isHidden", "value": True},
        {"op": "tmdl.set_expression", "kind": "measure", "name": "Avg Order Value",
         "table": "Sales", "value": "DIVIDE([Total Sales], DISTINCTCOUNT(Sales[OrderID]), 0)"},
    ]  # fmt: skip
    apply = {"tmdl.set_description": tmdl.apply_set_description,
             "tmdl.set_property": tmdl.apply_set_property,
             "tmdl.set_expression": tmdl.apply_set_expression}  # fmt: skip
    for intent in intents:
        after = apply[intent["op"]](before, intent)
        assert tmdl.check_edits(before, after, [intent]) == []
        assert "\n" not in after.replace("\r\n", "")
        diff = difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True), n=0
        )
        changed = [d[0] for d in diff if d[:1] in "+-" and not d.startswith(("+++", "---"))]
        assert changed in (["+"], ["-", "+"]), intent


def test_quoted_names_with_apostrophes() -> None:
    text = "table Sales\n\tmeasure 'Customer''s Count' = 1\n\t\tformatString: 0\n"
    doc = tmdl.parse(text)
    assert doc.find("measure", "Customer's Count", ("table", "Sales")) is not None
    out = tmdl.set_description(text, "measure", "Customer's Count", "Customers.", table="Sales")
    expected = (
        "table Sales\n\t/// Customers.\n\tmeasure 'Customer''s Count' = 1\n\t\tformatString: 0\n"
    )
    assert out == expected


def test_missing_object_raises() -> None:
    with pytest.raises(tmdl.TmdlError):
        tmdl.set_description(sales_text(), "measure", "Nope", "x", table="Sales")
    with pytest.raises(tmdl.TmdlError):
        tmdl.set_description(sales_text(), "measure", "Total Sales", "x")  # table missing


def test_check_catches_edits_outside_the_target() -> None:
    before = sales_text()
    intent = {"kind": "measure", "table": "Sales", "name": "Total Sales", "value": "Total sales."}
    after = tmdl.apply_set_description(before, intent)
    assert tmdl.check_set_description(before, after, [intent]) == []
    sneaky = after.replace("formatString: 0.0%", "formatString: 0%", 1)
    assert tmdl.check_set_description(before, sneaky, [intent])
    wrong_text = after.replace("Total sales.", "Something else.")
    assert tmdl.check_set_description(before, wrong_text, [intent])


def test_description_cannot_contain_fence() -> None:
    with pytest.raises(tmdl.TmdlError):
        tmdl.set_description(sales_text(), "measure", "Total Sales", "```", table="Sales")


def test_object_paths_round_trip() -> None:
    path = make_path("Retail", "P/L", "measure", "a\\b / c")
    assert split_path(path) == ["Retail", "P/L", "measure", "a\\b / c"]
    assert make_path("Retail", "Sales", "measure", "Margin %") == "Retail/Sales/measure/Margin %"

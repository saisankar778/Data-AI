from __future__ import annotations

from pathlib import Path

import pytest

from datawright.config import FIXTURES_DIR
from datawright.connectors.powerbi import tmdl
from datawright.core.paths import make_path, split_path

DEF = FIXTURES_DIR / "pbip" / "retail-issues" / "Retail.SemanticModel" / "definition"
SALES = DEF / "tables" / "Sales.tmdl"
GOLDEN = Path(__file__).parent / "golden"


def sales_text() -> str:
    return tmdl.read_text(SALES)


def test_parses_tables_measures_and_columns() -> None:
    model = tmdl.load_model("Retail", DEF)
    assert set(model.tables) == {"Sales", "Product", "Customer", "Calendar"}
    sales = model.tables["Sales"]
    assert [m.name for m in sales.measures] == [
        "Total Sales",
        "Total Cost",
        "Profit",
        "Margin %",
        "Order Lines",
        "Sales YoY %",
        "Avg Order Value",
    ]
    assert [c.name for c in sales.columns] == [
        "OrderID",
        "OrderDate",
        "ProductKey",
        "CustomerKey",
        "Quantity",
        "UnitPrice",
        "Amount",
        "Cost",
    ]
    assert sales.measure("Order Lines").description == "Number of sales order lines."
    assert sales.measure("Total Sales").description is None
    assert sales.measure("Total Sales").expression == "SUM(Sales[Amount])"
    assert sales.measure("Sales YoY %").display_folder == "Growth"
    unit_price = next(c for c in sales.columns if c.name == "UnitPrice")
    assert unit_price.data_type == "double"
    assert model.tables["Product"].columns[1].name == "Product Name"


def test_multiline_and_fenced_expressions() -> None:
    sales = tmdl.load_model("Retail", DEF).tables["Sales"]
    yoy = sales.measure("Sales YoY %").expression.splitlines()
    assert yoy[0] == "VAR CurrentSales = [Total Sales]"
    assert yoy[2] == "RETURN"
    assert yoy[3].startswith("\tIFERROR(")
    assert sales.measure("Avg Order Value").expression == (
        "DIVIDE([Total Sales], DISTINCTCOUNT(Sales[OrderID]))"
    )


def test_nested_objects_and_properties_stay_separate() -> None:
    doc = tmdl.parse(sales_text())
    order_id = doc.find("column", "OrderID", ("table", "Sales"))
    assert order_id is not None
    assert [c.kind for c in order_id.children] == ["annotation"]
    assert "annotation" not in order_id.properties
    partition = doc.find("partition", "Sales", ("table", "Sales"))
    assert partition is not None and partition.expression == "m"
    assert "Csv.Document" in partition.properties["source"]
    cal = tmdl.parse(tmdl.read_text(DEF / "tables" / "Calendar.tmdl"))
    source = cal.find("partition", "Calendar", ("table", "Calendar")).properties["source"]  # type: ignore[union-attr]
    assert source.startswith("ADDCOLUMNS(") and source.endswith(")")


def test_relationships() -> None:
    rels = tmdl.load_model("Retail", DEF).relationships
    assert len(rels) == 3
    both = [r for r in rels if r.cross_filtering == "bothDirections"]
    assert [(r.from_column, r.to_column) for r in both] == [
        ("Sales.CustomerKey", "Customer.CustomerKey")
    ]


@pytest.mark.parametrize(
    ("name", "description", "golden"),
    [
        ("Total Sales", "Total sales amount, before returns.", "Sales.total-sales-described.tmdl"),
        (
            "Order Lines",
            "Count of order lines.\nOne row per product per order.",
            "Sales.order-lines-replaced.tmdl",
        ),
        ("Order Lines", None, "Sales.order-lines-removed.tmdl"),
    ],
)
def test_set_description_matches_golden_bytes(
    name: str, description: str | None, golden: str
) -> None:
    out = tmdl.set_description(sales_text(), "measure", name, description, table="Sales")
    assert out.encode("utf-8") == (GOLDEN / golden).read_bytes()


def test_unchanged_description_is_byte_identical() -> None:
    text = sales_text()
    assert (
        tmdl.set_description(
            text, "measure", "Order Lines", "Number of sales order lines.", table="Sales"
        )
        == text
    )


def test_crlf_and_bom_are_preserved() -> None:
    crlf = "\ufeff" + sales_text().replace("\n", "\r\n")
    out = tmdl.set_description(
        crlf, "measure", "Total Sales", "Total sales amount, before returns.", table="Sales"
    )
    expected = "\ufeff" + (GOLDEN / "Sales.total-sales-described.tmdl").read_text(
        encoding="utf-8"
    ).replace("\n", "\r\n")
    assert out == expected
    assert "\n" not in out.replace("\r\n", "")


def test_quoted_names_with_apostrophes() -> None:
    text = "table Sales\n\tmeasure 'Customer''s Count' = 1\n\t\tformatString: 0\n"
    doc = tmdl.parse(text)
    assert doc.find("measure", "Customer's Count", ("table", "Sales")) is not None
    out = tmdl.set_description(text, "measure", "Customer's Count", "Customers.", table="Sales")
    assert (
        out
        == "table Sales\n\t/// Customers.\n\tmeasure 'Customer''s Count' = 1\n\t\tformatString: 0\n"
    )


def test_missing_object_raises() -> None:
    with pytest.raises(tmdl.TmdlError):
        tmdl.set_description(sales_text(), "measure", "Nope", "x", table="Sales")


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

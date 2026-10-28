"""Regenerate the PBIP fixtures: `python fixtures/generate.py`.

`retail-clean` follows every rule; `retail-issues` plants the problems listed in fixtures/README.md.
Indentation is written with 4 spaces here and converted to tabs, as TMDL requires.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).parent / "pbip"
FENCE = "```"


class Tags:
    def __init__(self, prefix: str) -> None:
        self.prefix, self.n = prefix, 0

    def __call__(self) -> str:
        self.n += 1
        return f"6c1f0a3e-2b4d-4e5f-{self.prefix}-{self.n:012x}"


def tabs(s: str) -> str:
    out = []
    for line in s.strip("\n").split("\n"):
        lead = re.match(r"^((?:    )*)", line).group(1)  # type: ignore[union-attr]
        out.append("\t" * (len(lead) // 4) + line[len(lead) :] if line.strip() else "")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)) + "\n"


def desc(text: str | None, indent: int = 1) -> str:
    return f"{'    ' * indent}/// {text}\n" if text else ""


def column(
    tag: Tags,
    name: str,
    dtype: str,
    *,
    description: str | None = None,
    hidden: bool = False,
    key: bool = False,
    summarize: str = "none",
    fmt: str | None = None,
    category: str | None = None,
    sort_by: str | None = None,
    source: str | None = None,
) -> str:
    quoted = f"'{name}'" if not re.fullmatch(r"[A-Za-z_]\w*", name) else name
    lines = [f"    column {quoted}", f"        dataType: {dtype}"]
    if key:
        lines.append("        isKey")
    if fmt:
        lines.append(f"        formatString: {fmt}")
    if hidden:
        lines.append("        isHidden")
    lines.append(f"        lineageTag: {tag()}")
    if category:
        lines.append(f"        dataCategory: {category}")
    lines.append(f"        summarizeBy: {summarize}")
    lines.append(f"        sourceColumn: {source or name}")
    if sort_by:
        lines.append(f"        sortByColumn: {sort_by}")
    return desc(description) + "\n".join(lines) + "\n\n"


def measure(
    tag: Tags,
    name: str,
    expr: str,
    *,
    description: str | None = None,
    fmt: str | None = "#,0.00",
    folder: str | None = None,
    hidden: bool = False,
) -> str:
    quoted = f"'{name}'" if not re.fullmatch(r"[A-Za-z_]\w*", name) else name
    if "\n" in expr:
        body = "\n".join("            " + ln for ln in expr.strip("\n").split("\n"))
        head = f"    measure {quoted} =\n{body}\n"
    else:
        head = f"    measure {quoted} = {expr}\n"
    props = []
    if fmt:
        props.append(f"        formatString: {fmt}")
    if folder:
        props.append(f"        displayFolder: {folder}")
    if hidden:
        props.append("        isHidden")
    props.append(f"        lineageTag: {tag()}")
    return desc(description) + head + "\n".join(props) + "\n\n"


def m_partition(table: str, source_expr: str, columns: int, typed: str | None) -> str:
    steps = [
        f"Source = Csv.Document(File.Contents({source_expr}), "
        f'[Delimiter=",", Columns={columns}, Encoding=65001]),',
        "Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true])"
        + ("," if typed else ""),
    ]
    if typed:
        steps.append(f"Typed = Table.TransformColumnTypes(Promoted, {typed})")
    last = "Typed" if typed else "Promoted"
    body = "\n".join("                    " + s for s in steps)
    return (
        f"    partition {table} = m\n        mode: import\n        source =\n"
        f"                let\n{body}\n                in\n                    {last}\n\n"
    )


def table(
    tag: Tags, name: str, body: str, *, description: str | None = None, category: str | None = None
) -> str:
    head = desc(description, 0) + f"table {name}\n"
    if category:
        head += f"    dataCategory: {category}\n"
    head += f"    lineageTag: {tag()}\n\n"
    return tabs(head + body + "    annotation PBI_ResultType = Table\n")


def build(clean: bool) -> dict[str, str]:
    tag = Tags("8a90" if clean else "9b01")

    def path(file: str) -> str:
        return f'DataFolder & "{file}"' if clean else f'"C:\\Data\\{file}"'

    def typed(spec: str) -> str | None:
        return spec if clean else None

    def d(text: str) -> str | None:
        return text if clean else None

    # ---------------------------------------------------------------- Sales
    sales = ""
    sales += measure(
        tag,
        "Total Sales",
        "SUM(Sales[Amount])",
        description=d("Total sales amount in the order currency, before returns."),
    )
    sales += measure(
        tag,
        "Total Cost",
        "SUM(Sales[Cost])",
        description=d("Total cost of goods sold for the selected orders."),
    )
    sales += measure(
        tag,
        "Profit",
        "[Total Sales] - [Total Cost]" if clean else "Sales[Total Sales] - [Total Cost]",
        description=d("Gross profit: total sales minus total cost."),
    )
    sales += measure(
        tag,
        "Margin %",
        "DIVIDE([Profit], [Total Sales])" if clean else "[Profit] / [Total Sales]",
        description=d("Gross margin as a share of total sales."),
        fmt="0.0%",
    )
    sales += measure(
        tag,
        "Order Lines",
        "COUNTROWS(Sales)",
        description="Number of sales order lines.",
        fmt="#,0",
    )
    yoy_return = (
        "DIVIDE(CurrentSales - PriorSales, PriorSales)"
        if clean
        else "IFERROR((CurrentSales - PriorSales) / PriorSales, BLANK())"
    )
    sales += measure(
        tag,
        "Sales YoY %",
        "VAR CurrentSales = [Total Sales]\n"
        "VAR PriorSales = CALCULATE([Total Sales], SAMEPERIODLASTYEAR('Calendar'[Date]))\n"
        f"RETURN\n    {yoy_return}",
        description=d("Change in total sales compared with the same period last year."),
        fmt="0.0%",
        folder="Growth",
    )
    aov = (
        f"    measure 'Avg Order Value' = {FENCE}\n"
        f"            DIVIDE([Total Sales], DISTINCTCOUNT(Sales[OrderID]))\n"
        f"            {FENCE}\n"
    )
    sales += desc(d("Average sales amount per order.")) + aov
    sales += (
        "        formatString: #,0.00\n" if clean else ""
    ) + f"        lineageTag: {tag()}\n\n"
    if clean:
        sales += measure(
            tag, "Units", "SUM(Sales[Quantity])", description="Number of units sold.", fmt="#,0"
        )
        sales += measure(
            tag,
            "Sales by Ship Date",
            "CALCULATE([Total Sales], USERELATIONSHIP(Sales[ShipDate], 'Calendar'[Date]))",
            description="Total sales counted on the ship date instead of the order date.",
        )
    else:
        sales += measure(tag, "Units ", "SUM([Quantity])", fmt="#,0")
        sales += measure(tag, "Revenue", "[Total Sales]")
        sales += measure(tag, "Gross Sales", "SUM(Sales[Amount])")

    hide_keys = clean
    key_sum = "none" if clean else "sum"
    sales += column(tag, "OrderID", "int64", fmt="0", description=d("Order number."))
    sales += column(
        tag, "OrderDate", "dateTime", fmt="Long Date", hidden=hide_keys, description=None
    )
    sales += column(tag, "ShipDate", "dateTime", fmt="Long Date", hidden=hide_keys)
    sales += column(tag, "ProductKey", "int64", fmt="0", hidden=hide_keys, summarize=key_sum)
    sales += column(tag, "CustomerKey", "int64", fmt="0", hidden=hide_keys, summarize=key_sum)
    sales += column(
        tag,
        "Quantity",
        "int64",
        fmt="#,0",
        summarize="none" if clean else "sum",
        description=d("Units on the order line."),
    )
    sales += column(
        tag,
        "UnitPrice",
        "decimal" if clean else "double",
        fmt="#,0.00",
        summarize="none" if clean else "sum",
        description=d("Price per unit at the time of the order."),
    )
    sales += column(
        tag,
        "Amount",
        "decimal",
        fmt="#,0.00",
        summarize="none" if clean else "sum",
        description=d("Line amount: quantity times unit price."),
    )
    sales += column(
        tag,
        "Cost",
        "decimal",
        fmt="#,0.00",
        summarize="none" if clean else "sum",
        description=d("Cost of goods for the order line."),
    )
    if not clean:
        sales += column(tag, "LoadBatch", "int64", fmt="0", hidden=True)
    sales += m_partition(
        "Sales",
        path("sales.csv"),
        9 if clean else 10,
        typed(
            '{{"OrderID", Int64.Type}, {"OrderDate", type date}, {"ShipDate", type date}, '
            '{"ProductKey", Int64.Type}, {"CustomerKey", Int64.Type}, {"Quantity", Int64.Type}, '
            '{"UnitPrice", Currency.Type}, {"Amount", Currency.Type}, {"Cost", Currency.Type}}'
        ),
    )

    # ---------------------------------------------------------------- Product
    product = measure(
        tag,
        "Product Count",
        "DISTINCTCOUNT(Product[ProductKey])",
        description=d("Number of distinct products."),
        fmt="#,0",
    )
    product += column(
        tag,
        "ProductKey",
        "int64",
        fmt="0",
        key=clean,
        hidden=clean,
        description="Product identifier.",
    )
    product += column(tag, "Product Name", "string", description="Name of the product.")
    product += column(tag, "Category", "string", description="Product category.")
    product += column(
        tag,
        "ListPrice",
        "decimal" if clean else "double",
        fmt="#,0.00",
        description="Current list price.",
    )
    if not clean:
        product += column(tag, "LegacyCode", "string", hidden=True)
    product += m_partition(
        "Product",
        path("product.csv"),
        4 if clean else 5,
        typed(
            '{{"ProductKey", Int64.Type}, {"Product Name", type text}, {"Category", type text}, '
            '{"ListPrice", Currency.Type}}'
        ),
    )

    # ---------------------------------------------------------------- Customer
    customer = measure(
        tag,
        "Customer Count",
        "DISTINCTCOUNT(Customer[CustomerKey])",
        description=d("Number of distinct customers."),
        fmt="#,0",
    )
    if not clean:
        customer += measure(
            tag, "Old Customer Count", "COUNTROWS(Customer)", fmt="#,0", hidden=True
        )
    customer += column(
        tag,
        "CustomerKey",
        "int64",
        fmt="0",
        key=clean,
        hidden=clean,
        description="Customer identifier.",
    )
    customer += column(tag, "Customer Name", "string", description="Customer name.")
    customer += column(
        tag,
        "City",
        "string",
        category="City" if clean else None,
        description="City of the customer's billing address.",
    )
    customer += column(tag, "Segment", "string", description="Customer segment.")
    customer += m_partition(
        "Customer",
        path("customer.csv"),
        4,
        typed(
            '{{"CustomerKey", Int64.Type}, {"Customer Name", type text}, {"City", type text}, '
            '{"Segment", type text}}'
        ),
    )

    # ---------------------------------------------------------------- Calendar
    calendar = column(
        tag,
        "Date",
        "dateTime",
        key=True,
        fmt="Long Date",
        description="Calendar date.",
        source="[Date]",
    )
    calendar += column(tag, "Year", "int64", fmt="0", description="Calendar year.", source="[Year]")
    calendar += column(
        tag,
        "Month",
        "string",
        sort_by="MonthNumber" if clean else None,
        description="Month name, sorted by month number.",
        source="[Month]",
    )
    calendar += column(
        tag,
        "MonthNumber",
        "int64",
        fmt="0",
        hidden=clean,
        description=None if clean else "Month number (1-12).",
        source="[MonthNumber]",
    )
    calendar += (
        f"    partition Calendar = calculated\n        mode: import\n        source = {FENCE}\n"
        "                ADDCOLUMNS(\n"
        "                    CALENDAR(DATE(2024, 1, 1), DATE(2026, 12, 31)),\n"
        '                    "Year", YEAR([Date]),\n'
        '                    "Month", FORMAT([Date], "MMM"),\n'
        '                    "MonthNumber", MONTH([Date])\n'
        "                )\n"
        f"                {FENCE}\n\n"
    )

    files = {
        "definition/tables/Sales.tmdl": table(
            tag, "Sales", sales, description=d("One row per order line.")
        ),
        "definition/tables/Product.tmdl": table(
            tag, "Product", product, description="Products we sell."
        ),
        "definition/tables/Customer.tmdl": table(
            tag, "Customer", customer, description="Customers who placed orders."
        ),
        "definition/tables/Calendar.tmdl": table(
            tag,
            "Calendar",
            calendar,
            description="One row per day for time intelligence.",
            category="Time" if clean else None,
        ),
    }
    if not clean:
        targets = column(tag, "Region", "string", description="Sales region.", source="[Region]")
        targets += column(
            tag,
            "Target",
            "decimal",
            fmt="#,0.00",
            description="Sales target for the region.",
            source="[Target]",
        )
        targets += (
            f"    partition Targets = calculated\n        mode: import\n        source = {FENCE}\n"
            '                DATATABLE("Region", STRING, "Target", CURRENCY, '
            '{{"North", 100000}, {"South", 80000}})\n'
            f"                {FENCE}\n\n"
        )
        files["definition/tables/Targets.tmdl"] = table(
            tag, "Targets", targets, description="Sales targets by region (not yet related)."
        )

    rels = [
        ("e01", "Sales.ProductKey", "Product.ProductKey", ""),
        (
            "e02",
            "Sales.CustomerKey",
            "Customer.CustomerKey",
            "" if clean else "    crossFilteringBehavior: bothDirections\n",
        ),
        ("e03", "Sales.OrderDate", "Calendar.Date", ""),
        ("e04", "Sales.ShipDate", "Calendar.Date", "    isActive: false\n"),
    ]
    files["definition/relationships.tmdl"] = tabs(
        "\n".join(
            f"relationship 7d1e0b2a-5c44-4f6e-8a10-1a2b3c4d5{rid}\n{extra}"
            f"    fromColumn: {frm}\n    toColumn: {to}\n"
            for rid, frm, to, extra in rels
        )
    )
    refs = ["Sales", "Product", "Customer", "Calendar"] + ([] if clean else ["Targets"])
    files["definition/model.tmdl"] = tabs(
        "model Model\n    culture: en-US\n    defaultPowerBIDataSourceVersion: powerBI_V3\n"
        "    sourceQueryCulture: en-US\n    dataAccessOptions\n        legacyRedirects\n"
        "        returnErrorValuesAsNull\n\n"
        f"annotation __PBI_TimeIntelligenceEnabled = {0 if clean else 1}\n\n"
        f"annotation PBI_QueryOrder = {json.dumps(refs, separators=(',', ':'))}\n\n"
        + "".join(f"ref table {r}\n" for r in refs)
    )
    files["definition/database.tmdl"] = tabs("database\n    compatibilityLevel: 1567\n")
    if clean:
        files["definition/expressions.tmdl"] = tabs(
            'expression DataFolder = "C:\\Data\\" meta [IsParameterQuery=true, Type="Text", '
            "IsParameterQueryRequired=true]\n"
            f"    lineageTag: {tag()}\n\n    annotation PBIResultType = Text\n"
        )
    return files


SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"

# (page, visual id, visual type, [(role, "Column"|"Measure", table, field), ...])
# Uses every visible field of retail-clean except measure [Order Lines] and Customer[Segment].
REPORT_VISUALS = [
    ("overview", "kpis", "multiRowCard", [
        ("Values", "Measure", "Sales", m) for m in (
            "Total Sales", "Margin %", "Sales YoY %", "Avg Order Value", "Units",
            "Sales by Ship Date")
    ] + [("Values", "Measure", "Product", "Product Count"),
         ("Values", "Measure", "Customer", "Customer Count")]),
    ("overview", "bycategory", "clusteredBarChart", [
        ("Category", "Column", "Product", "Category"), ("Y", "Measure", "Sales", "Total Sales")]),
    ("overview", "trend", "lineChart", [
        ("Category", "Column", "Calendar", "Month"), ("Y", "Measure", "Sales", "Total Sales")]),
    ("overview", "year", "slicer", [("Values", "Column", "Calendar", "Year")]),
    ("details", "products", "tableEx", [
        ("Values", "Column", "Product", "Product Name"),
        ("Values", "Column", "Product", "ListPrice"),
        ("Values", "Column", "Sales", "UnitPrice"),
        ("Values", "Column", "Customer", "Customer Name")]),
    ("details", "cities", "map", [
        ("Category", "Column", "Customer", "City"), ("Size", "Measure", "Sales", "Total Sales")]),
]  # fmt: skip


def report_files(
    visuals: list[tuple[str, str, str, list[tuple[str, str, str, str]]]],
) -> dict[str, str]:
    """`Retail.pbip` plus a PBIR report (`Retail.Report`) over the retail model. These are the
    exact formats Power BI Desktop (Oct 2026) opened successfully on 2026-10-27; keep them unless
    a newer Desktop proves otherwise. With no visuals it is a blank one-page report, which is
    enough for Desktop to open the project."""

    def dump(data: object) -> str:
        return json.dumps(data, indent=2) + "\n"

    pages = {"overview": "Overview", "details": "Details"} if visuals else {"page1": "Page 1"}
    first = next(iter(pages))
    files = {
        "Retail.pbip": dump({
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/"
                       "1.0.0/schema.json",
            "version": "1.0",
            "artifacts": [{"report": {"path": "Retail.Report"}}],
            "settings": {"enableAutoRecovery": True},
        }),
        "Retail.Report/definition.pbir": dump(
            {"version": "4.0", "datasetReference": {"byPath": {"path": "../Retail.SemanticModel"}}}
        ),
        "Retail.Report/definition/version.json": dump(
            {"$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"}
        ),
        "Retail.Report/definition/report.json": dump(
            {"$schema": f"{SCHEMA}/report/1.0.0/schema.json"}
        ),
        "Retail.Report/definition/pages/pages.json": dump({
            "$schema": f"{SCHEMA}/pagesMetadata/1.0.0/schema.json",
            "pageOrder": list(pages), "activePageName": first,
        }),
    }  # fmt: skip
    for page, title in pages.items():
        files[f"Retail.Report/definition/pages/{page}/page.json"] = dump({
            "$schema": f"{SCHEMA}/page/1.0.0/schema.json", "name": page, "displayName": title,
            "displayOption": "FitToPage", "height": 720, "width": 1280,
        })  # fmt: skip
    for i, (page, vid, vtype, fields) in enumerate(visuals):
        state: dict[str, dict[str, list[object]]] = {}
        for role, kind, table, prop in fields:
            state.setdefault(role, {"projections": []})["projections"].append({
                "field": {kind: {"Expression": {"SourceRef": {"Entity": table}}, "Property": prop}},
                "queryRef": f"{table}.{prop}", "nativeQueryRef": prop,
            })  # fmt: skip
        files[f"Retail.Report/definition/pages/{page}/visuals/{vid}/visual.json"] = dump({
            "$schema": f"{SCHEMA}/visualContainer/1.0.0/schema.json", "name": vid,
            "position": {"x": 40 + 320 * (i % 3), "y": 40 + 240 * (i // 3), "z": i,
                         "width": 300, "height": 220},
            "visual": {"visualType": vtype, "query": {"queryState": state}},
        })  # fmt: skip
    return files


CITIES = ["Hyderabad", "Vijayawada", "Guntur", "Visakhapatnam", "Chennai", "Bengaluru"]
PRODUCTS = [
    ("Road Bike", "Bikes", 42000), ("Mountain Bike", "Bikes", 38000),
    ("City Bike", "Bikes", 21000), ("Helmet", "Accessories", 2400),
    ("Bike Light", "Accessories", 900), ("Water Bottle", "Accessories", 350),
    ("Lock", "Accessories", 1200), ("Pump", "Accessories", 1500),
    ("Jersey", "Clothing", 1800), ("Gloves", "Clothing", 700),
    ("Shorts", "Clothing", 1600), ("Socks", "Clothing", 300),
]  # fmt: skip


def data_files() -> dict[str, str]:
    """Small deterministic CSVs the retail models load (fixtures/data). The last column of
    sales.csv and product.csv only exists in retail-issues; retail-clean reads fewer columns."""
    import datetime as dt
    import random

    rng = random.Random(7)
    product = ["ProductKey,Product Name,Category,ListPrice,LegacyCode"]
    for key, (name, category, price) in enumerate(PRODUCTS, start=1):
        product.append(f"{key},{name},{category},{price:.2f},P-{key:03d}")
    customer = ["CustomerKey,Customer Name,City,Segment"]
    for key in range(1, 11):
        segment = "Business" if key % 3 == 0 else "Retail"
        customer.append(f"{key},Customer {key:02d},{CITIES[key % len(CITIES)]},{segment}")
    sales = ["OrderID,OrderDate,ShipDate,ProductKey,CustomerKey,Quantity,UnitPrice,Amount,Cost,"
             "LoadBatch"]  # fmt: skip
    start = dt.date(2024, 1, 1)
    for order in range(1001, 1081):
        day = start + dt.timedelta(days=rng.randrange(0, 365 * 3 - 10))
        ship = day + dt.timedelta(days=rng.randrange(1, 6))
        customer_key = rng.randrange(1, 11)
        for _ in range(rng.randrange(1, 4)):
            pkey = rng.randrange(1, len(PRODUCTS) + 1)
            qty = rng.randrange(1, 5)
            price = PRODUCTS[pkey - 1][2]
            amount = qty * price
            sales.append(f"{order},{day.isoformat()},{ship.isoformat()},{pkey},{customer_key},"
                         f"{qty},{price:.2f},{amount:.2f},{amount * 0.62:.2f},1")  # fmt: skip
    return {"sales.csv": "\n".join(sales) + "\n", "product.csv": "\n".join(product) + "\n",
            "customer.csv": "\n".join(customer) + "\n"}  # fmt: skip


def main() -> None:
    variants = (("retail-clean", True, False), ("retail-issues", False, False),
                ("retail-report", True, True))  # fmt: skip
    for variant, clean, with_report in variants:
        if (ROOT / variant).exists():
            shutil.rmtree(ROOT / variant)
        model_dir = ROOT / variant / "Retail.SemanticModel"
        (model_dir / "definition" / "tables").mkdir(parents=True)
        (model_dir / "definition.pbism").write_text(
            json.dumps({"version": "4.0", "settings": {}}, indent=2) + "\n", newline="\n"
        )
        for rel, text in build(clean).items():
            (model_dir / rel).write_text(text, encoding="utf-8", newline="\n")
        for rel, text in report_files(REPORT_VISUALS if with_report else []).items():
            target = ROOT / variant / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
    data = ROOT.parent / "data"
    data.mkdir(exist_ok=True)
    for name, text in data_files().items():
        (data / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"fixtures written to {ROOT} and {data}")


if __name__ == "__main__":
    main()

"""The .pbix reader against a real Microsoft sample (MIT licence; see fixtures/pbix/README.md)."""

from __future__ import annotations

from datawright.config import FIXTURES_DIR
from datawright.connectors.powerbi.health import health
from datawright.connectors.powerbi.pbip import PbixFileConnector, open_source
from datawright.core.capabilities import Capability

PBIX = FIXTURES_DIR / "pbix" / "Revenue Opportunities.pbix"


def test_reads_model_metadata() -> None:
    connector = open_source(PBIX)
    assert isinstance(connector, PbixFileConnector)
    assert connector.max_capability == Capability.READ
    model = connector.models()["Revenue Opportunities"]
    assert model.source == "pbix" and model.definition_dir is None
    assert {"Account", "Calendar", "Calculations", "Opportunity", "Product"} <= set(model.tables)
    assert not any(name.startswith(("H$", "R$", "U$")) for name in model.tables)
    measure_names = {m.name for _, m in model.all_measures()}
    assert "Avg Opportunity Days" in measure_names
    _, aod = next((t, m) for t, m in model.all_measures() if m.name == "Avg Opportunity Days")
    assert aod.format_string == "#,0" and aod.expression
    types = {c.data_type for _, c in model.all_columns()}
    assert {"string", "int64", "dateTime"} <= types and None not in types
    assert model.relationships and all(r.to_cardinality == "one" for r in model.relationships)
    month = model.tables["Calendar"].column("Month")
    assert month is not None and month.sort_by_column == "Month Number"


def test_health_and_objects() -> None:
    connector = open_source(PBIX)
    report = health(connector.models()["Revenue Opportunities"])
    assert 0 <= report.score <= 100 and report.visible > 0
    assert "mark-primary-keys" in report.by_rule
    objects = connector.objects()
    assert all(o.file_path is None for o in objects)  # .pbix has no TMDL files to edit
    some = next(o for o in objects if o.kind == "measure")
    assert connector.resolve(some.object_path).name == some.name

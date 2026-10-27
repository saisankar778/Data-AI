# .pbix fixtures

`Revenue Opportunities.pbix` is an unmodified copy of a Microsoft sample report from [microsoft/powerbi-desktop-samples](https://github.com/microsoft/powerbi-desktop-samples) (`2026 Power BI Samples Revamp/`), under the MIT licence (see `NOTICE`).

`tests/test_pbix.py` uses it to check that the `.pbix` reader maps real engine metadata (types, summarise-by, sort-by columns, relationships, format strings) onto Datawright's model. Datawright reads only metadata from it, never table data.

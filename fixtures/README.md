# Fixtures

## `pbip/retail-clean` and `pbip/retail-issues`

Two hand-written semantic models in the PBIP/TMDL format: a small retail model with `Sales`, `Product`, `Customer` and `Calendar` tables. Only the `Retail.SemanticModel` folder exists; there's no report yet.

> These files have **not been opened in Power BI Desktop yet**, because Desktop isn't installed on the dev machine. The Phase 1 exit criteria require opening them in Desktop, or replacing them with Desktop-saved copies of the same model.

`retail-clean` should produce no findings. `retail-issues` contains these planted problems:

| Problem | Where | Rule |
|---|---|---|
| Measures without descriptions | 8 measures (all except `Order Lines`) | `measure-missing-description` |
| `/` instead of `DIVIDE` | `Margin %`, `Sales YoY %` | `use-divide-function` |
| Floating-point columns | `Sales[UnitPrice]`, `Product[ListPrice]` | `avoid-floating-point` |
| Bidirectional relationship | Sales → Customer | `avoid-bidirectional-relationship` |
| Key columns summarised and visible | `Sales[ProductKey]`, `Sales[CustomerKey]` | Phase 1 rules |
| `IFERROR` | `Sales YoY %` | Phase 1 rule |
| Date table not marked; auto date/time on | `Calendar`, `model.tmdl` | Phase 1 rules |

All files use LF line endings and tab indentation. `.gitattributes` keeps Git from converting them, because tests compare bytes.

# Phase A: validate before building more

Goal: within 2 weeks, find **3 design partners** who will try Datawright on their real Power BI models, and learn whether at least 2 would pay. Phase 1 starts only after this gate.

## 1. Who to talk to (aim for 15 conversations)

- Power BI freelancers and small consultancies (LinkedIn, Upwork, Power BI community groups, college alumni in analytics jobs).
- Analysts and BI developers at SMBs, especially teams on **Pro licences** (no Copilot).
- One or two people from teams that *do* have Fabric/Copilot, to hear whether Copilot answers well on their models.

## 2. Interview guide (20 minutes, listen more than you talk)

1. Walk me through the last time you inherited or built a Power BI model. What took the longest?
2. How do you document models today? Who reads the descriptions?
3. Do you use PBIP / Git, or `.pbix` files? Power BI Pro, PPU or Fabric capacity?
4. Have you tried Copilot in Power BI? What happened?
5. Have you tried ChatGPT/Claude for DAX or model work? What stopped you using it more?
6. If a tool audited a model and wrote the descriptions, and you approved every change, what would that be worth to you per month? Who would pay: you, your company, or your client?
7. Can we try it on one of your real models next week? (This is the design-partner ask.)

Don't pitch features. Record their words, not your summary.

## 3. Concierge test (do this for 3 real models)

Phase 0 already does most of the work, so run the real tool and fill the gaps by hand:

1. Get a PBIP export of their model in a Git repo (or a `.pbix`, then use Desktop's *Save as PBIP*), with permission.
2. `datawright connect <name> <path> --capability propose`, then `datawright audit <name>` for findings.
3. With an API key set: `datawright document <name> --limit 20` for proposed descriptions. Review them in `datawright serve`.
4. By hand, add the checks the tool can't do yet (the Best Practice Analyzer rules in Tabular Editor) and write a one-page report.
5. Send them the report and the change set. Ask what they'd accept as-is, what they'd edit, and what they'd pay.

Measure: minutes saved compared with their usual way, the share of descriptions accepted without edits, and their own words about price.

## 4. Tracker

| # | Name / company | Role | Licence | .pbix / PBIP / Git | Pain in their words | Would pilot? | Would pay? (price) | Next step |
|---|---|---|---|---|---|---|---|---|
| 1 | | | | | | | | |
| 2 | | | | | | | | |
| 3 | | | | | | | | |

## Gate (end of week 2)

- [ ] 3 design partners committed to a pilot on real models
- [ ] At least 2 named a price they'd pay
- [ ] A one-page description of the target customer written down

If it fails: don't build Phase 1. Try the second wedge (Excel→Power BI migration) with the same interviews.

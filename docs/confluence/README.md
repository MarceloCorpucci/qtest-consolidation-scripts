# Confluence source

Markdown source for the Confluence space **qTest Migration Lifecycle & Governance**. The folder structure mirrors the page hierarchy:

```
qtest-migration-lifecycle-governance/
├─ index.md                                  (main page, Migration Control Dashboard)
├─ 01-migration-overview-and-scope.md
├─ 02-migration-governance-and-decision-log.md
└─ phase-N-<name>/index.md                   (phases 1-8, each with a Phase Exit Criteria & Approval gate)
```

Each file starts with front matter (`title`, `parent`, `labels`) that gives the Confluence page title and parent page. The page title is the `title` value; file names are only for ordering. Folder `index.md` files are the parent pages so sub-pages can be added later.

Statuses, owners and approvals in the dashboard and phase pages are initial values from the template; update them as gates progress. Blank cells (names, dates) are to be filled in Confluence.

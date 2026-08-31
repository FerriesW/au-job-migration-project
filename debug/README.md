# debug/

Dated reviews of the codebase itself, kept rather than thrown away.

These are not design documents. [`docs/adr/`](../docs/adr/) records decisions
before they are acted on; [`dev_log.txt`](../dev_log.txt) records what shipped.
This folder holds the third thing — periodic audits that look at what the code
actually became, including where it drifted from what the other two claim.

## What lives here

Self-contained HTML reports, named `<kind>-<YYYYMMDD>.html`, openable straight
from disk with no build step. They embed their own findings and numbers, so a
report stays readable after the code it describes has moved on.

| File | What it covers |
|---|---|
| `architecture-review-20260824.html` | First full audit after the multi-cloud build-out: dead code the ADRs claimed was alive, configuration read through two paths, six copies of the same logging setup, and an equivalence claim with no reproducible evidence behind it. |

## When to add one

After a stretch of work large enough that nobody could hold it all in their
head — a phase landing, a run of a dozen commits, or the moment the codebase
starts feeling scattered. The audit that produced the first report was
triggered by exactly that feeling, and it was right: two of the four findings
were documentation asserting things that were no longer true.

Write the report, keep it, and do not edit an old one to match new reality. A
review that has been quietly corrected is worth nothing; the value is in seeing
what was true on the day, and what the next audit found had rotted since.

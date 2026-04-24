# Dashboard Next 8 Steps Plan

Goal: make the local Nanobot Ops Dashboard materially more useful for day-to-day monitoring by improving readability, filtering, and historical analysis.

Ordered execution plan:

1. Add richer cycles table rendering
- parse `detail_json`
- show report source, artifact list, approval summary as readable fields

2. Add richer promotions table rendering
- show candidate path
- decision record / accepted record presence
- readable promotion summary fields

3. Add filtering to cycles and promotions pages
- query params for `source` and `status`
- preserve simple local UX

4. Add history analytics page
- snapshot counts by source
- cycle counts by source/status
- latest collection time
- recent trend summary

5. Improve approvals page readability
- show source, status, gate state, raw approval compactly
- include last-collected ordering clearly

6. Improve deployments page readability
- separate live eeepc vs repo-side proof fields
- show latest report/outbox/goal pointers more clearly

7. Extend tests
- app rendering tests for cycles/promotions/analytics/filtering/api
- keep collector/storage tests green

8. Update docs and commit final dashboard v1.1 slice
- update README and SHOWING_THE_DASHBOARD
- add a short feature summary note

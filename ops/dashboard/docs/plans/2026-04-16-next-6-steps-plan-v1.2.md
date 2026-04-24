# Dashboard Next 6 Steps Plan (v1.2)

Goal: make the local Nanobot Ops Dashboard easier to operate interactively and programmatically, while deepening historical visibility without adding heavy dependencies.

Ordered execution plan:

1. Add visible filter forms to cycles and promotions pages
- source/status inputs in the UI
- preserve query-string filtering already implemented

2. Add JSON history endpoints
- `/api/cycles`
- `/api/promotions`
- support `source` and `status` filters

3. Improve analytics with recent snapshot and recent cycle sections
- latest collections table
- recent cycle trend table

4. Improve overview with direct links and compact latest-source summaries
- latest eeepc snapshot time/status/goal
- latest repo snapshot time/status
- quick links to filtered views

5. Add approvals/deployments JSON endpoints
- `/api/approvals`
- `/api/deployments`
- useful for scripting/demo/export

6. Update docs and commit the completed v1.2 slice
- README
- SHOWING_THE_DASHBOARD
- IMPLEMENTATION_SUMMARY

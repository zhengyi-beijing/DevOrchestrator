import unittest

from dev_orchestrator.core.lifecycle_projection import overlay_orchestration_lifecycle


class LifecycleProjectionTests(unittest.TestCase):
    def test_roles_overlay_without_mutating_raw_monitor_state(self):
        raw = {"projects": [{"project_id": "p1", "state": "IDLE"}]}
        planning = overlay_orchestration_lifecycle(
            raw, planner_state={"plans": {"x": {
                "project_id": "p1", "plan_id": "x", "state": "planning",
                "started_at": "2026-09-10T01:00:00+00:00",
            }}},
        )
        self.assertEqual(raw["projects"][0]["state"], "IDLE")
        self.assertEqual(planning["projects"][0]["lifecycle_state"], "PLANNING")

        reviewing = overlay_orchestration_lifecycle(
            {"projects": [{"project_id": "p1", "state": "WAITING_REVIEW"}]},
            reviewer_state={"reviews": {"r": {
                "project_id": "p1", "review_id": "r", "state": "running",
                "started_at": "2026-09-10T01:05:00+00:00",
            }}},
        )
        self.assertEqual(reviewing["projects"][0]["lifecycle_state"], "REVIEWING")

    def test_executing_worker_wins_over_stale_running_reviewer(self):
        summary = {"projects": [{"project_id": "p1", "state": "WORKER_RUNNING"}]}
        reviewer = {"reviews": {"old": {
            "project_id": "p1",
            "review_id": "old",
            "state": "running",
            "started_at": "2026-09-10T00:00:00+00:00",
        }}}
        projected = overlay_orchestration_lifecycle(
            summary,
            reviewer_state=reviewer,
        )
        self.assertEqual(
            projected["projects"][0]["lifecycle_state"],
            "EXECUTING",
        )

"""Bootstrap regression: terminal read-only Codex timeouts must use bounded failover."""
from dataclasses import replace
from pathlib import Path
import sys,tempfile,unittest
sys.path.insert(0,str(Path.cwd()/"tests_py"))
from test_ai_planner import ReviewerResourceFailoverPort,make_repo,wait_terminal
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator

class TerminalTimeoutPort(ReviewerResourceFailoverPort):
    def execute(self,request):
        result=super().execute(request)
        if result.status=="failed":
            return replace(result,error="Codex turn timed out",failure_classification=None)
        return result

class CancelledTimeoutPort(TerminalTimeoutPort):
    def execute(self,request):
        result=super().execute(request)
        return replace(result,status="cancelled") if result.status=="failed" else result
class NoCandidateTimeoutPort(TerminalTimeoutPort):
    def execute(self,request):
        result=super().execute(request)
        return replace(result,status="no_candidate") if result.status=="failed" else result
class NoResourceTimeoutPort(TerminalTimeoutPort):
    def execute(self,request):
        result=super().execute(request)
        return replace(result,resource_context=None) if result.status=="failed" else result
class SemanticTimeoutPort(TerminalTimeoutPort):
    def execute(self,request):
        result=super().execute(request)
        return replace(result,error="validation assertion timed out") if result.status=="failed" else result

class ReadOnlyReviewerTimeoutTests(unittest.TestCase):
    def run_plan(self,failures=1,port_type=TerminalTimeoutPort,**policy):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup)
        base=Path(td.name);repo=base/"repo";make_repo(repo)
        port=port_type(failures=failures)
        coordinator=AIPlannerCoordinator(base/"runtime",port)
        project={"project_id":"p1","repo_path":str(repo),"execution":{"engine":"aibroker"},"ai_roles":{"planner":{"enabled":True,"review_independence":"provider",**policy}}}
        snapshot={"project_id":"p1","state":"IDLE","next_status":"**PENDING DESIGN**","telemetry":{"task_id":"P14"}}
        plan_id,_=coordinator.start(project,snapshot,"review-timeout")
        return port,wait_terminal(coordinator,plan_id)
    def test_terminal_codex_timeout_fails_over_preserving_plan_and_independence(self):
        port,row=self.run_plan()
        self.assertEqual(row["state"],"ready",row.get("reason"))
        self.assertEqual(port.planner_calls,1)
        reviews=[r for r in port.requests if r.role=="reviewer"]
        self.assertEqual(len(reviews),2)
        self.assertEqual(reviews[1].excluded_resource_ids,("reviewer-r-1",))
        self.assertEqual(reviews[1].previous_resource_context,reviews[0].previous_resource_context)
        self.assertEqual(reviews[1].independence,"provider")
        self.assertEqual(row["reviewer_attempts"][0]["failure_classification"],"provider_temporarily_unavailable")
    def test_all_terminal_timeouts_stop_at_existing_resource_budget(self):
        port,row=self.run_plan(failures=3,max_reviewer_resource_failovers=2)
        self.assertEqual(row["state"],"failed")
        self.assertEqual(port.review_calls,3)
        self.assertEqual(port.planner_calls,1)
        self.assertIn("all eligible reviewer resources exhausted",row["reason"])
    def test_zero_failover_budget_never_retries(self):
        port,row=self.run_plan(max_reviewer_resource_failovers=0)
        self.assertEqual(row["state"],"failed")
        self.assertEqual(port.review_calls,1)
        self.assertEqual(port.planner_calls,1)

    def test_cancelled_terminal_never_fails_over(self):
        port,row=self.run_plan(port_type=CancelledTimeoutPort)
        self.assertEqual(row["state"],"failed")
        self.assertEqual(port.review_calls,1)
        self.assertIsNone(row["reviewer_attempts"][0]["failure_classification"])
    def test_no_candidate_terminal_never_fails_over(self):
        port,row=self.run_plan(port_type=NoCandidateTimeoutPort)
        self.assertEqual(row["state"],"failed")
        self.assertEqual(port.review_calls,1)
        self.assertIsNone(row["reviewer_attempts"][0]["failure_classification"])
    def test_missing_resource_identity_never_fails_over(self):
        port,row=self.run_plan(port_type=NoResourceTimeoutPort)
        self.assertEqual(row["state"],"failed")
        self.assertEqual(port.review_calls,1)
    def test_semantic_timeout_never_fails_over(self):
        port,row=self.run_plan(port_type=SemanticTimeoutPort)
        self.assertEqual(row["state"],"failed")
        self.assertEqual(port.review_calls,1)
    def test_worker_port_classification_stays_conservative(self):
        from dev_orchestrator.ai.aibroker_subprocess import _failure_classification
        self.assertIsNone(_failure_classification({"status":"failed","error":"Codex turn timed out"}))

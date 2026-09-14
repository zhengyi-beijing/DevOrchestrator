import json
import subprocess
from pathlib import Path

from dev_orchestrator.accounting import build_p11_report
from test_p11_reporting import representative_events, ts


ROOT = Path(__file__).resolve().parents[1]


def render_text(payload: dict) -> dict:
    app = str(ROOT / "web" / "app.js")
    script = f"""
class Element {{
  constructor() {{ this.children=[]; this.className=''; this.textContent=''; }}
  replaceChildren(...items) {{ this.children=items; }}
  append(...items) {{ this.children.push(...items); }}
  appendChild(item) {{ this.children.push(item); return item; }}
}}
const elements = new Map();
global.document = {{
  getElementById: id => {{ if (!elements.has(id)) elements.set(id,new Element()); return elements.get(id); }},
  createElement: () => new Element(),
}};
const {{renderAccounting}} = require({json.dumps(app)});
renderAccounting({json.dumps(payload)});
function flatten(item) {{ return [item.textContent, ...item.children.flatMap(flatten)].filter(Boolean).join('|'); }}
const result={{}}; for (const [key,value] of elements) result[key]=flatten(value);
console.log(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, encoding="utf-8",
        timeout=5, check=True,
    )
    return json.loads(completed.stdout)


def test_dashboard_renders_measured_derived_and_warning_fixture() -> None:
    payload = {
        "available": True,
        **build_p11_report(
            representative_events(), ts(0), ts(200),
            project_id="devorchestrator", task_id="P11d",
        ).as_dict(),
    }
    rendered = render_text(payload)
    assert "20.0%" in rendered["accountingSummary"]
    assert "derived_from_measured" in rendered["accountingBottleneck"]
    assert "rdc_deadlock" in rendered["accountingBottleneck"]
    assert "FAIL" in rendered["acceptanceGates"]
    assert "ai_context_switching" in rendered["hypothesisEvidence"]
    assert "OBSERVED" in rendered["hypothesisEvidence"]
    assert "oversized_rdc_commands" in rendered["hypothesisEvidence"]
    assert "NOT OBSERVED" in rendered["hypothesisEvidence"]
    assert "devorchestrator / P11d / worker" in rendered["scopeBreakdown"]
    assert "EDR 15.0%" in rendered["scopeBreakdown"]
    assert "unavailable" in rendered["evidenceWarnings"]


def test_dashboard_renders_unavailable_sources_as_unavailable_not_zero() -> None:
    payload = {"available": True, **build_p11_report([], ts(0), ts(10)).as_dict()}
    rendered = render_text(payload)
    assert "unavailable" in rendered["accountingSummary"]
    assert "0.0%" not in rendered["accountingSummary"]
    assert "RDC evidence unavailable" in rendered["rdcEvidence"]
    assert "UNAVAILABLE" in rendered["hypothesisEvidence"]
    assert "No measured project/task/role time" in rendered["scopeBreakdown"]

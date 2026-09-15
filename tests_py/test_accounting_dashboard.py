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


def test_control_dashboard_renders_only_server_advertised_actions() -> None:
    app = str(ROOT / "web" / "app.js")
    payload = {"data": {
        "control_enabled": True,
        "projects": [{
            "project_id": "p1", "name": "Project One", "repo_path": "C:/repo",
            "lifecycle_state": "READY_TO_RUN",
            "control_identity": {"task_id": "P12", "branch": "main", "head": "abcdef"},
            "owner_control": {"paused": False}, "conversation": {"binding": None},
            "controls": [
                {"action": "continue", "available": True, "reason": "ready"},
                {"action": "retry", "available": False, "reason": "no exact target"},
            ],
        }],
        "sessions": [], "bindings": [], "commands": [{"project_id": "p1", "action": "pause", "state": "accepted"}],
    }}
    script = f"""
class Element {{
  constructor(tag='div') {{ this.tag=tag; this.children=[]; this.className=''; this.textContent=''; this.disabled=false; this.title=''; this.value=''; }}
  replaceChildren(...items) {{ this.children=items; }} append(...items) {{ this.children.push(...items); }}
  appendChild(item) {{ this.children.push(item); return item; }} addEventListener() {{}}
}}
const elements=new Map(); global.document={{
  getElementById:id=>{{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);}},
  createElement:tag=>new Element(tag),
}};
const {{renderControlSurface}}=require({json.dumps(app)}); renderControlSurface({json.dumps(payload)});
function all(item){{return [item,...item.children.flatMap(all)];}}
const nodes=all(elements.get('controlProjects'));
console.log(JSON.stringify({{buttons:nodes.filter(x=>x.tag==='button').map(x=>[x.textContent,x.disabled,x.title]),status:elements.get('controlStatus').textContent,commands:all(elements.get('controlCommands')).map(x=>x.textContent).filter(Boolean)}}));
"""
    completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=5, check=True)
    rendered = json.loads(completed.stdout)
    assert rendered["buttons"] == [["continue", False, "ready"], ["retry", True, "no exact target"]]
    assert rendered["status"] == "guarded actions enabled"
    assert any("pause · accepted" in item for item in rendered["commands"])


def test_control_dashboard_confirms_stop_and_polls_pending_result() -> None:
    app = str(ROOT / "web" / "app.js")
    project = {"project_id": "p1", "control_identity": {"revision": "r1"}}
    script = f"""
global.window={{confirm:()=>false}}; let calls=[];
global.fetch=async(url,options)=>{{calls.push(url);throw new Error('fetch must not run when cancelled')}};
const {{sendControl}}=require({json.dumps(app)});
(async()=>{{
  const cancelled=await sendControl({json.dumps(project)},'stop');
  window.confirm=()=>true;
  global.setTimeout=(fn)=>{{fn();return 0;}};
  let step=0; global.fetch=async(url,options)=>{{calls.push(url);step+=1;
    if(step===1)return {{ok:true,json:async()=>({{data:{{csrf_token:'csrf'}}}})}};
    if(step===2)return {{ok:true,json:async()=>({{data:{{command_id:'c1',state:'pending'}}}})}};
    return {{ok:true,json:async()=>({{data:{{command_id:'c1',state:'accepted'}}}})}};
  }};
  const result=await sendControl({json.dumps(project)},'stop');
  console.log(JSON.stringify({{cancelled,result,calls}}));
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=5, check=True)
    rendered = json.loads(completed.stdout)
    assert rendered["cancelled"] is None
    assert rendered["result"]["state"] == "accepted"
    assert rendered["calls"] == [
        "/api/v1/control/browser-sessions",
        "/api/v1/control/commands",
        "/api/v1/control/commands/c1",
    ]


def test_control_dashboard_creates_displays_and_revokes_adapter_pairing() -> None:
    app = str(ROOT / "web" / "app.js")
    script = f"""
class Element {{ constructor() {{ this.textContent=''; this.disabled=true; }} }}
const elements=new Map(); global.document={{
  getElementById:id=>{{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);}},
}};
let calls=[]; let step=0;
global.fetch=async(url,options)=>{{calls.push([url,options.method,options.headers]);step+=1;
  if(step===1)return {{ok:true,json:async()=>({{data:{{csrf_token:'csrf'}}}})}};
  if(step===2)return {{ok:true,json:async()=>({{data:{{pairing_id:'pair-1',code:'123456'}}}})}};
  return {{ok:true,json:async()=>({{data:{{revoked:true}}}})}};
}};
const {{createAdapterPairing,revokeAdapterPairing}}=require({json.dumps(app)});
(async()=>{{
  await createAdapterPairing();
  const shown={{text:elements.get('pairingCode').textContent,disabled:elements.get('revokeAdapter').disabled}};
  await revokeAdapterPairing();
  console.log(JSON.stringify({{shown,revoked:{{text:elements.get('pairingCode').textContent,disabled:elements.get('revokeAdapter').disabled}},calls}}));
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, encoding="utf-8",
        timeout=5, check=True,
    )
    rendered = json.loads(completed.stdout)
    assert rendered["shown"] == {"text": "pair-1:123456", "disabled": False}
    assert rendered["revoked"] == {"text": "Pairing revoked.", "disabled": True}
    assert [call[:2] for call in rendered["calls"]] == [
        ["/api/v1/control/browser-sessions", "POST"],
        ["/api/v1/control/adapter-pairings", "POST"],
        ["/api/v1/control/adapter-pairings/pair-1/revoke", "POST"],
    ]
    assert rendered["calls"][1][2]["X-DevOrch-CSRF"] == "csrf"
    assert rendered["calls"][2][2]["X-DevOrch-CSRF"] == "csrf"

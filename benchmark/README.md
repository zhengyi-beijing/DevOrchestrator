# AI Capability Benchmark (aibench)

The `aibench` project is an isolated, reproducible benchmarking suite for measuring the real capability, efficiency, and reliability of AI models and resources available through DevOrchestrator and AIResourceBroker. It specifically determines whether shared repository retrieval (e.g. `zvec-grep`) provides measurable benefit to be promoted into the standard Agent Harness.

## Core Principles

1. **Isolation**: Benchmark trials run against synthetic repositories, never against production source trees.
2. **Deterministic A/B Comparison**:
   - **Track A (Native)**: Pristine repository copy with standard search/read tools only.
   - **Track B (Retrieval)**: Same pristine repository copy plus untracked `.aibench/tools/zvec-grep` retrieval interface.
   - **Identical Prompts**: Byte-identical prompt text, prompt hash, task revision, target resource, role, timeout, and rubric across each paired trial cell.
3. **Exact Resource Pinning**:
   - Uses AIResourceBroker's supported `excluded_resource_ids` contract to isolate individual model resources without modifying production interfaces.
   - Bounded reliability chains test failover behavior on explicit provider/quota errors only.
4. **Code-Only Ground-Truth Scoring**:
   - Automated citation verification against exact file bytes and line spans.
   - Required findings detection and false-findings penalties.
   - Patch syntax verification and test suite execution in disposable workspaces.
   - Zero subjective model judges.
5. **Isolation & Optional Windows Hardening**:
   - Current-user live benchmark is supported by default; no dedicated Windows account is required.
   - Disposable Git workspaces and external scratch roots keep benchmark mutations out of production repositories.
   - Dedicated SID ACL deny rules, Task Scheduler identity, and outbound-deny firewall rules remain optional hardening features only.
   - Optional containment audits record whether hardening is enabled without blocking ordinary live capability benchmarking.
6. **Total, Bounded Promotion Decision**:
   - Emits strictly `PROMOTE` or `NO_PROMOTE`.
   - Unsupported `zvec` yields `NO_PROMOTE` with `capability_unsupported` and marks downstream retrieval gates as not applicable due to capability failure.

## CLI Commands

```powershell
python -m aibench corpus-verify --corpus-path <path>
python -m aibench containment-audit --config <path>
python -m aibench zvec-probe --zvec-path <path>
python -m aibench plan-freeze --output <path> --repeats <n> --seed <seed>
python -m aibench submit --plan <path> --queue <path>
python -m aibench run --plan <path> --output-dir <path>
python -m aibench report --results <path> --output <path>
python -m aibench decide --summary <path> --output <path>
```

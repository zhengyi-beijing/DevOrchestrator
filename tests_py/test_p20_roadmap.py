"""Owner-authorized P20 sequence uses the existing successor resolver."""
from pathlib import Path

from dev_orchestrator.core.staged_roadmap import read_successor
from dev_orchestrator.core.successor_consistency import resolve_successor


def test_p20_has_one_review_gated_sequence_and_no_invented_successor():
    repo = Path(__file__).resolve().parent.parent
    sequence = ["P19.2", "P20.1", "P20.2", "P20.3", "P20.4", "P20.5"]
    for source, target in zip(sequence, sequence[1:]):
        staged = read_successor(repo, source)
        assert staged.kind == "successor", (source, staged)
        assert staged.successor_task_id == target
        resolved = resolve_successor(repo, source)
        assert resolved.kind == "successor", (source, resolved)
        assert resolved.successor_task_id == target
        assert "Status: **PENDING DESIGN**" in staged.spec_text
        assert "docs/P20_OWNER_REQUIREMENTS.md" in staged.spec_text
    assert resolve_successor(repo, "P20.5").kind == "end_of_roadmap"

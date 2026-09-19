"""Review models, closed enums, structured findings, and SARIF 2.1.0 serialization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional

REVIEW_SCHEMA_VERSION = 1

REVIEW_MODES = frozenset({"diff", "scan"})
DIFF_MODES = frozenset({"workspace", "range", "commit"})
FINDING_SEVERITIES = frozenset({"blocking", "warning", "info"})
COVERAGE_COMPLETENESS = frozenset({"complete", "partial", "failed", "cancelled"})
SESSION_STATES = frozenset({
    "initialized",
    "preparing",
    "prepared",
    "submitted",
    "running",
    "completed",
    "failed",
    "cancelled",
    "unknown_recovery",
})

SEVERITY_TO_SARIF_LEVEL = {
    "blocking": "error",
    "warning": "warning",
    "info": "note",
}


def _nonblank(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value.strip()


def _normalize_repo_path(raw_path: str, name: str = "file") -> str:
    cleaned = _nonblank(raw_path, name).replace("\\", "/")
    if cleaned.startswith("/") or (len(cleaned) > 1 and cleaned[1] == ":"):
        raise ValueError(f"{name} must be repository-relative: {raw_path!r}")
    posix = PurePosixPath(cleaned)
    if ".." in posix.parts:
        raise ValueError(f"{name} cannot contain path traversal: {raw_path!r}")
    return str(posix)


def compute_finding_fingerprint(
    file: str,
    start_line: int,
    end_line: int,
    rule_id: str,
    category: str,
) -> str:
    raw = f"{_normalize_repo_path(file)}:{int(start_line)}:{int(end_line)}:{rule_id.strip()}:{category.strip()}"
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReviewRequest:
    request_id: str
    project_id: str
    task_id: str
    source_request_id: str
    mode: str = "diff"
    diff_mode: str = "workspace"
    diff_refs: dict[str, str] = field(default_factory=dict)
    scan_roots: list[str] = field(default_factory=lambda: ["."])
    branch: str = ""
    head: str = ""
    status_hash: str = ""
    rule_pack_path: Optional[str] = None
    rule_pack_sha256: Optional[str] = None
    file_limits: dict[str, int] = field(default_factory=lambda: {
        "max_files": 100,
        "max_total_bytes": 1024 * 1024,
        "max_packet_files": 10,
        "max_packet_bytes": 200 * 1024,
    })
    blocking_severities: list[str] = field(default_factory=lambda: ["blocking"])
    transport: str = "local"
    idempotency_key: str = ""
    independent_gates: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _nonblank(self.request_id, "request_id"))
        object.__setattr__(self, "project_id", _nonblank(self.project_id, "project_id"))
        object.__setattr__(self, "task_id", _nonblank(self.task_id, "task_id"))
        object.__setattr__(self, "source_request_id", _nonblank(self.source_request_id, "source_request_id"))
        if self.mode not in REVIEW_MODES:
            raise ValueError(f"unsupported mode {self.mode!r}, must be one of {sorted(REVIEW_MODES)}")
        if self.mode == "diff" and self.diff_mode not in DIFF_MODES:
            raise ValueError(f"unsupported diff_mode {self.diff_mode!r}, must be one of {sorted(DIFF_MODES)}")
        if self.transport not in ("local", "ssh"):
            raise ValueError(f"unsupported transport {self.transport!r}, must be 'local' or 'ssh'")
        norm_roots = [_normalize_repo_path(r, "scan_root") for r in self.scan_roots]
        object.__setattr__(self, "scan_roots", norm_roots)
        if self.rule_pack_path:
            object.__setattr__(self, "rule_pack_path", _normalize_repo_path(self.rule_pack_path, "rule_pack_path"))
        for s in self.blocking_severities:
            if s not in FINDING_SEVERITIES:
                raise ValueError(f"invalid severity {s!r} in blocking_severities")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewRequest:
        allowed = {f for f in cls.__dataclass_fields__}
        filtered = {k: data[k] for k in allowed if k in data and data[k] is not None}
        if "independent_gates" in filtered:
            raw_gates = filtered["independent_gates"]
            if isinstance(raw_gates, dict):
                filtered["independent_gates"] = [str(k) for k in raw_gates.keys()]
            elif isinstance(raw_gates, list):
                filtered["independent_gates"] = [str(g) for g in raw_gates]
        return cls(**filtered)


@dataclass
class ReviewFinding:
    file: str
    start_line: int
    end_line: int
    severity: str
    category: str
    rule_id: str
    message: str
    fingerprint: Optional[str] = None
    rule_provenance: Optional[str] = None
    evidence: Optional[str] = None
    resource_context: Optional[dict[str, Any]] = None
    session_id: str = ""
    packet_id: Optional[str] = None
    status: str = "open"

    def __post_init__(self) -> None:
        self.file = _normalize_repo_path(self.file, "file")
        self.start_line = int(self.start_line)
        self.end_line = int(self.end_line)
        if self.start_line < 1:
            raise ValueError(f"start_line must be >= 1, got {self.start_line}")
        if self.end_line < self.start_line:
            raise ValueError(f"end_line ({self.end_line}) cannot be less than start_line ({self.start_line})")
        if self.severity not in FINDING_SEVERITIES:
            raise ValueError(f"invalid severity {self.severity!r}, must be one of {sorted(FINDING_SEVERITIES)}")
        self.category = _nonblank(self.category, "category")
        self.rule_id = _nonblank(self.rule_id, "rule_id")
        self.message = _nonblank(self.message, "message")
        if not self.fingerprint:
            self.fingerprint = compute_finding_fingerprint(
                self.file, self.start_line, self.end_line, self.rule_id, self.category
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewFinding:
        allowed = {f for f in cls.__dataclass_fields__}
        filtered = {k: data[k] for k in allowed if k in data and data[k] is not None}
        return cls(**filtered)


@dataclass
class ReviewCoverage:
    completeness: str = "complete"
    selected_count: int = 0
    reviewed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    excluded_count: int = 0
    coverage_rate: float = 1.0
    files: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.completeness not in COVERAGE_COMPLETENESS:
            raise ValueError(f"invalid completeness {self.completeness!r}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewCoverage:
        allowed = {f for f in cls.__dataclass_fields__}
        filtered = {k: data[k] for k in allowed if k in data and data[k] is not None}
        return cls(**filtered)


@dataclass
class ReviewManifest:
    manifest_id: str
    ocr_version: Optional[str] = None
    resolved_refs: dict[str, str] = field(default_factory=dict)
    selected_files: list[dict[str, Any]] = field(default_factory=list)
    excluded_files: list[dict[str, Any]] = field(default_factory=list)
    rules: list[dict[str, Any]] = field(default_factory=list)
    input_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewManifest:
        allowed = {f for f in cls.__dataclass_fields__}
        filtered = {k: data[k] for k in allowed if k in data and data[k] is not None}
        return cls(**filtered)


@dataclass
class ReviewPacket:
    packet_id: str
    session_id: str
    packet_index: int
    files: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    broker_request_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewPacket:
        allowed = {f for f in cls.__dataclass_fields__}
        filtered = {k: data[k] for k in allowed if k in data and data[k] is not None}
        return cls(**filtered)


@dataclass
class ReviewResult:
    session_id: str
    job_id: str
    disposition: str
    completeness: str
    findings: list[ReviewFinding] = field(default_factory=list)
    coverage: ReviewCoverage = field(default_factory=ReviewCoverage)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["findings"] = [f.to_dict() if isinstance(f, ReviewFinding) else f for f in self.findings]
        d["coverage"] = self.coverage.to_dict() if isinstance(self.coverage, ReviewCoverage) else self.coverage
        return d

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewResult:
        findings_raw = data.get("findings", [])
        findings = [ReviewFinding.from_dict(f) if isinstance(f, dict) else f for f in findings_raw]
        cov_raw = data.get("coverage")
        cov = ReviewCoverage.from_dict(cov_raw) if isinstance(cov_raw, dict) else ReviewCoverage()
        return cls(
            session_id=str(data["session_id"]),
            job_id=str(data["job_id"]),
            disposition=str(data.get("disposition", "failed")),
            completeness=str(data.get("completeness", "partial")),
            findings=findings,
            coverage=cov,
            artifacts=dict(data.get("artifacts") or {}),
            reason=str(data.get("reason", "")),
        )


@dataclass
class ReviewSession:
    session_id: str
    request: ReviewRequest
    state: str = "initialized"
    job_id: Optional[str] = None
    manifest: Optional[ReviewManifest] = None
    packets: list[ReviewPacket] = field(default_factory=list)
    result: Optional[ReviewResult] = None
    timestamps: dict[str, Any] = field(default_factory=dict)
    failure_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.state not in SESSION_STATES:
            raise ValueError(f"invalid session state {self.state!r}")

    def to_dict(self) -> dict[str, Any]:
        d = {
            "session_id": self.session_id,
            "request": self.request.to_dict(),
            "state": self.state,
            "job_id": self.job_id,
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "packets": [p.to_dict() for p in self.packets],
            "result": self.result.to_dict() if self.result else None,
            "timestamps": self.timestamps,
            "failure_reason": self.failure_reason,
        }
        return d

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewSession:
        req = ReviewRequest.from_dict(data["request"])
        manifest = ReviewManifest.from_dict(data["manifest"]) if data.get("manifest") else None
        packets = [ReviewPacket.from_dict(p) for p in data.get("packets", [])]
        result = ReviewResult.from_dict(data["result"]) if data.get("result") else None
        return cls(
            session_id=str(data["session_id"]),
            request=req,
            state=str(data.get("state", "initialized")),
            job_id=data.get("job_id"),
            manifest=manifest,
            packets=packets,
            result=result,
            timestamps=dict(data.get("timestamps") or {}),
            failure_reason=data.get("failure_reason"),
        )


def to_sarif(
    findings: list[ReviewFinding],
    run_metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Generate deterministic SARIF 2.1.0 document from review findings."""
    rules_dict: dict[str, dict[str, Any]] = {}
    sarif_results: list[dict[str, Any]] = []

    # Sort findings deterministically by file, start_line, end_line, rule_id
    sorted_findings = sorted(
        findings,
        key=lambda f: (f.file, f.start_line, f.end_line, f.rule_id, f.fingerprint or "")
    )

    for f in sorted_findings:
        if f.rule_id not in rules_dict:
            rules_dict[f.rule_id] = {
                "id": f.rule_id,
                "shortDescription": {"text": f.rule_id},
                "defaultConfiguration": {
                    "level": SEVERITY_TO_SARIF_LEVEL.get(f.severity, "warning")
                },
                "properties": {
                    "category": f.category,
                    "provenance": f.rule_provenance,
                }
            }

        res: dict[str, Any] = {
            "ruleId": f.rule_id,
            "level": SEVERITY_TO_SARIF_LEVEL.get(f.severity, "warning"),
            "message": {"text": f.message},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {
                            "uri": f.file,
                            "uriBaseId": "%SRCROOT%",
                        },
                        "region": {
                            "startLine": f.start_line,
                            "endLine": f.end_line,
                        }
                    }
                }
            ],
            "partialFingerprints": {
                "primaryLocationLineHash": f.fingerprint or "",
            },
            "properties": {
                "severity": f.severity,
                "category": f.category,
                "status": f.status,
                "session_id": f.session_id,
            }
        }
        if f.evidence:
            res["locations"][0]["physicalLocation"]["region"]["snippet"] = {
                "text": f.evidence
            }
        if f.resource_context:
            res["properties"]["resource_context"] = f.resource_context

        sarif_results.append(res)

    rules_list = sorted(rules_dict.values(), key=lambda r: str(r["id"]))
    meta = run_metadata or {}

    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "DevOrchestrator-ReviewerHarness",
                        "semanticVersion": "1.0.0",
                        "informationUri": "https://github.com/zhengyi-beijing/DevOrchestrator",
                        "rules": rules_list,
                    }
                },
                "invocations": [
                    {
                        "executionSuccessful": True,
                        "startTimeUtc": meta.get("start_time"),
                        "endTimeUtc": meta.get("end_time"),
                    }
                ],
                "results": sarif_results,
            }
        ],
    }

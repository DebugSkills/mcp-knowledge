"""Quality validation package — Фаза 4: Knowledge Quality.

Содержит:
- issues.py — append-only JSONL store для quality-issues (4.1)
- gates.py — pre-write frontmatter validation gate (4.2)
- scoring.py — staleness_score чистая функция (4.5)
- scanner.py — periodic quality scan оркестрация (4.5)
- edit_war.py — git-based edit-war detection (4.4)
- dup_gate.py — semantic duplicate detection (4.3)
- lifecycle.py — 2-state lifecycle published|deprecated (4.7)
"""

from __future__ import annotations

from .dup_gate import (
    DUP_SIMILARITY_THRESHOLD,
    check_duplicates,
    compute_cosine,
    find_duplicates,
)
from .edit_war import (
    EDIT_WAR_THRESHOLD,
    EDIT_WAR_WINDOW_H,
    detect_all_edit_wars,
    detect_edit_war,
)
from .gates import (
    GateIssue,
    GateResult,
    evaluate_frontmatter,
)
from .issues import (
    Issue,
    IssueSeverity,
    IssueStatus,
    IssueType,
    create_issue,
    get_issues_store_path,
    list_issues,
    update_issue_status,
)
from .lifecycle import (
    build_search_filter,
    get_status,
    make_deprecation_payload_update,
    make_published_payload_update,
    make_restore_payload_update,
    validate_transition,
)
from .scanner import (
    run_scan,
)
from .scoring import (
    REVIEW_THRESHOLD,
    StalenessInput,
    should_review,
    staleness_score,
)

__all__ = [
    "DUP_SIMILARITY_THRESHOLD",
    "EDIT_WAR_THRESHOLD",
    "EDIT_WAR_WINDOW_H",
    "REVIEW_THRESHOLD",
    "GateIssue",
    # gates (4.2)
    "GateResult",
    # issues (4.1)
    "Issue",
    "IssueSeverity",
    "IssueStatus",
    "IssueType",
    # scoring (4.5)
    "StalenessInput",
    "build_search_filter",
    "check_duplicates",
    # dup_gate (4.3)
    "compute_cosine",
    "create_issue",
    "detect_all_edit_wars",
    # edit_war (4.4)
    "detect_edit_war",
    "evaluate_frontmatter",
    "find_duplicates",
    "get_issues_store_path",
    # lifecycle (4.7)
    "get_status",
    "list_issues",
    "make_deprecation_payload_update",
    "make_published_payload_update",
    "make_restore_payload_update",
    # scanner (4.5)
    "run_scan",
    "should_review",
    "staleness_score",
    "update_issue_status",
    "validate_transition",
]

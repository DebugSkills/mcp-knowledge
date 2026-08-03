"""Quality validation package — Фаза 4: Knowledge Quality.

Содержит:
- issues.py — append-only JSONL store для quality-issues (4.1)
- gates.py — pre-write frontmatter validation gate (4.2)
- scoring.py — staleness_score чистая функция (4.5)
- scanner.py — periodic quality scan оркестрация (4.5)
- edit_war.py — git-based edit-war detection (4.4)
"""

from __future__ import annotations

from .issues import (
    Issue,
    IssueType,
    IssueSeverity,
    IssueStatus,
    create_issue,
    list_issues,
    update_issue_status,
    get_issues_store_path,
)
from .gates import (
    GateResult,
    GateIssue,
    evaluate_frontmatter,
)
from .scoring import (
    StalenessInput,
    REVIEW_THRESHOLD,
    staleness_score,
    should_review,
)
from .scanner import (
    run_scan,
)
from .edit_war import (
    detect_edit_war,
    detect_all_edit_wars,
    EDIT_WAR_WINDOW_H,
    EDIT_WAR_THRESHOLD,
)

__all__ = [
    # issues (4.1)
    "Issue",
    "IssueType",
    "IssueSeverity",
    "IssueStatus",
    "create_issue",
    "list_issues",
    "update_issue_status",
    "get_issues_store_path",
    # gates (4.2)
    "GateResult",
    "GateIssue",
    "evaluate_frontmatter",
    # scoring (4.5)
    "StalenessInput",
    "REVIEW_THRESHOLD",
    "staleness_score",
    "should_review",
    # scanner (4.5)
    "run_scan",
    # edit_war (4.4)
    "detect_edit_war",
    "detect_all_edit_wars",
    "EDIT_WAR_WINDOW_H",
    "EDIT_WAR_THRESHOLD",
]

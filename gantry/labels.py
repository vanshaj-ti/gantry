"""Human-friendly labels for Gantry run statuses."""

STATUS_LABELS = {
    "queued": "Queued — waiting on prerequisite run(s)",
    "awaiting_spec": "Awaiting product spec (human)",
    "spec_running": "Writing product spec",
    "spec_complete": "Spec ready for review",
    "spec_failed": "Spec stage errored",
    "awaiting_design": "Awaiting architecture design (human)",
    "design_running": "Writing architecture design",
    "design_complete": "Design ready for review",
    "design_failed": "Design stage errored",
    "awaiting_plan": "Ready to plan",
    "plan_running": "Writing implementation plan",
    "plan_complete": "Plan complete",
    "build_running": "Building & testing",
    "build_complete": "Build complete",
    "evidence_running": "Generating evidence",
    "evidence_complete": "Evidence complete",
    "review_running": "Independent review in progress",
    "review_approved": "Review APPROVED — ready to ship",
    "review_changes_requested": "Review requested changes — rebuilding",
    "review_escalated": "Review ESCALATED — human decision needed",
    "blocked": "Blocked — needs input",
    "checks_high_risk_escalated": "High-risk path touched — human decision needed",
    "checks_escalated": "Checks ESCALATED — auto-retry exhausted",
    "resolve_running": "Resolver agent fixing escalated checks",
    "resolve_escalated": "Resolver ESCALATED — auto-fix exhausted",
    "shipped": "Shipped — PR open",
    "shipped_manually": "Shipped (manual) — PR open",
    "ship_failed": "Ship FAILED — push/PR error",
    "ship_checks_failed": "Ship BLOCKED — re-verification found a real problem",
    "held": "Held — human working on this run manually",
    "cancelled": "Cancelled",
}


def label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


SHORT_STATUS_LABELS = {
    "queued": "Queued",
    "awaiting_spec": "Awaiting spec",
    "spec_running": "Writing spec",
    "spec_complete": "Spec review",
    "spec_failed": "Spec failed",
    "awaiting_design": "Awaiting design",
    "design_running": "Writing design",
    "design_complete": "Design review",
    "design_failed": "Design failed",
    "awaiting_plan": "Ready to plan",
    "plan_running": "Planning",
    "plan_complete": "Plan done",
    "build_running": "Building",
    "build_complete": "Build done",
    "evidence_running": "Evidence",
    "evidence_complete": "Evidence done",
    "review_running": "Reviewing",
    "review_approved": "Approved",
    "review_changes_requested": "Changes requested",
    "review_escalated": "Review escalated",
    "blocked": "Blocked",
    "checks_high_risk_escalated": "High-risk escalated",
    "checks_escalated": "Checks escalated",
    "resolve_running": "Resolving",
    "resolve_escalated": "Resolve escalated",
    "shipped": "Shipped",
    "shipped_manually": "Shipped (manual)",
    "ship_failed": "Ship failed",
    "ship_checks_failed": "Ship blocked",
    "held": "Held",
    "cancelled": "Cancelled",
}


def short_label(status: str) -> str:
    return SHORT_STATUS_LABELS.get(status, status)

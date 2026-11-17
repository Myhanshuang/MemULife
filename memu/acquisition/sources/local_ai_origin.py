"""Native session provenance; internal reviewers are never user conversations."""

REVIEW_PREFIXES = (
    'The following is the Codex agent history whose request action you are assessing.',
    'You are assessing whether the following action is allowed',
)


def session_kind(origin):
    if not isinstance(origin, dict) or 'subagent' not in origin:
        return 'conversation'
    subagent = origin['subagent']
    if isinstance(subagent, dict) and subagent.get('other') in {'guardian', 'approval_reviewer'}:
        return 'internal_review'
    return 'delegated'


def reviewer_prompt(body):
    return isinstance(body, str) and body.lstrip().startswith(REVIEW_PREFIXES)

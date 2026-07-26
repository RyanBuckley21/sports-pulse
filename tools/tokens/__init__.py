"""Design-token tooling for the web frontend.

    python3 -m tools.tokens.audit            measure the token set, exit 1 on violations
    python3 -m tools.tokens.audit --teams    team-colour collisions only
    python3 -m tools.tokens.render_preview   regenerate web/tokens.html
    python3 -m tools.tokens.test_colorkit    verify the colour math

`tokens.py` is the single source of truth for the proposed values; the audit
and the preview page both read from it. Run all three from the repo root.
"""

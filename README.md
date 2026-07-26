# sports-pulse

## Setup

After cloning, point git at the tracked hooks directory (once per clone —
`.git/hooks` is not version-controlled, so this is what makes the hooks travel):

```sh
git config core.hooksPath .githooks
```

This enables a `pre-commit` hook that refuses commits made directly on `main`.
`main` is the deploy branch — pushing to it publishes the site — so it should
only advance by fast-forwarding from a working branch. Override a deliberate
commit with `SP_ALLOW_MAIN_COMMIT=1 git commit ...`.

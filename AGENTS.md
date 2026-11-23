# Repository instructions

- This is a public repository. Never commit credentials, tokens, private keys,
  authentication files, local environment files, or private runtime logs.
- Read credentials only through the configured tool or environment when needed.
  Do not print credential values in commands, diagnostics, examples or reviews.
- Examples must use clearly nonfunctional placeholders or environment references.
- Keep runtime state, task source code from private projects and Codex session data
  outside the source repository.
- Before every commit run `python3 scripts/check_secrets.py`; before every push
  also run `python3 scripts/check_secrets.py --history`.
- Local hooks are enabled with `git config core.hooksPath .githooks`.
  The scanner is a basic guard, not a guarantee; inspect the changed files too.
- If a real credential is detected, stop publishing and ask the owner to revoke
  or rotate it. Never print the matched value or silently rewrite Git history.

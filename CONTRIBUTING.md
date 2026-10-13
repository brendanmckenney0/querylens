# Contributing

All changes land through pull requests. Direct pushes to `main` are
blocked by branch protection.

## Workflow

1. Create a branch from `main`:
   `feature/<short-name>`, `fix/<short-name>`, `docs/<short-name>`,
   `chore/<short-name>`
2. Make focused commits following
   [Conventional Commits](https://www.conventionalcommits.org/):
   - `feat:` — new functionality
   - `fix:` — bug fix
   - `docs:` — documentation only
   - `refactor:` — code change, no behavior change
   - `test:` — tests / eval only
   - `chore:` — tooling, dependencies, repo hygiene
3. Push the branch and open a PR against `main` (`gh pr create`).
4. Fill in the PR template: what, why, how to verify.
5. Merge via squash (`gh pr merge --squash`) after review, then delete
   the branch.

## Standards

- Keep PRs small and single-purpose.
- Update README/docs when behavior changes.
- Run the eval suite before opening the PR.
- Never commit secrets, API keys, or real company data. Fictional demo
  data only.

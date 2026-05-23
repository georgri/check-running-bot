---
name: redeploy-and-push
description: Redeploys the check-running-bot service to VPS and pushes latest safe changes to GitHub main. Use when code changes are finished, especially when the user asks to redeploy, push, or "do both after every change".
---

# Redeploy And Push

## When to use

- User asks to redeploy and push in one flow.
- User says to run this after each code change.
- Code edits are complete and verified.

## Workflow

1. Validate working tree and inspect changes:
   - `git status --short`
   - `git diff`
2. Run a lightweight secret scan before commit/push:
   - Ensure no real tokens/passwords are present in tracked files.
3. Redeploy first:
   - `./deploy.sh`
   - `./service-status.sh`
4. Commit only requested/relevant files:
   - `git add <files>`
   - `git commit -m "<message>"`
5. Push to GitHub main:
   - `GIT_SSH_COMMAND='ssh -i /Users/georgiiriskov/.ssh/id_rsa_georgri_github -o IdentitiesOnly=yes' git push origin main`
6. Confirm final state:
   - `git status --short` (should be clean unless intentionally left dirty)
   - report deploy status and pushed commit hash.

## Commit message style

- Use concise imperative subject.
- Add a short second paragraph for reason/impact when helpful.

## Safety rules

- Never commit `.env`, `state*.json`, or private keys.
- Never print full secrets in output.
- If deploy fails, fix and redeploy before pushing when possible.

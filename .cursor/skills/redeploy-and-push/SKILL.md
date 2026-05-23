---
name: redeploy-and-push
description: Redeploy this project to the configured VPS and push changes to GitHub after code updates. Use when user asks to redeploy, push, ship latest changes, or do both after edits.
---
# Redeploy And Push

## When to use

Use this skill when the user asks to:

- redeploy to VPS
- push latest code to GitHub
- do both in sequence after changes

## Workflow

Run these steps in order:

1. Validate local changes quickly.
2. Redeploy to VPS.
3. Verify service is healthy.
4. Commit changes.
5. Push to `origin/main`.

## Step 1: Validate Local Changes

- Run:
  - `python3 -m py_compile app.py`
  - `git status --short`
- If Python syntax fails, fix before continuing.

## Step 2: Redeploy

- Run:
  - `./deploy.sh`

## Step 3: Verify Runtime On VPS

- Prefer:
  - `./service-status.sh`
- If unavailable, run:
  - `ssh root@77.238.234.181 "systemctl status check-running-bot.service --no-pager"`
  - `ssh root@77.238.234.181 "journalctl -u check-running-bot.service -n 20 --no-pager"`

## Step 4: Commit

- Before commit, ensure no secrets are staged.
- Never commit `.env` or runtime state files.
- Include only relevant changed files.
- Use a commit message that explains intent.

## Step 5: Push

- Push with repository SSH key if needed:
  - `GIT_SSH_COMMAND='ssh -i /Users/georgiiriskov/.ssh/id_rsa_georgri_github -o IdentitiesOnly=yes' git push origin main`

## Safety Rules

- Do not print or commit secret values.
- Keep credentials in VPS `.env` only.
- If deployment fails, report the failure and stop before push unless user asks otherwise.

# GitHub settings this repo expects

These are one-time settings in the GitHub UI (or via `gh`), not code. They turn the
rules in `RUNBOOK.md` into things the platform enforces.

## 1. Protect `main`

Require a pull request, require the CI check, forbid force-push and deletion.

```bash
gh api -X PUT repos/hexarsolutions/crewhex-screen-client/branches/main/protection \
  -f 'required_status_checks[strict]=true' \
  -f 'required_status_checks[contexts][]=ci' \
  -F 'enforce_admins=true' \
  -F 'required_pull_request_reviews[required_approving_review_count]=1' \
  -F 'required_pull_request_reviews[dismiss_stale_reviews]=true' \
  -F 'restrictions=' \
  -F 'allow_force_pushes=false' \
  -F 'allow_deletions=false'
```

## 2. Signing and deploy secrets

| Secret | What it holds | Used by |
|---|---|---|
| `OTA_SIGNING_KEY` | the ed25519 PRIVATE key for screen OTA bundles | `package.yml`, tags only |
| `CREWHEX_DEPLOY_KEY` | a deploy key restricted to `/srv/crewhex/public` | `package.yml`, publishes the signed bundle |

The private key exists in exactly two places: the repo secret and the password
manager. It must never live on a laptop or an agent machine, or whoever owns that
machine can push code to every screen.

## 3. Scanning

- Secret scanning + push protection: Settings → Code security → enable both. This
  matters more on public repos.
- Dependabot alerts and security updates: enabled by `.github/dependabot.yml` here;
  turn alerts on in Settings → Code security.
- Branch protection plus secret scanning is what stops a leaked key being *used*
  even if it is committed.

## 4. The agent's identity

Give automation its own GitHub account (or a fine-grained token) with `contents:
write` on these repos only, no `admin`, and no ability to bypass branch protection.

# sovereign-signal

AI-powered social engagement platform for The Sovereign Signal and StackMotive.

Automates LinkedIn content publishing and engagement with a human-in-the-loop approval loop via Resend email.

## Architecture

- **Publisher agent** — posts approved content to LinkedIn on schedule
- **Listener agent** — watches for comments on Andy's posts, drafts replies
- **Scout agent** — trawls LinkedIn for engagement opportunities, drafts comments
- **Approval loop** — all drafts sent to Andy via Resend email, posted only after reply approval

## Stack

- FastAPI (control plane)
- PostgreSQL (ss_ schema, shared DO managed database)
- Playwright (LinkedIn browser automation)
- Claude API (draft engine)
- Resend (approval email loop)
- Kubernetes on DigitalOcean (prod-cluster, syd1, namespace: sovereign-signal)

## Deploy pattern

Tag → digest → deploy. Andy creates all git tags manually after PR merge. Never create tags in this repo.

## Session handover

Session handovers are stored in the repo root as `SESSION_HANDOVER_N.md`.

# aws-ecs-project

A small FastAPI app deployed to **ECS on Fargate**, with every AWS piece built by hand in the console, a GitHub Actions deploy workflow that can tell a real deploy from a rollback, and a comparison against **ECS Express Mode**.

- **Built:** CI to ECR over OIDC, an ECS on Fargate service, a manual deploy workflow, and deployment-failure alerts
- **Hardest problem:** `aws ecs wait services-stable` passes after a rollback, so a failed deploy can look green
- **Result:** 2 rollbacks tested with the site staying up, 4 min 11 s deploys, $0.24 total cost

**Stack:** ECS Fargate · ECR · GitHub Actions · OIDC · least-privilege IAM · circuit-breaker rollback · EventBridge · SNS · CloudWatch Logs · cost allocation tags

> **Status:** archived and torn down on 2026-11-13. Full write-up: *Built It Before the Button* on my portfolio.

## Problem and goals

Learn to host containers on AWS by building everything by hand first, then decide what's worth automating.

- Run the app on ECS on Fargate, built in the console
- Prove a bad release rolls back on its own while the site stays up
- Deploy from GitHub with no stored AWS keys
- Get an email when a deployment fails
- Compare the hand build against ECS Express Mode

**Out of scope:** EKS (separate project), infrastructure as code, and the dropped stages (ECS on EC2, ECS Managed Instances).

## Architecture

```text
Pull request ──► test ──► build
Merge to main ──► test ──► OIDC ──► push to ECR (tag = full commit SHA, immutable)

Deploy (manual, pick a SHA) ──► OIDC deploy role
   ──► read task definition ──► swap image ──► register revision
   ──► update service ──► poll until COMPLETED on the new revision, or fail

ECS cluster (Fargate only)
   └── service: 1 task, public IP, port 8000 open to one IP
         ├── logs ──► CloudWatch Logs (1 week)
         └── deployment events ──► EventBridge ──► SNS ──► email
```

## Key decisions

| Decision | Chosen | Alternative | Why | Trade-off accepted |
| --- | --- | --- | --- | --- |
| How to build | By hand in the console | Terraform from day one | Learn every field before automating it | Not reproducible |
| Compute | ECS on Fargate | ECS on EC2, EKS | AWS runs the servers | Higher price per vCPU, no host control |
| Traffic | Public task IP, one IP allowed | ALB | An ALB bills hourly even when idle | No HTTPS, new IP per deploy |
| Network | Default VPC, public subnets | Private subnets + NAT or endpoints | NAT and endpoints bill hourly | Tasks reachable from the internet, one IP only |
| Deploy trigger | Manual, with a SHA | Deploy on merge | Choose the version, watch each rollout | Someone starts each deploy |
| Deploy check | Poll rollout + compare revisions | `services-stable` | The wait passes after a rollback | More workflow code |
| Image tags | Full SHA, immutable | `latest` | Deploy exactly the chosen image | Re-pushing a commit fails, by design |
| Alerting | EventBridge → SNS email | CloudWatch alarms | The failure is a deployment event, not a metric | Raw JSON emails, no paging |
| Rollback | Rolling update + circuit breaker | Blue/green | Keeps the old task serving | Only catches tasks that fail to start |
| Budget | $20 lab budget + account-wide budget | One budget | Catches untagged costs | Two budgets to maintain |

## Services

| Layer | Version / setting |
| --- | --- |
| App | Python 3.13, FastAPI ≥ 0.115, uvicorn ≥ 0.30, boto3 ≥ 1.35 |
| Image | `python:3.13-slim`, `linux/amd64`, non-root (uid 10001), port 8000 |
| Compute | ECS on Fargate, 0.25 vCPU / 0.5 GB, platform LATEST, ap-southeast-1 |
| Registry | ECR, private, immutable tags, scan on push |
| Actions | checkout v7.0.1, setup-python v7.1.0, configure-aws-credentials v6.3.0, amazon-ecr-login v2.1.7 (all pinned to full SHAs) |

## Infrastructure

No infrastructure as code, on purpose: every AWS resource was created by hand in the console. Everything carries the cost allocation tag `project=ecs`, with ECS managed tags on and tags propagated from the service. The GitHub workflows only build, push and deploy app versions. They never create or change infrastructure.

## CI/CD

**`ci.yml`** runs on pull requests, pushes to main and manual runs. It starts with `permissions: {}`.

1. **test:** install dev dependencies, `python -m pytest` (9 tests), `docker build --platform linux/amd64`
2. **push** (main only): OIDC → ECR login → build → tag with the full `github.sha` → push

Branch ruleset on main: pull request required, status check `test` required, force pushes blocked, no bypass.

**`deploy.yml`** is a manual `workflow_dispatch` with inputs `cluster`, `service` and `image_tag` (full 40-character SHA).

1. Guards: main only, 40-hex tag, safe names; inputs reach the shell only through `env`
2. Assume the deploy role over OIDC
3. Read the service's task definition, swap only the image of container `app`, register a new revision
4. Update the service
5. Poll every 15 s for up to 15 min. Green only if the service is on the new revision **and** the PRIMARY rollout is `COMPLETED`

One deploy per service at a time (`concurrency` group).

### Run locally

```bash
python -m pytest                      # plain `pytest` can't import app
docker compose up --build             # app on :8000, notes in memory
docker compose run --rm crash         # CRASH_ON_START: exits non-zero
docker compose --profile s3 up --build  # + S3Mock, notes "in S3" on :8001
```

## Reliability

No SLO was defined. The tested rule: a deploy either finishes on the new revision, or rolls back while the old task keeps serving.

**Failures tested**

| Failure | How detected | Recovery | Time to recover |
| --- | --- | --- | --- |
| Release crashes on startup | Circuit breaker, 3 failed tasks | Automatic rollback to previous revision | ~4 min, site stayed up |
| Image tag doesn't exist | Circuit breaker (`CannotPullContainerError`), deploy job red, alert email | Automatic rollback | ~10.5 min, site stayed up |

**Deploy times:** first deploy by hand 2 min 47 s · update by hand 2 min 48 s · workflow 4 min 11 s · Express Mode 8 min 23 s.

**Alerts**

| Rule | Fires on | Action | Tested |
| --- | --- | --- | --- |
| Deployment failed | ECS `SERVICE_DEPLOYMENT_FAILED` for this service | SNS → email | Yes |
| Service action | ECS service event, WARN or ERROR | SNS → email | Built, never triggered |

No CloudWatch alarms.

**Rollback:** tested twice (above), with screenshots.

## Security

- **OIDC:** GitHub assumes AWS roles with short-lived tokens. Trust limited to this repository's main branch
- **Least privilege:** the CI role pushes to one ECR repository. The deploy role updates one service and can pass only the execution role, only to `ecs-tasks.amazonaws.com`. The execution role pulls from one repository, writes to one log group, and has no `CreateLogGroup`
- **Secrets:** none stored; no task role (the app doesn't call AWS)
- **Supply chain:** actions pinned to full SHAs, Dependabot weekly
- **Network:** port 8000 open to one /32
- **Known gap:** both OIDC roles trust the same `main` subject, so `ci.yml` could assume the deploy role. Fix: a GitHub environment on the deploy job, and a deploy role that trusts only `repo:…:environment:production`
- Skipped: HTTPS / ALB (cost)
- Skipped: WAF (no load balancer to attach it to)
- Skipped: private subnets (NAT or VPC endpoints bill hourly)

## Costs

Actual charges from Cost Explorer, 2026-11-03 to 2026-11-13, ap-southeast-1, credits excluded, filtered to tag `project=ecs`.

| Service | Item | Actual |
| --- | --- | --- |
| ECS | Fargate task time | $0.14 |
| Elastic Load Balancing | Express Mode's load balancer, ~80 min | $0.05 |
| VPC | Public IPv4 | $0.05 |
| ECR | Image storage | $0.01 |
| SNS, CloudWatch, ACM | | $0.00 |
| **Total** | | **$0.24** |

Account-wide total: $0.26. The extra $0.02 is public IPv4 without the tag.

## What broke

1. **Deploy run went red, but the deploy worked.** `deploy.yml` → `services-stable` returned ~3 min before the rollout was COMPLETED → replaced it with polling of rollout state and revision → never treat "stable" as "my version shipped".
2. **Express Mode couldn't write logs.** First Express deploy → it picks its own log group, and the execution role only allowed mine → added its log group to the role → check a managed feature's naming before pointing a scoped role at it.
3. **Alert rule that would never fire.** EventBridge console's suggested pattern → matched `clusterArn`, which deployment events don't have; the console-made SNS role trusted only one rule → matched on the service, widened the trust to both rules → fire every new alert once on purpose.

## Evidence

| Claim | Proof |
| --- | --- |
| CI push and branch protection | Screenshots: image in ECR, rejected direct push, blocked failing PR, denied OIDC from another branch |
| First deploy | Screenshots: page with task ID and version, `/healthz`, CloudWatch JSON logs |
| Graceful replacement | Screenshots: service events, SIGTERM and shutdown logs |
| Crash rollback | Screenshots: "Rollback successful", circuit breaker 3/3 |
| Bad tag rollback + alert | Screenshots: failed run, stopped reason, alert email |
| Express Mode | Before/after inventories |
| Cost | Cost Explorer, $0.24 |

## What's missing

- No infrastructure as code
- No SLO, no CloudWatch alarms
- No ALB, HTTPS or second task built by hand (Express Mode built them; I reviewed them)
- Service action alert never triggered
- Shared OIDC trust between CI and deploy roles
- Dropped: S3 task role, ECS Exec, scheduled scaling, Fargate Spot, Container Insights, private subnets with VPC endpoints, ECS on EC2, ECS Managed Instances

## Status and links

Archived and torn down, 2026-11-13. Case study: *Built It Before the Button* on my portfolio.

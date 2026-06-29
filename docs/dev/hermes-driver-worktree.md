# Hermes Driver Worktree Proof

## Purpose

This document captures the minimum safe path for Hermes to make an owned local code/documentation change while preserving the hard publication gate. The point is not that Hermes can type commands. The point is that Hermes can operate inside a constrained local lane, produce reviewable evidence, and stop before remote mutation.

## Trust Boundary

Hermes may own the steering wheel inside the driver worktree:

- create a worktree through the sanctioned helper;
- edit files inside that worktree only;
- run local verification;
- create a local commit on the assigned `hermes/<slug>` branch;
- run push preflight to write an approval request.

Hermes must not own the accelerator to remote:

- no direct `git push`;
- no PR creation;
- no merge;
- no provider, credential, dashboard, gateway, cron, or service mutation;
- no approval-token minting.

Remote publication remains an explicit Josep L4/root-install/token gate.

## Worktree Creation Flow

Use the driver worktree helper, not an ad-hoc `git worktree add`:

```bash
~/.hermes/scripts/hermes-driver-wt.sh new hermes-driver-worktree
```

The helper is load-bearing because it:

1. fetches `fork/main` before branching;
2. creates branch `hermes/hermes-driver-worktree`;
3. bases the branch on remote-tracking `fork/main`, not the dirty live checkout;
4. places the worktree under `~/.hermes/worktrees/hermes-driver/<slug>`;
5. refuses relay-path/path-traversal slug tricks;
6. prints only the clean worktree path on stdout for callers.

## Minimum Local Boundary Checks

After creation, verify the sandbox before editing:

```bash
cd /home/josep/.hermes/worktrees/hermes-driver/hermes-driver-worktree
git rev-parse --show-toplevel
git branch --show-current
git rev-parse HEAD
git rev-parse fork/main
git merge-base HEAD fork/main
git status --short --branch
```

Expected properties:

- path is under `/home/josep/.hermes/worktrees/hermes-driver/`;
- branch is exactly `hermes/hermes-driver-worktree`;
- `HEAD`, `fork/main`, and their merge-base match before edits;
- worktree status is clean before mutation.

## Local Edit and Commit Flow

Make the scoped change inside the worktree only. For this proof, the scoped change is this documentation file:

```text
docs/dev/hermes-driver-worktree.md
```

Then verify and commit locally:

```bash
git status --short --branch
git diff -- docs/dev/hermes-driver-worktree.md
git add docs/dev/hermes-driver-worktree.md
git commit -m "docs: add Hermes driver worktree proof"
git rev-parse HEAD
git status --short --branch
```

A clean status after commit is part of the proof.

## Push-Gate Preflight Flow

Run preflight only:

```bash
~/.hermes/scripts/hermes-push.sh hermes-driver-worktree --preflight
```

The preflight must:

- require the current branch to be `hermes/hermes-driver-worktree`;
- refuse dirty worktrees;
- generate a SHA-bound request id from `branch:HEAD_SHA`;
- write a request JSON under `~/.hermes/push-gate/requests/`;
- display the exact fork-only refspec;
- exit without pushing.

The expected target shape is:

```text
git push https://github.com/jjaavvin-web/hermes-agent.git \
  refs/heads/hermes/hermes-driver-worktree:refs/heads/hermes/hermes-driver-worktree
```

## Learning Closeout Checklist

Before reporting DONE, Hermes must close the loop with evidence rather than vibes:

1. record the worktree path;
2. record branch and commit SHA;
3. record doc section headers or changed-file summary;
4. record verification commands and real outputs;
5. record the preflight `req_id` and request-file path;
6. confirm no push/PR/merge/service/config/auth/provider/cron mutation occurred;
7. record MVMS completion with evidence refs;
8. decide whether the reusable learning belongs in an existing skill patch, a new skill proposal, or no skill.

## Skill Decision Rule

Do not create a generic "use worktrees" skill from this proof. The reusable landmine pattern is narrower:

- branch from `fork/main`, not live dirty HEAD;
- keep driver worktrees outside relay/shared paths;
- constrain branch/path names by slug;
- make publication preflight-only until Josep approval;
- bind approval to the exact commit SHA.

If that pattern is missing from existing Hermes/worktree skills, patch the existing skill or stage a focused skill proposal. If it is already captured, do not create duplicate skill clutter.

## Stop Line

This proof is complete at the preflight request. Stop there. The next command is not another Hermes action; it is Josep's approval gate:

```bash
hermes-approve.sh <req_id>
```

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Distalker is a **Dispatcharr plugin** that adds Stalker/MAG portal support: it syncs portal
channel lists into a native M3U account and resolves short-lived stream links at tune time.
The README documents it for users, CONTRIBUTING.md for whoever changes it, and this file holds
the little that is specific to working here with an agent.

## Commands

Tests, build and release are documented in
[CONTRIBUTING.md](CONTRIBUTING.md). The two rules worth repeating because they
fail silently:

- **No test may touch a real Redis.** Pass a fake client everywhere; the
  `client or get_redis()` default opens a connection that succeeds on a machine
  running Redis and fails in CI. `REDIS_PORT=6390 python3 tests/test_state.py`
  reproduces CI anywhere.
- **`build.sh` archives `HEAD`**, so only committed files ship.

Django is not installed for the tests: they exercise the Django-free paths only,
and code importing models must degrade rather than raise.

## Architecture and constraints

They live in [CONTRIBUTING.md](CONTRIBUTING.md), which is the same material a
human contributor needs: the two halves and why they never meet, Redis as a
cache rather than a database, the plugin being loaded once per process, the
settings panel that never re-reads itself, the two stream-profile lookups, why
there is no Celery task, and the three probes for diagnosing a live install.

Read it before changing anything. Almost every oddity in this codebase is a
workaround for Dispatcharr behaviour that is invisible from our own source, and
the reasoning is recorded there rather than repeated here.

## Conventions

In [CONTRIBUTING.md](CONTRIBUTING.md) too, and they apply to anything written
here: comments and commit messages explain **why**, recording the constraint
that forced a decision rather than narrating the code; workflow changes go in
their own commit; the logos are generated from their SVGs with `cairosvg`.

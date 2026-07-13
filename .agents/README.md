# TrainingOS Parallel Implementation Project

**Created:** 2026-07-10
**Scope:** 12 open GitHub issues organized into 5 sequential phases with 6 parallel Phase 1 branches
**Projected Timeline:** ~1-2 weeks (3 hours elapsed for Phase 1 + sequential phases after)

---

## What Was Created

This directory contains everything you need to execute the 12 GitHub issues in parallel and sequential phases:

### Documentation Files
- **`QUICK-START.md`** — Quick reference card for navigating worktrees, issue details, and commands
- **`parallel-issues-plan.md`** — Full dependency analysis, blocking relationships, and time estimates (source of truth for phase structure)
- **`worktree-guides.md`** — Detailed implementation checklist for each Phase 1 issue
- **`README.md`** — This file

### Git Worktrees (Phase 1 - Ready to Start)
```
worktrees/
├── 61-database-intensity/      [Database migration: add intensity columns]
├── 63-fit-parser-effort/       [FIT parser: extract Garmin effort/mood]
├── 69-training-blocks-phases/  [Domain: define marathon training phases]
├── 43-weather-enrichment/      [Provider: Open-Meteo integration]
├── 47-health-dashboard/        [Dashboard: daily health panels]
└── 49-embedding-retrieval/     [Retrieval: hybrid embedding + FTS search]
```

Each worktree is an independent git branch ready for implementation.

---

## Phase Structure

### Phase 1: Foundation & Independent Features (In Parallel - ~3 hours elapsed)
**6 issues, ~18 hours total effort, can be parallelized**

| Issue | Branch | Effort | Key Files | Notes |
|-------|--------|--------|-----------|-------|
| #61 | `61-database-intensity` | 1-2h | `storage/sql/013_*.sql` | Blocks #62, #64, #65 |
| #63 | `63-fit-parser-effort` | 2-3h | `ingestion/fit.py` | Independent |
| #69 | `69-training-blocks-phases` | 1-2h | `models.py` or config | Independent |
| #43 | `43-weather-enrichment` | 3-4h | `weather/provider.py` | Independent |
| #47 | `47-health-dashboard` | 2-3h | `coach_web.py` | Independent |
| #49 | `49-embedding-retrieval` | 4-5h | `providers/embedding.py` | Independent |

For full dependency details and time estimates across all 5 phases, see `parallel-issues-plan.md`.

---

## Getting Started

### For a Single Issue
```bash
# 1. Pick an issue
# 2. Read the quick start
cat .agents/QUICK-START.md

# 3. Navigate to your worktree
cd /Users/tikeysus/Documents/projects/trainingOS/worktrees/61-database-intensity/

# 4. See the implementation guide
cat ../../.agents/worktree-guides.md | grep -A 50 "^## #61"

# 5. Start implementing (tests first!)
pytest tests/ -v
```

### For Distributing Work (Team)
```bash
# Assign one worktree to each team member
Developer A: worktrees/61-database-intensity/
Developer B: worktrees/63-fit-parser-effort/
Developer C: worktrees/69-training-blocks-phases/
Developer D: worktrees/43-weather-enrichment/
Developer E: worktrees/47-health-dashboard/
Developer F: worktrees/49-embedding-retrieval/

# All work in parallel, targeting master
# After all 6 PRs merge (Phase 1 complete), proceed to Phase 2
```

### For Solo Implementation (Sequential)
```bash
# Tackle Phase 1 in dependency order:
1. #69 (simplest, purely domain)
2. #61 (foundation, enables others)
3. #63 (FIT parsing, self-contained)
4. #43 (weather, self-contained)
5. #47 (dashboard, self-contained)
6. #49 (most complex, but independent)

# After Phase 1 merges, follow sequential phases
```

---

## Documentation Map

```
Read This First
│
├─ QUICK-START.md ──────────────── Grab & go reference
│   (5 min read, has all commands)
│
├─ worktree-guides.md ──────────── Deep dive on implementation
│   (Use this when starting a worktree, has checklist + test templates)
│
└─ parallel-issues-plan.md ─────── Strategic overview
    (Full dependency graph, time estimates, all 12 issues)
```

---

## Dependency Map

```
┌────────────────────────────────────────────────────────────────────┐
│ Phase 1: Foundation & Independent (PARALLEL - ~3h elapsed)          │
├──────┬──────────┬──────────┬──────────┬──────────┬──────────┤
│ #61  │ #63      │ #69      │ #43      │ #47      │ #49      │
│ DB   │ FIT      │ Blocks   │ Weather  │ Dashboard│ Embedding│
│ 1-2h │ 2-3h     │ 1-2h     │ 3-4h     │ 2-3h     │ 4-5h     │
└──┬───┴──────────┴──────────┴──────────┴──────────┴──────────┘
   │
   ├─ #62 (Activity Model) [after #61] ─────┐
   │                                          │
   ├─ #64 (Intensity) [after #62] ───────────┤
   │                                          │
   ├─ #65 (Sync) [after #64] ────────────────┤
   │                                          │
   ├─ #66 (API) [after #65] ─────────────────┤
   │                                          │
   └─ #67, #68 (Frontend) [after #66] ───────┘
```

| Phase | Issues | Duration | Parallelization |
|-------|--------|----------|-----------------|
| **Phase 1** | #61, #63, #69, #43, #47, #49 | ~18h total → **3h elapsed** | 6 parallel |
| **Phase 2** | #62, #64 | 5h total → **5h elapsed** | Sequential (dependency) |
| **Phase 3** | #65 | 2h | Sequential |
| **Phase 4** | #66 | 4h | Sequential |
| **Phase 5** | #67, #68 | 8h → **8h elapsed** | 2 parallel |
| **TOTAL** | **12 issues** | **~37 hours** | **~1-2 weeks** |

*Elapsed time assumes full-time focus; real schedule depends on team availability.*

---

## Troubleshooting

### "I'm confused where to start"
→ Read `QUICK-START.md` first (5 min), then pick an issue.

### "I need implementation details for my issue"
→ Open `worktree-guides.md` and search for `## #<issue-number>`.

### "My tests are failing"
→ Review test fixtures in `tests/conftest.py` and existing test patterns.

### "I need to understand the full architecture"
→ See `docs/` and top-level `CLAUDE.md` in the project.

### "What do I do after I finish Phase 1?"
→ Create a PR, get review, merge to master. Then Phase 2 (#62, #64) becomes unblocked.

---

## Quick Links

- **Repository:** https://github.com/tikeysus/trainingOS
- **Worktrees:** `/Users/tikeysus/Documents/projects/trainingOS/worktrees/`
- **Issue List:** `gh issue list` or https://github.com/tikeysus/trainingOS/issues
- **Full Issue Details:** `gh issue view <ISSUE_NUMBER>`

---

## Summary

- **6 Phase 1 worktrees created and ready**
- **Full dependency analysis documented**
- **Implementation guides with checklists per issue**
- **Quick reference cards for commands and navigation**

**Next Step:** Pick an issue and start implementing. Read `QUICK-START.md` first!

---

*This project structure follows git worktree best practices, conventional commits (CLAUDE.md), and exhaustive test-first development.*

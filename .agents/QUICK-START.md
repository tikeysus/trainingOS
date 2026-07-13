# Phase 1 Parallel Implementation - Quick Start Card

## Worktree Directory Structure
```
/Users/tikeysus/Documents/projects/trainingOS/
├── worktrees/
│   ├── 61-database-intensity/      🗄️  Schema: intensity columns
│   ├── 63-fit-parser-effort/       📊  Parser: Garmin effort/mood
│   ├── 69-training-blocks-phases/  🏋️  Domain: Marathon phases
│   ├── 43-weather-enrichment/      ☁️  Provider: Open-Meteo integration
│   ├── 47-health-dashboard/        ❤️  Dashboard: Daily health panels
│   └── 49-embedding-retrieval/     🔍 Retrieval: Hybrid embedding search
└── .agents/
    ├── parallel-issues-plan.md     (Full dependency analysis)
    ├── worktree-guides.md          (Detailed implementation guides)
    └── QUICK-START.md              (This file)
```

## Phase 1 Issues Status

| Issue | Branch | Effort | Status | Blocker |
|-------|--------|--------|--------|---------|
| #61 | `61-database-intensity` | 1-2h | Ready | #62, #64, #65 |
| #63 | `63-fit-parser-effort` | 2-3h | Ready | None |
| #69 | `69-training-blocks-phases` | 1-2h | Ready | None |
| #43 | `43-weather-enrichment` | 3-4h | Ready | None |
| #47 | `47-health-dashboard` | 2-3h | Ready | None |
| #49 | `49-embedding-retrieval` | 4-5h | Ready | None |

**Total Effort:** ~18 hours (can be parallelized to ~3 hours elapsed time)

## Getting Started with a Worktree

### Example: Start Issue #61
```bash
# 1. Navigate to the worktree
cd /Users/tikeysus/Documents/projects/trainingOS/worktrees/61-database-intensity/

# 2. Verify you're on the right branch
git branch -vv
# Output: * 61-database-intensity 963717b [origin/master: gone] ...

# 3. Review the issue
gh issue view 61

# 4. Read the implementation guide
cat ../../.agents/worktree-guides.md | grep -A 50 "^## #61"

# 5. Start implementing
# Write tests first, then implementation
```

### Example: Work on Any Worktree
```bash
cd /Users/tikeysus/Documents/projects/trainingOS/worktrees/<BRANCH>/

# Make changes, write tests
pytest tests/ -v

# Commit with conventional commits
git commit -m "feat(#<issue>): description of change"

# When done, push and create PR
git push -u origin <branch-name>
gh pr create --title "[<Issue>] Title" --body "Fixes #<issue>\n\n## Checklist\n- [x] All acceptance criteria met\n- [x] Tests pass"
```

## Phase Dependencies

```
Phase 1 (Parallel - ~3 hours elapsed)
├─ #61 (Schema)       ─────┐
├─ #63 (FIT parser)   ─────┤
├─ #69 (Blocks)       ─────┤
├─ #43 (Weather)      ─────┤
├─ #47 (Dashboard)    ─────┤
└─ #49 (Embeddings)   ─────┤
                            ↓
Phase 2 (Sequential - after #61 merges)
├─ #62 (Activity model) depends on #61
├─ #64 (Intensity algo) depends on #62
                            ↓
Phase 3 (Sequential - after #64 merges)
├─ #65 (Sync integration) depends on #64
                            ↓
Phase 4 (Sequential - after #65 merges)
├─ #66 (API endpoints) depends on #65
                            ↓
Phase 5 (Parallel - after #66 merges)
├─ #67 (SvelteKit dashboard)
├─ #68 (Recharts charts) (part of #67)
```

## Recommended Task Breakdown

### Day 1: Phase 1 Implementation (distribute across team)
- **Developer A:** `61-database-intensity`
- **Developer B:** `63-fit-parser-effort`
- **Developer C:** `69-training-blocks-phases`
- **Developer D:** `43-weather-enrichment`
- **Developer E:** `47-health-dashboard`
- **Developer F:** `49-embedding-retrieval`

Or if solo, tackle in this order for learnings:
1. #69 (simplest, purely domain)
2. #61 (foundation for #62)
3. #63 (FIT parsing, can test independently)
4. #43 (weather enrichment, self-contained)
5. #47 (dashboard, self-contained)
6. #49 (most complex, but independent)

### Day 2-4: Phase 2-5 Sequential
After Phase 1 issues merge, proceed through Phases 2-5 in order.

## Key Commands

### Check Worktree Status
```bash
git worktree list
# Shows all worktrees and branches
```

### Switch Between Worktrees
```bash
cd /Users/tikeysus/Documents/projects/trainingOS/worktrees/61-database-intensity/
# (each worktree is independent)
```

### View Issue in Terminal
```bash
gh issue view <ISSUE_NUMBER>
gh issue view 61
```

### Run All Tests
```bash
pytest tests/ -v
pytest tests/test_<feature>.py -v  # Single file
```

### Commit Conventions
```bash
feat(<scope>):  new feature
fix(<scope>):   bug fix
chore(<scope>): maintenance, migrations
test(<scope>):  test additions
docs(<scope>):  documentation
refactor(<scope>): code cleanup

# Example:
git commit -m "feat(#61): add intensity_score column to Activity"
git commit -m "test(#61): verify migration on existing database"
```

### Create PR from Worktree
```bash
git push -u origin <branch-name>
gh pr create --title "[#<issue>] Feature title" \
  --body "Implements #<issue> with full acceptance criteria. Tests pass."
```

## Testing Checklist (per CLAUDE.md)

Before committing any code:

```bash
# 1. Write exhaustive test cases FIRST
pytest tests/test_<feature>.py::test_<case> -v

# 2. Implement feature
# (read CLAUDE.md testing section for fixtures, mocks, DB setup)

# 3. All tests pass
pytest tests/ -v --tb=short

# 4. Code quality
pylint src/trainingos/<module>/ --disable=C0114

# 5. No unwrap() outside tests (if Rust code applies)
# 5. Conventions: const NAMING, snake_case functions

# 6. Commit logical chunks
git log --oneline -5

# 7. Ready to push
git push -u origin <branch>
```

## Support

**Questions about:**
- **Issue details:** `gh issue view <ISSUE>`
- **Implementation guide:** See `.agents/worktree-guides.md`
- **Full dependency analysis:** See `.agents/parallel-issues-plan.md`
- **Repo architecture:** See `docs/` or top-level `CLAUDE.md`
- **Testing setup:** See `tests/conftest.py` and existing test patterns

For FAQ-style troubleshooting ("I'm confused where to start", failing tests, etc.), see `README.md` § Troubleshooting.

## Success Criteria

Phase 1 is complete when:
- [ ] All 6 worktree PRs are merged to master
- [ ] All acceptance criteria from each issue met
- [ ] All tests passing
- [ ] Code follows CLAUDE.md conventions
- [ ] Team notified that Phase 2 (#62) can begin

---

**Total Timeline:** ~1-2 weeks (3 hours elapsed for Phase 1 + sequential phases)


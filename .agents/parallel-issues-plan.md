# Parallel Implementation Plan: 15 Open Issues

## Dependency Analysis & Parallel Execution Strategy

### Phase 1: Foundation & Independent Features (Can run in parallel)
**These 6 issues have no blocking dependencies and can be implemented simultaneously:**

1. **#61: [Database] Add intensity and Garmin self-evaluation columns**
   - Foundation migration: `intensity_score`, `perceived_effort`, `perceived_mood`
   - No dependencies
   - Blocks: #62, #64, #65
   - Est. effort: 1-2 hours

2. **#63: [FIT Parser] Extract Garmin self-evaluation (effort and mood)**
   - Extract from Garmin FIT files
   - Independent of #61 (can develop in parallel)
   - Feeds into: #62, #64
   - Est. effort: 2-3 hours

3. **#69: [Training Blocks] Define Phase 2 and Phase 3 marathon training plans**
   - Domain data definition
   - Independent of other issues
   - Est. effort: 1-2 hours

4. **#43: [Weather Enrichment] Implement Open-Meteo integration**
   - Weather provider interface + Open-Meteo implementation
   - Independent enrichment pipeline
   - Est. effort: 3-4 hours

5. **#47: [Dashboard] Daily health panels (sleep, HRV, resting HR, Body Battery)**
   - Reporting view + coach UI panels
   - Depends on existing daily_health table
   - Est. effort: 2-3 hours

6. **#49: [Retrieval] Embedding-based search with FTS hybrid re-rank**
   - EmbeddingProvider implementation + hybrid retrieval
   - Can use mocked embeddings if Ollama unavailable
   - Est. effort: 4-5 hours

### Phase 2: Model & Analytics (After Phase 1)
**Depends on #61 being merged:**

7. **#62: [Activity Model] Update Activity dataclass for intensity and mood**
   - Depends on: #61 (schema exists)
   - Feeds into: #64, #65
   - Est. effort: 1-2 hours

8. **#64: [Analytics] Implement intensity score computation module**
   - Depends on: #62 (Activity model has new fields)
   - Formula: pace (25%) + HR zone (25%) + effort (30%) + weather (10%) + mood (10%)
   - Est. effort: 2-3 hours

### Phase 3: Integration (After Phase 2)
**Depends on #64 being merged:**

9. **#65: [Sync] Integrate intensity score computation into daily_sync**
   - Depends on: #64 (intensity computation ready)
   - Calls compute_intensity_score() during normalization
   - Est. effort: 1-2 hours

### Phase 4: API Layer (After Phase 3)
**Depends on #65 being merged:**

10. **#66: [API] Add 7 new endpoints for activities, summaries, training blocks**
    - Depends on: #61, #64, #65 (data ready)
    - Endpoints: activities, activity/{id}, weekly-summaries, training-blocks, PATCH activity
    - Est. effort: 3-4 hours

### Phase 5: Frontend (After Phase 4)
**Depends on #66 being merged:**

11. **#67: [Frontend] Build SvelteKit dashboard with MDX markdown structure**
    - Depends on: #66 (API endpoints)
    - Single-page layout, glassmorphism, time navigation, filters
    - Est. effort: 6-8 hours

12. **#68: [Charts] Implement Recharts components for distance, pace, HR, zones**
    - Part of #67 implementation
    - Charting library for dashboard
    - Est. effort: included in #67

## Worktree Structure

```
trainingOS/
├── master (main branch)
├── worktrees/
│   ├── 61-database-intensity/     (Phase 1)
│   ├── 63-fit-parser-effort/       (Phase 1)
│   ├── 69-training-blocks-phases/  (Phase 1)
│   ├── 43-weather-enrichment/      (Phase 1)
│   ├── 47-health-dashboard/        (Phase 1)
│   ├── 49-embedding-retrieval/     (Phase 1)
│   ├── 62-activity-model/          (Phase 2, ready after #61)
│   ├── 64-intensity-analytics/     (Phase 2, ready after #62)
│   ├── 65-sync-intensity/          (Phase 3, ready after #64)
│   ├── 66-api-endpoints/           (Phase 4, ready after #65)
│   ├── 67-sveltekit-dashboard/     (Phase 5, ready after #66)
│   └── 68-recharts-charts/         (Phase 5, part of #67)
```

## Recommended Execution

### Immediate (Now)
- Create Phase 1 worktrees (6 branches) — all can proceed independently
- Assign team members or work sequentially

### Workflow per Branch
1. Create worktree: `git worktree add -b <branch-name> worktrees/<branch-name>`
2. Implement feature per issue acceptance criteria
3. Test thoroughly (write tests FIRST per CLAUDE.md)
4. Commit with conventional commits (feat:, fix:, chore:, test:)
5. Create PR with: issue #, acceptance criteria checklist, test summary
6. Code review & merge
7. Notify dependent phases (e.g., when #61 merges, #62 can start)

### Critical Dependencies to Monitor
- **#61 → #62**: Schema migration must be applied before Activity model can reflect new columns
- **#62 → #64**: Activity dataclass must have intensity fields before analytics module can use them
- **#64 → #65**: Intensity computation must be unit-tested before sync integration can use it
- **#65 → #66**: Data must be normalized in sync before API can expose it
- **#66 → #67**: All 7 endpoints must be stable before frontend starts consuming them

## Time Estimate

| Phase | Issues | Parallel | Sequential | Total |
|-------|--------|----------|-----------|-------|
| Phase 1 | 6 | 2-3 hrs × 6 parallel = ~3 hrs | N/A | 3 hours |
| Phase 2 | 2 | 1-2 + 2-3 hrs = ~3 hrs | After Phase 1 | 3 hours |
| Phase 3 | 1 | 1-2 hrs | After Phase 2 | 2 hours |
| Phase 4 | 1 | 3-4 hrs | After Phase 3 | 4 hours |
| Phase 5 | 2 | 6-8 hrs (parallel) | After Phase 4 | 8 hours |
| **TOTAL** | **12 issues** | | | **~20 hours** |

**Note:** With parallel Phase 1 work, total elapsed time ≈ 1-2 weeks (depending on team size and capacity).


# Worktree Rebase Guide: Claude-Only Provider Alignment

All 6 Phase 1 worktrees are based on commit `963717b` and need to be rebased onto `master` (`a16f41d`) to align with the Claude-exclusive provider vision.

**What changed on master:**
- ✅ Updated `/docs/coach.md` — explicit "Claude via Anthropic API" language
- ✅ Updated `/docs/architecture.md` — removed hedging about "future provider integrations"
- ✅ Updated `/CLAUDE.md` — clarified "Claude API exclusively"
- ✅ Updated `/.agents/parallel-issues-plan.md` — removed Ollama fallback references
- ✅ Updated `/.agents/worktree-guides.md` — changed #49 from Ollama to Anthropic embeddings
- ✅ Deprecated `/.agents/skills/add-ai-provider/SKILL.md`

**What each worktree needs to do:**
1. Rebase onto master
2. Clean up `tests/test_providers.py` to remove Ollama references
3. Resolve any conflicts

---

## Automated Rebase Script

Run this script from the repository root to rebase all 6 worktrees:

```bash
#!/bin/bash

WORKTREES=(
  "43-weather-enrichment"
  "47-health-dashboard"
  "49-embedding-retrieval"
  "61-database-intensity"
  "63-fit-parser-effort"
  "69-training-blocks-phases"
)

REPO_ROOT="/Users/tikeysus/Documents/projects/trainingOS"

for wt in "${WORKTREES[@]}"; do
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "Rebasing: $wt"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  
  cd "$REPO_ROOT/worktrees/$wt"
  
  # Fetch latest master
  git fetch origin master
  
  # Rebase onto master
  git rebase origin/master || {
    echo "⚠️  Rebase conflict in $wt — please resolve manually"
    echo "   Run: git rebase --continue (after resolving)"
    exit 1
  }
  
  echo "✅ Rebased: $wt"
done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "All worktrees rebased successfully"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
```

---

## Manual Rebase Steps (for each worktree)

### Step 1: Enter worktree
```bash
cd worktrees/49-embedding-retrieval  # (or whichever)
git status  # Verify branch
```

### Step 2: Rebase onto master
```bash
git fetch origin master
git rebase origin/master
```

### Step 3: Resolve conflicts (if any)
Most conflicts will likely be in:
- `tests/test_providers.py` — Ollama test imports/methods vs. updated Anthropic code
- `tests/test_config.py` — Ollama config vars vs. Anthropic config

**If conflict occurs:**
```bash
# View conflict markers
git diff

# Edit file to resolve (keep Anthropic, remove Ollama)
vim tests/test_providers.py

# Mark as resolved
git add tests/test_providers.py

# Continue rebase
git rebase --continue
```

### Step 4: Clean up test_providers.py in this worktree

**Remove these Ollama imports:**
```python
# DELETE these imports:
OllamaChatProvider,
OllamaEmbeddingProvider,
check_ollama_health,
```

**Remove these test methods** (grep for "ollama"):
```python
# DELETE these entire test methods:
test_ollama_chat_provider_maps_non_streaming_response()
test_ollama_embedding_provider_maps_response()
test_ollama_timeout_maps_to_sanitized_timeout_error()
test_ollama_connection_error_maps_to_unavailable()
test_ollama_http_errors_are_normalized()
test_ollama_malformed_json_maps_to_malformed_response()
test_ollama_health_reports_available_model()
test_ollama_health_reports_missing_service_and_model()
```

**Remove Ollama from test_config.py:**
```python
# DELETE these config tests:
TRAININGOS_AI_PROVIDER=ollama
TRAININGOS_OLLAMA_BASE_URL
TRAININGOS_OLLAMA_CHAT_MODEL
TRAININGOS_OLLAMA_EMBEDDING_MODEL
```

### Step 5: Commit cleanup
```bash
git add tests/test_providers.py tests/test_config.py
git commit -m "chore: Remove Ollama provider references, align with Claude-only vision"
```

### Step 6: Verify
```bash
# Tests should still pass
pytest tests/test_providers.py -v
pytest tests/test_config.py -v

# Check no Ollama references remain
grep -r "ollama\|Ollama" tests/ src/  # Should return empty
```

---

## Worktree-Specific Notes

### #49: Embedding-Retrieval
- **Most affected** — this is the embeddings worktree
- Update `tests/test_providers.py` to test `AnthropicEmbeddingProvider` instead of `OllamaEmbeddingProvider`
- The updated `.agents/worktree-guides.md` now references `AnthropicEmbeddingProvider` — use this as the implementation target

### #43, #47, #61, #63, #69
- Light Ollama references (mostly in inherited test fixtures)
- Rebase cleanly; remove Ollama imports and tests
- No special handling needed

---

## Verification Checklist

After rebasing each worktree:

- [ ] `git log -1` shows commit after `a16f41d` (on master timeline)
- [ ] `git diff master -- tests/test_providers.py` shows only Anthropic tests
- [ ] `grep -r "ollama" tests/ src/` returns empty
- [ ] `pytest tests/ -v` passes locally
- [ ] `.agents/worktree-guides.md` in the worktree matches master version

---

## If Rebase Fails

**Abort and try again:**
```bash
git rebase --abort
git fetch origin master
git rebase origin/master
```

**If conflicts are complex:**
1. Note which files conflict
2. Cherry-pick relevant commits manually onto a fresh branch from master
3. Delete the old worktree: `git worktree remove worktrees/<name>`
4. Create fresh: `git worktree add -b <branch-name> worktrees/<branch-name>`

---

## After All Rebases Complete

1. Each worktree is now aligned with Claude-only vision
2. Next: Implement features per updated `.agents/worktree-guides.md` (which now references Anthropic APIs)
3. When PR is opened, verify:
   - No Ollama references in diff
   - Tests pass
   - Docs reference Claude/Anthropic correctly


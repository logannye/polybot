# Lean Into Winners — Config Tuning Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Increase frequency and size of the two profitable strategies (MR big moves and snipe) via four config-only changes.

**Architecture:** All changes are .env overrides — no code modifications. The bot reads pydantic Settings from .env on startup. Two existing .env keys get new values, two new keys are added.

**Tech Stack:** .env config, launchctl restart

---

### Task 1: Config Changes

**Files:**
- Modify: `.env`

Four changes, all in `.env`:

- [ ] **Step 1: Expand MR scanner coverage from 600 to 2000 markets**

In `.env`, add this new line (after the existing MR settings):

```
MR_HISTORY_MAX_MARKETS=2000
```

This triples the price history scanner coverage from 13% to ~42% of Polymarket's 4,758 markets, catching more big-move opportunities.

- [ ] **Step 2: Cut MR cooldown from 6h to 3h**

In `.env`, change:

```
MR_COOLDOWN_HOURS=6
```

To:

```
MR_COOLDOWN_HOURS=3
```

Allows re-entry on volatile markets that have a second independent overreaction within the same session.

- [ ] **Step 3: Raise snipe entry cap from 2 to 4 per market**

In `.env`, change:

```
SNIPE_MAX_ENTRIES_PER_MARKET=2
```

To:

```
SNIPE_MAX_ENTRIES_PER_MARKET=4
```

The current cap of 2 is actively blocking — snipe finds 1 candidate every 60s but can't trade because it already has 2 entries. Raising to 4 captures more convergence on active resolution days (March 30 showed 167 trades were possible across 11 markets).

- [ ] **Step 4: Add second-tier Kelly boost for giant moves (>25% → 1.6x)**

In `.env`, add this new line:

```
MR_BIG_MOVE_KELLY_BOOST=1.6
```

This raises the Kelly boost from the code default of 1.3x to 1.6x. With base Kelly 0.35, effective Kelly becomes 0.56x on moves >15%. The data shows the single 25%+ move produced +$22.54 — the biggest winner by far. The 1.6x boost is still below Kelly-optimal for a 67% win rate (which supports ~0.70x).

- [ ] **Step 5: Commit**

```bash
cd ~/polybot && git add polybot/core/config.py && git commit -m "$(cat <<'EOF'
tune: expand MR scanner, cut cooldown, raise snipe cap, boost Kelly

- MR scanner: 600 → 2000 markets (catch more big moves)
- MR cooldown: 6h → 3h (allow re-entry on volatile markets)
- Snipe entry cap: 2 → 4 per market (unblock convergence trades)
- MR Kelly boost: 1.3x → 1.6x on >15% moves (lean into winners)

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

Note: .env is gitignored (contains secrets), so only config.py default changes (if any) would be committed. If no config.py defaults changed, skip the commit.

---

### Task 2: Restart and Verify

**Files:** None (operational)

- [ ] **Step 1: Restart the bot**

```bash
launchctl kickstart -k gui/$(id -u)/ai.polybot.trader
```

- [ ] **Step 2: Verify scanner coverage expanded**

```bash
sleep 15 && grep "price_history_scan_complete" ~/polybot/data/polybot_stdout.log | tail -3
```

Expected: `"scanned": 2000` (was 600).

- [ ] **Step 3: Verify snipe can trade past old cap**

```bash
grep "snipe_entry_cap" ~/polybot/data/polybot_stdout.log | tail -3
```

Expected: `"max": 4` (was 2). If the single candidate still has 2 entries, it should now be allowed to enter 2 more.

- [ ] **Step 4: Verify MR cooldown is 3h**

Watch for MR activity — if a market that was recently traded (>3h ago) triggers again, it should be allowed through.

```bash
grep "mr_trade\|mr_rejected" ~/polybot/data/polybot_stdout.log | tail -5
```

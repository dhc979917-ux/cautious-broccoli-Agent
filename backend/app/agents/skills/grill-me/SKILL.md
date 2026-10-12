---
name: grill-me
description: Relentlessly interview the user about a plan, decision, or idea until every branch of the design tree is resolved. Use when the user wants to stress-test their thinking, validate a plan or design before implementation, or uses any 'grill' trigger phrases.
required_role: viewer
---

# Grill Me

Relentlessly sharpen a plan or design by interviewing the user until every branch of the design tree is resolved. Adapted from Matt Pocock's grill-me / grilling skills (MIT, https://github.com/mattpocock/skills).

## How to run this skill

When the user triggers grill (says "grill" / "拷问" / wants to stress-test a plan), do it in **one** step — never more:

1. Call `reset_tools` **once**, activating this skill group together with everything it needs in a single replace call:
   `{"skills_grill_me": true, "file_ops": true, "interview": true}`
   - Do NOT call reset_tools empty first and then again — bundle all needed groups in one call.
   - Do NOT Glob/Read this SKILL.md: once the `interview` group is active, the full methodology is already in your system prompt under **Interview Rules**. This file is only the trigger.

2. Then follow the **Interview Rules** injected by the `interview` group: each round call `CollectRequirements` with `mode="interview"`, throwing the whole **frontier** of currently-answerable decisions (usually 3-8 questions, each with `assume` = your recommended answer). Use `allow_other=true` on choice/multi so the user can answer outside your options; use `multiline=true` on open text decisions. Wait for the user's answers, recompute the frontier, call CollectRequirements again for the next round.

## Design tree (summary)

Map the session as a design tree: every decision branches into the decisions that hang off it. Each round, the **frontier** = decisions whose prerequisites are already settled (questions you can ask now without guessing at answers you haven't heard yet). Ask the whole frontier in one CollectRequirements form. A question whose answer depends on another still-open question belongs to a later round, not this one.

## Facts are your job, never the user's

When a frontier question needs a fact from the environment (filesystem, tools, code, docs), look it up yourself; do not ask the user for anything you could find on your own. Put only **decisions** to the user.

## When the session is done

Done when the frontier is empty: every branch visited, nothing silently assumed. Do not act on anything until the user confirms you have reached a shared understanding.

# Instructions for AI Agent: Commit Message Language

## Core Rule
**ALL commit messages (subject and body) MUST be written in English.** No transliteration
("Pol'zovatelskie stroki..."), no Cyrillic, no mixed language. This applies to every
commit created by the agent or requested from the user's changes.

## Format
- Subject: imperative mood, <= 72 chars, no trailing period
  (e.g. "Fix early meeting start caused by double timezone shift").
- Body (optional): explains WHAT and WHY, bullet points allowed, also in English.
- Code identifiers, file paths, API routes, env vars, branch names stay as-is
  (they are English by nature); quoted user-facing UI strings may stay in their
  original language only inside quotes when essential to the meaning.

## Rewriting old messages
Only rewrite history for commits not yet pushed to a shared remote, using
`git rebase -i` (reword) or `git filter-branch --msg-filter`.

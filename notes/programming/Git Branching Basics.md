# Git Branching Basics

A branch is a movable pointer to a commit. Create one per feature with `git switch -c feature-name`, commit small, then merge or rebase back to main. Keep main always working. Use .gitignore to keep secrets, caches and virtual environments out of history, as described in [[Python Virtual Environments]]. Small commits make [[Code Review Habits]] much easier.

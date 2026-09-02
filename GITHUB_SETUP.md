This document explains how to create the GitHub repository for this project in the Arizona-Beverages-USA-LLC organization.

Prerequisites
- Install Git and the GitHub CLI (`gh`).
- Authenticate: `gh auth login`.
- You must have permission to create repositories in the `Arizona-Beverages-USA-LLC` organization.

Quick automated creation (PowerShell)
From the repository root folder run:

```powershell
pwsh ./create_github_repo.ps1 -Org "Arizona-Beverages-USA-LLC" -Repo "arizona-copilot-shared-libs" -Visibility "public" -Description "Shared libraries for Arizona Copilot agents"
```

Manual GH CLI command

```bash
gh repo create Arizona-Beverages-USA-LLC/arizona-copilot-shared-libs --public --source . --remote origin --push --description "Shared libraries for Arizona Copilot agents"
```

Notes
- If you do not have permission to create org repos, ask an organization admin to run the command or to create the repo and add you as a collaborator.
- After creation, the script will push the current directory as the initial commit to `origin`.

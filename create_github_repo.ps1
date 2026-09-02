param(
    [string]$Org = "Arizona-Beverages-USA-LLC",
    [string]$Repo = "arizona-copilot-shared-libs",
    [string]$Visibility = "public",
    [string]$Description = "Shared libraries for Arizona Copilot agents"
)

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Error "GitHub CLI (gh) not found. Install from https://cli.github.com/ and run 'gh auth login' to authenticate."
    exit 1
}

$fullRepo = "$Org/$Repo"
$visFlag = if ($Visibility -eq "private") { "--private" } else { "--public" }

# Check if repo already exists
$exists = gh repo view $fullRepo 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Repository $fullRepo already exists on GitHub."
    exit 0
}

# Create repo (requires rights in the organization) and push current directory
Write-Host "Creating repository $fullRepo with visibility '$Visibility'..."
gh repo create $fullRepo $visFlag --description "$Description" --source . --remote origin --push

if ($LASTEXITCODE -eq 0) {
    Write-Host "Created and pushed to $fullRepo"
} else {
    Write-Error "Failed to create or push to $fullRepo. Check permissions and gh auth status."
}

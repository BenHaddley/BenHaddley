#!/usr/bin/env python3
"""
Fetches BenHaddley's public GitHub statistics and bakes them into
dark_mode.svg / light_mode.svg by filling in the {{PLACEHOLDER}} tokens
found in templates/profile_dark.svg and templates/profile_light.svg.

Auth: uses the token in GITHUB_TOKEN. In the GitHub Actions workflow this
is the repo-scoped ${{ secrets.GITHUB_TOKEN }} -- no personal access token
is required. Because that token authenticates as the workflow bot rather
than as BenHaddley personally, GitHub only returns PUBLIC data for it:
public repos, public stars, public contribution counts, and public commit
history. That matches what this card displays (see README / summary for
the exact limitation).

Run locally for testing with:
    GITHUB_TOKEN=$(gh auth token) python3 scripts/update_stats.py
"""

import datetime
import os
import sys
import time

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(REPO_ROOT, "templates")
CACHE_DIR = os.path.join(REPO_ROOT, "cache")

USERNAME = os.environ.get("PROFILE_USERNAME", "BenHaddley")
TOKEN = os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    sys.exit("GITHUB_TOKEN is not set")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
}
GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com"

# Safety cap so a repo with a huge commit history can't blow out the
# workflow runtime on a cold cache. Repos over this size keep whatever LOC
# figure is already cached (0 the very first time) rather than the job
# hanging for tens of minutes on a single repo.
MAX_COMMITS_PER_REPO = 400

CACHE_FILE = os.path.join(CACHE_DIR, f"{USERNAME.lower()}.cache")


def graphql(query, variables=None):
    resp = requests.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables or {}},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


def get_created_at():
    query = """
    query($login: String!) {
      user(login: $login) { createdAt }
    }
    """
    return graphql(query, {"login": USERNAME})["user"]["createdAt"]


def get_followers():
    query = """
    query($login: String!) {
      user(login: $login) { followers { totalCount } }
    }
    """
    return graphql(query, {"login": USERNAME})["user"]["followers"]["totalCount"]


def get_repos_and_stars():
    """Public, non-fork repositories owned by USERNAME (paginated)."""
    query = """
    query($login: String!, $after: String) {
      user(login: $login) {
        repositories(first: 100, after: $after, ownerAffiliations: OWNER,
                      isFork: false, privacy: PUBLIC) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes {
            name
            nameWithOwner
            stargazerCount
            defaultBranchRef {
              target {
                ... on Commit { history { totalCount } }
              }
            }
          }
        }
      }
    }
    """
    repos, stars, total = [], 0, 0
    after = None
    while True:
        data = graphql(query, {"login": USERNAME, "after": after})["user"]["repositories"]
        total = data["totalCount"]
        for node in data["nodes"]:
            stars += node["stargazerCount"]
            repos.append(node)
        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]
    return total, stars, repos


def get_contributed_to_count():
    query = """
    query($login: String!) {
      user(login: $login) {
        repositoriesContributedTo(first: 1,
          contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, REPOSITORY]) {
          totalCount
        }
      }
    }
    """
    return graphql(query, {"login": USERNAME})["user"]["repositoriesContributedTo"]["totalCount"]


def get_public_commit_contributions(created_at):
    """Sum totalCommitContributions across every year since account
    creation. Only public commits are visible to a non-owner token such as
    the workflow's GITHUB_TOKEN -- private-repo commits are not counted."""
    start = datetime.datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc
    )
    now = datetime.datetime.now(datetime.timezone.utc)

    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          totalCommitContributions
        }
      }
    }
    """

    total = 0
    year_start = start
    while year_start < now:
        year_end = min(year_start + datetime.timedelta(days=365), now)
        data = graphql(
            query,
            {
                "login": USERNAME,
                "from": year_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": year_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )["user"]["contributionsCollection"]
        total += data["totalCommitContributions"]
        year_start = year_end
    return total


def load_cache():
    cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) == 4:
                    cache[parts[0]] = (int(parts[1]), int(parts[2]), int(parts[3]))
    return cache


def save_cache(cache):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        for repo, (commit_count, added, deleted) in sorted(cache.items()):
            f.write(f"{repo},{commit_count},{added},{deleted}\n")


def repo_loc(repo, cache):
    """Additions/deletions authored by USERNAME on a repo's default branch.
    Cached by commit count so a repo with no new commits since last run
    costs zero extra API calls."""
    name = repo["nameWithOwner"]
    branch_ref = repo.get("defaultBranchRef")
    if not branch_ref or not branch_ref.get("target"):
        return 0, 0

    commit_count = branch_ref["target"]["history"]["totalCount"]
    cached = cache.get(name)
    if cached and cached[0] == commit_count:
        return cached[1], cached[2]

    if commit_count > MAX_COMMITS_PER_REPO and not cached:
        print(f"  skipping LOC for {name}: {commit_count} commits exceeds "
              f"the {MAX_COMMITS_PER_REPO}-commit cold-cache safety cap", file=sys.stderr)
        cache[name] = (commit_count, 0, 0)
        return 0, 0

    added = deleted = 0
    page = 1
    while True:
        resp = requests.get(
            f"{REST_URL}/repos/{name}/commits",
            headers=HEADERS,
            params={"author": USERNAME, "per_page": 100, "page": page},
            timeout=30,
        )
        if resp.status_code == 409:
            break  # empty repository
        resp.raise_for_status()
        commits = resp.json()
        if not commits:
            break

        for c in commits:
            detail = requests.get(
                f"{REST_URL}/repos/{name}/commits/{c['sha']}", headers=HEADERS, timeout=30
            )
            if detail.status_code != 200:
                continue
            stats = detail.json().get("stats", {})
            added += stats.get("additions", 0)
            deleted += stats.get("deletions", 0)
            time.sleep(0.02)

        if len(commits) < 100:
            break
        page += 1

    cache[name] = (commit_count, added, deleted)
    return added, deleted


def get_loc(repos):
    cache = load_cache()
    total_added = total_deleted = 0
    for repo in repos:
        added, deleted = repo_loc(repo, cache)
        total_added += added
        total_deleted += deleted
    save_cache(cache)
    return total_added, total_deleted, total_added - total_deleted


def fmt(n):
    return f"{n:,}"


def render(template_path, out_path, values):
    with open(template_path) as f:
        svg = f.read()
    for key, val in values.items():
        svg = svg.replace("{{" + key + "}}", val)
    remaining = [tok for tok in ("{{OS}}", "{{ROLE}}", "{{EDITOR}}", "{{REPOS}}", "{{COMMITS}}",
                                  "{{STARS}}", "{{FOLLOWERS}}", "{{CONTRIBUTED}}", "{{LOC}}",
                                  "{{ADDITIONS}}", "{{DELETIONS}}") if tok in svg]
    if remaining:
        raise RuntimeError(f"{out_path}: unresolved placeholders {remaining}")
    with open(out_path, "w") as f:
        f.write(svg)


def main():
    print(f"Collecting public stats for {USERNAME}...")

    created_at = get_created_at()
    followers = get_followers()
    repo_count, stars, repos = get_repos_and_stars()
    contributed_to = get_contributed_to_count()
    commits = get_public_commit_contributions(created_at)
    added, deleted, net = get_loc(repos)

    values = {
        "OS": "Arch Linux / Debian",
        "ROLE": "Signaller / Network Engineer",
        "EDITOR": "Neovim, VS Code",
        "REPOS": fmt(repo_count),
        "COMMITS": fmt(commits),
        "STARS": fmt(stars),
        "FOLLOWERS": fmt(followers),
        "CONTRIBUTED": fmt(contributed_to),
        "LOC": fmt(net),
        "ADDITIONS": fmt(added),
        "DELETIONS": fmt(deleted),
    }

    for label, value in values.items():
        print(f"  {label}: {value}")

    render(os.path.join(TEMPLATES_DIR, "profile_dark.svg"),
           os.path.join(REPO_ROOT, "dark_mode.svg"), values)
    render(os.path.join(TEMPLATES_DIR, "profile_light.svg"),
           os.path.join(REPO_ROOT, "light_mode.svg"), values)
    print("Wrote dark_mode.svg and light_mode.svg")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Pulls live GitHub stats via the GraphQL/REST APIs and writes them into the
id'd <text> elements of dark_mode.svg / light_mode.svg.

Requires env var GITHUB_TOKEN (a PAT with 'repo' and 'read:user' scopes).
Optional env var GITHUB_ACTOR overrides the username (defaults to USERNAME below).
"""

import os
import time
import hashlib
import datetime
import xml.etree.ElementTree as ET

import requests

USERNAME = os.environ.get("GITHUB_ACTOR") or os.environ.get("PROFILE_USERNAME", "BenHaddley")
TOKEN = os.environ["GITHUB_TOKEN"]

HEADERS = {"Authorization": f"bearer {TOKEN}"}
GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com"

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
CACHE_FILE = os.path.join(CACHE_DIR, f"{USERNAME.lower()}.cache")

SVG_FILES = ["dark_mode.svg", "light_mode.svg"]

ET.register_namespace("", "http://www.w3.org/2000/svg")


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


def get_user_id_and_created_at():
    query = """
    query($login: String!) {
      user(login: $login) {
        id
        createdAt
      }
    }
    """
    data = graphql(query, {"login": USERNAME})["user"]
    return data["id"], data["createdAt"]


def get_followers():
    query = """
    query($login: String!) {
      user(login: $login) {
        followers { totalCount }
      }
    }
    """
    return graphql(query, {"login": USERNAME})["user"]["followers"]["totalCount"]


def get_repos_and_stars():
    """Sum stargazers across all repos the user owns (paginated)."""
    query = """
    query($login: String!, $after: String) {
      user(login: $login) {
        repositories(first: 100, after: $after, ownerAffiliations: OWNER, isFork: false) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes {
            nameWithOwner
            stargazerCount
            defaultBranchRef {
              name
              target {
                ... on Commit {
                  history { totalCount }
                }
              }
            }
          }
        }
      }
    }
    """
    repos = []
    stars = 0
    total_count = 0
    after = None
    while True:
        data = graphql(query, {"login": USERNAME, "after": after})["user"]["repositories"]
        total_count = data["totalCount"]
        for node in data["nodes"]:
            stars += node["stargazerCount"]
            repos.append(node)
        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]
    return total_count, stars, repos


def get_contributed_to_count():
    query = """
    query($login: String!) {
      user(login: $login) {
        repositoriesContributedTo(first: 1, contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, REPOSITORY]) {
          totalCount
        }
      }
    }
    """
    return graphql(query, {"login": USERNAME})["user"]["repositoriesContributedTo"]["totalCount"]


def get_total_commits(created_at):
    """Sum contributionsCollection.totalCommitContributions across every year
    since account creation (public + private, restricted count included)."""
    start = datetime.datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc
    )
    now = datetime.datetime.now(datetime.timezone.utc)

    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          totalCommitContributions
          restrictedContributionsCount
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
        total += data["totalCommitContributions"] + data["restrictedContributionsCount"]
        year_start = year_end
    return total


def load_cache():
    cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) == 4:
                    cache[parts[0]] = (int(parts[1]), int(parts[2]), int(parts[3]))
    return cache


def save_cache(cache):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        for repo, (commit_count, added, deleted) in cache.items():
            f.write(f"{repo},{commit_count},{added},{deleted}\n")


def repo_loc(repo, cache):
    """Additions/deletions authored by USERNAME on a repo's default branch,
    via the REST commits + stats endpoint. Cached per-repo by commit count
    so unchanged repos are skipped on subsequent runs."""
    name = repo["nameWithOwner"]
    branch_ref = repo.get("defaultBranchRef")
    if not branch_ref:
        return 0, 0

    commit_count = branch_ref["target"]["history"]["totalCount"]
    cached = cache.get(name)
    if cached and cached[0] == commit_count:
        return cached[1], cached[2]

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
            # empty repository
            break
        resp.raise_for_status()
        commits = resp.json()
        if not commits:
            break

        for c in commits:
            sha = c["sha"]
            detail = requests.get(
                f"{REST_URL}/repos/{name}/commits/{sha}", headers=HEADERS, timeout=30
            )
            if detail.status_code != 200:
                continue
            stats = detail.json().get("stats", {})
            added += stats.get("additions", 0)
            deleted += stats.get("deletions", 0)
            time.sleep(0.02)  # stay comfortably under the REST rate limit

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


def update_svg(path, values):
    tree = ET.parse(path)
    root = tree.getroot()
    for element_id, text in values.items():
        el = root.find(f".//*[@id='{element_id}']")
        if el is None:
            raise RuntimeError(f"{path}: no element with id={element_id!r}")
        el.text = text
    tree.write(path, xml_declaration=False, encoding="unicode")


def main():
    print(f"Collecting stats for {USERNAME}...")

    _, created_at = get_user_id_and_created_at()
    followers = get_followers()
    repo_count, stars, repos = get_repos_and_stars()
    contributed_to = get_contributed_to_count()
    commits = get_total_commits(created_at)
    added, deleted, net = get_loc(repos)

    values = {
        "repo_data": fmt(repo_count),
        "star_data": fmt(stars),
        "follower_data": fmt(followers),
        "contrib_data": fmt(contributed_to),
        "commit_data": fmt(commits),
        "loc_data": fmt(net),
        "add_data": fmt(added),
        "del_data": fmt(deleted),
    }

    for label, value in values.items():
        print(f"  {label}: {value}")

    for svg_path in SVG_FILES:
        full_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), svg_path)
        update_svg(full_path, values)
        print(f"Updated {svg_path}")


if __name__ == "__main__":
    main()

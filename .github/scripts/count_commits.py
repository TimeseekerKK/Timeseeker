"""Count a user's authored commits across owned repositories and branch heads.

COMMIT_STATS_TOKEN must belong to the user and cover every owned repository
with Contents: read. --public-only is an explicitly narrower diagnostic mode.
"""

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


class StatsError(RuntimeError):
    """A scan could not produce a complete result."""


class GitHubClient:
    def __init__(self, token="", opener=urlopen, sleeper=time.sleep):
        self.token = token
        self.opener = opener
        self.sleeper = sleeper

    def get(self, path, params=None):
        url = "https://api.github.com" + path
        if params:
            url += "?" + urlencode(params)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "TimeseekerKK-commit-stats",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        for attempt in range(4):
            try:
                with self.opener(Request(url, headers=headers), timeout=30) as response:
                    return json.load(response)
            except HTTPError as error:
                limited = error.code == 429 or (
                    error.code == 403 and (
                        error.headers.get("X-RateLimit-Remaining") == "0"
                        or error.headers.get("Retry-After") is not None
                    )
                )
                if attempt == 3 or not (limited or error.code in (500, 502, 503, 504)):
                    # Do not print the URL or response body: repositories may be private.
                    raise StatsError(f"GitHub request failed (HTTP {error.code}).") from None
                delay = 2 ** (attempt + 1)
                if limited:
                    retry_after = error.headers.get("Retry-After", "60")
                    delay = max(delay, float(retry_after) if retry_after.isdigit() else 60)
                    reset = error.headers.get("X-RateLimit-Reset", "")
                    if reset.isdigit():
                        delay = max(delay, int(reset) - time.time() + 1)
                    if delay > 60:
                        raise StatsError("GitHub rate limit exhausted; rerun after the quota resets.") from None
                self.sleeper(min(delay, 60))
            except (URLError, TimeoutError):
                if attempt == 3:
                    raise StatsError("GitHub request failed after four attempts.") from None
                self.sleeper(2 ** (attempt + 1))
            except (ValueError, UnicodeError):
                raise StatsError("GitHub returned an invalid JSON response.") from None

    def pages(self, path, params=None):
        params = dict(params or {})
        params["per_page"] = 100
        page = 1
        while True:
            params["page"] = page
            items = self.get(path, params)
            if not isinstance(items, list):
                raise StatsError("GitHub returned an invalid paginated response.")
            yield from items
            if len(items) < 100:
                return
            page += 1


@dataclass(frozen=True)
class CommitStats:
    total: int
    repositories: int
    branches: int
    public_only: bool


def owned_repositories(client, username, public_only=False):
    username_key = username.casefold()
    if public_only:
        profile = client.get("/users/" + quote(username, safe=""))
        path = "/users/" + quote(username, safe="") + "/repos"
        params = {"type": "owner", "sort": "full_name", "direction": "asc"}
    else:
        if not client.token:
            raise StatsError("Set COMMIT_STATS_TOKEN with read access to all owned repositories.")
        profile = client.get("/user")
        if profile.get("login", "").casefold() != username_key:
            raise StatsError("COMMIT_STATS_TOKEN must belong to the configured username.")
        path = "/user/repos"
        params = {"affiliation": "owner", "visibility": "all", "sort": "full_name"}

    repos = {}
    for repo in client.pages(path, params):
        if repo["owner"]["login"].casefold() == username_key:
            repos[repo["id"]] = repo

    expected_public = profile.get("public_repos")
    actual_public = sum(not repo["private"] for repo in repos.values())
    if not isinstance(expected_public, int) or actual_public != expected_public:
        raise StatsError("Public repository coverage is incomplete; previous card retained.")
    if not public_only:
        expected_private = profile.get("owned_private_repos")
        actual_private = sum(repo["private"] for repo in repos.values())
        if not isinstance(expected_private, int) or actual_private != expected_private:
            raise StatsError(
                "Private repository coverage could not be verified. Select all owned "
                "repositories for the token and allow access to the user's private profile."
            )
    return list(repos.values())


def repository_commits(client, full_name, username, heads=None):
    """Return unique authored SHAs reachable from the supplied branch snapshot."""
    path = "/repos/" + quote(full_name, safe="/")
    if heads is None:
        heads = {branch["commit"]["sha"] for branch in client.pages(path + "/branches")}
    else:
        heads = set(heads)
    shas = set()
    for head in sorted(heads):
        for commit in client.pages(path + "/commits", {"sha": head, "author": username}):
            author = commit.get("author") or {}
            if author.get("login", "").casefold() == username.casefold():
                shas.add(commit["sha"])
    return shas


def count_commits(client, username, public_only=False):
    repos = owned_repositories(client, username, public_only)
    # Snapshot all branch heads before reading their immutable commit histories.
    snapshots = []
    branches = 0
    for repo in repos:
        path = "/repos/" + quote(repo["full_name"], safe="/") + "/branches"
        repo_branches = list(client.pages(path))
        heads = {branch["commit"]["sha"] for branch in repo_branches}
        branches += len(repo_branches)
        snapshots.append((repo["full_name"], heads))

    shas = set()
    for full_name, heads in snapshots:
        shas.update(repository_commits(client, full_name, username, heads))
    return CommitStats(len(shas), len(repos), branches, public_only)


def fetch_stats_card(username, opener=urlopen):
    # This public service never receives COMMIT_STATS_TOKEN.
    url = "https://github-stats-extended.vercel.app/api?" + urlencode({
        "username": username, "include_all_commits": "true",
        "border_radius": "10", "theme": "default",
    })
    try:
        with opener(Request(url, headers={"User-Agent": "TimeseekerKK-commit-stats"}),
                    timeout=30) as response:
            return response.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError, UnicodeError):
        raise StatsError("Stats card download failed; previous card retained.") from None


def render_card(source, stats=None):
    """Keep the upstream card and replace its commit metric with our own count."""
    namespace = "http://www.w3.org/2000/svg"
    ET.register_namespace("", namespace)
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        raise StatsError("Stats service returned invalid SVG; previous card retained.") from None
    nodes = root.findall(".//*[@data-testid='commits']")
    description = root.find(f"{{{namespace}}}desc")
    if root.tag != f"{{{namespace}}}svg" or len(nodes) != 1 or description is None:
        raise StatsError("Stats card commit row is missing; previous card retained.")
    value_node = nodes[0]
    row = next((node for node in root.iter() if value_node in list(node)), None)
    labels = [node for node in row if node.tag == f"{{{namespace}}}text" and node is not value_node]
    if len(labels) != 1 or not (labels[0].text or "").startswith("Total Commits"):
        raise StatsError("Stats card commit row changed; previous card retained.")
    value = f"{stats.total:,}" if stats else "pending"
    scope = "public repositories" if stats and stats.public_only else "public + private repositories"
    details = (
        f"Authored commits in {scope}, all current branches, including merges, "
        "deduplicated by SHA across repositories."
    )
    if stats:
        details += f" Scanned {stats.repositories} repositories and {stats.branches} branches."
    else:
        details += " Awaiting the first complete scan with COMMIT_STATS_TOKEN."
    label = "Public Commits" if stats and stats.public_only else "Total Commits"
    updated_description, replacements = re.subn(
        r"Total Commits[^:]*:\s*\d+(?:,\d{3})*", f"{label}: {value}", description.text or "", count=1,
    )
    if replacements != 1:
        raise StatsError("Stats card description changed; previous card retained.")
    labels[0].text = label + ":"
    value_node.text = value
    description.text = updated_description + ". " + details
    return "\n".join(line.rstrip() for line in ET.tostring(root, encoding="unicode").splitlines()) + "\n"


def write_card(output, source, stats=None):
    content = render_card(source, stats)
    output = Path(output)
    if output.exists() and output.read_text(encoding="utf-8") == content:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.replace(output)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="TimeseekerKK")
    parser.add_argument("--output", type=Path, default=Path("assets/github-stats.svg"))
    parser.add_argument("--public-only", action="store_true",
                        help="Diagnostic mode: count only public repositories, labeled accordingly.")
    args = parser.parse_args(argv)
    try:
        stats = count_commits(GitHubClient(os.environ.get("COMMIT_STATS_TOKEN", "")),
                              args.username, args.public_only)
        source = fetch_stats_card(args.username)
        write_card(args.output, source, stats)
    except (StatsError, OSError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, StatsError) else "Scan or card write failed."
        print(message, file=sys.stderr)
        return 1
    scope = "public only" if stats.public_only else "public + private"
    print(f"Complete scan ({scope}): {stats.total} commits; "
          f"{stats.repositories} repositories; {stats.branches} branches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

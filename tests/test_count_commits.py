import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET


SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/count_commits.py"
SPEC = importlib.util.spec_from_file_location("count_commits", SCRIPT)
stats = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = stats
SPEC.loader.exec_module(stats)


def repo(number, private=False, fork=False):
    return {"id": number, "full_name": f"TobeBestKK/repo{number}",
            "owner": {"login": "TobeBestKK"}, "private": private, "fork": fork}


def commit(sha, author="TobeBestKK", parents=1):
    return {"sha": sha, "author": {"login": author},
            "parents": [{} for _ in range(parents)]}


class CountingTests(unittest.TestCase):
    def client(self, repos):
        client = Mock(token="test-token")
        client.get.return_value = {"login": "TobeBestKK", "public_repos": 1,
                                   "owned_private_repos": 1}
        client.pages.return_value = iter(repos)
        return client

    def test_owner_scope_includes_private_and_fork_and_excludes_membership(self):
        foreign = repo(3)
        foreign["owner"]["login"] = "another-owner"
        client = self.client([repo(1, fork=True), repo(2, private=True), foreign])
        self.assertEqual([r["id"] for r in stats.owned_repositories(client, "TobeBestKK")], [1, 2])
        self.assertEqual(client.pages.call_args.args[0], "/user/repos")
        self.assertEqual(client.pages.call_args.args[1]["affiliation"], "owner")

    def test_inaccessible_private_repo_fails_coverage_check(self):
        client = self.client([repo(1)])
        with self.assertRaisesRegex(stats.StatsError, "Private repository coverage"):
            stats.owned_repositories(client, "TobeBestKK")

    def test_missing_private_profile_count_cannot_claim_complete_scan(self):
        client = self.client([repo(1)])
        client.get.return_value = {"login": "TobeBestKK", "public_repos": 1}
        with self.assertRaises(stats.StatsError):
            stats.owned_repositories(client, "TobeBestKK")

    def test_token_must_belong_to_author(self):
        client = self.client([])
        client.get.return_value = {"login": "another-user"}
        with self.assertRaisesRegex(stats.StatsError, "must belong"):
            stats.owned_repositories(client, "TobeBestKK")

    def test_public_mode_is_explicit_and_checks_coverage(self):
        client = self.client([repo(1)])
        self.assertEqual(len(stats.owned_repositories(client, "TobeBestKK", True)), 1)
        self.assertEqual(client.get.call_args.args[0], "/users/TobeBestKK")
        self.assertEqual(client.pages.call_args.args[0], "/users/TobeBestKK/repos")
        client.pages.return_value = iter([])
        with self.assertRaisesRegex(stats.StatsError, "Public repository coverage"):
            stats.owned_repositories(client, "TobeBestKK", True)

    def test_all_heads_are_snapshotted_and_shas_are_globally_deduplicated(self):
        client = Mock()
        routes = {
            "/repos/TobeBestKK/repo1/branches": [{"commit": {"sha": "head1"}},
                                                  {"commit": {"sha": "head2"}}],
            "/repos/TobeBestKK/repo2/branches": [{"commit": {"sha": "head3"}}],
        }
        commits = {
            "head1": [commit("shared"), commit("merge", parents=2), commit("bot", "github-actions[bot]")],
            "head2": [commit("shared"), commit("feature", "tobebestkk"), commit("unlinked", None)],
            "head3": [commit("shared"), commit("private")],
        }
        def pages(path, params=None):
            if path.endswith("/branches"):
                return iter(routes[path])
            self.assertEqual(params["author"], "TobeBestKK")
            return iter(commits[params["sha"]])
        client.pages.side_effect = pages
        # An unlinked primary author must not be treated as this user's commit.
        commits["head2"][-1]["author"] = None
        with patch.object(stats, "owned_repositories", return_value=[repo(1, fork=True), repo(2, private=True)]):
            result = stats.count_commits(client, "TobeBestKK")
        self.assertEqual(result, stats.CommitStats(4, 2, 3, False))
        self.assertTrue(all(call.args[0].endswith("/branches")
                            for call in client.pages.call_args_list[:2]))

    def test_empty_repository_contributes_zero(self):
        client = Mock()
        client.pages.return_value = iter([])
        with patch.object(stats, "owned_repositories", return_value=[repo(1)]):
            self.assertEqual(stats.count_commits(client, "TobeBestKK").total, 0)


class ClientTests(unittest.TestCase):
    def test_pagination_reads_beyond_first_hundred(self):
        client = stats.GitHubClient()
        client.get = Mock(side_effect=[list(range(100)), [100, 101]])
        result = list(client.pages("/example", {"author": "TobeBestKK"}))
        self.assertEqual(result, list(range(102)))
        self.assertEqual(client.get.call_count, 2)

    def test_real_requests_keep_filters_and_advance_page(self):
        pages = []
        def opener(request, timeout):
            params = parse_qs(urlparse(request.full_url).query)
            pages.append(params)
            page = int(params["page"][0])
            return io.BytesIO(json.dumps(list(range(100)) if page == 1 else []).encode())
        client = stats.GitHubClient(opener=opener)
        self.assertEqual(len(list(client.pages("/example", {"sha": "fixed-head", "author": "TobeBestKK"}))), 100)
        self.assertEqual([p["page"] for p in pages], [["1"], ["2"]])
        self.assertTrue(all(p["sha"] == ["fixed-head"] and p["author"] == ["TobeBestKK"] for p in pages))

    def test_rate_limit_retries_without_leaking_private_path_or_token(self):
        error = HTTPError("https://api.github.com/repos/private-name", 429, "limited",
                          {"Retry-After": "2"}, io.BytesIO(b"secret-body"))
        opener = Mock(side_effect=[error, io.BytesIO(b"[]")])
        sleeper = Mock()
        client = stats.GitHubClient("secret-token", opener, sleeper)
        self.assertEqual(client.get("/repos/private-name"), [])
        sleeper.assert_called_once_with(2)

    def test_forbidden_and_retry_exhaustion_fail(self):
        for code, expected_attempts in ((403, 1), (503, 4)):
            with self.subTest(code=code):
                error = HTTPError("https://api.github.com/repos/private-name", code, "failed", {}, None)
                opener = Mock(side_effect=error)
                client = stats.GitHubClient("secret-token", opener, Mock())
                with self.assertRaises(stats.StatsError) as raised:
                    client.get("/repos/private-name")
                self.assertEqual(opener.call_count, expected_attempts)
                self.assertNotIn("private-name", str(raised.exception))
                self.assertNotIn("secret-token", str(raised.exception))

    def test_long_rate_limit_window_fails_without_repeated_waits(self):
        error = HTTPError("https://api.github.com/example", 403, "limited",
                          {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3600"}, None)
        sleeper = Mock()
        with patch.object(stats.time, "time", return_value=100):
            client = stats.GitHubClient(opener=Mock(side_effect=error), sleeper=sleeper)
            with self.assertRaisesRegex(stats.StatsError, "rate limit exhausted"):
                client.get("/example")
        sleeper.assert_not_called()


class OutputTests(unittest.TestCase):
    def test_failed_scan_retains_previous_badge(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parents[2]) as directory:
            output = Path(directory) / "badge.svg"
            output.write_text("previous complete result", encoding="utf-8")
            with patch.object(stats, "count_commits", side_effect=stats.StatsError("incomplete")), \
                 patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(stats.main(["--output", str(output)]), 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous complete result")

    def test_missing_token_fails_before_network_or_output(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parents[2]) as directory, patch.dict(os.environ, {}, clear=True), \
             patch.object(stats.GitHubClient, "get") as get, patch("sys.stderr", new_callable=io.StringIO):
            output = Path(directory) / "badge.svg"
            self.assertEqual(stats.main(["--output", str(output)]), 1)
            get.assert_not_called()
            self.assertFalse(output.exists())

    def test_svg_labels_pending_public_and_complete_scopes(self):
        cases = [(None, "pending"), (stats.CommitStats(52, 1, 3, True), "Public commits"),
                 (stats.CommitStats(152, 16, 20, False), "Total commits")]
        for result, expected in cases:
            svg = stats.render_badge(result)
            root = ET.fromstring(svg)
            self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
            self.assertIn(expected, svg)
        self.assertNotIn("public + private", stats.render_badge(cases[1][0]))

    def test_unchanged_badge_is_not_rewritten(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parents[2]) as directory:
            output = Path(directory) / "badge.svg"
            result = stats.CommitStats(52, 1, 3, False)
            stats.write_badge(output, result)
            before = output.stat().st_mtime_ns
            stats.write_badge(output, result)
            self.assertEqual(output.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()

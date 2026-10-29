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
    user_id = 149032340 if author and author.casefold() == "tobebestkk" else 42
    return {"sha": sha, "author": {"login": author, "id": user_id},
            "parents": [{} for _ in range(parents)]}


CARD = '''<svg xmlns="http://www.w3.org/2000/svg" width="450" height="195" viewBox="0 0 450 195">
<title>Timeseeker's GitHub Stats, Rank: C</title>
<desc>Total Stars Earned: 3, Total Commits  : 94, Total PRs: 13</desc>
<style>.stat { fill: #434d58; }</style>
<rect data-testid="card-bg" rx="10"/>
<text data-testid="header">Timeseeker's GitHub Stats</text>
<g><text>Total Stars Earned:</text><text data-testid="stars">3</text></g>
<g><text class="stat">Total Commits:</text><text class="stat" data-testid="commits" x="204.01">94</text></g>
<g><text>Total PRs:</text><text data-testid="prs">13</text></g>
<g data-testid="rank-circle"><text data-testid="level-rank-icon">C</text></g>
</svg>'''


class CountingTests(unittest.TestCase):
    def client(self, repos):
        client = Mock(token="test-token")
        client.get.return_value = {"login": "TobeBestKK", "id": 149032340, "public_repos": 1,
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
        client.get.return_value = {"login": "TobeBestKK", "id": 149032340, "public_repos": 1}
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
        client.get.return_value = {"login": "TobeBestKK", "id": 149032340}
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
        client.get.return_value = {"login": "TobeBestKK", "id": 149032340}
        client.pages.return_value = iter([])
        with patch.object(stats, "owned_repositories", return_value=[repo(1)]):
            self.assertEqual(stats.count_commits(client, "TobeBestKK").total, 0)

    def test_rename_recovers_email_results_and_deduplicates_every_source(self):
        email = "149032340+TobeBestKK@users.noreply.github.com"
        old = commit("old")
        renamed = commit("new")
        renamed["author"]["login"] = "TimeseekerKK"
        client = Mock()
        client.get.return_value = {"login": "TimeseekerKK", "id": 149032340}
        def pages(path, params=None):
            if path.endswith("/branches"):
                return iter([{"commit": {"sha": "head1"}}, {"commit": {"sha": "head2"}}])
            self.assertIn(params["sha"], ("head1", "head2"))
            # The new login misses the old commit even though its author ID is correct.
            return iter([renamed] if params["author"] == "TimeseekerKK" else [old, renamed])
        client.pages.side_effect = pages
        with patch.object(stats, "owned_repositories", return_value=[repo(1), repo(2)]):
            result = stats.count_commits(client, "TimeseekerKK", author_emails=[email, email.upper()])
        self.assertEqual(result, stats.CommitStats(2, 2, 4, False))
        self.assertEqual(len(client.pages.call_args_list), 10)
        self.assertTrue(all(call.args[0].endswith("/branches")
                            for call in client.pages.call_args_list[:2]))

    def test_only_confirmed_unlinked_email_is_accepted_and_conflicting_id_is_rejected(self):
        email = "149032340+TobeBestKK@users.noreply.github.com"
        def raw(sha, author, address):
            return {"sha": sha, "author": author, "commit": {"author": {"email": address}}}
        client = Mock()
        client.pages.side_effect = lambda *args: iter([
            raw("confirmed", None, email.upper()),
            raw("unknown", None, "someone@example.com"),
            raw("conflict", {"id": 42, "login": "TimeseekerKK"}, email),
            raw("missing-id", {"login": "TimeseekerKK"}, email),
            raw("same-name", None, "TobeBestKK@users.noreply.github.com"),
            raw("missing-email", None, ""),
            raw("null-email", None, None),
        ])
        result = stats.repository_commits(client, "owner/repo", "TimeseekerKK", ["head"],
                                          user_id=149032340, author_emails=[email])
        self.assertEqual(result, {"confirmed"})

    def test_missing_account_id_fails_before_enumerating_repositories(self):
        client = self.client([])
        del client.get.return_value["id"]
        with self.assertRaisesRegex(stats.StatsError, "account ID"):
            stats.count_commits(client, "TobeBestKK")
        client.pages.assert_not_called()


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
    def test_first_complete_scan_can_replace_pending_card(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parents[2]) as directory:
            output = Path(directory) / "card.svg"
            stats.write_card(output, CARD)
            stats.write_card(output, CARD, stats.CommitStats(129, 13, 22, False))
            self.assertEqual(ET.fromstring(output.read_text(encoding="utf-8"))
                             .find(".//*[@data-testid='commits']").text, "129")

    def test_large_drop_retains_card_and_explicit_override_allows_it(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parents[2]) as directory:
            output = Path(directory) / "card.svg"
            stats.write_card(output, CARD, stats.CommitStats(129, 13, 22, False))
            original = output.read_bytes()
            with patch.object(stats, "count_commits", return_value=stats.CommitStats(2, 13, 22, False)), \
                 patch.object(stats, "fetch_stats_card", return_value=CARD), \
                 patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(stats.main(["--output", str(output)]), 1)
                self.assertIn("129 to 2", stderr.getvalue())
            self.assertEqual(output.read_bytes(), original)
            stats.write_card(output, CARD, stats.CommitStats(2, 13, 22, False), allow_decrease=True)
            root = ET.fromstring(output.read_text(encoding="utf-8"))
            self.assertEqual(root.find(".//*[@data-testid='commits']").text, "2")

    def test_decrease_threshold_allows_small_changes_but_checks_both_limits(self):
        for previous, current, blocked in [(100, 80, False), (100, 79, True),
                                           (20, 11, False), (20, 10, True), (0, 0, False),
                                           (129, 130, False), (1000, 2, True)]:
            with self.subTest(previous=previous, current=current), \
                 tempfile.TemporaryDirectory(dir=SCRIPT.parents[2]) as directory:
                output = Path(directory) / "card.svg"
                stats.write_card(output, CARD, stats.CommitStats(previous, 13, 22, False))
                result = stats.CommitStats(current, 13, 22, False)
                if blocked:
                    with self.assertRaisesRegex(stats.StatsError, "count dropped"):
                        stats.write_card(output, CARD, result)
                else:
                    stats.write_card(output, CARD, result)

    def test_cli_passes_confirmed_emails_and_decrease_override(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parents[2]) as directory:
            output = Path(directory) / "card.svg"
            stats.write_card(output, CARD, stats.CommitStats(129, 13, 22, False))
            with patch.object(stats, "count_commits", return_value=stats.CommitStats(2, 13, 22, False)) as count, \
                 patch.object(stats, "fetch_stats_card", return_value=CARD), \
                 patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(stats.main(["--output", str(output), "--author-email", "old@example.com",
                                             "--author-email", "other@example.com", "--allow-decrease"]), 0)
            self.assertEqual(count.call_args.args[3], ["old@example.com", "other@example.com"])

    def test_failed_scan_retains_previous_card(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parents[2]) as directory:
            output = Path(directory) / "card.svg"
            output.write_text("previous complete result", encoding="utf-8")
            with patch.object(stats, "count_commits", side_effect=stats.StatsError("incomplete")), \
                 patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(stats.main(["--output", str(output)]), 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous complete result")

    def test_missing_token_fails_before_network_or_output(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parents[2]) as directory, patch.dict(os.environ, {}, clear=True), \
             patch.object(stats.GitHubClient, "get") as get, patch("sys.stderr", new_callable=io.StringIO):
            output = Path(directory) / "card.svg"
            self.assertEqual(stats.main(["--output", str(output)]), 1)
            get.assert_not_called()
            self.assertFalse(output.exists())

    def test_svg_labels_pending_public_and_complete_scopes(self):
        cases = [(None, "pending"), (stats.CommitStats(52, 1, 3, True), "Public Commits"),
                 (stats.CommitStats(1252, 16, 20, False), "Total Commits")]
        for result, expected in cases:
            svg = stats.render_card(CARD, result)
            root = ET.fromstring(svg)
            self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
            self.assertIn(expected, svg)
        self.assertNotIn("public + private", stats.render_card(CARD, cases[1][0]))

    def test_unchanged_card_is_not_rewritten(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parents[2]) as directory:
            output = Path(directory) / "card.svg"
            result = stats.CommitStats(52, 1, 3, False)
            stats.write_card(output, CARD, result)
            before = output.stat().st_mtime_ns
            stats.write_card(output, CARD, result)
            self.assertEqual(output.stat().st_mtime_ns, before)

    def test_commit_replacement_preserves_card_layout_and_other_metrics(self):
        root = ET.fromstring(stats.render_card(CARD, stats.CommitStats(1252, 16, 20, False)))
        self.assertEqual(root.attrib, ET.fromstring(CARD).attrib)
        for key, expected in (("commits", "1,252"), ("stars", "3"), ("prs", "13"),
                              ("header", "Timeseeker's GitHub Stats"), ("level-rank-icon", "C")):
            self.assertEqual(root.find(f".//*[@data-testid='{key}']").text, expected)
        self.assertEqual(root.find(".//*[@data-testid='commits']").get("x"), "204.01")
        description = root.find("{http://www.w3.org/2000/svg}desc").text
        self.assertIn("Total Commits: 1,252, Total PRs: 13", description)
        self.assertNotIn(": 94", description)

    def test_invalid_or_changed_upstream_card_retains_previous_output(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parents[2]) as directory:
            output = Path(directory) / "card.svg"
            output.write_text("previous complete result", encoding="utf-8")
            sources = ["not SVG", CARD.replace('data-testid="commits"', 'data-testid="other"'),
                       CARD.replace("Total Commits  : 94", "Changed description"),
                       CARD.replace('>Total Commits:</text>', '>Changed label:</text>')]
            for source in sources:
                with self.subTest(source=source), self.assertRaises(stats.StatsError):
                    stats.write_card(output, source, stats.CommitStats(52, 1, 3, False))
                self.assertEqual(output.read_text(encoding="utf-8"), "previous complete result")

    def test_failed_card_download_retains_previous_output(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parents[2]) as directory:
            output = Path(directory) / "card.svg"
            output.write_text("previous complete result", encoding="utf-8")
            with patch.object(stats, "count_commits", return_value=stats.CommitStats(52, 1, 3, False)), \
                 patch.object(stats, "fetch_stats_card", side_effect=stats.StatsError("download failed")), \
                 patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(stats.main(["--output", str(output)]), 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous complete result")

    def test_successful_scan_writes_count_inside_full_card(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parents[2]) as directory:
            output = Path(directory) / "card.svg"
            with patch.object(stats, "count_commits", return_value=stats.CommitStats(52, 1, 3, False)), \
                 patch.object(stats, "fetch_stats_card", return_value=CARD), \
                 patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(stats.main(["--output", str(output)]), 0)
            root = ET.fromstring(output.read_text(encoding="utf-8"))
            self.assertEqual(root.find(".//*[@data-testid='commits']").text, "52")

    def test_public_stats_service_never_receives_the_scan_token(self):
        opener = Mock(return_value=io.BytesIO(CARD.encode()))
        with patch.dict(os.environ, {"COMMIT_STATS_TOKEN": "secret-token"}):
            self.assertEqual(stats.fetch_stats_card("TobeBestKK", opener), CARD)
        request = opener.call_args.args[0]
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("secret-token", request.full_url)
        self.assertEqual(parse_qs(urlparse(request.full_url).query)["include_all_commits"], ["true"])


if __name__ == "__main__":
    unittest.main()

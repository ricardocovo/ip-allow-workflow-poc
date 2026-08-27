from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import ip_allow_list


FIXTURES = Path(__file__).parent / "fixtures"


class DownloadDiscoveryTests(unittest.TestCase):
    def test_finds_dated_links_and_selects_latest(self) -> None:
        page = (FIXTURES / "download-page.html").read_text(encoding="utf-8")

        urls = ip_allow_list.find_download_urls(page)

        self.assertEqual(2, len(urls))
        latest = ip_allow_list.select_latest_download_url(urls)
        self.assertTrue(latest.endswith("ServiceTags_Public_20260824.json"))

    def test_rejects_page_without_download_link(self) -> None:
        self.assertEqual([], ip_allow_list.find_download_urls("<html></html>"))


class PrefixProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(
            (FIXTURES / "service-tags.json").read_text(encoding="utf-8")
        )

    def test_extracts_only_canada_central_and_normalizes(self) -> None:
        prefixes = ip_allow_list.extract_prefixes(self.document)

        self.assertEqual(
            [
                "4.206.229.128/27",
                "20.48.202.16/29",
                "2603:1030:f05::/122",
            ],
            prefixes,
        )

    def test_compares_added_and_removed_prefixes(self) -> None:
        added, removed = ip_allow_list.compare_prefixes(
            ["10.0.0.0/24", "2001:db8::/32"],
            ["10.0.0.0/24", "192.0.2.0/24"],
        )

        self.assertEqual(["2001:db8::/32"], added)
        self.assertEqual(["192.0.2.0/24"], removed)

    def test_rejects_missing_duplicate_and_invalid_tags(self) -> None:
        with self.assertRaisesRegex(ip_allow_list.ServiceTagsError, "found 0"):
            ip_allow_list.extract_prefixes({"values": []})

        duplicate = {"values": [self.document["values"][1], self.document["values"][1]]}
        with self.assertRaisesRegex(ip_allow_list.ServiceTagsError, "found 2"):
            ip_allow_list.extract_prefixes(duplicate)

        invalid = json.loads(json.dumps(self.document))
        invalid["values"][1]["properties"]["addressPrefixes"] = ["not-a-cidr"]
        with self.assertRaisesRegex(ip_allow_list.ServiceTagsError, "Invalid CIDR"):
            ip_allow_list.extract_prefixes(invalid)

    def test_process_file_writes_changed_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ServiceTags_Public_20260824.json"
            snapshot = root / "snapshot.json"
            actions = root / "actions.json"
            summary = root / "summary.md"
            source.write_text(json.dumps(self.document), encoding="utf-8")
            ip_allow_list.write_json(
                snapshot,
                {
                    "serviceTag": ip_allow_list.SERVICE_TAG_NAME,
                    "prefixes": ["4.206.229.128/27", "192.0.2.0/24"],
                },
            )

            outputs = ip_allow_list.process_file(
                source, snapshot, actions, summary, "2026-08-27"
            )

            self.assertEqual("true", outputs["changed"])
            self.assertEqual(
                {
                    "dateUpdated": "2026-08-27",
                    "actions": {
                        "remove": ["192.0.2.0/24"],
                        "add": ["20.48.202.16/29", "2603:1030:f05::/122"],
                    },
                },
                json.loads(actions.read_text(encoding="utf-8")),
            )
            refreshed = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertEqual("2026-08-24", refreshed["sourceDate"])
            self.assertIn("## Added (2)", summary.read_text(encoding="utf-8"))
            self.assertIn("## Removed (1)", summary.read_text(encoding="utf-8"))

    def test_unchanged_file_does_not_rewrite_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ServiceTags_Public_20260824.json"
            snapshot = root / "snapshot.json"
            actions = root / "actions.json"
            summary = root / "summary.md"
            source.write_text(json.dumps(self.document), encoding="utf-8")
            ip_allow_list.write_json(
                snapshot,
                {
                    "serviceTag": ip_allow_list.SERVICE_TAG_NAME,
                    "prefixes": ip_allow_list.extract_prefixes(self.document),
                },
            )
            original_snapshot = snapshot.read_bytes()

            outputs = ip_allow_list.process_file(
                source, snapshot, actions, summary, "2026-08-27"
            )

            self.assertEqual("false", outputs["changed"])
            self.assertFalse(actions.exists())
            self.assertEqual(original_snapshot, snapshot.read_bytes())
            self.assertIn("_None_", summary.read_text(encoding="utf-8"))

    def test_pending_snapshot_uses_main_snapshot_for_action_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ServiceTags_Public_20260824.json"
            pending_snapshot = root / "pending.json"
            main_snapshot = root / "main.json"
            actions = root / "actions.json"
            summary = root / "summary.md"
            source.write_text(json.dumps(self.document), encoding="utf-8")
            ip_allow_list.write_json(
                pending_snapshot,
                {
                    "serviceTag": ip_allow_list.SERVICE_TAG_NAME,
                    "prefixes": ["4.206.229.128/27", "20.48.202.16/29"],
                },
            )
            ip_allow_list.write_json(
                main_snapshot,
                {
                    "serviceTag": ip_allow_list.SERVICE_TAG_NAME,
                    "prefixes": ["4.206.229.128/27"],
                },
            )

            outputs = ip_allow_list.process_file(
                source,
                pending_snapshot,
                actions,
                summary,
                "2026-08-27",
                main_snapshot,
            )

            self.assertEqual("true", outputs["changed"])
            action_document = json.loads(actions.read_text(encoding="utf-8"))
            self.assertEqual(
                ["20.48.202.16/29", "2603:1030:f05::/122"],
                action_document["actions"]["add"],
            )

    def test_reverted_proposal_does_not_publish_empty_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ServiceTags_Public_20260824.json"
            pending_snapshot = root / "pending.json"
            main_snapshot = root / "main.json"
            actions = root / "actions.json"
            summary = root / "summary.md"
            source.write_text(json.dumps(self.document), encoding="utf-8")
            live = ip_allow_list.extract_prefixes(self.document)
            ip_allow_list.write_json(
                pending_snapshot,
                {
                    "serviceTag": ip_allow_list.SERVICE_TAG_NAME,
                    "prefixes": live + ["203.0.113.0/24"],
                },
            )
            ip_allow_list.write_json(
                main_snapshot,
                {"serviceTag": ip_allow_list.SERVICE_TAG_NAME, "prefixes": live},
            )

            outputs = ip_allow_list.process_file(
                source,
                pending_snapshot,
                actions,
                summary,
                "2026-08-27",
                main_snapshot,
            )

            self.assertEqual("false", outputs["changed"])
            self.assertFalse(actions.exists())


class SnowMockTests(unittest.TestCase):
    def test_mock_returns_success_for_valid_actions(self) -> None:
        result = ip_allow_list.create_snow_entry(
            {
                "dateUpdated": "2026-08-27",
                "actions": {"add": ["192.0.2.0/24"], "remove": []},
            }
        )

        self.assertEqual({"status": "success", "mock": True}, result)

    def test_mock_rejects_invalid_payload(self) -> None:
        with self.assertRaisesRegex(ip_allow_list.ServiceTagsError, "actions.add"):
            ip_allow_list.create_snow_entry(
                {"dateUpdated": "2026-08-27", "actions": {"remove": [], "add": "bad"}}
            )


if __name__ == "__main__":
    unittest.main()

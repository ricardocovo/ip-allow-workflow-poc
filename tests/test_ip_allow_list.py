from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import ip_allow_list


FIXTURES = Path(__file__).parent / "fixtures"


class DownloadTests(unittest.TestCase):
    DOWNLOAD_URL = (
        "https://download.microsoft.com/download/7/1/d/example/"
        "ServiceTags_Public_20260824.json"
    )

    def test_downloads_explicit_url_and_writes_outputs(self) -> None:
        payload = (FIXTURES / "service-tags.json").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "github-output"

            with patch.object(ip_allow_list, "_request_bytes", return_value=payload):
                result = ip_allow_list.main(
                    [
                        "download",
                        "--download-url",
                        self.DOWNLOAD_URL,
                        "--download-dir",
                        str(root / "downloads"),
                        "--github-output",
                        str(output),
                    ]
                )

            self.assertEqual(0, result)
            downloaded = root / "downloads" / "ServiceTags_Public_20260824.json"
            self.assertEqual(payload, downloaded.read_bytes())
            self.assertEqual(
                {
                    "available=true",
                    f"download_path={downloaded}",
                    "source_filename=ServiceTags_Public_20260824.json",
                    "source_date=20260824",
                },
                set(output.read_text(encoding="utf-8").splitlines()),
            )

    def test_http_404_is_a_successful_noop(self) -> None:
        error = HTTPError(self.DOWNLOAD_URL, 404, "Not Found", {}, None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "github-output"

            with (
                patch("scripts.ip_allow_list.urlopen", side_effect=error),
                redirect_stderr(io.StringIO()),
            ):
                result = ip_allow_list.main(
                    [
                        "download",
                        "--download-url",
                        self.DOWNLOAD_URL,
                        "--download-dir",
                        str(root / "downloads"),
                        "--github-output",
                        str(output),
                    ]
                )

            self.assertEqual(0, result)
            self.assertEqual("available=false\n", output.read_text(encoding="utf-8"))
            self.assertFalse((root / "downloads").exists())

    def test_non_404_http_error_fails(self) -> None:
        error = HTTPError(self.DOWNLOAD_URL, 503, "Unavailable", {}, None)
        with patch("scripts.ip_allow_list.urlopen", side_effect=error):
            with self.assertRaisesRegex(ip_allow_list.ServiceTagsError, "HTTP 503"):
                ip_allow_list._request_bytes(self.DOWNLOAD_URL)

    def test_rejects_unexpected_download_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ip_allow_list.ServiceTagsError, "unexpected filename"
            ):
                ip_allow_list.download_service_tags(
                    Path(directory),
                    "https://download.microsoft.com/download/service-tags.json",
                )

    def test_rejects_malformed_download_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ip_allow_list.ServiceTagsError, "valid HTTPS URL"
            ):
                ip_allow_list.download_service_tags(
                    Path(directory), "ServiceTags_Public_20260824.json"
                )

    def test_rejects_invalid_json_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(ip_allow_list, "_request_bytes", return_value=b"invalid"),
                self.assertRaisesRegex(ip_allow_list.ServiceTagsError, "not valid JSON"),
            ):
                ip_allow_list.download_service_tags(
                    Path(directory), self.DOWNLOAD_URL
                )


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

            outputs = ip_allow_list.process_file(source, snapshot, actions, summary)

            self.assertEqual("true", outputs["changed"])
            self.assertEqual(
                {
                    "dateUpdated": "2026-08-24",
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

            outputs = ip_allow_list.process_file(source, snapshot, actions, summary)

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
                main_snapshot,
            )

            self.assertEqual("true", outputs["changed"])
            action_document = json.loads(actions.read_text(encoding="utf-8"))
            self.assertEqual(
                ["20.48.202.16/29", "2603:1030:f05::/122"],
                action_document["actions"]["add"],
            )

    def test_repeated_pending_proposal_rebuilds_desired_files(self) -> None:
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
                {"serviceTag": ip_allow_list.SERVICE_TAG_NAME, "prefixes": live},
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
                main_snapshot,
            )

            self.assertEqual("false", outputs["changed"])
            self.assertEqual("true", outputs["has_delta"])
            self.assertEqual(live, ip_allow_list.load_snapshot(pending_snapshot))
            self.assertEqual(
                ["20.48.202.16/29", "2603:1030:f05::/122"],
                json.loads(actions.read_text(encoding="utf-8"))["actions"]["add"],
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
            main_snapshot_bytes = main_snapshot.read_bytes()

            outputs = ip_allow_list.process_file(
                source,
                pending_snapshot,
                actions,
                summary,
                main_snapshot,
            )

            self.assertEqual("false", outputs["changed"])
            self.assertEqual("false", outputs["has_delta"])
            self.assertFalse(actions.exists())
            self.assertEqual(main_snapshot_bytes, pending_snapshot.read_bytes())


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

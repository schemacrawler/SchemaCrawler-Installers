import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


spec = importlib.util.spec_from_file_location(
    "release_summary", Path(__file__).with_name("release-summary.py")
)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def package_results(version="17.16.0"):
    return {
        job: {
            "result": "success",
            "outputs": {
                "version-output": f"SchemaCrawler {version}\nDatabase schema discovery tool"
            },
        }
        for job in report.PACKAGES
    }


class ReleaseSummaryTest(unittest.TestCase):
    def test_package_versions(self):
        for version in ("17.16.0", "17.16.0-RC.1", "17.16.0+build.2", "15.05.01.01"):
            with self.subTest(version=version):
                rows, errors = report.package_rows(package_results(version))
                self.assertEqual(len(rows), 6)
                self.assertFalse(errors)
                self.assertTrue(all(f"| {version} | success |" in row for row in rows))

    def test_failed_skipped_and_cancelled_packages_remain_visible(self):
        results = package_results()
        for job, status in zip(results, ("failure", "skipped", "cancelled")):
            results[job] = {"result": status, "outputs": {}}
        rows, errors = report.package_rows(results)
        self.assertFalse(errors)
        for row, status in zip(rows, ("failure", "skipped", "cancelled")):
            self.assertIn(f"| Not reported | {status} |", row)

    def test_success_without_recognizable_version_is_reporting_error(self):
        results = package_results()
        results["test-sdkman"]["outputs"]["version-output"] = "SDKMAN 5.20.0"
        rows, errors = report.package_rows(results)
        self.assertIn("| Not reported | Version not recognized |", rows[1])
        self.assertEqual(len(errors), 1)
        self.assertIn("SDKMAN", errors[0])

    def test_artifact_discovery_filters_unrelated_links(self):
        index = report.ArtifactIndex()
        index.feed(
            '<a href="schemacrawler/">core</a>'
            '<a href="schemacrawler-db2/">DB2</a>'
            '<a href="schemacrawler-db2/">duplicate</a>'
            '<a href="other/">other</a><a href="../">parent</a>'
            '<a href="https://example.com/schemacrawler/">external</a>'
        )
        self.assertEqual(index.artifacts, {"schemacrawler", "schemacrawler-db2"})

    def test_maven_versions_are_independent_and_poms_excluded(self):
        responses = {
            report.MAVEN_URL: (
                b'<a href="schemacrawler/">core</a>'
                b'<a href="schemacrawler-api/">API</a>'
                b'<a href="schemacrawler-parent/">parent</a>'
            ),
        }
        for artifact, version, packaging in (
            ("schemacrawler", "17.16.0", ""),
            ("schemacrawler-api", "17.1.7-A", "<packaging>jar</packaging>"),
            ("schemacrawler-parent", "17.16.0", "<packaging>pom</packaging>"),
        ):
            base = f"{report.MAVEN_URL}{artifact}/"
            responses[f"{base}maven-metadata.xml"] = (
                f"<metadata><versioning><release>{version}</release>"
                "</versioning></metadata>"
            ).encode()
            responses[f"{base}{version}/{artifact}-{version}.pom"] = (
                '<project xmlns="http://maven.apache.org/POM/4.0.0">'
                f"{packaging}</project>"
            ).encode()
            if artifact != "schemacrawler-parent":
                responses[f"{base}{version}/{artifact}-{version}.jar"] = b""
        with patch.object(report, "fetch", side_effect=lambda url, **_: responses[url]) as fetch:
            rows, errors = report.maven_rows()
        self.assertFalse(errors)
        self.assertEqual(len(rows), 2)
        self.assertIn("| us.fatehi:schemacrawler | 17.16.0 |", rows[0])
        self.assertIn("| us.fatehi:schemacrawler-api | 17.1.7-A |", rows[1])
        jar_requests = [call for call in fetch.call_args_list if call.kwargs]
        self.assertEqual(len(jar_requests), 2)
        self.assertTrue(all(call.kwargs == {"method": "HEAD"} for call in jar_requests))

    def test_missing_release_and_missing_jar_are_reported(self):
        for metadata in (
            b"<metadata/>",
            b"<metadata><versioning><release>17.16.0</release></versioning></metadata>",
            b"invalid XML",
        ):
            def fetch(url, method="GET"):
                if url == report.MAVEN_URL:
                    return b'<a href="schemacrawler/">core</a>'
                if url.endswith(".xml"):
                    return metadata
                if url.endswith(".pom"):
                    return b'<project xmlns="http://maven.apache.org/POM/4.0.0"/>'
                raise HTTPError(url, 404, "Not Found", {}, None)

            with self.subTest(metadata=metadata), patch.object(report, "fetch", fetch):
                rows, errors = report.maven_rows()
                self.assertEqual(len(errors), 1)
                self.assertIn("Not available", rows[0])

    def test_empty_discovery_is_an_error(self):
        with patch.object(report, "fetch", return_value=b"<html/>"):
            with self.assertRaisesRegex(ValueError, "No SchemaCrawler artifacts"):
                report.maven_rows()

    def test_summary_is_written_before_reporting_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                summary = Path(directory) / "summary.md"
                env = {
                    "PACKAGE_RESULTS": json.dumps(package_results()),
                    "GITHUB_STEP_SUMMARY": str(summary),
                }
                with patch.dict(os.environ, env), patch.object(report, "maven_rows") as maven:
                    if failure:
                        maven.side_effect = OSError("Maven unavailable")
                    else:
                        maven.return_value = (["| us.fatehi:schemacrawler | 17.16.0 | JAR |"], [])
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(report.main(), int(failure))
                text = summary.read_text(encoding="utf-8")
                self.assertIn("| SDKMAN | 17.16.0 | success |", text)
                self.assertIn("## Maven Central JAR releases", text)
                if failure:
                    self.assertIn("Maven Central discovery failed: Maven unavailable", text)


if __name__ == "__main__":
    unittest.main()

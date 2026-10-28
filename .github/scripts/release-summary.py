import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen


MAVEN_URL = "https://repo.maven.apache.org/maven2/us/fatehi/"
PACKAGES = {
    "test-brew": "Homebrew",
    "test-sdkman": "SDKMAN",
    "test-scoop": "Scoop",
    "test-winget": "Winget",
    "test-chocolatey": "Chocolatey",
    "test-docker": "Docker (latest)",
}


class ArtifactIndex(HTMLParser):
    def __init__(self):
        super().__init__()
        self.artifacts = set()

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        if re.fullmatch(r"schemacrawler(?:-[a-z0-9-]+)?/", href):
            self.artifacts.add(href.rstrip("/"))


def fetch(url, method="GET"):
    request = Request(url, method=method, headers={"User-Agent": "SchemaCrawler-release-test"})
    with urlopen(request, timeout=30) as response:
        return response.read()


def cell(value):
    return str(value).replace("|", r"\|").replace("\r", "").replace("\n", " ")


def package_rows(results):
    rows = []
    errors = []
    for job, name in PACKAGES.items():
        result = results[job]
        output = result["outputs"].get("version-output", "")
        match = re.search(
            r"\bSchemaCrawler\s+(?:version\s*[:=]?\s*)?v?"
            r"(\d+(?:\.\d+){2,}(?:[-+][0-9A-Za-z.-]+)?)\b",
            output,
            re.IGNORECASE,
        )
        version = match[1] if match else "Not reported"
        status = result["result"]
        if not match and status == "success":
            status = "Version not recognized"
            errors.append(f"{name}: no SchemaCrawler version found in {output!r}")
        rows.append(f"| {name} | {cell(version)} | {cell(status)} |")
    return rows, errors


def maven_rows():
    index = ArtifactIndex()
    index.feed(fetch(MAVEN_URL).decode("utf-8"))
    if not index.artifacts:
        raise ValueError("No SchemaCrawler artifacts found in Maven Central")

    rows = []
    errors = []
    for artifact in sorted(index.artifacts):
        coordinate = f"us.fatehi:{artifact}"
        try:
            metadata = ET.fromstring(fetch(f"{MAVEN_URL}{artifact}/maven-metadata.xml"))
            version = metadata.findtext("versioning/release")
            if not version or not re.fullmatch(r"[0-9A-Za-z_.+-]+", version):
                raise ValueError(f"Invalid or missing release version: {version!r}")
            base_url = f"{MAVEN_URL}{artifact}/{version}/{artifact}-{version}"
            pom = ET.fromstring(fetch(f"{base_url}.pom"))
            namespace = {"m": "http://maven.apache.org/POM/4.0.0"}
            packaging = pom.findtext("m:packaging", default="jar", namespaces=namespace)
            if packaging == "pom":
                continue
            fetch(f"{base_url}.jar", method="HEAD")
            rows.append(f"| {coordinate} | {cell(version)} | [JAR]({base_url}.jar) |")
        except (OSError, ET.ParseError, ValueError) as error:
            errors.append(f"{coordinate}: {error}")
            rows.append(f"| {coordinate} | Not available | {cell(error)} |")
    if not rows:
        errors.append("No SchemaCrawler JAR releases found in Maven Central")
    return rows, errors


def main():
    results = json.loads(os.environ["PACKAGE_RESULTS"])
    packages, errors = package_rows(results)
    lines = [
        "# Published SchemaCrawler versions",
        "",
        "## Installed packages",
        "",
        "| Channel | SchemaCrawler version | Test result |",
        "| --- | --- | --- |",
        *packages,
        "",
        "## Maven Central JAR releases",
        "",
        "Latest release of each `us.fatehi:schemacrawler*` JAR, queried independently "
        "from Maven Central metadata. POM-only artifacts are excluded.",
        "",
        "| Artifact | Published version | Download / error |",
        "| --- | --- | --- |",
    ]
    try:
        jars, maven_errors = maven_rows()
        lines.extend(jars)
        errors.extend(maven_errors)
    except (OSError, ET.ParseError, ValueError) as error:
        errors.append(f"Maven Central discovery failed: {error}")
        lines.append(f"| Discovery failed | Not available | {cell(error)} |")
    if errors:
        lines.extend(["", "## Reporting errors", "", *(f"- {cell(error)}" for error in errors)])
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

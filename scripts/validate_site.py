#!/usr/bin/env python3
"""Verify Hugo bilingual output without requiring browser dependencies."""

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path("public")


class Document(HTMLParser):
    def __init__(self):
        super().__init__()
        self.language = None
        self.menu = []
        self.switches = []
        self.entries = 0
        self._inside_switch = False
        self._inside_menu = False
        self._capture = None

    def handle_starttag(self, tag, raw_attrs):
        attrs = dict(raw_attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "html":
            self.language = attrs.get("lang")
        if tag == "ul" and "lang-switch" in classes:
            self._inside_switch = True
        if tag == "ul" and attrs.get("id") == "menu":
            self._inside_menu = True
        if tag == "article" and "post-entry" in classes:
            self.entries += 1
        if tag == "a" and (self._inside_switch or self._inside_menu):
            item = {"href": attrs.get("href", ""), "text": ""}
            if self._inside_switch:
                self.switches.append(item)
            else:
                self.menu.append(item)
            self._capture = item

    def handle_data(self, data):
        if self._capture is not None:
            self._capture["text"] += data

    def handle_endtag(self, tag):
        if tag == "a":
            self._capture = None
        if tag == "ul":
            self._inside_switch = False
            self._inside_menu = False


def check(path, language, expected_switch, expected_home, expected_archive, min_posts=None):
    file = ROOT / path
    assert file.is_file(), f"Missing: {file}"
    html = file.read_text(encoding="utf-8")
    doc = Document()
    doc.feed(html)
    assert doc.language == language, f"{file}: html lang={doc.language!r}, expected {language}"
    switches = [(s["text"].strip(), urlparse(s["href"]).path) for s in doc.switches]
    assert switches == [(expected_switch, expected_home)], f"{file}: unexpected language switch {switches}"
    archive_links = [urlparse(m["href"]).path for m in doc.menu if "Archives" in m["text"]]
    assert archive_links == [expected_archive], f"{file}: archives menu URL {archive_links}"
    if min_posts is not None:
        assert doc.entries >= min_posts, f"{file}: only {doc.entries} articles"
    return html


def main():
    zh = check("index.html", "zh", "EN", "/en/", "/archives/", min_posts=4)
    en = check("en/index.html", "en", "中文", "/", "/en/archives/", min_posts=3)
    check("zh/posts/tent-internal-arch/index.html", "zh", "EN", "/en/", "/archives/")
    check("en/posts/tent-internal-arch/index.html", "en", "中文", "/", "/en/archives/")

    for path in ["archives/index.html", "en/archives/index.html",
                 "zh/posts/tent-internal-orchestrator-part-1/index.html",
                 "en/posts/tent-internal-orchestrator-part-1/index.html"]:
        assert (ROOT / path).is_file(), f"Missing legacy URL: {path}"

    for path in ["zh/posts/ccf-storage-evaluation-2026/index.html",
                 "zh/posts/thinking-of-career/index.html"]:
        assert not (ROOT / path).exists(), f"Draft leaked into build: {path}"

    for path in ["index.html", "en/index.html", "archives/index.html",
                 "en/archives/index.html"]:
        source = (ROOT / path).read_text(encoding="utf-8")
        for forbidden in ["CCF Storage Evaluation 2026 is Coming",
                          "系统研究者的职业选择",
                          "libfeng.com"]:
            assert forbidden not in source, f"Unexpected text {forbidden} in {path}"

    assert (ROOT / "zh/index.html").exists(), "Missing backwards-compatible /zh/ homepage alias"
    print("Bilingual Hugo output, navigation, permalinks, and drafts: PASS")


if __name__ == "__main__":
    main()

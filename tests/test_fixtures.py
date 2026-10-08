"""Fixture-quality tests (M0.4): generated sample media are valid; fixture HTML is usable."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image

EXPECTED_FORMATS = {"png": "PNG", "jpeg": "JPEG", "gif": "GIF", "webp": "WEBP"}


def test_sample_images_are_valid(sample_images: dict[str, Path]) -> None:
    assert set(sample_images) == set(EXPECTED_FORMATS)
    for key, path in sample_images.items():
        assert path.is_file()
        with Image.open(path) as image:
            assert image.format == EXPECTED_FORMATS[key]
            if key == "gif":
                assert image.n_frames == 3
            image.verify()


def test_sample_comment_fixture_shape(sample_comment_html_path: Path) -> None:
    soup = BeautifulSoup(sample_comment_html_path.read_text(encoding="utf-8"), "html.parser")

    # Two comments, each with one attachment (the media later adapters must collect).
    comments = soup.select("article.comment")
    assert len(comments) == 2
    attachments = soup.select("article.comment a.attachment img")
    assert len(attachments) == 2
    assert {comment["data-comment-id"] for comment in comments} == {"918271", "918273"}

    # Page chrome present so adapters can prove it is ignored (AGENTS.md §6).
    assert soup.select_one(".comic-panel") is not None
    assert len(soup.select(".avatar")) == 2
    assert soup.select_one(".site-logo") is not None
    assert soup.select_one(".ad-banner") is not None

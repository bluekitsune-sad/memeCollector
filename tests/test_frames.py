"""GIF frame sampling tests (M3.4) — PRD §13 rules, Pillow only, offline."""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest
from PIL import Image

from backend.ai.frames import guess_mime_type, is_gif, is_video, sample_frames


def make_animated_gif(path: Path, *, frames: int, size: tuple[int, int] = (32, 32)) -> Path:
    """Write a GIF whose frames have distinct, exactly-representable red-channel colors."""
    layers = [Image.new("RGB", size, (index * 12, 0, 0)) for index in range(frames)]
    layers[0].save(
        path, format="GIF", save_all=True, append_images=layers[1:], duration=100, loop=0
    )
    return path


def decoded_pixels(frames: list[bytes]) -> list[tuple[int, int, int]]:
    """Decode sampled PNG frames and return one pixel per frame for comparison."""
    pixels: list[tuple[int, int, int]] = []
    for frame in frames:
        with Image.open(io.BytesIO(frame)) as image:
            assert image.format == "PNG"
            pixels.append(image.convert("RGB").getpixel((5, 5)))
    return pixels


def test_short_gif_samples_first_middle_last(tmp_path: Path) -> None:
    gif = make_animated_gif(tmp_path / "four.gif", frames=4)
    frames = sample_frames(gif)
    # PRD §13: short GIFs → first, middle, final frame (indices 0, 2, 3).
    assert len(frames) == 3
    reds = [pixel[0] for pixel in decoded_pixels(frames)]
    assert reds == [0, 24, 36]


def test_three_frame_gif_returns_all_frames(tmp_path: Path) -> None:
    gif = make_animated_gif(tmp_path / "three.gif", frames=3)
    frames = sample_frames(gif)
    assert len(frames) == 3
    reds = [pixel[0] for pixel in decoded_pixels(frames)]
    assert reds == [0, 12, 24]


def test_long_gif_is_evenly_spaced_up_to_max_frames(tmp_path: Path) -> None:
    gif = make_animated_gif(tmp_path / "long.gif", frames=20)
    frames = sample_frames(gif)
    assert len(frames) == 6  # default max_frames
    reds = [pixel[0] for pixel in decoded_pixels(frames)]
    assert len(set(reds)) == 6  # evenly spaced → distinct frames, endpoints included
    assert reds[0] == 0 and reds[-1] == 228


def test_max_frames_is_honoured(tmp_path: Path) -> None:
    gif = make_animated_gif(tmp_path / "long.gif", frames=20)
    assert len(sample_frames(gif, max_frames=4)) == 4
    assert len(sample_frames(gif, max_frames=1)) == 1


def test_static_image_yields_single_frame(tmp_path: Path) -> None:
    png = tmp_path / "still.png"
    Image.new("RGB", (32, 32), (9, 9, 9)).save(png)
    frames = sample_frames(png)
    assert len(frames) == 1
    assert decoded_pixels(frames) == [(9, 9, 9)]


def test_malformed_gif_returns_empty_list_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="backend.ai.frames")
    broken = tmp_path / "broken.gif"
    broken.write_bytes(b"this is definitely not a gif")
    assert sample_frames(broken) == []
    assert "frame sampling failed" in caplog.text


def test_truncated_gif_never_raises(tmp_path: Path) -> None:
    gif = make_animated_gif(tmp_path / "trunc.gif", frames=8)
    data = gif.read_bytes()
    truncated = tmp_path / "halved.gif"
    truncated.write_bytes(data[: len(data) // 2])
    frames = sample_frames(truncated)
    assert isinstance(frames, list)
    assert 0 <= len(frames) <= 8  # partial decode is acceptable; crashing is not


def test_missing_file_returns_empty_list(tmp_path: Path) -> None:
    assert sample_frames(tmp_path / "does_not_exist.gif") == []


def test_max_frames_must_be_positive(tmp_path: Path) -> None:
    png = tmp_path / "still.png"
    Image.new("RGB", (8, 8)).save(png)
    with pytest.raises(ValueError, match="max_frames"):
        sample_frames(png, max_frames=0)


def test_is_gif_checks_mime_then_extension() -> None:
    assert is_gif("image/gif")
    assert is_gif("IMAGE/GIF")
    assert not is_gif("image/png")
    assert is_gif(None, "gif")
    assert is_gif(None, ".GIF")
    assert not is_gif(None, None)
    assert not is_gif(None, "png")


def test_is_video_checks_mime_then_extension() -> None:
    assert is_video("video/mp4")
    assert is_video(None, "webm")
    assert is_video(None, ".MP4")
    assert not is_video("image/gif", "gif")
    assert not is_video(None, None)


def test_guess_mime_type_priority() -> None:
    assert guess_mime_type("image/webp", "png") == "image/webp"  # declared wins
    assert guess_mime_type(None, "jpeg") == "image/jpeg"
    assert guess_mime_type(None, ".PNG") == "image/png"
    assert guess_mime_type(None, None) == "image/jpeg"  # documented fallback

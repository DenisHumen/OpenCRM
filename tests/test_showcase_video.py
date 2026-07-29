"""Видео на витрине: беззвучное зацикленное превью прямо в сетке."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import API
from web.main import app

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="нужен ffmpeg для генерации тестового ролика"
)


@pytest.fixture(scope="module")
def mp4_bytes() -> bytes:
    directory = Path(tempfile.mkdtemp(prefix="opencrm-video-"))
    path = directory / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=320x200:rate=15:duration=1",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path)],
        check=True,
    )
    return path.read_bytes()


def test_showcase_autoplays_video_muted(manager_client, mp4_bytes):
    board = manager_client.post(f"{API}/boards", json={"title": "Витрина видео"}).json()
    upload = manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("clip.mp4", mp4_bytes, "video/mp4")},
    )
    assert upload.status_code == 202, upload.text
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    page = TestClient(app).get(f"/b/{share['token']}")
    assert page.status_code == 200
    assert "<video" in page.text
    # без muted браузер автозапуск запретит, без playsinline iOS откроет на весь экран
    for attribute in ("muted", "loop", "playsinline"):
        assert attribute in page.text
    # источник подставляет скрипт, когда ролик попал в кадр: иначе доска на
    # несколько видео тянула бы их все сразу
    assert "data-src=" in page.text
    assert 'preload="none"' in page.text
    assert "video.mp4" in page.text


def test_video_work_still_opens_in_lightbox(manager_client, mp4_bytes):
    """Клик по плитке открывает полноэкранный просмотр — там уже со звуком."""
    board = manager_client.post(f"{API}/boards", json={"title": "Видео лайтбокс"}).json()
    manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("clip.mp4", mp4_bytes, "video/mp4")},
    )
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    page = TestClient(app).get(f"/b/{share['token']}").text
    assert 'class="tile' in page
    assert 'data-index="0"' in page
    # в лайтбоксе плеер создаётся скриптом с controls и звуком
    assert 'el.controls = true' in page

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


def test_video_has_no_fragment_to_choose(manager_client, mp4_bytes):
    """У ролика на плитке показан постер — выбирать по вертикали нечего.

    Правило обрезки (`layout.is_cropped`) видео не касается: круг ▶ стоит ровно
    там, где встала бы плашка «открыть целиком», и два обещания в одной точке
    спорили бы друг с другом. Служба отказывает по той же причине.
    """
    board = manager_client.post(f"{API}/boards", json={"title": "Фрагмент ролика"}).json()
    work = manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("clip.mp4", mp4_bytes, "video/mp4")},
    ).json()
    rejected = manager_client.patch(
        f"{API}/boards/{board['id']}/works/{work['id']}", json={"preview_focus": 0.5}
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "not_a_croppable_work"


def test_video_keeps_the_centre_to_the_play_circle(manager_client, mp4_bytes):
    """Плашки «открыть целиком» у ролика нет — в центре кадра уже стоит ▶.

    Кнопку получила каждая картинка, и соблазн раздать её вообще всем велик.
    Но у видео центр кадра занят кругом воспроизведения: два знака в одной
    точке спорили бы, а обещание у них одно и то же — «откроется крупно».
    """
    board = manager_client.post(f"{API}/boards", json={"title": "Центр ролика"}).json()
    manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("clip.mp4", mp4_bytes, "video/mp4")},
    )
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    page = TestClient(app).get(f"/b/{share['token']}").text
    assert 'class="play"' in page
    assert 'class="more"' not in page

from pathlib import Path

import videocp.ytdlp as ytdlp


def test_download_with_ytdlp_prioritizes_resolution_before_codec(tmp_path: Path, monkeypatch):
    commands = []
    output_path = tmp_path / "video.mp4"

    class FakeResult:
        returncode = 0
        stderr = ""

    def fake_run(cmd, capture_output, text, timeout):
        commands.append(cmd)
        output_path.write_bytes(b"video")
        return FakeResult()

    monkeypatch.setattr(ytdlp.subprocess, "run", fake_run)

    ytdlp.download_with_ytdlp("https://www.youtube.com/watch?v=example", output_path, timeout_secs=10)

    sort_arg = commands[0][commands[0].index("-S") + 1]
    assert sort_arg.split(",")[:2] == ["res", "fps"]
    assert "vcodec:h264" in sort_arg

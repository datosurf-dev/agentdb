from pathlib import Path

from agentdb.core.file_integrity import compute_file_sha256, observe_file, observe_files


def test_compute_file_sha256_is_stable(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("hello agentdb", encoding="utf-8")
    h1 = compute_file_sha256(path)
    h2 = compute_file_sha256(path)
    assert h1 == h2
    assert len(h1) == 64


def test_observe_file_returns_expected_fields(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    file_path = root / "docs.md"
    file_path.write_text("docs", encoding="utf-8")

    observation = observe_file(file_path, base_dir=root).to_dict()
    assert observation["path"] == "docs.md"
    assert isinstance(observation["sha256"], str)
    assert observation["size_bytes"] == 4
    assert "T" in observation["captured_at"]
    assert "T" in observation["modified_at"]


def test_observe_files_collects_multiple(tmp_path):
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("a", encoding="utf-8")
    f2.write_text("b", encoding="utf-8")

    observations = observe_files([f1, f2])
    paths = {Path(item["path"]).name for item in observations}
    assert len(observations) == 2
    assert paths == {"a.txt", "b.txt"}

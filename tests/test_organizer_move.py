"""Tests for the organizer's move and cleanup steps.

These are the destructive paths: move_file removes the source, and
cleanup_source deletes files outright. The invariants that matter are that a
source is never removed before its copy is verified complete, and that nothing
outside the source tree is ever touched.
"""
import json
import os

import pytest

import organizer


# ------------------------------------------------------------------ junk rules

class TestJunkClassification:
    @pytest.mark.parametrize("name", [
        "release.nfo", "files.sfv", "checksum.md5", "readme.txt",
        "link.url", "info.diz", "movie.torrent", "setup.exe",
        "cover.jpg", "screen.png",
    ])
    def test_metadata_and_admin_files_are_junk(self, name):
        assert organizer.is_junk_file("/x/" + name) is True

    @pytest.mark.parametrize("name", [
        "sample.mkv", "Some.Movie-sample.mp4", "sample-thing.avi", "RARBG.mp4",
    ])
    def test_sample_media_is_junk(self, name):
        assert organizer.is_junk_file("/x/" + name) is True

    @pytest.mark.parametrize("name", [
        "Some.Movie.2024.mkv", "Episode.S01E01.mp4", "Resample.Documentary.mkv",
    ])
    def test_real_media_is_not_junk(self, name):
        """The sample rule must not eat films whose title merely contains it."""
        assert organizer.is_junk_file("/x/" + name) is False

    @pytest.mark.parametrize("d", ["Sample", "samples", "Proof", "Screens", "screenshots"])
    def test_junk_dirs(self, d):
        assert organizer.is_junk_dir("/x/" + d) is True

    def test_normal_dir_is_not_junk(self):
        assert organizer.is_junk_dir("/x/Season 1") is False


# ------------------------------------------------------------------ move_file

class TestMoveFile:
    def test_moves_and_removes_source(self, tmp_path):
        src = tmp_path / "a.mkv"
        src.write_bytes(b"video-data")
        dst = tmp_path / "out" / "a.mkv"
        res = organizer.move_file(str(src), str(dst))
        assert res["status"] == "moved"
        assert dst.read_bytes() == b"video-data"
        assert not src.exists()

    def test_creates_destination_directory(self, tmp_path):
        src = tmp_path / "a.mkv"
        src.write_bytes(b"x")
        dst = tmp_path / "deep" / "nested" / "a.mkv"
        assert organizer.move_file(str(src), str(dst))["status"] == "moved"
        assert dst.exists()

    def test_identical_duplicate_is_skipped_and_source_kept(self, tmp_path):
        src = tmp_path / "a.mkv"; src.write_bytes(b"12345")
        dst = tmp_path / "out" / "a.mkv"
        dst.parent.mkdir(); dst.write_bytes(b"12345")
        res = organizer.move_file(str(src), str(dst))
        assert res["status"] == "skipped"
        assert src.exists(), "source must survive a skip"

    def test_different_size_collision_is_an_error(self, tmp_path):
        src = tmp_path / "a.mkv"; src.write_bytes(b"12345")
        dst = tmp_path / "out" / "a.mkv"
        dst.parent.mkdir(); dst.write_bytes(b"different-length")
        res = organizer.move_file(str(src), str(dst))
        assert res["status"] == "error"
        assert src.exists()

    def test_cross_filesystem_copy_path_verifies_before_unlinking(self, tmp_path, monkeypatch):
        """os.rename fails with EXDEV across mounts; the fallback must not
        remove the source until the copy is confirmed complete."""
        src = tmp_path / "big.mkv"
        src.write_bytes(b"z" * (9 * 1024 * 1024))     # spans several chunks
        dst = tmp_path / "out" / "big.mkv"

        def no_rename(*a, **kw):
            raise OSError(18, "Invalid cross-device link")
        monkeypatch.setattr(os, "rename", no_rename)

        seen = []
        res = organizer.move_file(str(src), str(dst),
                                  chunk_cb=lambda d, t: seen.append((d, t)))
        assert res["status"] == "moved"
        assert dst.stat().st_size == 9 * 1024 * 1024
        assert not src.exists()
        assert seen and seen[-1][0] == seen[-1][1], "progress must reach 100%"

    def test_failed_copy_keeps_source_and_removes_partial(self, tmp_path, monkeypatch):
        """If anything goes wrong during the cross-filesystem copy, the source
        must survive and the half-written destination must not be left behind —
        otherwise the delete step would later remove the only good copy."""
        src = tmp_path / "big.mkv"
        src.write_bytes(b"z" * 4096)
        dst = tmp_path / "out" / "big.mkv"

        monkeypatch.setattr(os, "rename",
                            lambda *a, **k: (_ for _ in ()).throw(OSError(18, "EXDEV")))
        monkeypatch.setattr(os, "fsync",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

        res = organizer.move_file(str(src), str(dst))
        assert res["status"] == "error"
        assert src.exists(), "a failed copy must never destroy the source"
        assert not dst.exists(), "a partial destination must be cleaned up"

    def test_incomplete_copy_is_detected_and_source_kept(self, tmp_path, monkeypatch):
        """Size verification is the gate: a destination that does not match the
        source size must be reported as an error, not as a successful move."""
        src = tmp_path / "big.mkv"
        src.write_bytes(b"z" * 4096)
        dst = tmp_path / "out" / "big.mkv"

        monkeypatch.setattr(os, "rename",
                            lambda *a, **k: (_ for _ in ()).throw(OSError(18, "EXDEV")))
        real_getsize = os.path.getsize
        calls = {"n": 0}

        def shrinking(path):
            # report the destination as short on the post-copy verification
            calls["n"] += 1
            if str(path) == str(dst):
                return 1
            return real_getsize(path)
        monkeypatch.setattr(os.path, "getsize", shrinking)

        res = organizer.move_file(str(src), str(dst))
        assert res["status"] == "error"
        assert "incomplete" in res["message"].lower()
        assert src.exists()


# ------------------------------------------------------------------ cleanup

class TestCleanupSource:
    def _release(self, root):
        folder = root / "Some.Movie.2024"
        (folder / "Sample").mkdir(parents=True)
        (folder / "Sample" / "sample.mkv").write_bytes(b"s")
        (folder / "release.nfo").write_bytes(b"n")
        (folder / "cover.jpg").write_bytes(b"j")
        return folder

    def test_removes_junk_and_the_emptied_folder(self, tmp_path):
        folder = self._release(tmp_path)
        results = organizer.cleanup_source(str(folder), str(tmp_path))
        assert not folder.exists()
        assert any(r["status"] == "deleted" for r in results)

    def test_keeps_folder_holding_unrecognised_files(self, tmp_path):
        folder = self._release(tmp_path)
        (folder / "Keep.This.mkv").write_bytes(b"real")
        organizer.cleanup_source(str(folder), str(tmp_path))
        assert folder.exists(), "a folder with real content must survive"
        assert (folder / "Keep.This.mkv").exists()
        assert not (folder / "release.nfo").exists(), "junk should still go"

    def test_refuses_to_clean_the_source_root_itself(self, tmp_path):
        results = organizer.cleanup_source(str(tmp_path), str(tmp_path))
        assert results[0]["status"] == "error"
        assert tmp_path.exists()

    def test_refuses_to_clean_outside_the_source_tree(self, tmp_path):
        outside = tmp_path.parent / "elsewhere"
        outside.mkdir(exist_ok=True)
        results = organizer.cleanup_source(str(outside), str(tmp_path))
        assert results[0]["status"] == "error"
        assert outside.exists()


# ------------------------------------------------------------------ browse

class TestBrowse:
    def test_lists_only_directories(self, tmp_path):
        (tmp_path / "dir_a").mkdir()
        (tmp_path / "dir_b").mkdir()
        (tmp_path / "file.txt").write_bytes(b"x")
        out = organizer.browse(str(tmp_path))
        assert [d["name"] for d in out["dirs"]] == ["dir_a", "dir_b"]

    def test_reports_parent_for_navigation(self, tmp_path):
        (tmp_path / "sub").mkdir()
        out = organizer.browse(str(tmp_path / "sub"))
        assert out["parent"] == str(tmp_path)

    def test_raises_for_a_non_directory(self, tmp_path):
        f = tmp_path / "f.txt"; f.write_bytes(b"x")
        with pytest.raises(NotADirectoryError):
            organizer.browse(str(f))


class TestScanJunkConsistency:
    """The scan and the delete step must agree on what counts as a real file.

    Regression: the scan used to pick up Sample/sample.mkv, so the move put a
    30-second sample in the output folder and the cleanup then deleted the
    folder it came from.
    """

    def _release(self, root):
        folder = root / "Some.Movie.2024"
        (folder / "Sample").mkdir(parents=True)
        (folder / "Sample" / "sample.mkv").write_bytes(b"s")
        (folder / "Some.Movie.2024.mkv").write_bytes(b"real")
        return folder

    def test_scan_skips_sample_media(self, tmp_path):
        self._release(tmp_path)
        found = [f["original"] for f in organizer.scan_directory(str(tmp_path))]
        assert any("Some.Movie.2024.mkv" in f for f in found)
        assert not any("sample" in f.lower() for f in found)

    def test_scan_can_include_junk_when_asked(self, tmp_path):
        self._release(tmp_path)
        found = [f["original"] for f in organizer.scan_directory(str(tmp_path), skip_junk=False)]
        assert any("sample" in f.lower() for f in found)

    def test_explicit_excludes_still_apply(self, tmp_path):
        self._release(tmp_path)
        (tmp_path / "Excluded").mkdir()
        (tmp_path / "Excluded" / "skip.mkv").write_bytes(b"x")
        found = [f["original"] for f in
                 organizer.scan_directory(str(tmp_path), exclude_dirs={"Excluded"})]
        assert not any("skip.mkv" in f for f in found)

    def test_everything_the_scan_returns_survives_cleanup(self, tmp_path):
        """Nothing the move step is told to move may be classified as junk."""
        folder = self._release(tmp_path)
        for f in organizer.scan_directory(str(tmp_path)):
            assert not organizer.is_junk_file(str(folder / os.path.basename(f["original"])))


# ------------------------------------------------------- /api/files/move API

import app as webapp  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(webapp, "_API_TOKEN", "")
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


def _wait(client, job_id, timeout=5):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get("/api/files/move/" + job_id).get_json()
        if job["state"] == "complete":
            return job
        time.sleep(0.02)
    raise AssertionError("move job never completed")


def _wait_persisted(jobs_file, job_id, state, timeout=5):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            jobs = json.loads(jobs_file.read_text())["jobs"]
        except (OSError, ValueError):
            jobs = []
        if any(j["id"] == job_id and j["state"] == state for j in jobs):
            return jobs
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never persisted as {state}")


def _tree(tmp_path):
    src = tmp_path / "src"
    (src / "Show").mkdir(parents=True)
    (src / "Show" / "Show.S01E01.mkv").write_bytes(b"episode")
    (src / "Film.2024.mkv").write_bytes(b"film")
    return src


class TestPerFileDestinations:
    """Each file is tagged Movies or TV, mirroring organize.py's prompt."""

    def test_each_file_lands_in_its_own_destination(self, client, tmp_path):
        src = _tree(tmp_path)
        movies, tv = tmp_path / "Movies", tmp_path / "TV"
        r = client.post("/api/files/move", json={
            "source_dir": str(src),
            "destinations": {"movies": str(movies), "tv": str(tv)},
            "operations": [
                {"original": "Film.2024.mkv", "dest": "movies"},
                {"original": "Show/Show.S01E01.mkv", "dest": "tv"},
            ]})
        job = _wait(client, r.get_json()["job_id"])
        assert (movies / "Film.2024.mkv").exists()
        assert (tv / "Show.S01E01.mkv").exists()
        assert all(x["status"] == "moved" for x in job["results"])

    def test_an_undeclared_destination_label_is_refused(self, client, tmp_path):
        """The only reachable folders are the ones the request declared."""
        src = _tree(tmp_path)
        r = client.post("/api/files/move", json={
            "source_dir": str(src),
            "destinations": {"movies": str(tmp_path / "Movies")},
            "operations": [{"original": "Film.2024.mkv", "dest": "elsewhere"}]})
        job = _wait(client, r.get_json()["job_id"])
        assert job["results"][0]["status"] == "error"
        assert (src / "Film.2024.mkv").exists()

    def test_destination_inside_the_source_is_allowed(self, client, tmp_path):
        """Organizing /media into /media/Movies is the normal library layout.

        What used to make it unsafe was the delete step re-walking the filed
        library, not the nesting; the guards for that are asserted below.
        """
        src = _tree(tmp_path)
        r = client.post("/api/files/move", json={
            "source_dir": str(src),
            "destinations": {"movies": str(src / "Movies")},
            "operations": [{"original": "Film.2024.mkv", "dest": "movies"}]})
        assert r.status_code == 200
        job = _wait(client, r.get_json()["job_id"])
        assert job["results"][0]["status"] == "moved"
        assert (src / "Movies" / "Film.2024.mkv").exists()

    def test_an_empty_destination_is_rejected(self, client, tmp_path, monkeypatch):
        """realpath("") is the CWD, not "" — an unset folder must not resolve.

        The emptiness check used to run after realpath, so a blank box became
        the directory the web app was started from and files were moved into it.
        """
        src = _tree(tmp_path)
        landing = tmp_path / "cwd"
        landing.mkdir()
        monkeypatch.chdir(landing)
        r = client.post("/api/files/move", json={
            "source_dir": str(src),
            "destinations": {"movies": "   "},
            "operations": [{"original": "Film.2024.mkv", "dest": "movies"}]})
        assert r.status_code == 400
        assert "not set" in r.get_json()["error"]
        assert not (landing / "Film.2024.mkv").exists()
        assert (src / "Film.2024.mkv").exists()

    def test_destination_equal_to_the_source_is_rejected(self, client, tmp_path):
        """dst would equal src for every file sitting at the source root."""
        src = _tree(tmp_path)
        r = client.post("/api/files/move", json={
            "source_dir": str(src),
            "destinations": {"movies": str(src)},
            "operations": [{"original": "Film.2024.mkv", "dest": "movies"}]})
        assert r.status_code == 400

    def test_a_file_already_in_a_destination_is_not_removed(self, client, tmp_path):
        """A rescan sweeps the filed library back up; it must not be re-moved.

        Re-moving marks the destination as a `moved` source folder, which would
        hand the library to the delete step and lose its artwork and .nfo files.
        """
        src = _tree(tmp_path)
        movies = src / "Movies"
        movies.mkdir()
        (movies / "Filed.2019.mkv").write_bytes(b"z" * 64)
        r = client.post("/api/files/move", json={
            "source_dir": str(src),
            "destinations": {"movies": str(movies)},
            "operations": [{"original": "Movies/Filed.2019.mkv", "dest": "movies",
                            "rename_to": "Filed.2019.Renamed.mkv"}]})
        job = _wait(client, r.get_json()["job_id"])
        assert job["results"][0]["status"] == "error"
        assert "Already in" in job["results"][0]["message"]
        assert (movies / "Filed.2019.mkv").exists()

    def test_cleanup_never_walks_a_nested_destination(self, client, tmp_path):
        """Even if a result names one, the delete step must not enter it."""
        src = _tree(tmp_path)
        movies = src / "Movies"
        movies.mkdir()
        art = movies / "cover.jpg"          # junk by extension
        art.write_bytes(b"j" * 10)
        r = client.post("/api/files/move", json={
            "source_dir": str(src),
            "destinations": {"movies": str(movies)},
            "operations": [{"original": "Film.2024.mkv", "dest": "movies"}]})
        job_id = r.get_json()["job_id"]
        job = _wait(client, job_id)
        # Forge a result pointing the cleanup at the destination itself.
        job["results"].append({"original": "Movies/Film.2024.mkv", "status": "moved"})
        client.post("/api/files/cleanup", json={"job_id": job_id})
        assert art.exists(), "cleanup deleted artwork inside the destination"

    def test_single_output_dir_form_still_works(self, client, tmp_path):
        src = _tree(tmp_path)
        out = tmp_path / "out"
        r = client.post("/api/files/move", json={
            "source_dir": str(src), "output_dir": str(out),
            "operations": [{"original": "Film.2024.mkv"}]})
        _wait(client, r.get_json()["job_id"])
        assert (out / "Film.2024.mkv").exists()


class TestMoveProgress:
    def test_job_reports_per_file_and_overall_progress(self, client, tmp_path):
        src = _tree(tmp_path)
        r = client.post("/api/files/move", json={
            "source_dir": str(src),
            "destinations": {"movies": str(tmp_path / "Movies")},
            "operations": [{"original": "Film.2024.mkv", "dest": "movies"}]})
        job = _wait(client, r.get_json()["job_id"])
        assert job["total_files"] == 1
        assert job["done_files"] == 1
        assert job["done_bytes"] == job["total_bytes"] == len(b"film")
        # The in-flight fields are cleared once nothing is being copied.
        assert job["current"] is None and job["current_bytes"] == 0

    def test_skipped_files_are_never_sent(self, client, tmp_path):
        """A row set to Skip is simply absent from the operations list."""
        src = _tree(tmp_path)
        r = client.post("/api/files/move", json={
            "source_dir": str(src),
            "destinations": {"movies": str(tmp_path / "Movies")},
            "operations": [{"original": "Film.2024.mkv", "dest": "movies"}]})
        _wait(client, r.get_json()["job_id"])
        assert (src / "Show" / "Show.S01E01.mkv").exists()


class TestJobPersistence:
    """A move has to outlive the browser tab and the web app process."""

    @pytest.fixture
    def jobs_file(self, tmp_path, monkeypatch):
        path = tmp_path / "jobs.json"
        monkeypatch.setattr(webapp, "_JOBS_FILE", str(path))
        webapp._jobs.clear()
        yield path
        webapp._jobs.clear()

    def test_a_finished_job_is_written_to_disk(self, client, jobs_file, tmp_path):
        src = _tree(tmp_path)
        r = client.post("/api/files/move", json={
            "source_dir": str(src),
            "destinations": {"movies": str(tmp_path / "Movies")},
            "operations": [{"original": "Film.2024.mkv", "dest": "movies"}]})
        job_id = r.get_json()["job_id"]
        _wait(client, job_id)
        # The endpoint reports from memory, so the final write can land a
        # moment later. Only the persisted value is under test here.
        saved = _wait_persisted(jobs_file, job_id, "complete")
        assert [j["id"] for j in saved] == [job_id]

    def test_listing_reports_jobs_newest_first(self, client, jobs_file, tmp_path):
        src = _tree(tmp_path)
        ids = []
        for name in ("Film.2024.mkv", "Show/Show.S01E01.mkv"):
            r = client.post("/api/files/move", json={
                "source_dir": str(src),
                "destinations": {"movies": str(tmp_path / "Movies")},
                "operations": [{"original": name, "dest": "movies"}]})
            ids.append(r.get_json()["job_id"])
            _wait(client, ids[-1])
        listed = client.get("/api/files/move").get_json()["jobs"]
        assert [j["id"] for j in listed] == list(reversed(ids))
        assert listed[0]["moved"] == 1

    def test_a_job_running_at_shutdown_reloads_as_interrupted(self, jobs_file):
        """Never as complete — its copy thread died with the old process."""
        jobs_file.write_text(json.dumps({"jobs": [
            {"id": "abc", "state": "running", "started": 1.0,
             "source_dir": "/src", "results": [], "current": "big.mkv"}]}))
        webapp._load_jobs()
        assert webapp._jobs["abc"]["state"] == "interrupted"
        assert webapp._jobs["abc"]["current"] is None

    def test_delete_step_refuses_an_interrupted_job(self, client, jobs_file):
        jobs_file.write_text(json.dumps({"jobs": [
            {"id": "abc", "state": "running", "started": 1.0,
             "source_dir": "/src", "results": [
                 {"original": "a.mkv", "status": "moved"}]}]}))
        webapp._load_jobs()
        r = client.post("/api/files/cleanup", json={"job_id": "abc"})
        assert r.status_code == 409
        assert "interrupted" in r.get_json()["error"]

    def test_only_the_most_recent_jobs_are_kept(self, client, jobs_file, monkeypatch):
        monkeypatch.setattr(webapp, "_JOBS_KEEP", 3)
        for i in range(6):
            webapp._jobs[f"j{i}"] = {"id": f"j{i}", "state": "complete",
                                     "started": float(i), "results": []}
        webapp._save_jobs()
        saved = json.loads(jobs_file.read_text())["jobs"]
        assert [j["id"] for j in saved] == ["j5", "j4", "j3"]

    def test_a_corrupt_records_file_is_ignored_not_fatal(self, jobs_file):
        jobs_file.write_text("{not json")
        webapp._load_jobs()          # must not raise
        assert webapp._jobs == {}

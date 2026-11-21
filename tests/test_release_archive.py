import hashlib
import zipfile

import pytest

import windows.encrypt_release as release_archive
from windows.encrypt_release import _is_aes256, _run, _seven_zip, encrypt_release


@pytest.fixture
def seven_zip():
    try:
        return _seven_zip()
    except FileNotFoundError:
        pytest.skip("7-Zip is required for the encrypted release test")


def test_release_archive_encrypts_and_verifies_both_files(tmp_path, seven_zip):
    package = tmp_path / "test-signed.msix"
    package.write_bytes(b"synthetic release package")
    archive = tmp_path / "release.zip"
    password = "synthetic-password-for-testing"

    encrypt_release(package, archive, password, seven_zip=seven_zip)

    with zipfile.ZipFile(archive) as result:
        assert {entry.filename for entry in result.infolist()} == {
            package.name, package.name + ".sha256"
        }
        assert all(_is_aes256(entry) for entry in result.infolist())
    assert _run(["t", str(archive)], password, seven_zip=seven_zip) == 0
    assert _run(["t", str(archive)], "incorrect-password", seven_zip=seven_zip) != 0
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    assert (tmp_path / "release.zip.sha256").read_text().split()[0] == digest


def test_release_archive_rejects_short_password_and_existing_output(tmp_path, seven_zip):
    package = tmp_path / "test.msix"
    package.write_bytes(b"synthetic package")
    archive = tmp_path / "release.zip"
    with pytest.raises(ValueError, match="at least 16 characters"):
        encrypt_release(package, archive, "short", seven_zip=seven_zip)
    archive.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        encrypt_release(package, archive, "synthetic-password-for-testing", seven_zip=seven_zip)
    assert archive.read_bytes() == b"existing"
    archive.unlink()
    checksum = tmp_path / "release.zip.sha256"
    checksum.write_text("existing checksum")
    with pytest.raises(FileExistsError):
        encrypt_release(package, archive, "synthetic-password-for-testing", seven_zip=seven_zip)
    assert checksum.read_text() == "existing checksum"
    assert not archive.exists()


def test_release_archive_cleans_up_after_seven_zip_failure(tmp_path, monkeypatch):
    package = tmp_path / "test.msix"
    package.write_bytes(b"synthetic package")
    archive = tmp_path / "release.zip"
    monkeypatch.setattr(release_archive, "_run", lambda *args, **kwargs: 1)

    with pytest.raises(RuntimeError, match="Could not create"):
        encrypt_release(package, archive, "synthetic-password-for-testing", seven_zip="7z")

    assert list(tmp_path.iterdir()) == [package]

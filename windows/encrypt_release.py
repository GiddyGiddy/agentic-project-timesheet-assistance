import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path


def _seven_zip() -> str:
    executable = shutil.which("7z")
    if executable:
        return executable
    candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "7-Zip" / "7z.exe"
    if candidate.is_file():
        return str(candidate)
    raise FileNotFoundError("7-Zip is required to create the encrypted release archive")


def _run(arguments: list[str], password: str, *, seven_zip: str) -> int:
    environment = os.environ.copy()
    environment.pop("RELEASE_ARCHIVE_PASSWORD", None)
    result = subprocess.run(
        [seven_zip, *arguments],
        input=password + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=180,
        check=False,
    )
    return result.returncode


def _is_aes256(entry: zipfile.ZipInfo) -> bool:
    extra = entry.extra
    while len(extra) >= 4:
        kind, length = struct.unpack_from("<HH", extra)
        if len(extra) < 4 + length:
            return False
        data, extra = extra[4:4 + length], extra[4 + length:]
        if kind == 0x9901 and len(data) >= 7 and data[4] == 3:
            return bool(entry.flag_bits & 1) and entry.compress_type == 99
    return False


def encrypt_release(package: Path, destination: Path, password: str, *, seven_zip: str) -> None:
    if len(password) < 16 or any(character in password for character in "\r\n\0"):
        raise ValueError("The archive password must be at least 16 characters and contain no line breaks or NUL")
    package = package.resolve(strict=True)
    if not package.is_file() or package.suffix.lower() != ".msix":
        raise ValueError("The release input must be an MSIX file")
    destination = destination.resolve()
    checksum = destination.with_name(destination.name + ".sha256")
    if destination.suffix.lower() != ".zip" or destination.exists() or checksum.exists():
        raise FileExistsError("Choose a new ZIP and checksum destination")
    if not destination.parent.is_dir():
        raise FileNotFoundError("The release archive directory does not exist")

    with tempfile.TemporaryDirectory(prefix=".release-", dir=destination.parent) as directory:
        temporary = Path(directory)
        package_checksum = temporary / (package.name + ".sha256")
        with package.open("rb") as source:
            package_digest = hashlib.file_digest(source, "sha256").hexdigest()
        package_checksum.write_text(
            f"{package_digest}  {package.name}\n",
            encoding="ascii",
        )
        archive = temporary / destination.name
        if _run(
            ["a", "-tzip", "-mem=AES256", "-sccUTF-8", "-p", str(archive),
             str(package), str(package_checksum)],
            password,
            seven_zip=seven_zip,
        ) != 0:
            raise RuntimeError("Could not create the encrypted release archive")
        with zipfile.ZipFile(archive) as result:
            entries = result.infolist()
            if (
                len(entries) != 2
                or {item.filename for item in entries} != {package.name, package_checksum.name}
                or not all(_is_aes256(item) for item in entries)
            ):
                raise ValueError("Release archive does not contain exactly two AES-256 encrypted files")
        if _run(["t", "-sccUTF-8", str(archive)], password, seven_zip=seven_zip) != 0:
            raise RuntimeError("The encrypted release failed password and integrity verification")
        if _run(["t", "-sccUTF-8", str(archive)], password + "-incorrect", seven_zip=seven_zip) == 0:
            raise RuntimeError("The encrypted release accepted an incorrect password")

        created = False
        checksum_created = False
        try:
            with archive.open("rb") as source, destination.open("xb") as target:
                created = True
                shutil.copyfileobj(source, target)
            with destination.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            with checksum.open("x", encoding="ascii", newline="\n") as output:
                checksum_created = True
                output.write(f"{digest}  {destination.name}\n")
        except OSError:
            if checksum_created:
                checksum.unlink()
            if created:
                destination.unlink()
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a verified AES-256 release archive")
    parser.add_argument("package", type=Path)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    password = os.environ.pop("RELEASE_ARCHIVE_PASSWORD", "")
    encrypt_release(args.package, args.archive, password, seven_zip=_seven_zip())
    print(f"Verified encrypted archive and checksum: {args.archive.name}")


if __name__ == "__main__":
    main()

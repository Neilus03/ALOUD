"""Build a drag-to-Applications disk image on an Apple-silicon Mac."""

import argparse
from importlib.metadata import distribution
from pathlib import Path
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
from tempfile import TemporaryDirectory
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
MACHO_HEADERS = {
    bytes.fromhex(header) for header in ("cffaedfe", "feedfacf", "cafebabe", "cafebabf")
}


def run(*command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def copy_dependencies(site_packages):
    requirements = ROOT / "requirements-macos-arm64.txt"
    for line in requirements.read_text().splitlines():
        requirement = line.strip()
        if not requirement or requirement.startswith("#"):
            continue
        name, pinned, expected = requirement.partition("==")
        if not pinned:
            name, expected = requirement.split(" @ ", 1)
        dependency = distribution(name)
        actual = (dependency.version if pinned else
                  json.loads(dependency.read_text("direct_url.json") or "{}").get("url"))
        if actual != expected:
            raise SystemExit(f"Install the pinned build dependency {requirement} before building.")

        if dependency.files is None:
            raise SystemExit(f"Missing installed-file list for {dependency.name}.")
        for entry in dependency.files:
            if (".." in entry.parts or entry.is_absolute() or entry.suffix == ".pyc"
                    or entry.name == "direct_url.json"):
                continue
            target = site_packages / entry
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dependency.locate_file(entry), target)


def install_app_source(site_packages, work):
    source = work / "source"
    source.mkdir()
    for name in ("aloud", "licenses"):
        shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
    for name in ("LICENSE", "README.md", "pyproject.toml"):
        shutil.copy2(ROOT / name, source / name)
    wheels = work / "wheels"
    run("uv", "build", "--wheel", "--offline", "--no-cache", "--no-build-isolation",
        "--python", sys.executable, "--out-dir", wheels, source)
    wheel, = wheels.glob("aloud_macos-*.whl")
    with ZipFile(wheel) as package:
        package.extractall(site_packages)


def build_runtime(work):
    prefix = work / "python"
    stdlib = Path(sysconfig.get_path("stdlib"))
    runtime_stdlib = prefix / "lib" / stdlib.name
    shutil.copytree(stdlib, runtime_stdlib,
                    ignore=shutil.ignore_patterns("site-packages", "__pycache__", ".DS_Store"))
    python = prefix / "bin" / stdlib.name
    python.parent.mkdir()
    shutil.copy2(sys._base_executable, python)
    dylib = sysconfig.get_config_var("LDLIBRARY")
    source_dylib = Path(sysconfig.get_config_var("LIBDIR")) / dylib
    shutil.copy2(source_dylib, prefix / "lib" / dylib)
    run("install_name_tool", "-id", f"@rpath/{dylib}", prefix / "lib" / dylib)
    site_packages = runtime_stdlib / "site-packages"
    copy_dependencies(site_packages)
    install_app_source(site_packages, work)
    return prefix, python, dylib


def sign_app(app, identity):
    flags = ["--timestamp", "--options", "runtime"] if identity else ["--timestamp=none"]
    code = []
    for path in app.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as file:
                if file.read(4) in MACHO_HEADERS:
                    code.append(path.relative_to(app))
    run("codesign", "--force", "--sign", identity or "-", *flags, *code, cwd=app)
    run("codesign", "--force", "--sign", identity or "-", *flags, app)
    run("codesign", "--verify", "--deep", "--strict", app)


def build_stage(stage, work, identity, alba_assets):
    prefix, python, dylib = build_runtime(work)
    if alba_assets is not None:
        destination = prefix / "lib" / python.name / "site-packages" / "aloud" / "assets" / "alba"
        destination.mkdir()
        for name in ("model.safetensors", "tokenizer.model", "alba.safetensors", "cavaalign.onnx"):
            shutil.copy2(alba_assets / name, destination / name)
        run(python, "-I", "-B", "-c",
            "from aloud.pocket import ASSETS, alba_asset\nfor name in ASSETS:\n    alba_asset(name)")
    stage.mkdir()
    run(python, "-I", "-B", ROOT / "scripts/make-app.py", stage)
    app = stage / "ALOUD.app"
    resources = app / "Contents/Resources"
    launcher = app / "Contents/MacOS/ALOUD"
    shutil.move(prefix, resources / prefix.name)
    shutil.copy2(ROOT / "THIRD_PARTY.md", resources / "Third-Party Notices.md")
    run("clang", "-Wall", "-Wextra", "-arch", "arm64", "-mmacosx-version-min=14.0",
        "-I", sysconfig.get_path("include"), ROOT / "scripts/aloud-launcher.c",
        resources / "python/lib" / dylib, "-Wl,-rpath,@loader_path/../Resources/python/lib",
        "-o", launcher)
    shutil.copy2(ROOT / "examples/reading-in-sync.pdf", resources / "Reading in Sync.pdf")
    (stage / "Applications").symlink_to("/Applications", target_is_directory=True)
    sign_app(app, identity)
    run(launcher, "--version")
    run(resources / "python/bin" / python.name, "-I", "-B", ROOT / "scripts/smoke-test.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="output DMG path or staging directory")
    parser.add_argument("--stage-only", action="store_true", help="prepare the folder to image in Disk Utility")
    parser.add_argument("--alba-assets", type=Path, help="bundle locally downloaded Alba and CavaAlign model files")
    parser.add_argument("--sign-identity", help="Developer ID Application signing identity")
    parser.add_argument("--notary-profile", help="notarytool keychain profile for the signed disk image")
    args = parser.parse_args()
    if sys.platform != "darwin" or platform.machine() != "arm64" or sys.version_info[:2] != (3, 12):
        parser.error("Build with the managed arm64 Python 3.12 on macOS.")
    if args.notary_profile and (not args.sign_identity or args.stage_only):
        parser.error("--notary-profile requires a signing identity and a disk image output.")

    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        parser.error(f"Output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".aloud-build-", dir=output.parent) as scratch:
        work = Path(scratch)
        stage = work / "ALOUD"
        build_stage(stage, work, args.sign_identity, args.alba_assets)
        if args.stage_only:
            stage.rename(output)
        else:
            image = work / "ALOUD.dmg"
            run("hdiutil", "create", "-format", "UDZO", "-volname", "ALOUD",
                "-fs", "HFS+", "-srcfolder", stage, image)
            run("hdiutil", "verify", image)
            if args.sign_identity:
                run("codesign", "--sign", args.sign_identity, "--timestamp", image)
            if args.notary_profile:
                result = run("xcrun", "notarytool", "submit", image, "--keychain-profile",
                             args.notary_profile, "--wait", "--output-format", "json",
                             capture_output=True, text=True)
                submission = json.loads(result.stdout)
                if submission["status"] != "Accepted":
                    raise SystemExit(f"Notarization {submission['status']} for submission {submission['id']}.")
                run("xcrun", "stapler", "staple", image)
                run("xcrun", "stapler", "validate", image)
            os.link(image, output)
    print(f"Built {output}")


if __name__ == "__main__":
    main()

"""Create a traceable Windows/MinGW portable package from a clean committed build."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mingw-bin", default=r"C:\msys64\mingw64\bin")
    args = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("This portable package recipe is for the verified Windows/MinGW build")
    root = Path(__file__).resolve().parent.parent
    build, output, compiler = Path(args.build).resolve(), Path(args.output).resolve(), Path(args.mingw_bin).resolve()
    if output.drive.upper() == "C:":
        raise RuntimeError("Store release artifacts on E: or D:")
    env = os.environ.copy()
    env["PATH"] = str(compiler) + ";" + env.get("PATH", "")
    flags = subprocess.CREATE_NO_WINDOW
    version = json.loads(subprocess.run([str(build / "bin/bus_backend.exe"), "--version"], env=env, capture_output=True,
                                        text=True, timeout=5, check=True, creationflags=flags).stdout)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True,
                              creationflags=flags).stdout.strip()
    changes = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True,
                             creationflags=flags).stdout
    if changes or version["revision"] != revision:
        raise RuntimeError("Package requires a clean source tree and binaries built from its exact commit")
    output.mkdir(parents=True, exist_ok=False)
    (output / "bin").mkdir()
    for name in ("bus_backend.exe", "bus_capture_worker.exe", "bus_analyze.exe", "bus_simulated.dll", "bus_replay.dll"):
        shutil.copy2(build / "bin" / name, output / "bin" / name)
    for name in ("libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll"):
        shutil.copy2(compiler / name, output / "bin" / name)
    licenses = compiler.parent / "share/licenses"
    for name in ("gcc-libs", "libwinpthread"):
        shutil.copytree(licenses / name, output / "licenses" / name)
    shutil.copytree(root / "config", output / "config")
    shutil.copytree(root / "docs", output / "docs")
    shutil.copy2(root / "README.md", output / "README.md")
    subprocess.run(["git", "archive", "--format=zip", "--output=" + str(output / "source.zip"), revision],
                   cwd=root, check=True, creationflags=flags)
    files = {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(output.rglob("*")) if path.is_file()}
    manifest = {"build": version, "platform": "windows-x64-mingw", "acceptance_scope": "software-architecture-baseline",
                "hardware_acceptance": "pending-selected-bus-sdk-and-workload", "files": files}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files["manifest.json"] = hashlib.sha256((output / "manifest.json").read_bytes()).hexdigest()
    (output / "SHA256SUMS.txt").write_text("".join(f"{digest}  {name}\n" for name, digest in sorted(files.items())), encoding="utf-8")
    archive = Path(shutil.make_archive(str(output), "zip", output.parent, output.name))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(str(archive) + ".sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(json.dumps({"revision": revision, "package": str(archive), "sha256": digest, "files": len(files)}))


if __name__ == "__main__":
    main()

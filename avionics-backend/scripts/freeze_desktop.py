"""Build two native entry points sharing one bundled Python/Qt runtime."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import textwrap


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    work = output.parent / (output.name + "-freeze-build")
    if output.drive.upper() not in {"D:","E:"}:
        raise ValueError("Release builds must stay on D/E.")
    work.mkdir(parents=True,exist_ok=False)
    (work/"temp").mkdir()
    env = os.environ.copy()
    env.update(TEMP=str(work/"temp"),TMP=str(work/"temp"),PYINSTALLER_CONFIG_DIR=str(work/"cache"),PYTHONDONTWRITEBYTECODE="1")
    flags = subprocess.CREATE_NO_WINDOW
    info = json.loads(subprocess.run([str(args.python),"-B","-c",
        "import sys,PySide6,PyInstaller,sysconfig,json; print(json.dumps({'python':sys.version,'qt':PySide6.__version__,'pyinstaller':PyInstaller.__version__,'site':sysconfig.get_path('purelib'),'base':sys.base_prefix}))"],
        capture_output=True,text=True,env=env,check=True,creationflags=flags).stdout)
    site,base = Path(info.pop("site")),Path(info.pop("base"))
    env["PATH"] = str(base/"Library/bin")+os.pathsep+str(base/"DLLs")+os.pathsep+env["PATH"]
    if info["qt"] != "6.9.2" or info["pyinstaller"] != "6.22.3":
        raise RuntimeError("Install the pinned frontend/requirements-release.txt.")
    icon = work/"AEROBUS.ico"
    subprocess.run([str(args.python),"-B","-c",
        "import sys; from PySide6.QtGui import QImage; image=QImage(sys.argv[1]); assert not image.isNull(); assert image.scaled(256,256).save(sys.argv[2])",
        str(root/"asset/icons/monitor.png"),str(icon)],check=True,env=env,creationflags=flags)
    spec = work/"AEROBUS.spec"
    spec.write_text(textwrap.dedent(f"""\
        a = Analysis([{str(root/'frontend/frozen_entry.py')!r}],pathex=[{str(root/'frontend')!r}],
                     datas=[({str(root/'frontend/ground_station/plugins.json')!r},'ground_station')],
                     binaries=[({str(base/'Library/bin/sqlite3.dll')!r},'.'),({str(base/'Library/bin/ffi.dll')!r},'.')],
                     hiddenimports=[],hookspath=[],excludes=[],noarchive=False)
        pyz = PYZ(a.pure)
        gui = EXE(pyz,a.scripts,[],exclude_binaries=True,name='AEROBUS',console=False,debug=False,upx=False,icon={str(icon)!r})
        worker = EXE(pyz,a.scripts,[],exclude_binaries=True,name='AEROBUS-Analysis',console=True,debug=False,upx=False,icon={str(icon)!r})
        app = COLLECT(gui,worker,a.binaries,a.datas,strip=False,upx=False,name='runtime')
        """),encoding="utf-8")
    with (work/"freeze.log").open("w",encoding="utf-8") as log:
        try:
            subprocess.run([str(args.python),"-B","-m","PyInstaller","--noconfirm","--distpath",str(work/"dist"),
                "--workpath",str(work/"build"),str(spec)],stdout=log,stderr=subprocess.STDOUT,check=True,env=env,creationflags=flags)
        except subprocess.CalledProcessError as error:
            raise RuntimeError("Desktop build failed; inspect "+str(work/"freeze.log")) from error
    shutil.copytree(work/"dist/runtime",output,dirs_exist_ok=True)
    for name in ("pyside6-6.9.2.dist-info","pyside6_essentials-6.9.2.dist-info","pyside6_addons-6.9.2.dist-info",
                 "shiboken6-6.9.2.dist-info","pyinstaller-6.22.3.dist-info"):
        shutil.copytree(site/name,output/"licenses"/name)
    notices = []
    python_meta = list((base/"conda-meta").glob("python-3.13.9-*.json"))
    if len(python_meta) != 1:
        raise RuntimeError("This release recipe expects the audited Python 3.13.9 build.")
    python_package = json.loads(python_meta[0].read_text(encoding="utf-8"))
    packages = [python_package]
    dependencies = {dep.split()[0] for dep in python_package["depends"]} - {"pip","tk"}
    for path in (base/"conda-meta").glob("*.json"):
        package = json.loads(path.read_text(encoding="utf-8"))
        if package["name"] in dependencies:
            packages.append(package)
    for package in packages:
        directory = Path(package["extracted_package_dir"])/"info/licenses"
        notices.append({key:package[key] for key in ("name","version","license","url")})
        if directory.exists():
            shutil.copytree(directory,output/"licenses/conda"/package["name"])
    (output/"licenses/conda/notices.json").write_text(json.dumps(notices,indent=2),encoding="utf-8")
    (output/"THIRD-PARTY-NOTICES.txt").write_text(
        "AEROBUS bundles Python, PySide6/Qt, shiboken6, the PyInstaller bootloader and MinGW runtime libraries.\n"
        "Component license texts and package/source notices are in licenses/.\n"
        "Qt libraries remain separate DLLs. Matching sources:\n"
        "https://download.qt.io/archive/qt/6.9/6.9.2/\n"
        "https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.9.2\n"
        "https://github.com/pyinstaller/pyinstaller/tree/v6.22.3\n"
        "Application sources and build instructions: source.zip, docs/release-v0.6.md.\n",encoding="utf-8")
    (output/"Start-AEROBUS.cmd").write_text('@echo off\nstart "" "%~dp0AEROBUS.exe" %*\n',encoding="ascii")
    print(json.dumps(info))


if __name__ == "__main__":
    main()

import argparse
import os
from pathlib import Path
import sys

# All Python and Qt temporary output follows the configured data directory.
sys.dont_write_bytecode = True


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="AEROBUS Qt ground-test desktop")
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=root / "runs" / "desktop")
    parser.add_argument("--config", type=Path, default=root / "config" / "correlation-demo.ini")
    args = parser.parse_args()
    from .backend import writable_data_path
    data_dir = writable_data_path(str(args.data_dir))
    temp_dir = data_dir / ".temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = str(temp_dir)
    from PySide6.QtWidgets import QApplication
    from .window import MainWindow
    app = QApplication(sys.argv[:1])
    app.setApplicationName("AEROBUS Ground Station")
    app.setStyle("Fusion")
    window = MainWindow(args.bin_dir.resolve(), data_dir, args.config.resolve())
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

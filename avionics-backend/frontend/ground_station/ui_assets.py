"""Shared paths for the workbench's generated pictograms and vector marks."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap

ASSET_DIR = Path(__file__).resolve().parents[2] / "asset"


def icon(name, light=False):
    result = QIcon(str(ASSET_DIR / "icons" / f"{name}.png"))
    if light:
        pixmap = result.pixmap(64, 64)
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), Qt.GlobalColor.white)
        painter.end()
        return QIcon(pixmap)
    return result


def artwork(name):
    return QPixmap(str(ASSET_DIR / "illustrations" / f"{name}.png"))

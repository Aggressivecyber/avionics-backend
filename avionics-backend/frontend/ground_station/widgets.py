"""Native splitter handles with a thin visual line and a full-size hit region."""
from PySide6.QtCore import QEvent, QLineF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QSplitter, QSplitterHandle


class ThinSplitterHandle(QSplitterHandle):
    def paintEvent(self, event):
        color = QColor(19, 20, 22, 55)
        if self.underMouse():
            color.setAlpha(145 if QApplication.mouseButtons() & Qt.MouseButton.LeftButton else 100)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(color, 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if self.orientation() == Qt.Orientation.Horizontal:
            painter.drawLine(QLineF(self.width() / 2, 12, self.width() / 2, self.height() - 12))
        else:
            painter.drawLine(QLineF(12, self.height() / 2, self.width() - 12, self.height() / 2))

    def event(self, event):
        handled = super().event(event)
        if event.type() in (QEvent.Type.Enter, QEvent.Type.Leave,
                            QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            self.update()
        return handled


class ThinSplitter(QSplitter):
    def createHandle(self):
        return ThinSplitterHandle(self.orientation(), self)

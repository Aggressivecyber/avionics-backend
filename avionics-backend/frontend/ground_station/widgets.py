"""Native splitter handles with a thin visual line and a full-size hit region."""
from PySide6.QtCore import QEvent, QLineF, Qt, QEasingCurve, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QSplitter, QSplitterHandle, QPushButton,
    QStyle, QStyleOptionButton, QStylePainter, QStackedWidget, QGraphicsOpacityEffect, QLabel)


class MotionButton(QPushButton):
    """Press feedback changes painting only: layout, hit area and click timing stay fixed."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.press_depth = 0.0
        self.motion = QVariantAnimation(self)
        self.motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.motion.valueChanged.connect(self._paint_depth)
        self.pressed.connect(lambda: self._animate(1.0, 75))
        self.released.connect(lambda: self._animate(0.0, 140))

    def _animate(self, target, duration):
        self.motion.stop()
        self.motion.setDuration(duration)
        self.motion.setStartValue(self.press_depth)
        self.motion.setEndValue(target)
        self.motion.start()

    def _paint_depth(self, value):
        self.press_depth = value
        self.update()

    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        painter = QStylePainter(self)
        painter.translate(self.width()/2, self.height()/2 + self.press_depth)
        painter.scale(1 - .025*self.press_depth, 1 - .025*self.press_depth)
        painter.translate(-self.width()/2, -self.height()/2)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)


class MotionStack(QStackedWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.fade = QVariantAnimation(self)
        self.fade.setDuration(170)
        self.fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.fade.valueChanged.connect(self._opacity)
        self.fade.finished.connect(self._finish)
        self.fading_page = None

    def _opacity(self, value):
        self.fading_page.graphicsEffect().setOpacity(value)

    def _finish(self):
        if self.fading_page is not None:
            self.fading_page.setGraphicsEffect(None)
            self.fading_page = None

    def setCurrentIndex(self, index):
        if index == self.currentIndex():
            return
        self.fade.stop()
        self._finish()
        super().setCurrentIndex(index)
        if self.isVisible():
            self.fading_page = self.currentWidget()
            effect = QGraphicsOpacityEffect(self.fading_page)
            effect.setOpacity(.72)
            self.fading_page.setGraphicsEffect(effect)
            self.fade.setStartValue(.72)
            self.fade.setEndValue(1.0)
            self.fade.start()


class FeedbackLabel(QLabel):
    """Fade status changes once; ordinary polling of the same status never restarts it."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.effect)
        self.effect.setOpacity(1)
        self.motion = QVariantAnimation(self)
        self.motion.setDuration(170)
        self.motion.valueChanged.connect(self.effect.setOpacity)

    def setText(self, text):
        changed = text != self.text()
        super().setText(text)
        if changed and self.isVisible():
            self.motion.stop()
            self.motion.setStartValue(.55)
            self.motion.setEndValue(1.0)
            self.motion.start()


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

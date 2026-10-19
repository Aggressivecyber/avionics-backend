"""Framed-sheet tokens shared by Qt controls, tables and native plots."""

GROUND = "#f9fbfc"
SURFACE = "#ffffff"
RAISED = "#f4f6f8"
ACTIVE = "#ebedee"
BORDER = "#e8eaec"
RULE = "#f0f1f2"
CONTROL = "#cdced1"
INK = "#131416"
MUTED = "#5d5f60"
ACCENT = "#1b6c80"
POSITIVE = "#1e7b4f"
NEGATIVE = "#c4242a"
WARNING = "#92400e"
ALERT_STYLE = "background:#fffbeb;color:#92400e;"
RUNNING_STYLE = "background:#dcfae5;color:#04250e;"

STYLE = f"""
QWidget {{ font-family: 'Microsoft YaHei UI'; font-size: 14px; color: {INK}; }}
QMainWindow, QWidget#frame, QFrame#rail {{ background: {GROUND}; }}
QDialog {{ background: {GROUND}; }}
QWidget#pluginGrid {{ background: {SURFACE}; }}
QFrame#sheet {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 16px; }}
QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; }}
QFrame#tray {{ background: {RAISED}; border: none; border-radius: 8px; }}
QFrame#rail {{ border: none; }}
QFrame#rail QLabel#brand {{ font-size: 21px; font-weight: 600; }}
QFrame#rail QPushButton {{ background: transparent; color: {MUTED}; border: 1px solid transparent;
                         border-radius: 6px; padding: 8px 12px; text-align: left; }}
QFrame#rail QPushButton:hover {{ background: {RAISED}; color: {INK}; }}
QFrame#rail QPushButton:checked {{ background: {ACTIVE}; color: {INK}; font-weight: 600; }}
QFrame#rail QPushButton:focus {{ border-color: {INK}; }}
QLabel#heading {{ font-size: 22px; font-weight: 600; }}
QLabel#section {{ font-size: 16px; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; font-size: 12px; }}
QLabel#metric {{ font-size: 28px; font-weight: 400; }}
QLabel#metricLead {{ font-size: 32px; font-weight: 400; }}
QLabel#badge {{ border-radius: 4px; padding: 4px 8px; background: {ACTIVE}; color: {MUTED}; font-size: 12px; }}
QPushButton {{ background: {SURFACE}; border: 1px solid {CONTROL}; border-radius: 6px; padding: 6px 12px; min-height: 18px; }}
QPushButton:hover {{ border-color: {MUTED}; background: {RAISED}; }}
QPushButton:pressed {{ background: {ACTIVE}; }}
QPushButton:focus {{ border-color: {INK}; }}
QPushButton:checked {{ background: {ACTIVE}; }}
QPushButton:disabled {{ background: {RAISED}; color: {MUTED}; border-color: {BORDER}; }}
QPushButton#primary {{ background: {INK}; border-color: {INK}; color: {SURFACE}; font-weight: 600; }}
QPushButton#primary:hover {{ background: #353736; }}
QPushButton#primary:pressed {{ background: #000000; }}
QPushButton#primary:disabled {{ background: {ACTIVE}; color: {MUTED}; border-color: {ACTIVE}; }}
QLineEdit, QComboBox, QAbstractSpinBox {{ background: {SURFACE}; border: 1px solid {CONTROL}; border-radius: 6px;
                       padding: 6px 8px; min-height: 20px; selection-background-color: {ACTIVE}; selection-color: {INK}; }}
QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QAbstractSpinBox:disabled {{ color: {MUTED}; background: {RAISED}; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; selection-background-color: {ACTIVE}; selection-color: {INK}; }}
QTableView, QTableWidget {{ background: {SURFACE}; border: none; gridline-color: {RULE};
                         selection-background-color: {ACTIVE}; selection-color: {INK}; }}
QTableView::item {{ padding: 6px; border-bottom: 1px solid {RULE}; }}
QTableView::item:selected {{ background: {ACTIVE}; color: {INK}; }}
QHeaderView::section {{ background: {SURFACE}; color: {MUTED}; padding: 8px 6px; border: none; font-size: 12px; font-weight: 400; }}
QTabWidget::pane {{ background: {SURFACE}; border: none; }}
QTabBar::tab {{ padding: 8px 14px; color: {MUTED}; border-bottom: 2px solid {BORDER}; }}
QTabBar::tab:selected {{ color: {INK}; border-bottom-color: {INK}; }}
QPlainTextEdit {{ background: {RAISED}; border: 1px solid {BORDER}; border-radius: 6px; padding: 8px;
                 selection-background-color: {ACTIVE}; selection-color: {INK}; }}
QSplitter::handle {{ background: {SURFACE}; }}
QToolTip {{ color: {INK}; background: {SURFACE}; border: 1px solid {CONTROL}; padding: 6px; }}
"""

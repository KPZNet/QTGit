import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName("kence")
    app.setApplicationName("QTGit")

    window = MainWindow(start_directory=Path.cwd())
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
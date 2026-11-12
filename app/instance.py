"""An OS-released lock prevents two local processes monitoring the same SQLite file."""

import os
from pathlib import Path


class InstanceLock:
    def __init__(self, database: str):
        if database == ":memory:" or not database:
            raise ValueError("Scheduled monitoring requires a file-backed SQLite database.")
        self.path = Path(str(Path(database).resolve()) + ".monitor.lock")
        self.file = None

    def acquire(self) -> None:
        if self.file is not None:
            raise RuntimeError("Monitoring is already running.")
        handle = self.path.open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("Another PULSEBOT process is already using this database.") from None
        self.file = handle

    def release(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None

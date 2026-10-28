"""Un único servidor escritor por instalación; bloqueo liberado por el SO."""
import os
from .domain import AppError, ROOT


class InstanceLock:
    def __enter__(self):
        folder = ROOT / ".runtime"
        folder.mkdir(exist_ok=True)
        self.file = (folder / "server.lock").open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise AppError("Antijira ya se está ejecutando. Usa el servidor existente.", 409) from exc
        return self

    def __exit__(self, *args):
        self.file.close()

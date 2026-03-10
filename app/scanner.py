from __future__ import annotations
import shlex
import subprocess
from dataclasses import dataclass
from .config import get_settings


@dataclass
class ScanResult:
    clean: bool
    infected: bool
    threat_name: str | None = None
    raw_output: str = ""


class ScannerService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def scan_path(self, path: str) -> ScanResult:
        args = [self.settings.clamdscan_binary, *shlex.split(self.settings.clamdscan_args), path]
        proc = subprocess.run(args, capture_output=True, text=True)
        output = (proc.stdout or "") + (proc.stderr or "")

        if proc.returncode == 0:
            return ScanResult(clean=True, infected=False, raw_output=output)

        if proc.returncode == 1:
            threat = None
            for line in output.splitlines():
                if line.endswith(" FOUND") and ": " in line:
                    try:
                        _, suffix = line.split(": ", 1)
                        threat = suffix.removesuffix(" FOUND").strip()
                        break
                    except ValueError:
                        continue
            return ScanResult(clean=False, infected=True, threat_name=threat, raw_output=output)

        raise RuntimeError(f"clamdscan failed with exit code {proc.returncode}: {output.strip()}")

"""Launch the same analysis code from source and the self-contained release."""
from pathlib import Path
import sys


def start_analysis_process(process, arguments):
    if getattr(sys, "frozen", False):
        process.start(str(Path(sys.executable).with_name("AEROBUS-Analysis.exe")), ["--analysis-job", *arguments])
    else:
        process.setWorkingDirectory(str(Path(__file__).resolve().parents[1]))
        process.start(sys.executable, ["-B", "-m", "ground_station.analysis_job", *arguments])

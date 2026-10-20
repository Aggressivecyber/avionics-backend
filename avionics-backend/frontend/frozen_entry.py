"""Shared entry point for the GUI and console analysis executable."""
import sys

if sys.argv[1:2] == ["--analysis-job"]:
    from ground_station.analysis_job import main
    main(sys.argv[2:])
elif sys.argv[1:2] == ["--release-check"]:
    from ground_station.release_check import main
    main(sys.argv[2:])
else:
    from ground_station.__main__ import main
    raise SystemExit(main())

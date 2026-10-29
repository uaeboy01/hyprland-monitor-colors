# Validation

Checked on 2026-10-30 using Python 3.14.8, NumPy 2.5.4, PySide6 6.12.0,
and Little CMS 2.19.1 on CachyOS.

- All 12 unit tests passed, including configuration rollback, stale edits,
  protection of unmanaged ICC profiles, mapping validation, read-only listing,
  custom configuration paths, and offscreen GUI draft handling.
- All 15 ICC self-test cases passed. Maximum RGB error: 0.000278.
- CLI help ran successfully without requiring a monitor mapping.
- A wheel built locally without fetching dependencies. Its module and
  `monitor-colors` console-entry metadata were verified.
- The README preview was rendered offscreen with a fictional monitor and a
  temporary Lua file. No live display queries or Apply calls were made.

No live Apply, monitor modeset, compositor reload, service restart, driver
change, or dependency installation was performed while packaging this project.
The packaged live Apply path and lower Python versions have not been verified.
The monitor-manager override limitation is documented, not fixed in this release.

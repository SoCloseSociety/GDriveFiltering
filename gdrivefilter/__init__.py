"""GDriveFiltering -- backup, verify, dedup and reorganize Google Drive locally.

Safety contract (see CLAUDE.md):
  - Extraction is READ-ONLY on Google Drive.
  - Nothing is ever deleted before a verified mirror + manifest exist.
  - Cleaning/dedup only detect, report and quarantine (into a COPY).
"""

__version__ = "0.1.0"

# FileRecoveryTool: Deep Code Review vs. Best-in-Class Recovery Tools

## Scope
This review compares the current implementation against the capabilities typically found in advanced forensic and file-recovery tools (e.g., R-Studio, UFS Explorer, X-Ways/forensic workflows, and custom carving pipelines).

## Current Strengths
- Signature-based carving supports JPG/PNG/PDF/MP4/ZIP with configurable boundaries.
- Chunked scanning with overlap handling reduces boundary misses.
- Optional Windows NTFS allocation-bitmap filtering reduces allocated-space false positives.
- Post-recovery viability checks and repair attempts reduce bad outputs.
- Added heuristic fragment stitching candidate scoring and non-linear candidate search in aggressive mode.

## High-Impact Gaps (Critical)
1. No filesystem-native metadata reconstruction
   - Advanced tools restore file names, timestamps, ACLs, path hierarchy, and extents by parsing filesystem metadata (MFT, ext4 inodes, APFS structures).
   - Current implementation is mostly blind carving.

2. Limited fragmented-file reassembly strategy
   - While improved in this patch, stitching remains heuristic and local-window based.
   - Best-in-class tools use extent trees/runlists, journal artifacts, and global optimization across candidate fragments.

3. No container/deep format parsing
   - Real-world recoveries need robust parsers for Office docs, SQLite, PST, MOV/MP4 atom graphs, and partial archives.

4. No confidence model / explainability
   - The tool should output confidence scores, why each fragment was selected, and whether assembly is fully verified.

5. No corruption-tolerant iterative pass pipeline
   - Elite tools run: metadata pass -> exact carving -> fragmented reconstruction -> semantic validation -> repair.

## Recommended Architecture Upgrades
1. Multi-pass engine
   - Pass 1: Filesystem metadata recovery (NTFS/FAT/exFAT/ext/APFS where possible)
   - Pass 2: Smart carving with format-aware parsers
   - Pass 3: Fragment graph reconstruction (scored edges)
   - Pass 4: Validation and auto-repair

2. Fragment graph model
   - Treat candidate blocks as nodes.
   - Weighted edges from continuity tests (byte-level continuity, entropy transition, codec/format marker expectations, CRC/object checks).
   - Solve with beam search / A* / bounded dynamic programming.

3. Format-aware validators
   - JPEG: segment chain and Huffman/table coherence.
   - PNG: chunk sequence + CRC verification.
   - PDF: xref/object graph integrity.
   - ZIP: central directory and file-entry consistency.
   - MP4: atom tree consistency, stco/co64 sanity.

4. Forensic-grade output
   - JSON report per file with offsets, chosen fragments, confidence, validator outcomes, and repair actions.

## Patch Included in This Commit
- Added candidate-based fragment stitching that scans ahead and ranks blocks by continuity + type-specific markers.
- Added configurable stitch parameters:
  - `FRAGMENT_SCAN_STEP`
  - `MAX_FRAGMENT_CANDIDATES`
  - `FRAGMENT_SCORE_THRESHOLD`
- Added fragment score logging for traceability.
- Replaced silent extraction failure with explicit error logging.

## Implementation Status (Updated)
- Multi-pass execution flow is now represented in runtime logging:
  1) metadata discovery (best effort),
  2) signature carving,
  3) aggressive fragmented reassembly (opportunistic),
  4) validation/repair.
- Fragment stitching now supports path selection with beam-style search over scored candidates instead of a strict one-step greedy pick.
- Validators were hardened:
  - PNG: chunk CRC validation.
  - JPEG: marker sanity checks.
  - PDF/MP4: stronger structural checks.
- Forensic-grade JSON output now records offsets, selected fragment trace, confidence, validator outcome, and repair status (`recovery_report.json` in destination folder).

## Validation Strategy To Reach "Most Advanced" Goal
- Build a synthetic corpus with controlled fragmentation levels.
- Compare recovered-byte ratio, structurally valid files, and application-openable files.
- Track false positives and time-to-recover metrics.
- Benchmark against at least two reference tools on the same datasets.

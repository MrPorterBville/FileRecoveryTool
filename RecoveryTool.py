import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import os
import sys
import threading
import time
import re
import bisect
import ctypes
import json
import zlib
import math
import struct
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
import zipfile

# --- Universal Configuration ---
SECTOR_SIZE = 512 
CHUNK_SIZE = 8 * 1024 * 1024  
SCAN_OVERLAP = 256 * 1024
FRAGMENT_BLOCK_SIZE = 256 * 1024
FRAGMENT_SEARCH_WINDOW = 256 * 1024 * 1024
MAX_STITCH_BLOCKS = 1024
FRAGMENT_SCAN_STEP = 64 * 1024
MAX_FRAGMENT_CANDIDATES = 24
FRAGMENT_SCORE_THRESHOLD = 0.45
FRAGMENT_BEAM_WIDTH = 6
MAX_FRAGMENT_PATH_DEPTH = 10
THUMBNAIL_CUTOFF_BYTES = 200 * 1024
JPG_MIN_END_OFFSET_BYTES = 512 * 1024
DEFAULT_MIN_JPG_BYTES = 20 * 1024
DEFAULT_MIN_JPG_DIMENSION = 250
FORCE_RENDER_WIDTHS = (256, 320, 512, 640, 800, 1024, 1280, 1600, 1920)
PARALLEL_SCAN_MIN_CHUNK = 2 * 1024 * 1024

CONFIG = {
    'JPG': {'header': b'\xFF\xD8\xFF', 'footer': b'\xFF\xD9', 'max': 30*1024*1024, 'greedy': False},
    'PNG': {'header': b'\x89PNG\r\n\x1a\n', 'footer': b'\x49\x45\x4E\x44\xAE\x42\x60\x82', 'max': 30*1024*1024, 'greedy': False},
    'PDF': {'header': b'%PDF-', 'footer': b'%%EOF', 'max': 150*1024*1024, 'greedy': False},
    'MP4': {'header': b'ftyp', 'footer': None, 'max': 4000*1024*1024, 'greedy': True}, 
    'ZIP': {'header': b'PK\x03\x04', 'footer': None, 'max': 1000*1024*1024, 'greedy': True}, 
}

class UniversalRecoveryApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Phoenix Universal Recovery v7.5")
        self.root.geometry("1100x850")
        self.stop_event = threading.Event()
        self.files_found = 0
        self.recovery_report = []
        self.report_lock = threading.Lock()
        self.check_vars = {}
        self.aggressive_var = tk.BooleanVar(value=False)
        self.unallocated_only_var = tk.BooleanVar(value=False)
        self.skip_exif_thumbs_var = tk.BooleanVar(value=True)
        self.skip_small_jpg_var = tk.BooleanVar(value=True)
        self.min_jpg_size_kb_var = tk.IntVar(value=20)
        self.skip_small_dimensions_var = tk.BooleanVar(value=True)
        self.min_jpg_dimension_var = tk.IntVar(value=DEFAULT_MIN_JPG_DIMENSION)
        self._setup_ui()

    def _setup_ui(self):
        main = ttk.Frame(self.root, padding=15)
        main.pack(fill="both", expand=True)
        
        # --- Source Selection ---
        src_frame = ttk.LabelFrame(main, text=" 1. Select Source (Physical Drive or File Image) ", padding=10)
        src_frame.pack(fill="x", pady=5)
        
        self.ent_src = ttk.Entry(src_frame)
        self.ent_src.pack(fill="x", side="left", expand=True, padx=5)
        
        ttk.Button(src_frame, text="File/Image", command=self.browse_file).pack(side="left", padx=2)
        ttk.Button(src_frame, text="Physical Drive", command=self.browse_drive).pack(side="left", padx=2)

        # --- Filters ---
        filter_frame = ttk.LabelFrame(main, text=" 2. File Types to Recover ", padding=10)
        filter_frame.pack(fill="x", pady=10)
        for ftype in CONFIG.keys():
            var = tk.BooleanVar(value=True)
            self.check_vars[ftype] = var
            ttk.Checkbutton(filter_frame, text=ftype, variable=var).pack(side="left", padx=15)
        ttk.Checkbutton(filter_frame, text="Aggressive fragmented mode", variable=self.aggressive_var).pack(side="left", padx=15)
        ttk.Checkbutton(filter_frame, text="Recover from unallocated space only (Windows NTFS, best-effort)", variable=self.unallocated_only_var).pack(side="left", padx=15)
        
        jpg_filter_frame = ttk.LabelFrame(main, text=" 2b. JPG Quality Filters ", padding=10)
        jpg_filter_frame.pack(fill="x", pady=5)
        ttk.Checkbutton(jpg_filter_frame, text="Skip EXIF thumbnail regions", variable=self.skip_exif_thumbs_var).pack(side="left", padx=10)
        ttk.Checkbutton(jpg_filter_frame, text="Ignore JPGs smaller than (KB):", variable=self.skip_small_jpg_var).pack(side="left", padx=10)
        ttk.Spinbox(jpg_filter_frame, from_=1, to=50000, width=8, textvariable=self.min_jpg_size_kb_var).pack(side="left", padx=5)
        ttk.Checkbutton(jpg_filter_frame, text="Ignore JPG dimensions below (px):", variable=self.skip_small_dimensions_var).pack(side="left", padx=10)
        ttk.Spinbox(jpg_filter_frame, from_=32, to=10000, width=8, textvariable=self.min_jpg_dimension_var).pack(side="left", padx=5)

        # --- Destination ---
        dst_frame = ttk.LabelFrame(main, text=" 3. Save Destination ", padding=10)
        dst_frame.pack(fill="x", pady=5)
        self.ent_dst = ttk.Entry(dst_frame)
        self.ent_dst.pack(fill="x", side="left", expand=True, padx=5)
        ttk.Button(dst_frame, text="Browse", command=self.browse_dest).pack(side="left")

        # --- Controls ---
        self.pbar = ttk.Progressbar(main, maximum=100)
        self.pbar.pack(fill="x", pady=15)

        ctl_box = ttk.Frame(main)
        ctl_box.pack(fill="x")
        self.btn_go = ttk.Button(ctl_box, text="START RECOVERY", command=self.start_workflow)
        self.btn_go.pack(side="left", fill="x", expand=True)
        self.btn_stop = ttk.Button(ctl_box, text="STOP", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", fill="x", expand=True)
        
        # --- Results ---
        self.tree = ttk.Treeview(main, columns=("id", "type", "size", "offset"), show="headings")
        self.tree.heading("id", text="#"); self.tree.column("id", width=50)
        self.tree.heading("type", text="Type"); self.tree.column("type", width=100)
        self.tree.heading("size", text="Size"); self.tree.column("size", width=150)
        self.tree.heading("offset", text="Physical Offset"); self.tree.column("offset", width=200)
        self.tree.pack(fill="both", expand=True, pady=10)

        self.log_box = scrolledtext.ScrolledText(main, height=6, bg="#1a1a1a", fg="#00d4ff", font=("Consolas", 9))
        self.log_box.pack(fill="x")

    def log(self, msg):
        self.root.after(0, lambda: self.log_box.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n") or self.log_box.see(tk.END))

    def browse_file(self):
        f = filedialog.askopenfilename(title="Select Disk Image or File")
        if f: self.ent_src.delete(0, tk.END); self.ent_src.insert(0, f)

    def browse_drive(self):
        d = filedialog.askdirectory(title="Select Drive Letter")
        if d:
            drive = os.path.splitdrive(d)[0]
            path = f"\\\\.\\{drive}" if sys.platform == "win32" else drive
            self.ent_src.delete(0, tk.END); self.ent_src.insert(0, path)

    def browse_dest(self):
        d = filedialog.askdirectory()
        if d: self.ent_dst.delete(0, tk.END); self.ent_dst.insert(0, d)

    def stop(self):
        self.stop_event.set()

    def start_workflow(self):
        src, dst = self.ent_src.get(), self.ent_dst.get()
        active = [t for t, v in self.check_vars.items() if v.get()]
        if not src or not dst or not active: return
        
        self.stop_event.clear()
        self.btn_go.config(state="disabled"); self.btn_stop.config(state="normal")
        self.files_found = 0
        self.tree.delete(*self.tree.get_children())
        threading.Thread(target=self.scan, args=(src, dst, active, self.unallocated_only_var.get()), daemon=True).start()

    def scan(self, src, dst, active_types, unallocated_only=False):
        self.log(f"Initializing Universal Scan on: {src}")
        is_physical = src.startswith("\\\\.\\")
        processed_offsets = set()
        alloc_filter = None
        skip_thumbnail_candidates = self.skip_exif_thumbs_var.get()
        jpg_exclusion_ranges = []
        jpg_container_ranges = []
        self.recovery_report = []
        
        try:
            self.log("Pass 1/4: metadata discovery (best effort).")
            metadata_artifacts = self._run_metadata_pass(src, is_physical)
            self.log(f"Pass 1/4 complete. Artifacts discovered: {metadata_artifacts}.")

            # Handle drive size detection
            total_size = 0
            if not is_physical:
                total_size = os.path.getsize(src)

            if unallocated_only:
                alloc_filter = self._build_allocation_filter(src, is_physical)
                if alloc_filter is None:
                    self.log("Unable to build allocation map for this source. Disable 'unallocated-only' mode and retry.")
                    return
                self.log("Unallocated-only mode enabled: skipping signatures located in allocated clusters.")
            
            header_map = {CONFIG[t]['header']: t for t in active_types}
            header_offsets = {t: (4 if t == "MP4" else 0) for t in active_types}
            pattern = re.compile(b'|'.join([re.escape(h) for h in header_map.keys()]))
            header_overlap = max(len(h) for h in header_map.keys()) if header_map else 0
            scan_workers = max(1, min(8, (os.cpu_count() or 1)))
            if scan_workers > 1:
                self.log(f"Parallel signature scanning enabled with {scan_workers} workers.")

            with open(src, "rb") as f:
                self.log("Pass 2/4: signature carving.")
                off = 0
                while not self.stop_event.is_set():
                    # Align ONLY if physical
                    current_seek = (off // SECTOR_SIZE) * SECTOR_SIZE if is_physical else off
                    f.seek(current_seek)
                    
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk: break

                    if total_size: self.pbar['value'] = (current_seek / total_size) * 100
                    
                    matches = self._find_signature_matches_parallel(chunk, pattern, scan_workers, header_overlap)
                    for found_idx, header in matches:
                        if self.stop_event.is_set():
                            break
                        ftype = header_map.get(header)
                        abs_start = current_seek + found_idx - header_offsets.get(ftype, 0)
                        if abs_start < 0 or not ftype or abs_start in processed_offsets:
                            continue
                        if ftype == "MP4" and not self._looks_like_mp4_header_at(chunk, found_idx):
                            continue
                        if ftype == "JPG" and self.skip_exif_thumbs_var.get() and self._in_ranges(abs_start, jpg_exclusion_ranges):
                            self.log(f"Skipped JPG signature in EXIF thumbnail region @ {hex(abs_start)}.")
                            continue
                        if ftype == "JPG" and self._in_ranges(abs_start, jpg_container_ranges):
                            self.log(f"Skipped nested JPG signature inside previously recovered JPG @ {hex(abs_start)}.")
                            continue
                        if alloc_filter and self._offset_is_allocated(abs_start, alloc_filter):
                            continue
                        processed_offsets.add(abs_start)
                        if self.stop_event.is_set():
                            break
                        extract_result = self.extract(
                            src, abs_start, ftype, dst, active_types, is_physical,
                            self.aggressive_var.get(), skip_thumbnail_candidates
                        )
                        if ftype == "JPG" and extract_result and extract_result.get("exclude_ranges"):
                            jpg_exclusion_ranges.extend(extract_result["exclude_ranges"])
                        if ftype == "JPG" and extract_result and extract_result.get("container_range"):
                            cstart, cend = extract_result["container_range"]
                            if cend > cstart:
                                jpg_container_ranges.append((cstart, cend))

                    off = current_seek + CHUNK_SIZE - SCAN_OVERLAP
                self.log("Pass 2/4 complete.")

            self.log("Pass 3/4: fragmented reassembly is applied opportunistically per-file when aggressive mode is enabled.")
            self.log("Pass 4/4: format validation and repair is applied per recovered file.")

        except Exception as e:
            self.log(f"Error: {e}")
            if "PermissionError" in str(e): self.log("!!! RUN AS ADMINISTRATOR !!!")
        finally:
            self._flush_recovery_report(dst)
            self.root.after(0, lambda: self.btn_go.config(state="normal"))
            self.root.after(0, lambda: self.btn_stop.config(state="disabled"))
            self.log("Scan Finished.")

    def _offset_is_allocated(self, offset, alloc_filter):
        starts, ends = alloc_filter
        idx = bisect.bisect_right(starts, offset) - 1
        return idx >= 0 and offset < ends[idx]

    def _find_signature_matches_parallel(self, chunk, pattern, worker_count, overlap_bytes):
        if self.stop_event.is_set():
            return []
        if worker_count <= 1 or len(chunk) < PARALLEL_SCAN_MIN_CHUNK:
            return [(m.start(), m.group()) for m in pattern.finditer(chunk)]

        seg_size = max(PARALLEL_SCAN_MIN_CHUNK, len(chunk) // worker_count)
        segments = []
        start = 0
        while start < len(chunk):
            end = min(len(chunk), start + seg_size)
            segments.append((start, end))
            start = end

        def scan_segment(seg_start, seg_end):
            if self.stop_event.is_set():
                return []
            local_start = max(0, seg_start - overlap_bytes)
            local_end = min(len(chunk), seg_end + overlap_bytes)
            sub = chunk[local_start:local_end]
            hits = []
            for match in pattern.finditer(sub):
                if self.stop_event.is_set():
                    break
                abs_idx = local_start + match.start()
                if seg_start <= abs_idx < seg_end:
                    hits.append((abs_idx, match.group()))
            return hits

        matches = []
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(scan_segment, seg_start, seg_end) for seg_start, seg_end in segments]
            for future in futures:
                if self.stop_event.is_set():
                    break
                matches.extend(future.result())
        matches.sort(key=lambda x: x[0])
        return matches

    def _run_metadata_pass(self, src, is_physical):
        # Best-effort lightweight metadata signal discovery.
        # This is intentionally conservative to avoid heavy memory use on large sources.
        signatures = [b"FILE0", b"INDX", b"$MFT", b"NTFS", b"APFS", b"EXT4", b"exFAT"]
        discovered = 0
        try:
            with open(src, "rb") as f:
                buf = f.read(8 * 1024 * 1024)
            for sig in signatures:
                if sig in buf:
                    discovered += 1
            if discovered and is_physical:
                self.log("Metadata hints detected; future versions should parse full filesystem structures.")
        except Exception as e:
            self.log(f"Metadata pass warning: {e}")
        return discovered

    def _flush_recovery_report(self, dst):
        try:
            if not dst:
                return
            with self.report_lock:
                payload = {
                    "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "total_recovered": len(self.recovery_report),
                    "files": self.recovery_report,
                }
            os.makedirs(dst, exist_ok=True)
            out = os.path.join(dst, "recovery_report.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            self.log(f"Forensic report written: {out}")
        except Exception as e:
            self.log(f"Failed writing forensic report: {e}")

    def _build_allocation_filter(self, src, is_physical):
        if sys.platform != "win32":
            self.log("Unallocated-only mode currently supports Windows volumes only.")
            return None
        if not is_physical:
            self.log("Unallocated-only mode currently supports Physical Drive sources (\\\\.\\X:).")
            return None
        return self._build_windows_volume_bitmap_filter(src)

    def _build_windows_volume_bitmap_filter(self, volume_path):
        FSCTL_GET_VOLUME_BITMAP = 0x0009006F
        ERROR_MORE_DATA = 234
        OPEN_EXISTING = 3
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        GENERIC_READ = 0x80000000
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.DeviceIoControl.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID
        ]
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetDiskFreeSpaceW.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.GetDiskFreeSpaceW.restype = wintypes.BOOL

        if len(volume_path) < 6:
            self.log(f"Unexpected volume format: {volume_path}")
            return None
        drive_letter = volume_path[4:6]  # e.g. "C:"
        root_path = f"{drive_letter}\\"

        spc = wintypes.DWORD()
        bps = wintypes.DWORD()
        free_clusters = wintypes.DWORD()
        total_clusters = wintypes.DWORD()
        ok = kernel32.GetDiskFreeSpaceW(root_path, ctypes.byref(spc), ctypes.byref(bps), ctypes.byref(free_clusters), ctypes.byref(total_clusters))
        if not ok:
            self.log("Failed to query cluster size for selected volume.")
            return None

        bytes_per_cluster = spc.value * bps.value
        if bytes_per_cluster <= 0:
            self.log("Invalid cluster size returned by volume.")
            return None

        handle = kernel32.CreateFileW(
            volume_path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            self.log("Could not open volume for bitmap query. Try running as administrator.")
            return None

        starts = []
        ends = []
        try:
            start_lcn = 0
            total_lcns = None
            out_size = 4 * 1024 * 1024
            while not self.stop_event.is_set():
                in_buf = ctypes.create_string_buffer(start_lcn.to_bytes(8, byteorder="little", signed=True))
                out_buf = ctypes.create_string_buffer(out_size)
                ret = wintypes.DWORD(0)
                ok = kernel32.DeviceIoControl(
                    handle,
                    FSCTL_GET_VOLUME_BITMAP,
                    in_buf,
                    ctypes.sizeof(in_buf),
                    out_buf,
                    out_size,
                    ctypes.byref(ret),
                    None,
                )
                if not ok and ctypes.get_last_error() != ERROR_MORE_DATA:
                    self.log("Failed while reading volume bitmap.")
                    return None
                if ret.value < 16:
                    break

                raw = out_buf.raw[:ret.value]
                chunk_start_lcn = int.from_bytes(raw[0:8], "little", signed=True)
                bitmap_size = int.from_bytes(raw[8:16], "little", signed=True)
                bit_bytes = raw[16:]
                if total_lcns is None:
                    total_lcns = bitmap_size

                bits_in_chunk = min(len(bit_bytes) * 8, max(0, total_lcns - chunk_start_lcn))
                run_start = None
                for bit_index in range(bits_in_chunk):
                    allocated = (bit_bytes[bit_index // 8] >> (bit_index % 8)) & 1
                    current_lcn = chunk_start_lcn + bit_index
                    if allocated:
                        if run_start is None:
                            run_start = current_lcn
                    elif run_start is not None:
                        starts.append(run_start * bytes_per_cluster)
                        ends.append(current_lcn * bytes_per_cluster)
                        run_start = None
                if run_start is not None:
                    end_lcn = chunk_start_lcn + bits_in_chunk
                    starts.append(run_start * bytes_per_cluster)
                    ends.append(end_lcn * bytes_per_cluster)

                next_lcn = chunk_start_lcn + bits_in_chunk
                if total_lcns is not None and next_lcn >= total_lcns:
                    break
                if next_lcn <= start_lcn:
                    break
                start_lcn = next_lcn
        finally:
            kernel32.CloseHandle(handle)

        if not starts:
            self.log("Allocation map returned no allocated clusters.")
            return ([], [])
        return (starts, ends)

    def _block_looks_like_fragment(self, block, ftype):
        if not block:
            return False
        if ftype == "JPG":
            marker_count = block.count(b"\xFF")
            return marker_count > 100 and (block.count(b"\x00") / max(len(block), 1)) < 0.5
        if ftype == "PNG":
            known_chunks = [b'IHDR', b'IDAT', b'PLTE', b'IEND', b'tEXt', b'zTXt', b'iTXt']
            return any(chunk in block for chunk in known_chunks)
        if ftype == "PDF":
            return any(tok in block for tok in [b' obj', b'endobj', b'stream', b'xref', b'trailer'])
        return True

    def _score_fragment_candidate(self, prev_tail, block, ftype):
        if not block:
            return 0.0

        score = 0.0
        if prev_tail:
            overlap = min(len(prev_tail), 32)
            if overlap > 0:
                tail = prev_tail[-overlap:]
                head = block[:overlap]
                continuity = sum(1 for a, b in zip(tail, head) if abs(a - b) <= 8) / overlap
                score += continuity * 0.35

        if ftype == "JPG":
            marker_density = min(1.0, block.count(b"\xFF") / max(len(block) / 48.0, 1.0))
            has_segments = (b"\xFF\xDB" in block) or (b"\xFF\xC0" in block) or (b"\xFF\xDA" in block)
            score += 0.30 * marker_density
            score += 0.25 if has_segments else 0.0
        elif ftype == "PNG":
            chunk_hits = sum(1 for c in [b'IDAT', b'IHDR', b'PLTE', b'tEXt', b'zTXt', b'iTXt'] if c in block)
            score += min(0.55, chunk_hits * 0.16)
        elif ftype == "PDF":
            tok_hits = sum(1 for t in [b' obj', b'endobj', b'stream', b'xref', b'trailer'] if t in block)
            score += min(0.55, tok_hits * 0.14)
        else:
            score += 0.25

        if cfg := CONFIG.get(ftype):
            footer = cfg.get("footer")
            if footer and footer in block:
                score += 0.35

        return min(1.0, score)

    def _scan_fragment_candidates(self, fin, ftype, window_start, window_end, prev_tail, used_positions):
        candidates = []
        pos = window_start
        while pos < window_end and not self.stop_event.is_set():
            if pos in used_positions:
                pos += FRAGMENT_SCAN_STEP
                continue
            fin.seek(pos)
            block = fin.read(FRAGMENT_BLOCK_SIZE)
            if not block:
                break
            if not self._block_looks_like_fragment(block, ftype):
                pos += FRAGMENT_SCAN_STEP
                continue
            score = self._score_fragment_candidate(prev_tail, block, ftype)
            if score >= FRAGMENT_SCORE_THRESHOLD:
                candidates.append((score, pos, block))
            pos += FRAGMENT_SCAN_STEP

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[:MAX_FRAGMENT_CANDIDATES]

    def _pair_fragment_score(self, left_block, right_block):
        overlap = min(32, len(left_block), len(right_block))
        if overlap <= 0:
            return 0.0
        left_tail = left_block[-overlap:]
        right_head = right_block[:overlap]
        return sum(1 for a, b in zip(left_tail, right_head) if abs(a - b) <= 8) / overlap

    def _select_fragment_path(self, candidates):
        # candidates: [(score, pos, block), ...]
        if not candidates:
            return []
        beams = []
        for idx, (base_score, pos, block) in enumerate(candidates):
            beams.append((base_score, [idx], block))

        for _ in range(MAX_FRAGMENT_PATH_DEPTH - 1):
            next_beams = []
            for score, path, last_block in beams:
                used = set(path)
                for idx, (node_score, _, node_block) in enumerate(candidates):
                    if idx in used:
                        continue
                    edge = self._pair_fragment_score(last_block, node_block)
                    total = score + (node_score * 0.8) + (edge * 0.6)
                    next_beams.append((total, path + [idx], node_block))
            if not next_beams:
                break
            next_beams.sort(key=lambda x: x[0], reverse=True)
            beams = next_beams[:FRAGMENT_BEAM_WIDTH]

        best = max(beams, key=lambda x: x[0], default=None)
        if not best:
            return []
        return [candidates[i] for i in best[1]]

    def _stitch_fragmented(self, fin, fout, cfg, ftype, rec_len, prev_tail=b''):
        footer = cfg['footer']
        stitched = 0
        blocks = 0
        trace = []
        used_positions = set()
        initial_pos = fin.tell()
        scan_limit = initial_pos + FRAGMENT_SEARCH_WINDOW
        last_tail = prev_tail[-64:] if prev_tail else b''

        candidates = self._scan_fragment_candidates(fin, ftype, initial_pos, scan_limit, last_tail, used_positions)
        path = self._select_fragment_path(candidates)
        if not path:
            return rec_len, stitched, False, trace, 0.0

        avg_score = 0.0
        for score, block_pos, block in path:
            if blocks >= MAX_STITCH_BLOCKS or rec_len >= cfg['max'] or self.stop_event.is_set():
                break
            blocks += 1
            used_positions.add(block_pos)
            avg_score += score

            if footer:
                fpos = block.find(footer)
                if fpos != -1:
                    cut = fpos + len(footer)
                    to_write = block[:cut]
                    to_write = to_write[:max(0, cfg['max'] - rec_len)]
                    fout.write(to_write)
                    rec_len += len(to_write)
                    stitched += len(to_write)
                    trace.append({"offset": block_pos, "score": round(score, 4), "bytes_written": len(to_write), "footer_found": True})
                    if to_write:
                        last_tail = (last_tail + to_write)[-64:]
                    self.log(f"Fragment candidate score {score:.2f} selected at {hex(block_pos)} (footer found).")
                    conf = min(1.0, avg_score / max(1, blocks))
                    return rec_len, stitched, True, trace, conf

            to_write = block[:max(0, cfg['max'] - rec_len)]
            if not to_write:
                break
            fout.write(to_write)
            rec_len += len(to_write)
            stitched += len(to_write)
            last_tail = (last_tail + to_write)[-64:]
            trace.append({"offset": block_pos, "score": round(score, 4), "bytes_written": len(to_write), "footer_found": False})
            self.log(f"Fragment candidate score {score:.2f} selected at {hex(block_pos)}.")

        conf = min(1.0, avg_score / max(1, blocks))
        return rec_len, stitched, False, trace, conf

    def _looks_like_mp4_header_at(self, data, ftyp_index):
        if ftyp_index < 4 or ftyp_index + 8 > len(data):
            return False
        size = int.from_bytes(data[ftyp_index - 4:ftyp_index], "big", signed=False)
        if size < 8 or size > 128 * 1024 * 1024:
            return False
        brand = data[ftyp_index + 4:ftyp_index + 8]
        return all(32 <= b <= 126 for b in brand)

    def _parse_mp4_top_level(self, data):
        boxes = []
        pos = 0
        total = len(data)
        while pos + 8 <= total:
            size = int.from_bytes(data[pos:pos + 4], "big", signed=False)
            btype = data[pos + 4:pos + 8]
            header_len = 8

            if size == 1:
                if pos + 16 > total:
                    break
                size = int.from_bytes(data[pos + 8:pos + 16], "big", signed=False)
                header_len = 16
            elif size == 0:
                size = total - pos

            if size < header_len or (pos + size) > total:
                break
            if not all(32 <= c <= 126 for c in btype):
                break

            boxes.append((pos, size, btype))
            pos += size
        return boxes, pos

    def _in_ranges(self, offset, ranges):
        return any(start <= offset < end for start, end in ranges)

    def _parse_exif_thumbnail_ranges(self, app1_payload, app1_payload_start):
        if not app1_payload.startswith(b"Exif\x00\x00") or len(app1_payload) < 14:
            return []
        tiff_rel = 6
        tiff = app1_payload[tiff_rel:]
        byte_order = tiff[0:2]
        if byte_order == b"II":
            endian = "little"
        elif byte_order == b"MM":
            endian = "big"
        else:
            return []
        if int.from_bytes(tiff[2:4], endian) != 42:
            return []

        def read_u16(rel):
            if rel + 2 > len(tiff):
                return None
            return int.from_bytes(tiff[rel:rel + 2], endian)

        def read_u32(rel):
            if rel + 4 > len(tiff):
                return None
            return int.from_bytes(tiff[rel:rel + 4], endian)

        ifd0_rel = read_u32(4)
        if ifd0_rel is None or ifd0_rel >= len(tiff):
            return []
        ifd0_count = read_u16(ifd0_rel)
        if ifd0_count is None:
            return []
        next_ifd_ptr_rel = ifd0_rel + 2 + (ifd0_count * 12)
        ifd1_rel = read_u32(next_ifd_ptr_rel)
        if ifd1_rel is None or ifd1_rel == 0 or ifd1_rel >= len(tiff):
            return []

        ifd1_count = read_u16(ifd1_rel)
        if ifd1_count is None:
            return []

        thumb_offset = None
        thumb_length = None
        base = ifd1_rel + 2
        for i in range(ifd1_count):
            entry_rel = base + (i * 12)
            if entry_rel + 12 > len(tiff):
                break
            tag = int.from_bytes(tiff[entry_rel:entry_rel + 2], endian)
            value = int.from_bytes(tiff[entry_rel + 8:entry_rel + 12], endian)
            if tag == 0x0201:
                thumb_offset = value
            elif tag == 0x0202:
                thumb_length = value

        if thumb_offset is None or thumb_length is None or thumb_length <= 0:
            return []

        abs_start = app1_payload_start + tiff_rel + thumb_offset
        abs_end = abs_start + thumb_length
        if abs_end <= abs_start:
            return []
        return [(abs_start, abs_end)]

    def _parse_jpeg_structure(self, data, min_end_offset=0):
        result = {
            "end_offset": None,
            "width": None,
            "height": None,
            "nested_count": 0,
            "exif_thumbnail_ranges": [],
        }
        if len(data) < 4 or not data.startswith(b"\xFF\xD8"):
            return result

        pos = 2
        while pos + 1 < len(data):
            if data[pos] != 0xFF:
                pos += 1
                continue

            while pos < len(data) and data[pos] == 0xFF:
                pos += 1
            if pos >= len(data):
                break
            marker = data[pos]
            pos += 1

            if marker == 0xD9:
                eoi_end = pos
                if eoi_end >= min_end_offset:
                    result["end_offset"] = eoi_end
                    return result
                continue
            if marker == 0xD8:
                result["nested_count"] += 1
                continue
            if marker in (0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7):
                continue

            if pos + 2 > len(data):
                break
            seg_len = int.from_bytes(data[pos:pos + 2], "big")
            pos += 2
            if seg_len < 2:
                break
            payload_start = pos
            payload_end = pos + (seg_len - 2)
            if payload_end > len(data):
                break
            payload = data[payload_start:payload_end]

            if marker == 0xE1:
                result["exif_thumbnail_ranges"].extend(self._parse_exif_thumbnail_ranges(payload, payload_start))
            elif marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if len(payload) >= 5:
                    result["height"] = int.from_bytes(payload[1:3], "big")
                    result["width"] = int.from_bytes(payload[3:5], "big")
            elif marker == 0xDA:
                scan_pos = payload_end
                while scan_pos + 1 < len(data):
                    if data[scan_pos] == 0xFF:
                        nxt = data[scan_pos + 1]
                        if nxt == 0x00:
                            scan_pos += 2
                            continue
                        if nxt == 0xD8:
                            result["nested_count"] += 1
                        if nxt == 0xD9:
                            eoi_end = scan_pos + 2
                            if eoi_end >= min_end_offset:
                                result["end_offset"] = eoi_end
                                return result
                            scan_pos += 2
                            continue
                        if 0xD0 <= nxt <= 0xD7:
                            scan_pos += 2
                            continue
                        pos = scan_pos
                        break
                    scan_pos += 1
                else:
                    break
                continue

            soi_hits = payload.count(b"\xFF\xD8")
            if soi_hits:
                result["nested_count"] += soi_hits
            pos = payload_end
        return result

    def _validate_jpeg(self, data):
        if not data.startswith(b"\xFF\xD8"):
            return False
        parsed = self._parse_jpeg_structure(data, min_end_offset=2)
        if parsed["end_offset"] is None:
            return False
        if parsed["end_offset"] != len(data):
            return False
        return (parsed["width"] is not None) and (parsed["height"] is not None)

    def _validate_png(self, data):
        if not data.startswith(CONFIG["PNG"]["header"]):
            return False
        pos = 8
        saw_ihdr = False
        saw_idat = False
        saw_iend = False
        while pos + 12 <= len(data):
            length = int.from_bytes(data[pos:pos + 4], "big")
            ctype = data[pos + 4:pos + 8]
            end = pos + 12 + length
            if end > len(data):
                return False
            chunk = data[pos + 8:pos + 8 + length]
            crc = int.from_bytes(data[pos + 8 + length:pos + 12 + length], "big")
            calc = zlib.crc32(ctype)
            calc = zlib.crc32(chunk, calc) & 0xFFFFFFFF
            if crc != calc:
                return False
            if ctype == b"IHDR":
                saw_ihdr = True
            elif ctype == b"IDAT":
                saw_idat = True
            elif ctype == b"IEND":
                saw_iend = True
                break
            pos = end
        return saw_ihdr and saw_idat and saw_iend

    def _validate_pdf(self, data):
        return data.startswith(b"%PDF-") and (b"%%EOF" in data[-4096:]) and (b"xref" in data or b" obj" in data)

    def _validate_mp4(self, data):
        if len(data) < 16:
            return False
        ftyp_pos = data.find(b"ftyp")
        if ftyp_pos != 4:
            return False

        boxes, parsed_len = self._parse_mp4_top_level(data)
        if not boxes:
            return False

        box_types = [b for _, _, b in boxes]
        has_media = (b"mdat" in box_types) and ((b"moov" in box_types) or (b"moof" in box_types))
        return has_media and parsed_len >= max(1024, int(len(data) * 0.80))

    def _is_file_viable(self, path, ftype):
        try:
            with open(path, "rb") as f:
                data = f.read()
            if not data:
                return False

            if ftype == "JPG":
                return self._validate_jpeg(data)
            if ftype == "PNG":
                return self._validate_png(data)
            if ftype == "PDF":
                return self._validate_pdf(data)
            if ftype == "ZIP":
                with zipfile.ZipFile(path, "r") as zf:
                    return zf.testzip() is None
            if ftype == "MP4":
                return self._validate_mp4(data)
            return True
        except Exception:
            return False

    def _attempt_file_repair(self, path, ftype):
        try:
            with open(path, "rb") as f:
                data = f.read()
            if not data:
                return False

            repaired = data

            if ftype == "JPG":
                soi = repaired.find(b"\xFF\xD8")
                eoi = repaired.rfind(b"\xFF\xD9")
                if soi != -1 and eoi != -1 and eoi > soi:
                    repaired = repaired[soi:eoi + 2]
                elif soi != -1 and eoi == -1:
                    repaired = repaired[soi:] + b"\xFF\xD9"
                else:
                    return False
            elif ftype == "PNG":
                sig = CONFIG["PNG"]["header"]
                iend = CONFIG["PNG"]["footer"]
                sig_pos = repaired.find(sig)
                iend_pos = repaired.rfind(iend)
                if sig_pos == -1:
                    return False
                repaired = repaired[sig_pos:]
                if iend_pos != -1 and iend_pos >= sig_pos:
                    rel_iend = iend_pos - sig_pos
                    repaired = repaired[:rel_iend + len(iend)]
                elif not repaired.endswith(iend):
                    repaired += iend
            elif ftype == "PDF":
                pdf_pos = repaired.find(b"%PDF-")
                if pdf_pos == -1:
                    return False
                repaired = repaired[pdf_pos:]
                if b"%%EOF" not in repaired[-2048:]:
                    repaired += b"\n%%EOF\n"
            elif ftype == "ZIP":
                eocd = repaired.rfind(b"PK\x05\x06")
                if eocd == -1:
                    return False
                if len(repaired) >= eocd + 22:
                    comment_len = int.from_bytes(repaired[eocd + 20:eocd + 22], "little", signed=False)
                    repaired = repaired[:eocd + 22 + comment_len]
                else:
                    return False
            elif ftype == "MP4":
                ftyp = repaired.find(b"ftyp")
                if ftyp < 4:
                    return False
                repaired = repaired[ftyp - 4:]
                boxes, parsed_len = self._parse_mp4_top_level(repaired)
                if not boxes:
                    return False
                if parsed_len > 0 and parsed_len < len(repaired):
                    repaired = repaired[:parsed_len]
                if b"mdat" not in repaired or (b"moov" not in repaired and b"moof" not in repaired):
                    return False
            else:
                return False

            if repaired and repaired != data:
                with open(path, "wb") as f:
                    f.write(repaired)
            return self._is_file_viable(path, ftype)
        except Exception:
            return False

    def _force_render_jpg_preview(self, path):
        try:
            with open(path, "rb") as f:
                data = f.read()
            if not data:
                return None

            start = data.find(b"\xFF\xD8")
            end = data.rfind(b"\xFF\xD9")
            if start != -1:
                if end != -1 and end > start:
                    data = data[start:end + 2]
                else:
                    data = data[start:]

            data = data.replace(b"\xFF\xD8", b"").replace(b"\xFF\xD9", b"")
            if not data:
                return None

            arr = bytearray(data)
            usable_len = (len(arr) // 3) * 3
            if usable_len <= 0:
                return None
            arr = arr[:usable_len]

            width = None
            height = None
            for candidate in FORCE_RENDER_WIDTHS:
                h = usable_len // (3 * candidate)
                if h >= 8:
                    width = candidate
                    height = h
                    break

            if width is None or height is None:
                px = usable_len // 3
                side = int(math.sqrt(px))
                if side < 8:
                    return None
                width = side
                height = side

            pixels = width * height * 3
            rgb = bytes(arr[:pixels])
            base, _ = os.path.splitext(path)
            png_path = base + "_force_preview.png"

            def chunk(ctype, payload):
                c = ctype + payload
                return (
                    struct.pack(">I", len(payload))
                    + c
                    + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
                )

            rows = []
            stride = width * 3
            for y in range(height):
                row = rgb[y * stride:(y + 1) * stride]
                rows.append(b"\x00" + row)  # PNG filter type 0 (None)
            raw = b"".join(rows)
            compressed = zlib.compress(raw, level=6)

            png = b"".join([
                b"\x89PNG\r\n\x1a\n",
                chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
                chunk(b"IDAT", compressed),
                chunk(b"IEND", b""),
            ])

            with open(png_path, "wb") as out:
                out.write(png)
            return png_path
        except Exception:
            return None

    def extract(self, src, start, ftype, dst, active_types, is_physical, aggressive, skip_thumbnails):
        try:
            cfg = CONFIG[ftype]
            out_path = os.path.join(dst, ftype)
            os.makedirs(out_path, exist_ok=True)
            filename = os.path.join(out_path, f"recovered_{start}.{ftype.lower()}")
            stitched = 0
            stitch_trace = []
            stitch_confidence = 0.0
            repaired = False
            forced_preview_path = None
            nested_jpeg_count = 0
            jpg_dimensions = (None, None)
            exif_ranges = []
            min_jpg_bytes = max(1024, self.min_jpg_size_kb_var.get() * 1024) if self.skip_small_jpg_var.get() else DEFAULT_MIN_JPG_BYTES
            min_jpg_dimension = max(1, self.min_jpg_dimension_var.get())
            
            with open(src, "rb") as fin, open(filename, "wb") as fout:
                # Seek logic for mixed sources
                if is_physical:
                    aligned_start = (start // SECTOR_SIZE) * SECTOR_SIZE
                    skip = start - aligned_start
                    fin.seek(aligned_start)
                else:
                    fin.seek(start)
                    skip = 0
                
                rec_len = 0
                io_size = 512 * 1024 # 512KB for better throughput
                first = True
                found_end = False
                jpg_min_end_offset = JPG_MIN_END_OFFSET_BYTES if skip_thumbnails else 150 * 1024
                if ftype == "JPG" and self.skip_small_jpg_var.get():
                    jpg_min_end_offset = max(jpg_min_end_offset, min_jpg_bytes)
                lb_size = max(len(cfg['footer']) - 1, 32) if cfg['footer'] else 32
                lookbehind = b''
                # For greedy formats (MP4/ZIP), very short signatures such as JPG's
                # 3-byte marker create many false-positive boundaries and can truncate
                # otherwise healthy recoveries. Use only stronger, cross-type markers.
                greedy_headers = [
                    CONFIG[t]['header']
                    for t in active_types
                    if t != ftype and len(CONFIG[t]['header']) >= 4
                ]
                header_pattern = re.compile(b'|'.join([re.escape(h) for h in greedy_headers])) if greedy_headers else None

                if ftype == "JPG":
                    blob = fin.read(cfg['max'] + skip)
                    if self.stop_event.is_set():
                        return {"exclude_ranges": exif_ranges, "container_range": None}
                    blob = blob[skip:] if skip else blob
                    parsed = self._parse_jpeg_structure(blob, min_end_offset=jpg_min_end_offset)
                    carve_end = parsed["end_offset"] if parsed["end_offset"] else len(blob)
                    carve_end = min(carve_end, cfg['max'])
                    if carve_end > 0:
                        fout.write(blob[:carve_end])
                        rec_len = carve_end
                    found_end = parsed["end_offset"] is not None and parsed["end_offset"] <= cfg['max']
                    nested_jpeg_count = parsed["nested_count"]
                    jpg_dimensions = (parsed["width"], parsed["height"])
                    exif_ranges = [(start + s, start + e) for s, e in parsed["exif_thumbnail_ranges"] if e > s]
                else:
                    while rec_len < cfg['max'] and not self.stop_event.is_set():
                        buf = fin.read(io_size)
                        if not buf: break
                        if first:
                            buf = buf[skip:]
                            first = False
                            if not buf:
                                continue

                        data = lookbehind + buf

                        # Footer logic (boundary-safe) for JPG/PNG/PDF
                        if cfg['footer']:
                            fpos = data.find(cfg['footer'])
                            if fpos != -1 and ((rec_len + fpos) > jpg_min_end_offset or ftype != 'JPG'):
                                cut = fpos + len(cfg['footer'])
                                to_write = data[:cut]
                                to_write = to_write[:max(0, cfg['max'] - rec_len)]
                                fout.write(to_write)
                                rec_len += len(to_write)
                                found_end = True
                                break

                        # Greedy logic for MP4/ZIP (also boundary-safe)
                        if cfg['greedy'] and rec_len > 1024 * 1024 and header_pattern is not None:
                            h_match = header_pattern.search(data)
                            if h_match and h_match.start() > 0:
                                h_pos = h_match.start()
                                to_write = data[:h_pos]
                                to_write = to_write[:max(0, cfg['max'] - rec_len)]
                                fout.write(to_write)
                                rec_len += len(to_write)
                                found_end = True
                                break

                        if len(data) > lb_size:
                            flush_len = len(data) - lb_size
                            to_write = data[:flush_len]
                            to_write = to_write[:max(0, cfg['max'] - rec_len)]
                            if to_write:
                                fout.write(to_write)
                                rec_len += len(to_write)
                            lookbehind = data[flush_len:]
                        else:
                            lookbehind = data

                    if lookbehind and rec_len < cfg['max'] and not found_end:
                        to_write = lookbehind[:max(0, cfg['max'] - rec_len)]
                        if to_write:
                            fout.write(to_write)
                            rec_len += len(to_write)

                if aggressive and cfg['footer'] and not found_end and rec_len < cfg['max']:
                    rec_len, stitched, found_end, stitch_trace, stitch_confidence = self._stitch_fragmented(fin, fout, cfg, ftype, rec_len, lookbehind)
                    if stitched > 0:
                        self.log(f"Fragment stitch applied on {ftype} @ {hex(start)} (+{stitched // 1024} KB).")

            if rec_len > 0:
                if skip_thumbnails and ftype in ("JPG", "PNG") and rec_len < THUMBNAIL_CUTOFF_BYTES:
                    try:
                        os.remove(filename)
                        self.log(f"Skipped likely thumbnail {ftype} @ {hex(start)} ({rec_len // 1024} KB).")
                    except OSError:
                        pass
                    return {"exclude_ranges": exif_ranges, "container_range": None}
                if ftype == "JPG" and self.skip_small_jpg_var.get() and rec_len < min_jpg_bytes:
                    try:
                        os.remove(filename)
                        self.log(f"Skipped small JPG @ {hex(start)} ({rec_len // 1024} KB < {min_jpg_bytes // 1024} KB).")
                    except OSError:
                        pass
                    return {"exclude_ranges": exif_ranges, "container_range": None}
                if ftype == "JPG" and self.skip_small_dimensions_var.get():
                    w, h = jpg_dimensions
                    if w and h and (w < min_jpg_dimension or h < min_jpg_dimension):
                        try:
                            os.remove(filename)
                            self.log(f"Skipped thumbnail-sized JPG @ {hex(start)} ({w}x{h}).")
                        except OSError:
                            pass
                        return {"exclude_ranges": exif_ranges, "container_range": None}

                is_viable = self._is_file_viable(filename, ftype)
                if not is_viable:
                    self.log(f"Recovered {ftype} @ {hex(start)} failed viability check. Attempting repair...")
                    if self._attempt_file_repair(filename, ftype):
                        self.log(f"Repair successful for {ftype} @ {hex(start)}.")
                        is_viable = True
                        repaired = True
                    else:
                        if ftype == "JPG":
                            forced_preview_path = self._force_render_jpg_preview(filename)
                            if forced_preview_path:
                                self.log(f"Repair failed for JPG @ {hex(start)}. Force-render preview generated: {forced_preview_path}")
                                try:
                                    os.remove(filename)
                                except OSError:
                                    pass
                                with self.report_lock:
                                    self.recovery_report.append({
                                        "id": None,
                                        "type": "JPG_PREVIEW",
                                        "source_offset": start,
                                        "output_path": forced_preview_path,
                                        "forced_preview_path": forced_preview_path,
                                        "bytes_recovered": rec_len,
                                        "aggressive_mode": aggressive,
                                        "fragment_stitch_bytes": stitched,
                                        "fragment_trace": stitch_trace,
                                        "validator_passed": False,
                                        "repair_applied": False,
                                        "confidence": 0.25,
                                        "jpg_dimensions": {"width": jpg_dimensions[0], "height": jpg_dimensions[1]},
                                        "nested_jpeg_count": nested_jpeg_count,
                                        "secondary_image_detected": (nested_jpeg_count > 0),
                                        "exif_thumbnail_ranges": exif_ranges,
                                        "preview_only": True,
                                    })
                                return {"exclude_ranges": exif_ranges, "container_range": None}
                            else:
                                self.log(f"Repair failed for {ftype} @ {hex(start)}. Discarding likely false positive.")
                                try:
                                    os.remove(filename)
                                except OSError:
                                    pass
                                return
                        else:
                            self.log(f"Repair failed for {ftype} @ {hex(start)}. Discarding likely false positive.")
                            try:
                                os.remove(filename)
                            except OSError:
                                pass
                            return

                self.files_found += 1
                sz = f"{rec_len // 1024} KB" if rec_len < 1024*1024 else f"{rec_len // (1024*1024)} MB"
                self.root.after(0, lambda: self.tree.insert("", 0, values=(self.files_found, ftype, sz, hex(start))))
                if ftype == "JPG" and nested_jpeg_count > 0:
                    self.log(f"Detected nested JPEG markers in recovered JPG @ {hex(start)} (secondary images: {nested_jpeg_count}).")
                confidence = min(1.0, 0.55 + (0.25 if is_viable else 0.0) + min(0.20, stitch_confidence * 0.20))
                with self.report_lock:
                    self.recovery_report.append({
                        "id": self.files_found,
                        "type": ftype,
                        "source_offset": start,
                        "output_path": filename,
                        "forced_preview_path": forced_preview_path,
                        "bytes_recovered": rec_len,
                        "aggressive_mode": aggressive,
                        "fragment_stitch_bytes": stitched,
                        "fragment_trace": stitch_trace,
                        "validator_passed": is_viable,
                        "repair_applied": repaired,
                        "confidence": round(confidence, 4),
                        "jpg_dimensions": {"width": jpg_dimensions[0], "height": jpg_dimensions[1]} if ftype == "JPG" else None,
                        "nested_jpeg_count": nested_jpeg_count if ftype == "JPG" else 0,
                        "secondary_image_detected": (nested_jpeg_count > 0) if ftype == "JPG" else False,
                        "exif_thumbnail_ranges": exif_ranges if ftype == "JPG" else [],
                    })
            container_range = (start, start + rec_len) if (ftype == "JPG" and rec_len > 0) else None
            return {"exclude_ranges": exif_ranges, "container_range": container_range}
        except Exception as e:
            self.log(f"Extraction failure for {ftype} @ {hex(start)}: {e}")
            return {"exclude_ranges": [], "container_range": None}

if __name__ == "__main__":
    root = tk.Tk()
    app = UniversalRecoveryApp(root)
    root.mainloop()

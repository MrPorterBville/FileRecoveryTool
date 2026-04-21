import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import os
import sys
import threading
import time
import re
import bisect
import ctypes
from ctypes import wintypes

# --- Universal Configuration ---
SECTOR_SIZE = 512 
CHUNK_SIZE = 8 * 1024 * 1024  
SCAN_OVERLAP = 256 * 1024
FRAGMENT_BLOCK_SIZE = 256 * 1024
FRAGMENT_SEARCH_WINDOW = 256 * 1024 * 1024
MAX_STITCH_BLOCKS = 1024

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
        self.check_vars = {}
        self.aggressive_var = tk.BooleanVar(value=True)
        self.unallocated_only_var = tk.BooleanVar(value=False)
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
        
        try:
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
            pattern = re.compile(b'|'.join([re.escape(h) for h in header_map.keys()]))

            with open(src, "rb") as f:
                off = 0
                while not self.stop_event.is_set():
                    # Align ONLY if physical
                    current_seek = (off // SECTOR_SIZE) * SECTOR_SIZE if is_physical else off
                    f.seek(current_seek)
                    
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk: break

                    if total_size: self.pbar['value'] = (current_seek / total_size) * 100
                    
                    for match in pattern.finditer(chunk):
                        found_idx = match.start()
                        header = match.group()
                        ftype = header_map.get(header)
                        abs_start = current_seek + found_idx
                        if not ftype or abs_start in processed_offsets:
                            continue
                        if alloc_filter and self._offset_is_allocated(abs_start, alloc_filter):
                            continue
                        processed_offsets.add(abs_start)
                        self.extract(src, abs_start, ftype, dst, active_types, is_physical, self.aggressive_var.get())

                    off = current_seek + CHUNK_SIZE - SCAN_OVERLAP

        except Exception as e:
            self.log(f"Error: {e}")
            if "PermissionError" in str(e): self.log("!!! RUN AS ADMINISTRATOR !!!")
        finally:
            self.root.after(0, lambda: self.btn_go.config(state="normal"))
            self.root.after(0, lambda: self.btn_stop.config(state="disabled"))
            self.log("Scan Finished.")

    def _offset_is_allocated(self, offset, alloc_filter):
        starts, ends = alloc_filter
        idx = bisect.bisect_right(starts, offset) - 1
        return idx >= 0 and offset < ends[idx]

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

    def _stitch_fragmented(self, fin, fout, cfg, ftype, rec_len):
        footer = cfg['footer']
        stitched = 0
        scanned = 0
        blocks = 0
        while scanned < FRAGMENT_SEARCH_WINDOW and blocks < MAX_STITCH_BLOCKS and rec_len < cfg['max'] and not self.stop_event.is_set():
            block = fin.read(FRAGMENT_BLOCK_SIZE)
            if not block:
                break
            scanned += len(block)
            blocks += 1
            if not self._block_looks_like_fragment(block, ftype):
                continue
            if footer:
                fpos = block.find(footer)
                if fpos != -1:
                    cut = fpos + len(footer)
                    to_write = block[:cut]
                    to_write = to_write[:max(0, cfg['max'] - rec_len)]
                    fout.write(to_write)
                    rec_len += len(to_write)
                    stitched += len(to_write)
                    return rec_len, stitched, True
            to_write = block[:max(0, cfg['max'] - rec_len)]
            if not to_write:
                break
            fout.write(to_write)
            rec_len += len(to_write)
            stitched += len(to_write)
        return rec_len, stitched, False

    def extract(self, src, start, ftype, dst, active_types, is_physical, aggressive):
        try:
            cfg = CONFIG[ftype]
            out_path = os.path.join(dst, ftype)
            os.makedirs(out_path, exist_ok=True)
            filename = os.path.join(out_path, f"recovered_{start}.{ftype.lower()}")
            
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
                lb_size = max(len(cfg['footer']) - 1, 32) if cfg['footer'] else 32
                lookbehind = b''
                active_headers = [CONFIG[t]['header'] for t in active_types]
                header_pattern = re.compile(b'|'.join([re.escape(h) for h in active_headers]))
                
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
                        if fpos != -1 and ((rec_len + fpos) > 150 * 1024 or ftype != 'JPG'):
                            cut = fpos + len(cfg['footer'])
                            to_write = data[:cut]
                            to_write = to_write[:max(0, cfg['max'] - rec_len)]
                            fout.write(to_write)
                            rec_len += len(to_write)
                            found_end = True
                            break

                    # Greedy logic for MP4/ZIP (also boundary-safe)
                    if cfg['greedy'] and rec_len > 1024 * 1024:
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

                stitched = 0
                if aggressive and cfg['footer'] and not found_end and rec_len < cfg['max']:
                    rec_len, stitched, found_end = self._stitch_fragmented(fin, fout, cfg, ftype, rec_len)
                    if stitched > 0:
                        self.log(f"Fragment stitch applied on {ftype} @ {hex(start)} (+{stitched // 1024} KB).")

            if rec_len > 0:
                self.files_found += 1
                sz = f"{rec_len // 1024} KB" if rec_len < 1024*1024 else f"{rec_len // (1024*1024)} MB"
                self.root.after(0, lambda: self.tree.insert("", 0, values=(self.files_found, ftype, sz, hex(start))))
        except: pass

if __name__ == "__main__":
    root = tk.Tk()
    app = UniversalRecoveryApp(root)
    root.mainloop()

import argparse
import json
import os
import re
import struct
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import filedialog, scrolledtext, ttk
from typing import Callable, Dict, List, Optional, Tuple


@dataclass
class NTFSBootSector:
    bytes_per_sector: int
    sectors_per_cluster: int
    cluster_size: int
    mft_lcn: int
    mftmirr_lcn: int
    record_size: int
    index_buffer_size: int
    total_sectors: int

    @staticmethod
    def _decode_record_size(raw_byte: int, cluster_size: int) -> int:
        signed = struct.unpack("b", bytes([raw_byte]))[0]
        if signed < 0:
            return 1 << (-signed)
        return signed * cluster_size

    @classmethod
    def from_bytes(cls, bs: bytes) -> "NTFSBootSector":
        if len(bs) < 90:
            raise ValueError("Boot sector too short.")
        if bs[3:11] != b"NTFS    ":
            raise ValueError("Volume does not look like NTFS (missing OEM ID).")

        bps = struct.unpack_from("<H", bs, 11)[0]
        spc = bs[13]
        total_sectors = struct.unpack_from("<Q", bs, 40)[0]
        mft_lcn = struct.unpack_from("<Q", bs, 48)[0]
        mftmirr_lcn = struct.unpack_from("<Q", bs, 56)[0]
        rec_raw = bs[64]
        idx_raw = bs[68]

        if bps == 0 or spc == 0:
            raise ValueError("Invalid NTFS geometry in boot sector.")

        cluster_size = bps * spc
        record_size = cls._decode_record_size(rec_raw, cluster_size)
        index_buffer_size = cls._decode_record_size(idx_raw, cluster_size)
        if record_size <= 0:
            raise ValueError("Invalid MFT record size in boot sector.")

        return cls(
            bytes_per_sector=bps,
            sectors_per_cluster=spc,
            cluster_size=cluster_size,
            mft_lcn=mft_lcn,
            mftmirr_lcn=mftmirr_lcn,
            record_size=record_size,
            index_buffer_size=index_buffer_size,
            total_sectors=total_sectors,
        )


@dataclass
class DataStream:
    resident_data: Optional[bytes] = None
    nonresident_runs: List[Tuple[int, int]] = field(default_factory=list)
    data_size: int = 0


@dataclass
class MFTEntry:
    record_number: int
    in_use: bool
    is_dir: bool
    parent_record: Optional[int]
    name: str
    data_stream: DataStream


class NTFSReconstructor:
    def __init__(
        self,
        image_path: str,
        out_dir: str,
        max_records: int = 250000,
        recover_deleted_only: bool = True,
        progress_cb: Optional[Callable[[float], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ):
        self.image_path = image_path
        self.out_dir = out_dir
        self.max_records = max_records
        self.recover_deleted_only = recover_deleted_only
        self.progress_cb = progress_cb
        self.log_cb = log_cb
        self.stop_event = stop_event or threading.Event()
        self.report: Dict[str, object] = {
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_image": image_path,
            "filesystem": "NTFS",
            "boot": {},
            "recover_deleted_only": recover_deleted_only,
            "entries_scanned": 0,
            "files_written": 0,
            "directories_created": 0,
            "skipped_in_use": 0,
            "errors": [],
            "written": [],
        }

    def log(self, msg: str):
        if self.log_cb:
            self.log_cb(msg)
        else:
            print(msg, flush=True)

    def _progress(self, pct: float):
        if self.progress_cb:
            self.progress_cb(max(0.0, min(100.0, pct)))

    def _safe_name(self, value: str) -> str:
        value = value.strip().replace("\x00", "")
        value = re.sub(r'[<>:"/|?*\\]', "_", value)
        return value or "unnamed"

    def _apply_fixup(self, rec: bytes) -> bytes:
        if len(rec) < 42:
            raise ValueError("MFT record too short.")
        usa_offset, usa_count = struct.unpack_from("<HH", rec, 4)
        if usa_offset + usa_count * 2 > len(rec):
            raise ValueError("Invalid USA offset/count.")
        usa = rec[usa_offset:usa_offset + usa_count * 2]
        usn = usa[:2]
        updates = [usa[i:i + 2] for i in range(2, len(usa), 2)]

        patched = bytearray(rec)
        for i, val in enumerate(updates, start=1):
            end = i * 512
            if end - 2 >= len(patched):
                break
            if patched[end - 2:end] != usn:
                raise ValueError("USA signature mismatch.")
            patched[end - 2:end] = val
        return bytes(patched)

    def _decode_runlist(self, raw: bytes) -> List[Tuple[int, int]]:
        runs: List[Tuple[int, int]] = []
        idx = 0
        current_lcn = 0
        while idx < len(raw):
            header = raw[idx]
            idx += 1
            if header == 0:
                break
            len_size = header & 0x0F
            off_size = (header >> 4) & 0x0F
            if idx + len_size + off_size > len(raw):
                break

            run_len = int.from_bytes(raw[idx:idx + len_size], "little", signed=False)
            idx += len_size

            off_raw = raw[idx:idx + off_size]
            idx += off_size
            run_off = int.from_bytes(off_raw, "little", signed=True)

            current_lcn += run_off
            runs.append((current_lcn, run_len))
        return runs

    def _parse_mft_entry(self, rec: bytes, rec_no: int) -> Optional[MFTEntry]:
        if rec[:4] != b"FILE":
            return None

        fixed = self._apply_fixup(rec)
        flags = struct.unpack_from("<H", fixed, 22)[0]
        in_use = bool(flags & 0x01)
        is_dir = bool(flags & 0x02)

        first_attr = struct.unpack_from("<H", fixed, 20)[0]
        off = first_attr

        name = f"record_{rec_no}"
        parent_record = None
        data_stream = DataStream()

        while off + 8 <= len(fixed):
            atype = struct.unpack_from("<I", fixed, off)[0]
            if atype == 0xFFFFFFFF:
                break
            alen = struct.unpack_from("<I", fixed, off + 4)[0]
            if alen < 16 or off + alen > len(fixed):
                break

            non_resident = fixed[off + 8]
            if atype == 0x30 and non_resident == 0:  # FILE_NAME
                content_len = struct.unpack_from("<I", fixed, off + 16)[0]
                content_off = struct.unpack_from("<H", fixed, off + 20)[0]
                cstart = off + content_off
                cend = cstart + content_len
                if cend <= len(fixed) and content_len >= 66:
                    fdata = fixed[cstart:cend]
                    parent_ref = int.from_bytes(fdata[0:6], "little", signed=False)
                    nlen = fdata[64]
                    nstart = 66
                    nend = nstart + nlen * 2
                    if nend <= len(fdata):
                        decoded = fdata[nstart:nend].decode("utf-16le", errors="ignore")
                        if decoded:
                            name = decoded
                            parent_record = parent_ref

            if atype == 0x80:  # DATA
                if non_resident == 0:
                    dlen = struct.unpack_from("<I", fixed, off + 16)[0]
                    doff = struct.unpack_from("<H", fixed, off + 20)[0]
                    dstart = off + doff
                    dend = dstart + dlen
                    if dend <= len(fixed):
                        data_stream.resident_data = fixed[dstart:dend]
                        data_stream.data_size = len(data_stream.resident_data)
                else:
                    runlist_off = struct.unpack_from("<H", fixed, off + 32)[0]
                    dsize = struct.unpack_from("<Q", fixed, off + 48)[0]
                    rstart = off + runlist_off
                    rend = off + alen
                    if rstart < rend <= len(fixed):
                        runlist = fixed[rstart:rend]
                        data_stream.nonresident_runs = self._decode_runlist(runlist)
                        data_stream.data_size = dsize

            off += alen

        return MFTEntry(
            record_number=rec_no,
            in_use=in_use,
            is_dir=is_dir,
            parent_record=parent_record,
            name=self._safe_name(name),
            data_stream=data_stream,
        )

    def _path_for_entry(self, entry: MFTEntry, all_entries: Dict[int, MFTEntry]) -> str:
        parts = [entry.name]
        current = entry
        visited = {entry.record_number}
        while current.parent_record is not None and current.parent_record in all_entries:
            parent = all_entries[current.parent_record]
            if parent.record_number in visited:
                break
            visited.add(parent.record_number)
            if parent.record_number == 5:  # root
                break
            parts.append(parent.name)
            current = parent
        parts.reverse()
        return os.path.join(*parts) if parts else f"record_{entry.record_number}"

    def _read_nonresident(self, fh, runs: List[Tuple[int, int]], cluster_size: int, max_bytes: int) -> bytes:
        out = bytearray()
        remaining = max_bytes
        for lcn, length in runs:
            if remaining <= 0 or self.stop_event.is_set():
                break
            if lcn < 0 or length <= 0:
                continue
            byte_off = lcn * cluster_size
            byte_len = length * cluster_size
            take = min(byte_len, remaining)
            fh.seek(byte_off)
            chunk = fh.read(take)
            out.extend(chunk)
            remaining -= len(chunk)
            if len(chunk) < take:
                break
        return bytes(out)

    def reconstruct(self):
        os.makedirs(self.out_dir, exist_ok=True)

        with open(self.image_path, "rb") as fh:
            bs = fh.read(512)
            boot = NTFSBootSector.from_bytes(bs)
            self.report["boot"] = {
                "bytes_per_sector": boot.bytes_per_sector,
                "sectors_per_cluster": boot.sectors_per_cluster,
                "cluster_size": boot.cluster_size,
                "mft_lcn": boot.mft_lcn,
                "record_size": boot.record_size,
                "total_sectors": boot.total_sectors,
            }

            self.log(f"NTFS detected: cluster={boot.cluster_size} bytes, record={boot.record_size} bytes")
            mft_offset = boot.mft_lcn * boot.cluster_size

            entries: Dict[int, MFTEntry] = {}
            for rec_no in range(self.max_records):
                if self.stop_event.is_set():
                    self.log("Reconstruction stopped by user during MFT parsing.")
                    break
                rec_off = mft_offset + rec_no * boot.record_size
                fh.seek(rec_off)
                rec = fh.read(boot.record_size)
                if len(rec) < boot.record_size:
                    break
                if rec[:4] not in (b"FILE", b"BAAD"):
                    if rec_no % 2048 == 0:
                        self._progress((rec_no / max(1, self.max_records)) * 45.0)
                    continue
                try:
                    ent = self._parse_mft_entry(rec, rec_no)
                    if ent:
                        entries[rec_no] = ent
                except Exception as ex:
                    if len(self.report["errors"]) < 200:
                        self.report["errors"].append(f"record {rec_no}: {ex}")
                if rec_no % 1024 == 0:
                    self._progress((rec_no / max(1, self.max_records)) * 45.0)

            self.report["entries_scanned"] = len(entries)
            self.log(f"MFT entries parsed: {len(entries)}")
            self._progress(50.0)

            # Create directories first.
            for idx, ent in enumerate(entries.values(), start=1):
                if self.stop_event.is_set():
                    self.log("Reconstruction stopped by user during directory rebuild.")
                    break
                if self.recover_deleted_only and ent.in_use:
                    continue
                if not ent.is_dir:
                    continue
                rel = self._path_for_entry(ent, entries)
                abs_dir = os.path.join(self.out_dir, rel)
                os.makedirs(abs_dir, exist_ok=True)
                self.report["directories_created"] += 1
                if idx % 1024 == 0:
                    self._progress(50.0 + (idx / max(1, len(entries))) * 20.0)

            # Reconstruct files.
            for idx, ent in enumerate(entries.values(), start=1):
                if self.stop_event.is_set():
                    self.log("Reconstruction stopped by user during file extraction.")
                    break

                if self.recover_deleted_only and ent.in_use:
                    self.report["skipped_in_use"] += 1
                    continue
                if ent.is_dir:
                    continue

                ds = ent.data_stream
                if ds.resident_data is None and not ds.nonresident_runs:
                    continue

                rel = self._path_for_entry(ent, entries)
                abs_file = os.path.join(self.out_dir, rel)
                os.makedirs(os.path.dirname(abs_file), exist_ok=True)

                try:
                    if ds.resident_data is not None:
                        payload = ds.resident_data
                    else:
                        payload = self._read_nonresident(fh, ds.nonresident_runs, boot.cluster_size, ds.data_size)
                    if not payload:
                        continue

                    with open(abs_file, "wb") as out:
                        out.write(payload)

                    self.report["files_written"] += 1
                    if len(self.report["written"]) < 10000:
                        self.report["written"].append({
                            "record": ent.record_number,
                            "path": rel,
                            "bytes": len(payload),
                            "resident": ds.resident_data is not None,
                            "runs": len(ds.nonresident_runs),
                            "was_in_use": ent.in_use,
                        })
                except Exception as ex:
                    if len(self.report["errors"]) < 200:
                        self.report["errors"].append(f"write {ent.record_number}: {ex}")

                if idx % 256 == 0:
                    self._progress(70.0 + (idx / max(1, len(entries))) * 30.0)

        report_path = os.path.join(self.out_dir, "ntfs_reconstruction_report.json")
        with open(report_path, "w", encoding="utf-8") as rf:
            json.dump(self.report, rf, indent=2)
        self._progress(100.0)
        self.log(f"Reconstruction complete. Report: {report_path}")


class NTFSReconstructionApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Phoenix NTFS Full Reconstruction")
        self.root.geometry("1100x850")
        self.stop_event = threading.Event()
        self._setup_ui()

    def _setup_ui(self):
        main = ttk.Frame(self.root, padding=15)
        main.pack(fill="both", expand=True)

        src_frame = ttk.LabelFrame(main, text=" 1. Select NTFS Source (Raw Image or Physical Volume) ", padding=10)
        src_frame.pack(fill="x", pady=5)
        self.ent_src = ttk.Entry(src_frame)
        self.ent_src.pack(fill="x", side="left", expand=True, padx=5)
        ttk.Button(src_frame, text="Browse", command=self.browse_file).pack(side="left", padx=2)

        dst_frame = ttk.LabelFrame(main, text=" 2. Select Destination ", padding=10)
        dst_frame.pack(fill="x", pady=5)
        self.ent_dst = ttk.Entry(dst_frame)
        self.ent_dst.pack(fill="x", side="left", expand=True, padx=5)
        ttk.Button(dst_frame, text="Browse", command=self.browse_dest).pack(side="left")

        opt_frame = ttk.LabelFrame(main, text=" 3. Reconstruction Options ", padding=10)
        opt_frame.pack(fill="x", pady=10)
        self.deleted_only_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt_frame,
            text="Recover deleted/not-in-use entries only (skip active files)",
            variable=self.deleted_only_var,
        ).pack(side="left", padx=15)
        ttk.Label(opt_frame, text="Max MFT records:").pack(side="left", padx=5)
        self.max_records_var = tk.StringVar(value="250000")
        ttk.Entry(opt_frame, width=12, textvariable=self.max_records_var).pack(side="left")

        self.pbar = ttk.Progressbar(main, maximum=100)
        self.pbar.pack(fill="x", pady=15)

        ctl_box = ttk.Frame(main)
        ctl_box.pack(fill="x")
        self.btn_go = ttk.Button(ctl_box, text="START FULL RECONSTRUCTION", command=self.start)
        self.btn_go.pack(side="left", fill="x", expand=True)
        self.btn_stop = ttk.Button(ctl_box, text="STOP", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", fill="x", expand=True)

        self.tree = ttk.Treeview(main, columns=("id", "path", "size", "status"), show="headings")
        self.tree.heading("id", text="#")
        self.tree.column("id", width=60)
        self.tree.heading("path", text="Recovered Path")
        self.tree.column("path", width=520)
        self.tree.heading("size", text="Size")
        self.tree.column("size", width=130)
        self.tree.heading("status", text="Status")
        self.tree.column("status", width=140)
        self.tree.pack(fill="both", expand=True, pady=10)

        self.log_box = scrolledtext.ScrolledText(main, height=6, bg="#1a1a1a", fg="#00d4ff", font=("Consolas", 9))
        self.log_box.pack(fill="x")

    def log(self, msg: str):
        self.root.after(0, lambda: self.log_box.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n") or self.log_box.see(tk.END))

    def browse_file(self):
        f = filedialog.askopenfilename(title="Select NTFS Disk Image/Volume")
        if f:
            self.ent_src.delete(0, tk.END)
            self.ent_src.insert(0, f)

    def browse_dest(self):
        d = filedialog.askdirectory(title="Select Output Folder")
        if d:
            self.ent_dst.delete(0, tk.END)
            self.ent_dst.insert(0, d)

    def set_progress(self, pct: float):
        self.root.after(0, lambda: self.pbar.configure(value=pct))

    def stop(self):
        self.stop_event.set()

    def start(self):
        src = self.ent_src.get().strip()
        dst = self.ent_dst.get().strip()
        if not src or not dst:
            self.log("Please select source and destination.")
            return

        try:
            max_records = int(self.max_records_var.get().strip())
            if max_records <= 0:
                raise ValueError
        except Exception:
            self.log("Invalid max-records value.")
            return

        self.stop_event.clear()
        self.tree.delete(*self.tree.get_children())
        self.pbar.configure(value=0)
        self.btn_go.config(state="disabled")
        self.btn_stop.config(state="normal")

        threading.Thread(
            target=self._run_reconstruct,
            args=(src, dst, max_records, self.deleted_only_var.get()),
            daemon=True,
        ).start()

    def _run_reconstruct(self, src: str, dst: str, max_records: int, deleted_only: bool):
        try:
            recon = NTFSReconstructor(
                src,
                dst,
                max_records=max_records,
                recover_deleted_only=deleted_only,
                progress_cb=self.set_progress,
                log_cb=self.log,
                stop_event=self.stop_event,
            )
            recon.reconstruct()

            written = recon.report.get("written", [])
            for idx, item in enumerate(written, start=1):
                size = item.get("bytes", 0)
                size_s = f"{size // 1024} KB" if size < 1024 * 1024 else f"{size // (1024 * 1024)} MB"
                status = "Deleted" if not item.get("was_in_use", True) else "In-use"
                self.root.after(0, lambda i=idx, p=item.get("path", ""), s=size_s, st=status: self.tree.insert("", "end", values=(i, p, s, st)))

            self.log(f"Done. Files written: {recon.report.get('files_written', 0)}")
            self.log(f"Skipped active files: {recon.report.get('skipped_in_use', 0)}")
        except Exception as ex:
            self.log(f"Fatal error: {ex}")
        finally:
            self.root.after(0, lambda: self.btn_go.config(state="normal"))
            self.root.after(0, lambda: self.btn_stop.config(state="disabled"))


def main_cli():
    parser = argparse.ArgumentParser(description="Full NTFS reconstruction from a disk image/volume.")
    parser.add_argument("source", help="Path to NTFS disk image or raw volume")
    parser.add_argument("destination", help="Output directory for reconstructed files")
    parser.add_argument("--max-records", type=int, default=250000, help="Maximum number of MFT records to scan")
    parser.add_argument(
        "--include-in-use",
        action="store_true",
        help="Also export active/in-use files (default: recover deleted/not-in-use only)",
    )
    args = parser.parse_args()

    recon = NTFSReconstructor(
        args.source,
        args.destination,
        args.max_records,
        recover_deleted_only=not args.include_in_use,
    )
    recon.reconstruct()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--gui":
        root = tk.Tk()
        app = NTFSReconstructionApp(root)
        root.mainloop()
    else:
        main_cli()

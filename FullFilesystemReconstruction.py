import argparse
import json
import os
import re
import struct
import time
import threading
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Dict, List, Optional, Tuple


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
        logger=None,
    ):
        self.image_path = image_path
        self.out_dir = out_dir
        self.max_records = max_records
        self.recover_deleted_only = recover_deleted_only
        self.logger = logger
        self.report: Dict[str, object] = {
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_image": image_path,
            "filesystem": "NTFS",
            "boot": {},
            "entries_scanned": 0,
            "files_written": 0,
            "files_skipped_not_deleted": 0,
            "directories_created": 0,
            "errors": [],
            "written": [],
        }

    def log(self, msg: str):
        if self.logger:
            self.logger(msg)
        else:
            print(msg, flush=True)

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
                        try:
                            decoded = fdata[nstart:nend].decode("utf-16le", errors="ignore")
                            if decoded:
                                name = decoded
                                parent_record = parent_ref
                        except Exception:
                            pass

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
        if not parts:
            return f"record_{entry.record_number}"
        return os.path.join(*parts)

    def _read_nonresident(self, fh, runs: List[Tuple[int, int]], cluster_size: int, max_bytes: int) -> bytes:
        out = bytearray()
        remaining = max_bytes
        for lcn, length in runs:
            if remaining <= 0:
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
                rec_off = mft_offset + rec_no * boot.record_size
                fh.seek(rec_off)
                rec = fh.read(boot.record_size)
                if len(rec) < boot.record_size:
                    break
                if rec[:4] not in (b"FILE", b"BAAD"):
                    continue
                try:
                    ent = self._parse_mft_entry(rec, rec_no)
                    if ent:
                        entries[rec_no] = ent
                except Exception as ex:
                    if len(self.report["errors"]) < 200:
                        self.report["errors"].append(f"record {rec_no}: {ex}")

            self.report["entries_scanned"] = len(entries)
            self.log(f"MFT entries parsed: {len(entries)}")

            # Create directories first.
            for ent in entries.values():
                if not ent.in_use or not ent.is_dir:
                    continue
                rel = self._path_for_entry(ent, entries)
                abs_dir = os.path.join(self.out_dir, rel)
                os.makedirs(abs_dir, exist_ok=True)
                self.report["directories_created"] += 1

            # Reconstruct files.
            for ent in entries.values():
                if ent.is_dir:
                    continue
                if self.recover_deleted_only and ent.in_use:
                    self.report["files_skipped_not_deleted"] += 1
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
                        })
                except Exception as ex:
                    if len(self.report["errors"]) < 200:
                        self.report["errors"].append(f"write {ent.record_number}: {ex}")

        report_path = os.path.join(self.out_dir, "ntfs_reconstruction_report.json")
        with open(report_path, "w", encoding="utf-8") as rf:
            json.dump(self.report, rf, indent=2)
        self.log(f"Reconstruction complete. Report: {report_path}")


class NTFSReconstructionGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("NTFS Full Reconstruction")
        self.root.geometry("900x700")
        self.worker: Optional[threading.Thread] = None
        self._build()

    def _build(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        src_box = ttk.LabelFrame(main, text="1. Source NTFS image/volume", padding=10)
        src_box.pack(fill="x", pady=6)
        self.src_entry = ttk.Entry(src_box)
        self.src_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(src_box, text="Browse", command=self._pick_source).pack(side="left")

        dst_box = ttk.LabelFrame(main, text="2. Output folder", padding=10)
        dst_box.pack(fill="x", pady=6)
        self.dst_entry = ttk.Entry(dst_box)
        self.dst_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(dst_box, text="Browse", command=self._pick_destination).pack(side="left")

        opts = ttk.LabelFrame(main, text="3. Options", padding=10)
        opts.pack(fill="x", pady=6)
        ttk.Label(opts, text="Max MFT records:").pack(side="left")
        self.max_records_entry = ttk.Entry(opts, width=10)
        self.max_records_entry.insert(0, "250000")
        self.max_records_entry.pack(side="left", padx=(8, 20))
        self.deleted_only_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts,
            text="Skip files that are not deleted (recover deleted entries only)",
            variable=self.deleted_only_var,
        ).pack(side="left")

        ctl = ttk.Frame(main)
        ctl.pack(fill="x", pady=10)
        self.start_btn = ttk.Button(ctl, text="START RECONSTRUCTION", command=self._start)
        self.start_btn.pack(side="left", fill="x", expand=True)

        self.log_box = scrolledtext.ScrolledText(main, height=24)
        self.log_box.pack(fill="both", expand=True)

    def _pick_source(self):
        path = filedialog.askopenfilename(title="Select NTFS image/raw volume file")
        if path:
            self.src_entry.delete(0, tk.END)
            self.src_entry.insert(0, path)

    def _pick_destination(self):
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.dst_entry.delete(0, tk.END)
            self.dst_entry.insert(0, path)

    def _log(self, message: str):
        self.root.after(0, lambda: self.log_box.insert(tk.END, f"{message}\n") or self.log_box.see(tk.END))

    def _start(self):
        source = self.src_entry.get().strip()
        destination = self.dst_entry.get().strip()
        if not source or not destination:
            messagebox.showerror("Missing input", "Please select source and destination.")
            return
        try:
            max_records = int(self.max_records_entry.get().strip())
            if max_records <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid max records", "Max MFT records must be a positive integer.")
            return

        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Running", "A reconstruction job is already running.")
            return

        self.start_btn.config(state="disabled")
        self.log_box.delete("1.0", tk.END)

        def run():
            try:
                recon = NTFSReconstructor(
                    source,
                    destination,
                    max_records=max_records,
                    recover_deleted_only=self.deleted_only_var.get(),
                    logger=self._log,
                )
                recon.reconstruct()
                self.root.after(0, lambda: messagebox.showinfo("Done", "Reconstruction complete."))
            except Exception as ex:
                self._log(f"Error: {ex}")
                self.root.after(0, lambda: messagebox.showerror("Failed", str(ex)))
            finally:
                self.root.after(0, lambda: self.start_btn.config(state="normal"))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()


def main():
    parser = argparse.ArgumentParser(description="Full NTFS reconstruction from a disk image/volume.")
    parser.add_argument("source", nargs="?", help="Path to NTFS disk image or raw volume")
    parser.add_argument("destination", nargs="?", help="Output directory for reconstructed files")
    parser.add_argument("--max-records", type=int, default=250000, help="Maximum number of MFT records to scan")
    parser.add_argument(
        "--include-active",
        action="store_true",
        help="Also reconstruct files that are still active (not deleted).",
    )
    parser.add_argument("--gui", action="store_true", help="Launch the desktop GUI.")
    args = parser.parse_args()

    if args.gui:
        root = tk.Tk()
        NTFSReconstructionGUI(root)
        root.mainloop()
        return

    if not args.source or not args.destination:
        parser.error("source and destination are required unless --gui is used")

    recon = NTFSReconstructor(
        args.source,
        args.destination,
        args.max_records,
        recover_deleted_only=not args.include_active,
    )
    recon.reconstruct()


if __name__ == "__main__":
    main()

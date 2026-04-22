import argparse
import json
import os
import re
import struct
import time
from dataclasses import dataclass, field
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
    def __init__(self, image_path: str, out_dir: str, max_records: int = 250000):
        self.image_path = image_path
        self.out_dir = out_dir
        self.max_records = max_records
        self.report: Dict[str, object] = {
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_image": image_path,
            "filesystem": "NTFS",
            "boot": {},
            "entries_scanned": 0,
            "files_written": 0,
            "directories_created": 0,
            "errors": [],
            "written": [],
        }

    def log(self, msg: str):
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
                if not ent.in_use or ent.is_dir:
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


def main():
    parser = argparse.ArgumentParser(description="Full NTFS reconstruction from a disk image/volume.")
    parser.add_argument("source", help="Path to NTFS disk image or raw volume")
    parser.add_argument("destination", help="Output directory for reconstructed files")
    parser.add_argument("--max-records", type=int, default=250000, help="Maximum number of MFT records to scan")
    args = parser.parse_args()

    recon = NTFSReconstructor(args.source, args.destination, args.max_records)
    recon.reconstruct()


if __name__ == "__main__":
    main()

import struct
import hashlib
import bz2
import lzma
import os

import update_metadata_pb2 as um


def u64(x):
    return struct.unpack('>Q', x)[0]


def u32(x):
    return struct.unpack('>I', x)[0]


OP_TYPE_NAMES = {
    0: "REPLACE", 1: "REPLACE_BZ", 2: "MOVE", 3: "BSDIFF",
    4: "SOURCE_COPY", 5: "SOURCE_BSDIFF", 8: "REPLACE_XZ",
    6: "ZERO", 7: "DISCARD", 10: "BROTLI_BSDIFF",
    9: "PUFFDIFF", 11: "ZUCCHINI", 12: "LZ4DIFF_BSDIFF",
    13: "LZ4DIFF_PUFFDIFF", 14: "ZSTD",
}


class PayloadError(Exception):
    pass


class UnsupportedOpError(PayloadError):
    pass


def parse_header(data):
    if data[:4] != b'CrAU':
        raise PayloadError("Not an OTA payload (missing CrAU magic)")
    version = u64(data[4:12])
    manifest_size = u64(data[12:20])
    metadata_sig_size = u32(data[20:24]) if version > 1 else 0
    manifest_raw = data[24:24 + manifest_size]
    data_offset = 24 + manifest_size + metadata_sig_size
    dam = um.DeltaArchiveManifest()
    dam.ParseFromString(manifest_raw)
    return dam, data_offset, version


def list_partitions(dam):
    result = []
    for p in dam.partitions:
        info = p.new_partition_info
        size = info.size if info and info.size else 0
        result.append({
            "name": p.partition_name,
            "size": size,
            "size_mb": size / (1024 * 1024),
            "ops_count": len(p.operations),
        })
    return result


def get_zip_ranges_for_partitions(target_names, dam, data_offset, payload_zip_offset):
    all_ranges = []
    for p in dam.partitions:
        if p.partition_name not in target_names:
            continue
        for op in p.operations:
            if op.type in (0, 1, 8, 14):
                start = payload_zip_offset + data_offset + op.data_offset
                end = start + op.data_length - 1
                all_ranges.append((start, end, p.partition_name))
    return _merge_ranges(all_ranges)


def _merge_ranges(ranges, max_gap=100000):
    if not ranges:
        return []
    sorted_r = sorted(ranges, key=lambda r: r[0])
    merged = [(sorted_r[0][0], sorted_r[0][1], {sorted_r[0][2]})]
    for r in sorted_r[1:]:
        last = merged[-1]
        if r[0] <= last[1] + max_gap:
            merged[-1] = (last[0], max(last[1], r[1]), last[2] | {r[2]})
        else:
            merged.append((r[0], r[1], {r[2]}))
    return [(s, e, p) for s, e, p in merged]


def extract_partition(part, data_buf, buf_zip_start, payload_data_offset, payload_zip_offset, block_size, output_dir):
    out_path = os.path.join(output_dir, f"{part.partition_name}.img")
    with open(out_path, "wb") as out:
        for op in part.operations:
            _process_op_from_buf(op, data_buf, buf_zip_start, out,
                                 payload_data_offset, payload_zip_offset, block_size)
    return out_path


def _process_op_from_buf(op, buf, buf_zip_start, out_file,
                          payload_data_offset, payload_zip_offset, block_size):
    if op.type in (0, 1, 8, 14):
        payload_abs = payload_zip_offset + payload_data_offset + op.data_offset
        local_start = payload_abs - buf_zip_start
        compressed = buf[local_start:local_start + op.data_length]
        if op.data_sha256_hash and hashlib.sha256(compressed).digest() != op.data_sha256_hash:
            raise PayloadError(f"SHA256 mismatch for {op.type} op")

        if op.type == 0:
            _write_extents(out_file, compressed, op.dst_extents, block_size)
        elif op.type == 1:
            _write_extents(out_file, bz2.BZ2Decompressor().decompress(compressed),
                           op.dst_extents, block_size)
        elif op.type == 8:
            _write_extents(out_file, lzma.LZMADecompressor().decompress(compressed),
                           op.dst_extents, block_size)
        elif op.type == 14:
            import zstandard
            dec = zstandard.ZstdDecompressor().decompressobj()
            _write_extents(out_file, dec.decompress(compressed),
                           op.dst_extents, block_size)

    elif op.type == 6:
        for ext in op.dst_extents:
            out_file.seek(ext.start_block * block_size)
            out_file.write(b'\x00' * ext.num_blocks * block_size)

    elif op.type == 7:
        pass

    else:
        raise UnsupportedOpError(
            f"Unsupported operation type {op.type} ({OP_TYPE_NAMES.get(op.type, '?')}). "
            "Only full OTA payloads are supported."
        )


def _write_extents(out_file, data, dst_extents, block_size):
    offset = 0
    for ext in dst_extents:
        out_file.seek(ext.start_block * block_size)
        chunk = ext.num_blocks * block_size
        out_file.write(data[offset:offset + chunk])
        offset += chunk

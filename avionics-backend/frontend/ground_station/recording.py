"""Streaming identity check for a complete v2 file or v3 volume chain."""
import hashlib
from pathlib import Path
import struct
import zlib


def archive_identity(first):
    first = Path(first)
    aggregate = hashlib.sha256()
    previous = bytes(32)
    session = None
    number = 1
    records = 0
    while True:
        path = first if number == 1 else Path(str(first) + f".part{number:06d}.avbus")
        with path.open("rb") as stream:
            magic = stream.read(8)
            if magic not in (b"AVBUS02\n", b"AVBUS03\n") or (number > 1 and magic != b"AVBUS03\n"):
                raise ValueError("不支持的归档格式：" + str(path))
            identity = stream.read(16)
            if len(identity) != 16 or (session is not None and identity != session):
                raise ValueError("分卷会话编号不一致：" + str(path))
            session = identity
            version3 = magic == b"AVBUS03\n"
            if version3:
                extension = stream.read(36)
                if len(extension) != 36 or struct.unpack("<I", extension[:4])[0] != number or extension[4:] != previous:
                    raise ValueError("卷号或前一卷摘要不一致，请从第一卷打开。")
            footer_size = 65 if version3 else 64
            size = stream.seek(0, 2)
            if size < (60 if version3 else 24) + footer_size:
                raise ValueError("归档未封存，请先恢复有效记录。")
            stream.seek(0)
            data_hash = hashlib.sha256()
            remaining = size - footer_size
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("归档在读取时被截断。")
                data_hash.update(chunk); aggregate.update(chunk)
                remaining -= len(chunk)
            footer = stream.read(footer_size)
            if len(footer) != footer_size or stream.read(1):
                raise ValueError("归档在读取时发生变化。")
            length, crc = struct.unpack("<II", footer[:8])
            count, data_bytes, last = struct.unpack("<QQQ", footer[8:32])
            end = footer[-1] if version3 else 0
            if length != 0 or zlib.crc32(footer[8:]) != crc or data_bytes != size - footer_size or data_hash.digest() != footer[32:64] or end > 2:
                raise ValueError("归档封口校验失败，请先检查并恢复。")
            aggregate.update(footer)
            data_hash.update(footer); previous = data_hash.digest()
            records += count
        if end != 1:
            return {"file_sha256": aggregate.hexdigest(), "volumes": number, "records": records,
                    "recovered": end == 2, "recording_session_id": session.hex()}
        number += 1

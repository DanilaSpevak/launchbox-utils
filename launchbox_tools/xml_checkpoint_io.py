from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import BinaryIO

from .operation_lifecycle import OperationControl


IO_CHUNK_SIZE = 1024 * 1024
XML_CHECKPOINT_INTERVAL = 256


class CheckpointingBinaryReader:
    """Limit parser reads and check cancellation around every input chunk."""

    def __init__(self, source: BinaryIO, control: OperationControl) -> None:
        self.source = source
        self.control = control

    def read(self, size: int = -1) -> bytes:
        read_size = IO_CHUNK_SIZE if size < 0 else min(size, IO_CHUNK_SIZE)
        self.control.checkpoint()
        payload = self.source.read(read_size)
        self.control.checkpoint()
        return payload


def parse_xml_tree_with_checkpoints(
    path: Path,
    control: OperationControl,
) -> ET.ElementTree:
    control.checkpoint()
    with path.open("rb") as source:
        reader = CheckpointingBinaryReader(source, control)
        iterator = ET.iterparse(reader, events=("end",))
        for index, _event in enumerate(iterator, start=1):
            if index % XML_CHECKPOINT_INTERVAL == 0:
                control.checkpoint()
        root = iterator.root
    control.checkpoint()
    return ET.ElementTree(root)

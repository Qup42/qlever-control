from __future__ import annotations

import struct
from pathlib import Path

from qlever.command import QleverCommand
from qlever.log import log


class DebugIndexFileCommand(QleverCommand):
    """
    Class for executing the `debug-index-file` command.
    """

    def __init__(self):
        pass

    def description(self) -> str:
        return "Read and display the last 64-bit word from an index permutation file"

    def should_have_qleverfile(self) -> bool:
        return True

    def relevant_qleverfile_arguments(self) -> dict[str, list[str]]:
        return {"data": ["name"]}

    def additional_arguments(self, subparser) -> None:
        subparser.add_argument(
            "permutation",
            choices=["PSO", "SPO", "POS", "OPS", "OSP", "SOP"],
            help="Index permutation to read",
        )

    def execute(self, args) -> bool:
        # Construct filename
        index_file = f"{args.name}.index.{args.permutation.lower()}"
        relations_file = f"{args.name}.index.{args.permutation.lower()}.meta"

        if args.show:
            return True

        def validate_exists(file: str) -> bool:
            if not Path(file).exists():
                log.error(f'File "{file}" not found in current directory')
                return False
            return True
        def get_size(file: str) -> int:
            # Check file size
            file_size = Path(file).stat().st_size
            if file_size < 8:
                log.error(
                    f'File "{index_file}" is too small ({file_size} bytes), '
                    f"need at least 8 bytes to read a 64-bit word"
                )
                return -1
            return file_size

        if not validate_exists(index_file):
            return False
        if not validate_exists(relations_file):
            return False
        file_size = get_size(index_file)
        if file_size == -1:
            return False

        def read_uint64():
            return struct.unpack("<Q", f.read(8))[0]
        def read_uint32():
            return struct.unpack("<L", f.read(4))[0]
        def read_off_t():
            return struct.unpack("<q", f.read(8))[0]
        def read_string():
            length = read_uint64()
            return f.read(length).decode("utf-8")
        def read_bool():
            return struct.unpack("<?", f.read(1))[0]
        def read_OffsetAndCompressedSize():
            offset = read_uint64()
            compressed_size = read_uint64()
            return (offset, compressed_size)
        def read_CompressedBlockMetaData():
            offsets = read_vector(read_OffsetAndCompressedSize)
            for i, (offset, compressed_size) in enumerate(offsets):
                log.debug(
                    f"Column {i}: offset=0x{offset:016x}, "
                    f"compressed_size={compressed_size} bytes"
                )
            num_rows = read_uint64()
            log.debug(f"Number of rows in block: {num_rows}")
            first_triple = read_PermutedTriple()
            log.debug(f"First triple in block: {first_triple}")
            last_triple = read_PermutedTriple()
            log.debug(f"Last triple in block: {last_triple}")
            graph_info = read_optional(lambda: read_vector(read_id))
            if graph_info is not None:
                log.debug(f"Graph info IDs: {graph_info}")
            else:
                log.debug("No graph info IDs")
            contains_duplicate_graphs = read_bool()
            log.debug(f"Contains duplicate graphs: {contains_duplicate_graphs}")
            block_index = read_uint64()
            log.debug(f"Block index: {block_index}")
        def read_optional(read_element):
            has_value = read_bool()
            if has_value:
                return read_element()
            else:
                return None
        def read_vector(read_element):
            log.debug(f"Location: {f.tell():x}")
            length = read_uint64()
            log.debug(f"Reading vector of length {length}")
            return [read_element() for _ in range(length)]
        def read_id():
            return read_uint64()
        def read_PermutedTriple():
            return (read_id(), read_id(), read_id(), read_id())
        def read_float():
            return struct.unpack("<f", f.read(4))[0]
        def read_CompressedRelationMetadata():
            col0Id = read_id()
            numRows = read_uint64()
            multiplicityCol1 = read_float()
            multiplicityCol2 = read_float()
            offsetInBlock = read_uint64()
            log.debug(
                f"CompressedRelationMetadata: col0Id={col0Id}, "
                f"numRows={numRows}, multiplicityCol1={multiplicityCol1}, "
                f"multiplicityCol2={multiplicityCol2}, "
                f"offsetInBlock=0x{offsetInBlock:016x}"
            )

        # Read last 8 bytes
        try:
            with open(index_file, "rb") as f:
                f.seek(-8, 2)  # Seek to 8 bytes before end
                data_end = f.tell()
                data_begin = read_uint64()

                # Display results
                log.info(f"File: {index_file}")
                log.info(f"File size: 0x{file_size:08x} bytes")
                log.info(f"Data begin: 0x{data_begin:016x}")
                log.info(f"Data end: 0x{data_end:016x}")

                f.seek(data_begin, 0)

                magic_number = read_uint64()
                version = read_uint64()

                log.info(f"Magic number: 0x{magic_number:016x}")
                assert magic_number == 18446744073709551613 # std::numeric_limits<uint64_t>::max() - 2
                log.info(f"Version: {version}")
                assert version == 2

                name = read_string()
                log.info(f"Index name: {name} (length {len(name)})")

                # The relation metadata is stored on disk, so not here.
                #read_vector(read_CompressedRelationMetadata)
                read_vector(read_CompressedBlockMetaData)

                offsetAfter = read_uint64()
                log.info(f"Offset after metadata: 0x{offsetAfter:016x}")
                totalElements = read_uint64()
                log.info(f"Total elements in index: {totalElements}")
                numDistinctCol0 = read_uint64()
                log.info(f"Number of distinct values in column 0: {numDistinctCol0}")

                log.info(f"Location after reading metadata: 0x{f.tell():016x}")

        except Exception as e:
            log.error(f'Error reading file "{index_file}": {e}')
            return False

        file_size = get_size(index_file)
        if file_size == -1:
            return False

        with open(relations_file, "rb") as f:
            f.seek(-32, 2)  # Seek to 40 bytes before end
            log.info(f"Metadata begin at {f.tell():x}")
            size = read_uint64()
            log.info(f"Size: {size}")
            capacity = read_uint64()
            log.info(f"Capacity: {capacity}")
            bytesize = read_uint64()
            log.info(f"Bytesize: 0x{bytesize:016x}")
            magic_number = read_uint32()
            log.info(f"Magic number: {magic_number:08x}")
            assert magic_number == 7601577
            version = read_uint32()
            log.info(f"Version: {version}")
            assert version == 0

            f.seek(0, 0)
            for _ in range(size):
                read_CompressedRelationMetadata()



        return True

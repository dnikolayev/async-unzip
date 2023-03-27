from pathlib import PurePath, Path
from zipfile import ZipFile, is_zipfile, BadZipFile
from zlib import decompressobj, MAX_WBITS, error as ZLIB_error

missed_modules = 0
async_reader = 'aiofile'
try:
    from aiofile import async_open
except ModuleNotFoundError as err:
    missed_modules += 1

try:
    from aiofiles import open as async_open
    async_reader = 'aiofiles'
except ModuleNotFoundError as err:
    missed_modules += 1
except ImportError as err:
    missed_modules += 1

if missed_modules == 2:
    print("""Not aiofile nor aiofiles is present! Going to crash..
        please do:
            pip install aiofile
        or
            pip install aiofiles
        to make the code working, Thanks!""")


DEFAULT_READ_BUFFER_SIZE = 64 * 1024


async def unzip(zip_file, path=None, files=[], regex_files=None, buffer_size=None, __debug=None):
    read_block = buffer_size if (buffer_size and int(buffer_size)>0) else DEFAULT_READ_BUFFER_SIZE

    if is_zipfile(zip_file):
        files_info = ZipFile(zip_file).infolist()
        if path == None:
            extra_path = ''
        else:
            extra_path = PurePath(path)
        async with async_open(zip_file, mode='rb') as src:
            for in_file in files_info:
                file_name = in_file.filename
                unpack_filename_path = Path(str(PurePath(extra_path, file_name)))
                if __debug:
                    print(in_file)
                    print(unpack_filename_path)
                if async_reader == 'aiofile':
                    src.seek(in_file.header_offset)
                else:
                    await src.seek(in_file.header_offset)
                if __debug:
                    print(f'Done HEADER_OFFSET seek: {in_file.header_offset}')
                temp = await src.read(30)
                if __debug:
                    print(f'Done FILEPATH seek: {30} - {temp}')
                temp = await src.read(len(in_file.filename))
                if __debug:
                    print(f'Done FILENAME seek: {len(in_file.filename)} - {temp}')
                if in_file.file_size < 4294967296:
                    if len(in_file.extra) > 0:
                        t = await src.read(len(in_file.extra))
                        if __debug:
                            print(f'Done EXTRA seek: {len(in_file.extra)} {t}')
                    if len(in_file.comment) > 0:
                        await src.read(len(in_file.comment))
                        if __debug:
                            print(f'Done COMMENT seek: {len(in_file.comment)}')

                if in_file.is_dir():
                    unpack_filename_path.mkdir(parents=True, exist_ok=True)
                    continue
                else:
                    unpack_filename_path.parent.mkdir(parents=True, exist_ok=True)

                async with async_open(str(unpack_filename_path), 'wb+') as out:
                    i = in_file.compress_size
                    buf = await src.read(read_block)

                    decomp_window_bits = None
                    for window_bits in (-MAX_WBITS, MAX_WBITS | 16, MAX_WBITS):
                        try:
                            decomp_window_bits = window_bits
                            if __debug:
                                print(f"Try WindowBits: {window_bits}")
                            decompressobj(window_bits).decompress(buf)
                            break
                        except ZLIB_error:
                            if __debug:
                                print(f"Failed WindowBits: {window_bits}")

                    decomp = decompressobj(decomp_window_bits)
                    if __debug:
                        print(f'Incoming Length: {len(buf)}')
                    while buf:
                        result = decomp.decompress(buf)
                        await out.write(result)
                        curr_read_block = read_block if i > read_block else i
                        buf = await src.read(curr_read_block)
                        i -= curr_read_block
                        if __debug:
                            print(f'Length: {len(buf)}')

                    result = decomp.flush()
                    if __debug:
                        print(f'Flush Length: {len(buf)}')
                    await out.write(result)
    else:
        raise BadZipFile

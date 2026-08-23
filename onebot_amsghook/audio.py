"""将官机代发的语音转换为 QQ 官方 API 接受的标准 Silk。"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path


def _codec_path():
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == 'windows':
        name = 'silk_codec-windows-static-x64.exe' if machine in {'amd64', 'x86_64'} else 'silk_codec-windows-static-x86.exe'
    elif system == 'linux' and machine in {'x86_64', 'amd64'}:
        name = 'silk_codec-linux-x64'
    elif system == 'linux' and machine in {'aarch64', 'arm64'}:
        name = 'silk_codec-linux-arm64'
    else:
        return None
    path = Path(__file__).resolve().parent / 'bin' / name
    return path if path.is_file() else None


def _ffmpeg_path():
    executable = shutil.which('ffmpeg')
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        return executable if executable and os.path.isfile(executable) else None
    except (ImportError, RuntimeError, OSError):
        return None


def _run(command):
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )


async def convert_to_silk(data):
    """返回 Silk 字节；缺少工具或转换失败时返回 None。"""
    if not isinstance(data, bytes) or not data:
        return None
    if b'#!SILK' in data[:10]:
        return data
    codec = _codec_path()
    ffmpeg = _ffmpeg_path()
    if codec is None or ffmpeg is None:
        return None
    if os.name != 'nt':
        codec.chmod(codec.stat().st_mode | 0o111)

    def convert():
        with tempfile.TemporaryDirectory(prefix='elaina-silk-') as directory:
            root = Path(directory)
            source = root / 'source.audio'
            pcm = root / 'source.pcm'
            silk = root / 'source.silk'
            source.write_bytes(data)
            _run([
                str(ffmpeg), '-y', '-i', str(source), '-ar', '24000', '-ac', '1',
                '-f', 's16le', '-loglevel', 'quiet', '-hide_banner', str(pcm),
            ])
            if not pcm.is_file() or pcm.stat().st_size == 0:
                return None
            _run([str(codec), 'pts', '-i', str(pcm), '-o', str(silk), '-s', '24000'])
            return silk.read_bytes() if silk.is_file() and silk.stat().st_size else None

    try:
        return await asyncio.to_thread(convert)
    except (OSError, subprocess.SubprocessError):
        return None

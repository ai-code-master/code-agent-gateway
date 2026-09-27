"""Resolve external executables for launchd and interactive shells."""
import os
import shutil
from pathlib import Path


def executable(name, env_name, extras=()):
    """Return a usable executable path or raise a clear configuration error."""
    override = os.environ.get(env_name, '').strip()
    candidates = [override, shutil.which(name)]
    candidates.extend(str(Path(path) / name) for path in extras)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError(
        f'{name} 未找到；请安装后加入 PATH，或设置 {env_name} 为可执行文件路径')


def codex():
    return executable('codex', 'CODEX_BIN', (
        '/opt/homebrew/bin', '/usr/local/bin',
        str(Path.home() / '.local/bin'),
    ))

"""natives（本地库 DLL）目录准备。

两种世代：
  * 1.16 这种旧式：版本 JSON 里 libraries[].natives 指向带 classifier 的
    jar，运行时需要解压 dll 到 natives 目录，再由 -Djava.library.path 提供。
  * 26.3 这种新式：natives jar 直接在 classpath 上，由 LWJGL 自己解压；
    JSON 里的 ${natives_directory}/java、/jna、/lwjgl、/netty 只是工作目录，
    需要保证这些子目录存在。

HMCL 已经在 versions/<id>/natives-windows-x86_64 下准备好了这两种目录，
所以这里优先复用；缺失时才自行解压。
"""
from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from .version import MinecraftVersion, detect_arch, detect_os_name

log = logging.getLogger(__name__)


def natives_dir_name(os_name: str | None = None, os_arch: str | None = None) -> str:
    os_name = os_name or detect_os_name()
    os_arch = os_arch or detect_arch()
    return f"natives-{os_name}-{os_arch}"


def natives_dir(mc_dir: Path, version_id: str, *, os_name: str | None = None,
                os_arch: str | None = None) -> Path:
    return mc_dir / "versions" / version_id / natives_dir_name(os_name, os_arch)


def prepare(version: MinecraftVersion, *, os_name: str | None = None,
            os_arch: str | None = None) -> Path:
    """保证 natives 目录可用，返回其路径。"""
    target = natives_dir(version.mc_dir, version.id, os_name=os_name, os_arch=os_arch)

    # HMCL 已解压过：只要目录里有东西就直接用
    if target.is_dir() and any(target.iterdir()):
        _ensure_scratch_subdirs(version, target)
        return target

    target.mkdir(parents=True, exist_ok=True)
    extracted = 0
    for lib, classifier in _natives_entries(version, os_name, os_arch):
        if lib.path and lib.path.exists():
            extracted += _extract_jar(lib.path, target)
        else:
            log.warning("natives 缺少文件，跳过：%s", lib.path)
    if extracted:
        log.info("已解压 %d 个 natives 文件到 %s", extracted, target)
    _ensure_scratch_subdirs(version, target)
    return target


def _ensure_scratch_subdirs(version: MinecraftVersion, target: Path) -> None:
    """把 JVM 参数里引用到的 ${natives_directory}/xxx 子目录建出来。"""
    for arg in _flatten(version.jvm_args):
        if not isinstance(arg, str) or "${natives_directory}/" not in arg:
            continue
        tail = arg.split("${natives_directory}/", 1)[1]
        sub = tail.split()[0].split("=")[0].strip()
        if not sub or sub.startswith("$"):
            continue
        try:
            (target / sub).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("创建 natives 子目录失败 %s：%s", sub, exc)


def _natives_entries(version: MinecraftVersion, os_name: str | None,
                     os_arch: str | None) -> list[tuple]:
    from .version import resolve_libraries  # 局部导入避免循环
    resolved = resolve_libraries(version, os_name=os_name, os_arch=os_arch)
    return resolved.natives_to_extract


def _extract_jar(jar: Path, target: Path) -> int:
    import shutil

    count = 0
    try:
        with zipfile.ZipFile(jar) as zf:
            for info in zf.infolist():
                name = info.filename
                if info.is_dir() or name.startswith("META-INF/"):
                    continue
                if name.endswith(("/", ".git", ".sha1")):
                    continue
                dest = target / Path(name).name
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                count += 1
    except (zipfile.BadZipFile, OSError) as exc:
        log.warning("解压 natives 失败 %s：%s", jar, exc)
    return count


def _flatten(args: list) -> list:
    out = []
    for item in args:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            value = item.get("value")
            if isinstance(value, list):
                out.extend(v for v in value if isinstance(v, str))
            elif isinstance(value, str):
                out.append(value)
    return out

"""Platform-independent names for sources that must survive backup and restore."""
import re


_FORBIDDEN = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*\ud800-\udfff]')
_DEVICES = re.compile(r'(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])', re.I)


def is_device_name(name: str) -> bool:
    # Extensions do not make Windows device names ordinary files. Spaces
    # before the extension and the three superscript digits are aliases too.
    return _DEVICES.fullmatch(name.split('.', 1)[0].rstrip(' ')) is not None


def is_portable_file_name(name: object) -> bool:
    """Validate one component without rewriting it or accessing the filesystem."""
    return (isinstance(name, str) and bool(name) and not _FORBIDDEN.search(name)
            and not name.endswith(('.', ' ')) and not is_device_name(name))

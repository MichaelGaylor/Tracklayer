"""Install (or update) the Tracklayer plugin into KiCad's user plugin folder.

    python install_kicad_plugin.py            # every KiCad version found under Documents/KiCad
    python install_kicad_plugin.py --dir DIR  # a specific scripting/plugins folder
    python install_kicad_plugin.py --link     # symlink the checkout instead of copying (for development)

Afterwards start the PCB editor (or Tools > External Plugins > Refresh Plugins). The dialog is
under Tools > External Plugins > Tracklayer autoroute, and on the toolbar.
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PLUGIN = os.path.join(HERE, 'kicad_plugin')
SRC_LIB = os.path.join(HERE, 'tracklayer')


def candidate_dirs():
    out = []
    roots = []
    if sys.platform.startswith('win'):
        roots.append(os.path.join(os.path.expanduser('~'), 'Documents', 'KiCad'))
        roots.append(os.path.join(os.path.expanduser('~'), 'OneDrive', 'Documents', 'KiCad'))
    elif sys.platform == 'darwin':
        roots.append(os.path.join(os.path.expanduser('~'), 'Documents', 'KiCad'))
    else:
        roots.append(os.path.join(os.path.expanduser('~'), '.local', 'share', 'kicad'))
    for root in roots:
        if not os.path.isdir(root):
            continue
        for ver in sorted(os.listdir(root)):
            if ver[:1].isdigit() and os.path.isdir(os.path.join(root, ver)):
                out.append(os.path.join(root, ver, 'scripting', 'plugins'))
    return out


def install(plugins_dir: str, link: bool) -> str:
    os.makedirs(plugins_dir, exist_ok=True)
    dst = os.path.join(plugins_dir, 'tracklayer_plugin')
    if os.path.islink(dst) or os.path.isfile(dst):
        os.unlink(dst)
    elif os.path.isdir(dst):
        shutil.rmtree(dst)
    if link:
        os.symlink(SRC_PLUGIN, dst, target_is_directory=True)
        lib = os.path.join(SRC_PLUGIN, 'tracklayer')
        if not os.path.exists(lib):
            os.symlink(SRC_LIB, lib, target_is_directory=True)
    else:
        shutil.copytree(SRC_PLUGIN, dst, ignore=shutil.ignore_patterns('__pycache__', 'tracklayer'))
        shutil.copytree(SRC_LIB, os.path.join(dst, 'tracklayer'), ignore=shutil.ignore_patterns('__pycache__'))
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', help='a scripting/plugins folder to install into')
    ap.add_argument('--link', action='store_true', help='symlink the checkout instead of copying')
    a = ap.parse_args()
    dirs = [a.dir] if a.dir else candidate_dirs()
    if not dirs:
        print('No KiCad user folder found; pass --dir <KiCad>/<version>/scripting/plugins')
        return 1
    for d in dirs:
        print('installed to', install(d, a.link))
    print('Now open the PCB editor: Tools > External Plugins > Tracklayer autoroute')
    return 0


if __name__ == '__main__':
    sys.exit(main())

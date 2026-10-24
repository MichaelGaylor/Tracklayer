"""Build the KiCad Plugin and Content Manager package.

    python pcm/build_package.py [--url DOWNLOAD_URL]

Writes dist/tracklayer-pcm-<version>.zip (plugins/ + resources/ + metadata.json inside) and
dist/metadata.json for the KiCad addons repository, with the sha256 and sizes filled in.
Pass --url once the zip is attached to a GitHub release so the metadata points at it.
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from tracklayer import __version__  # noqa: E402

IDENT = 'com.github.michaelgaylor.tracklayer'
SITE = 'https://www.tiggyengineering.com'


def plugin_tree():
    """(archive path, file path) for everything that goes under plugins/."""
    out = []
    src = os.path.join(ROOT, 'kicad_plugin')
    # the PCM package is the IPC API plugin only (KiCad 9+); the classic plugin is for the installer script
    for name in ('plugin.json', 'requirements.txt', 'tracklayer_ipc.py', 'icon.png'):
        out.append(('plugins/' + name, os.path.join(src, name)))
    lib = os.path.join(ROOT, 'tracklayer')
    for name in os.listdir(lib):
        if name.endswith('.py'):
            out.append(('plugins/tracklayer/' + name, os.path.join(lib, name)))
    return out


def metadata(download_url: str, sha: str, dl_size: int, inst_size: int) -> dict:
    return {
        '$schema': 'https://go.kicad.org/pcm/schemas/v2',
        'name': 'Tracklayer autorouter',
        'description': 'Autoroute the open board: exact-geometry (shape) or grid engine, any number of copper layers.',
        'description_full': (
            'Tracklayer routes the unrouted nets of the open board and adds the tracks and vias as one undoable '
            'change, then refills the zones. The shape engine works on exact geometry: every obstacle is grown by '
            'the clearance plus half the track width and tracks run corner to corner, so clearances are met exactly '
            'and tight escapes from fine-pitch pads just work. The grid engine gives classic 45-degree tracks. Net '
            'classes, locked and hand-routed copper, keep-outs and pours are respected. Any number of copper layers '
            'with through vias.\n\nTools > External Plugins > Tracklayer autoroute. Pure Python. Runs through the KiCad API: enable it '
            'once under Preferences > Plugins > Enable KiCad API.\n\n'
            'Made by Tiggy Engineering (www.tiggyengineering.com). Free and open source; if it saves you an '
            'evening, have a look at what we make, or buy us a beer.'
        ),
        'identifier': IDENT,
        'type': 'plugin',
        'author': {'name': 'Mike Gaylor, Tiggy Engineering', 'contact': {'web': SITE}},
        'maintainer': {'name': 'Mike Gaylor, Tiggy Engineering', 'contact': {'web': SITE}},
        'license': 'MIT',
        'resources': {'homepage': 'https://github.com/MichaelGaylor/Tracklayer',
                      'Issues': 'https://github.com/MichaelGaylor/Tracklayer/issues',
                      'Tiggy Engineering': SITE},
        'tags': ['routing', 'autorouter', 'pcb'],
        'versions': [{
            'version': __version__,
            'status': 'testing',
            'runtime': 'ipc',
            'kicad_version': '9.0',
            'platforms': ['windows', 'macos', 'linux'],
            'download_url': download_url,
            'download_sha256': sha,
            'download_size': dl_size,
            'install_size': inst_size,
        }],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default=f'https://github.com/MichaelGaylor/Tracklayer/releases/download/v{__version__}/tracklayer-pcm-{__version__}.zip')
    a = ap.parse_args()
    dist = os.path.join(ROOT, 'dist')
    os.makedirs(dist, exist_ok=True)
    # 64 px icon for the PCM listing
    sys.path.insert(0, os.path.join(ROOT, 'kicad_plugin'))
    import make_icon
    icon64 = os.path.join(dist, 'icon.png')
    make_icon.draw(64, icon64)
    files = plugin_tree() + [('resources/icon.png', icon64)]
    # metadata inside the zip carries no download fields
    meta_in = metadata('', '', 0, 0)
    for k in ('download_url', 'download_sha256', 'download_size', 'install_size'):
        meta_in['versions'][0].pop(k, None)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for arc, f in files:
            z.write(f, arc)
        z.writestr('metadata.json', json.dumps(meta_in, indent=2))
    data = buf.getvalue()
    # KiCad counts every entry of the archive, metadata.json included
    inst_size = sum(i.file_size for i in zipfile.ZipFile(io.BytesIO(data)).infolist())
    zip_path = os.path.join(dist, f'tracklayer-pcm-{__version__}.zip')
    open(zip_path, 'wb').write(data)
    sha = hashlib.sha256(data).hexdigest()
    meta = metadata(a.url, sha, len(data), inst_size)
    json.dump(meta, open(os.path.join(dist, 'metadata.json'), 'w'), indent=2)
    print(zip_path, len(data), 'bytes, sha256', sha)
    print(os.path.join(dist, 'metadata.json'))


if __name__ == '__main__':
    main()

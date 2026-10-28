"""Create the GitHub release for the current version and attach the PCM zip.

    python pcm/github_release.py [notes.md]

Reads the version from tracklayer/__init__.py, expects dist/tracklayer-pcm-<version>.zip from
pcm/build_package.py, and takes the GitHub token from the Git Credential Manager (the same one
`git push` uses), so nothing is stored here. The release is marked a prerelease, like the others.
"""
import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = 'MichaelGaylor/Tracklayer'


def version():
    for line in open(os.path.join(ROOT, 'tracklayer', '__init__.py'), encoding='utf-8'):
        if line.startswith('__version__'):
            return line.split("'")[1]
    raise SystemExit('no __version__')


def token():
    out = subprocess.run(['git', 'credential', 'fill'], input='protocol=https\nhost=github.com\n', capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if line.startswith('password='):
            return line[len('password='):]
    raise SystemExit('no GitHub credential in the credential manager')


def main():
    v = version()
    zip_path = os.path.join(ROOT, 'dist', f'tracklayer-pcm-{v}.zip')
    if not os.path.isfile(zip_path):
        raise SystemExit(f'{zip_path} missing: run pcm/build_package.py first')
    notes = open(sys.argv[1], encoding='utf-8').read() if len(sys.argv) > 1 else ''
    hdr = {'Authorization': f'Bearer {token()}', 'Accept': 'application/vnd.github+json'}
    body = {'tag_name': f'v{v}', 'name': f'Tracklayer {v}', 'prerelease': True, 'body': notes}
    req = urllib.request.Request(f'https://api.github.com/repos/{REPO}/releases', data=json.dumps(body).encode(), headers=hdr, method='POST')
    rel = json.load(urllib.request.urlopen(req))
    upload = rel['upload_url'].split('{')[0] + f'?name=tracklayer-pcm-{v}.zip'
    req = urllib.request.Request(upload, data=open(zip_path, 'rb').read(), headers={**hdr, 'Content-Type': 'application/zip'}, method='POST')
    asset = json.load(urllib.request.urlopen(req))
    print('release', rel['html_url'])
    print('asset', asset['browser_download_url'], asset['size'], 'bytes')


if __name__ == '__main__':
    main()

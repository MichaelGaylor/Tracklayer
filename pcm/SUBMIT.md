# Publishing to KiCad's Plugin and Content Manager

The PCM reads a catalogue kept in KiCad's GitLab repository. Getting listed is a merge request
that adds one folder with a `metadata.json` and an icon. The package zip itself lives on a
GitHub release of this repository.

## Each release

1. Bump `__version__` in `tracklayer/__init__.py`, commit, tag `v<version>` and push the tag.
2. `python pcm/build_package.py` writes `dist/tracklayer-pcm-<version>.zip` and
   `dist/metadata.json` (sha256 and sizes filled in, download URL pointing at the GitHub
   release asset).
3. Create the GitHub release `v<version>` and attach the zip. The URL must match the one in
   `dist/metadata.json`:
   `https://github.com/MichaelGaylor/Tracklayer/releases/download/v<version>/tracklayer-pcm-<version>.zip`

## First listing (once)

1. Sign in at https://gitlab.com (KiCad's repositories live there) and fork
   https://gitlab.com/kicad/addons/metadata
2. In the fork add:
   - `packages/com.github.michaelgaylor.tracklayer/metadata.json` (copy of `dist/metadata.json`)
   - `packages/com.github.michaelgaylor.tracklayer/icon.png` (copy of `dist/icon.png`, 64 x 64)
3. Open a merge request against `main`. The CI validates the schema, downloads the zip and
   checks the sha256 and sizes. Reviewers usually reply within a few days.
4. Later versions: edit the same `metadata.json`, appending a new entry to `versions`
   (keep the old ones), and open another merge request.

## Trying the package locally before it is listed

In KiCad: Plugin and Content Manager > Install from File... and pick the zip. That installs it
exactly as the PCM would (under `3rdparty/plugins/com_github_michaelgaylor_tracklayer`).
If you also have the development copy in `scripting/plugins` you will see the plugin twice;
remove one.

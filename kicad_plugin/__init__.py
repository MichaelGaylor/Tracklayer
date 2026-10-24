"""Classic (SWIG) KiCad action plugin for KiCad 8, 9 and 10: "Tracklayer autoroute (classic)" under
Tools > External Plugins. KiCad 9 and later also get the IPC API version from plugin.json next to
this file, which is the one that survives KiCad 11. Installed by install_kicad_plugin.py or the
Plugin and Content Manager."""
try:
    import pcbnew as _pcbnew
    _major = int(str(_pcbnew.Version()).split('.')[0])
except Exception:
    _major = 0
if _major and _major <= 10:
    try:
        from .action import TracklayerAction
        TracklayerAction().register()
    except Exception:   # keep KiCad's plugin loader happy and show the reason in the scripting console
        import traceback
        traceback.print_exc()

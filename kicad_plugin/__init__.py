"""KiCad action plugin: adds "Tracklayer autoroute" to the PCB editor's Tools > External Plugins
menu and toolbar. Installed by install_kicad_plugin.py, which copies this folder (with a copy of
the tracklayer package inside it) into KiCad's user plugin directory."""
try:
    from .action import TracklayerAction
    TracklayerAction().register()
except Exception as _e:   # keep KiCad's plugin loader happy and show the reason in the scripting console
    import traceback
    traceback.print_exc()

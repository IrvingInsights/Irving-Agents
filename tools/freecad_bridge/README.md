# FreeCAD Bridge

This app can generate FreeCAD macros through Cloud Run, but the actual FreeCAD execution has to happen on your Windows machine.

## Start the local bridge

From the `Irving-Agents` repo root:

```powershell
python -m uvicorn tools.freecad_bridge.freecad_bridge:app --host 127.0.0.1 --port 8765
```

If `freecadcmd.exe` is not in the default install path, set:

```powershell
$env:FREECADCMD_PATH = 'C:\Path\To\freecadcmd.exe'
```

## What it does

- accepts a generated FreeCAD Python macro
- runs it through `freecadcmd.exe`
- saves `.FCStd`
- exports `.step` by default
- exports orthographic `.svg` drawing views
- can also export `.stl`

## Browser integration

When the chat returns a FreeCAD macro, the app will show a `Run in FreeCAD` action. That action posts to:

```text
http://127.0.0.1:8765/freecad/execute
```

and opens the generated local artifacts.

## Repo boundary

This bridge is intentionally under `tools/freecad_bridge` because it is a local Windows execution helper, not part of the hosted Irving Agents API. If it becomes useful beyond Irving Agents or PeakHinge, promote it into a separate `irving-freecad-bridge` repo.

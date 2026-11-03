# Changelog

## 0.4.0

- Detect the installed Abaqus and its version before loading; require 2024+.
- Add `bin/dsh-abaqus-cad.mjs doctor` (version/Python/solver/installs) and the
  `ABAQUS_MIN_VERSION` / `ABAQUS_ALLOW_UNSUPPORTED` controls.
- Package for npm and `github:Polymerizedcat/dsh-abaqus-cad` installs: `bin`, repository,
  publish metadata, CI, NOTICE.
- README rewritten as the GitHub landing page.

## 0.3.0

- GUI kernel namespace now injects `abaqusConstants`, `mesh`, `ElemType`.
- Job `write`/`submit` sync `.inp` from the CAE cwd to the work dir.
- `abaqus_odb` accepts numeric (1-based) steps and `MAGNITUDE` aliases.
- New `abaqus_part` kinds: `revolve`, `cone`, `tube`.
- `abaqus_mesh` supports `technique` / `shape`.
- New `abaqus_inspect`; actionable hints for common Abaqus errors.

## 0.2.1

- `abaqus_coupling`: reference point + kinematic/distributing coupling, optional
  force/moment on the RP.
- `finish` stops only the session mode in use (does not stop a user GUI bridge).

## 0.2.0

- Dual session modes: background kernel, GUI attach, managed visible GUI.
- AFX GUI bridge installed as `%USERPROFILE%\abaqus_plugins\*_plugin.py`.
- `abaqus_finish` close policy.

## 0.1.0

- Initial kernel bridge, 21 tools, `abaqus-fea` skill.

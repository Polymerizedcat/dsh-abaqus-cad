#!/usr/bin/env node
// Standalone diagnostics for dsh-abaqus-cad.
//
//   dsh-abaqus-cad doctor [--json]   detect Abaqus, version, Python, solver
//   dsh-abaqus-cad where             print resolved paths
//   dsh-abaqus-cad version           print package version
//
// `doctor` is safe to run before installing the dsh plugin: it only reads the
// filesystem and calls `abaqus information=versions`.
import { detectAbaqus } from "../lib/detect.mjs";
import { CONFIG_FILE, PKG, PKG_ROOT, resolveLayout } from "../lib/paths.mjs";

const argv = process.argv.slice(2);
const command = argv[0] || "doctor";

function printDoctor(asJson) {
  const d = detectAbaqus();
  if (asJson) {
    process.stdout.write(JSON.stringify(d, null, 2) + "\n");
  } else {
    console.log(`dsh-abaqus-cad ${PKG.version}`);
    console.log(`Abaqus found : ${d.found ? "yes" : "NO"}`);
    console.log(`Version      : ${d.version || "?"} (requires ${d.minVersion}+)`);
    console.log(`Supported    : ${d.supported ? "yes" : "NO"}`);
    if (d.reason) console.log(`Reason       : ${d.reason}`);
    console.log(`Command      : ${d.command || "?"}`);
    console.log(`Install root : ${d.installRoot || "?"}`);
    console.log(`Python       : ${d.python ? `${d.python.version} (${d.python.path})` : "?"}`);
    console.log(`Solver       : ${d.solver === null ? "unknown (custom install path)" : d.solver ? "present" : "MISSING (CAE-only install)"}`);
    if (d.installations.length) {
      console.log(
        `Installations: ${d.installations
          .map((i) => `${i.version}${i.solver ? "" : "(CAE only)"}`)
          .join(", ")}`,
      );
    }
    console.log(`Config       : ${CONFIG_FILE}`);
  }
  process.exitCode = d.found && d.supported ? 0 : 1;
}

if (command === "doctor") {
  printDoctor(argv.includes("--json"));
} else if (command === "where") {
  const d = detectAbaqus();
  const layout = resolveLayout({ detection: d });
  console.log(
    JSON.stringify(
      {
        package: PKG_ROOT,
        abaqus: layout.abaqusCmd,
        python: layout.python,
        workDir: layout.workDir,
        server: layout.serverScript,
        cae: layout.caeDir,
        config: CONFIG_FILE,
        detected: {
          version: d.version,
          supported: d.supported,
          solver: d.solver,
          python: d.python,
        },
      },
      null,
      2,
    ),
  );
} else if (command === "version") {
  console.log(PKG.version);
} else if (command === "help" || command === "--help" || command === "-h") {
  console.log(`dsh-abaqus-cad ${PKG.version}

  doctor [--json]   detect Abaqus / version / Python / solver (exit 1 if unsupported)
  where             print resolved paths
  version           print package version

Environment:
  ABAQUS_CMD              explicit abaqus.bat path
  ABAQUS_MIN_VERSION      minimum accepted year (default 2024)
  ABAQUS_ALLOW_UNSUPPORTED=1  load the plugin even below the baseline (experiments)
`);
} else {
  console.error(`unknown command: ${command}`);
  process.exitCode = 2;
}

// dsh-abaqus-cad entry point.
//
// A real cordis plugin: dsh imports this module by package name, so nothing has
// to be on PATH. On load it resolves the local Abaqus installation, writes the
// default config (~/.dsh/abaqus-cad.json), installs the abaqus-fea skill into
// ~/.dsh/skills, and registers one stdio MCP server.
//
// The MCP server advertises only search_tools + call_tool. The 21-tool
// catalogue is searched on demand, so an idle session pays ~250 tokens instead
// of the whole schema set.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import * as McpClient from "@deepseek-ai/dsh-mcp-client";

import { CONFIG_FILE, ensureConfig, exists, resolveLayout } from "./paths.mjs";
import { detectAbaqus } from "./detect.mjs";

export const name = "dsh-abaqus-cad";
export const inject = McpClient.inject;

const LOG = "[dsh-abaqus-cad]";

function say(msg) {
  process.stderr.write(`${LOG} ${msg}\n`);
}

function installSkill(layout) {
  const src = path.join(layout.skillSrc, "SKILL.md");
  if (!exists(src)) return false;
  try {
    fs.mkdirSync(layout.skillDest, { recursive: true });
    fs.cpSync(layout.skillSrc, layout.skillDest, { recursive: true, force: true });
    return true;
  } catch (err) {
    say(`could not install the abaqus-fea skill: ${err.message}`);
    return false;
  }
}

// Install the CAE GUI bridge loader. Abaqus/CAE imports *_plugin.py from
// %USERPROFILE%\abaqus_plugins at startup; the loader execs the bridge from a
// stable copy so moving the plugin package does not break an open CAE.
function installGuiBridge(layout) {
  const guiDir = path.join(os.homedir(), ".dsh", "abaqus-cad", "gui");
  fs.mkdirSync(guiDir, { recursive: true });
  for (const f of ["gui_bridge.py", "abq_helpers.py"]) {
    const src = path.join(layout.caeDir, f);
    if (exists(src)) fs.copyFileSync(src, path.join(guiDir, f));
  }
  const pluginDir = path.join(os.homedir(), "abaqus_plugins");
  fs.mkdirSync(pluginDir, { recursive: true });
  const loader = path.join(pluginDir, "dsh_abaqus_bridge_plugin.py");
  const text =
    "# Auto-loaded by Abaqus/CAE from abaqus_plugins (*_plugin.py).\n" +
    "import os\n" +
    `_CAE_DIR = ${JSON.stringify(guiDir)}\n` +
    `_PLUGIN = ${JSON.stringify(path.join(guiDir, "gui_bridge.py"))}\n` +
    "os.environ.setdefault('ABAQUS_DSH_CAE_DIR', _CAE_DIR)\n" +
    "os.environ.setdefault('ABAQUS_DSH_PORT', '48153')\n" +
    "if os.path.exists(_PLUGIN) and not globals().get('_DSH_ABAQUS_BRIDGE_LOADED'):\n" +
    "    with open(_PLUGIN, 'r', encoding='utf-8') as _fh:\n" +
    "        exec(compile(_fh.read(), _PLUGIN, 'exec'), globals())\n";
  fs.writeFileSync(loader, text, "utf8");
  return loader;
}

function serverConfig(layout) {
  const detection = layout.detection || {};
  return {
    serverName: "abaqus",
    transport: "stdio",
    command: layout.python,
    args: [layout.serverScript],
    env: {
      ABAQUS_MCP_WORKDIR: layout.workDir,
      ABAQUS_CMD: layout.abaqusCmd,
      ABAQUS_VERSION: String(detection.version || ""),
      ABAQUS_MIN_VERSION: String(detection.minVersion),
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
      PYTHONUNBUFFERED: "1",
    },
    // A 500 MB ODB extrema scan or an explicit job wait can take minutes.
    toolCallTimeoutMs: 900000,
    failOnStartupError: true,
    reconnect: { enabled: true, initialDelayMs: 500, maxDelayMs: 30000, maxAttempts: 10 },
  };
}

export function apply(ctx) {
  if (process.platform !== "win32") {
    throw new Error(`${LOG} Abaqus/CAE automation only runs on Windows.`);
  }

  const detection = detectAbaqus();
  if (!detection.found) {
    throw new Error(
      `${LOG} Abaqus was not found on this machine.\n` +
        `Install Abaqus 2024+ (CAE + Analysis), or set ABAQUS_CMD to abaqus.bat.\n` +
        `Run "node bin/dsh-abaqus-cad.mjs doctor" for details.`,
    );
  }
  if (!detection.supported && process.env.ABAQUS_ALLOW_UNSUPPORTED !== "1") {
    throw new Error(
      `${LOG} unsupported Abaqus ${detection.version || "?"}: this bundle requires ` +
        `${detection.minVersion}+ (Python 3).\n` +
        `Set ABAQUS_ALLOW_UNSUPPORTED=1 only for experiments; features may break.`,
    );
  }
  const layout = resolveLayout({ detection });

  if (!exists(layout.serverScript)) {
    throw new Error(`${LOG} MCP server is missing at ${layout.serverScript}`);
  }
  say(
    `Abaqus ${detection.version || "?"} detected ` +
      `(python ${detection.python ? detection.python.version : "?"}, ` +
      `solver ${detection.solver ? "yes" : "no"}, command ${detection.command || "?"})`,
  );

  const cfg = ensureConfig(layout);
  let guiLoader = null;
  try {
    guiLoader = installGuiBridge(layout);
  } catch (err) {
    say(`could not install the CAE GUI bridge: ${err.message}`);
  }
  const skill = installSkill(layout);

  ctx.plugin(McpClient, serverConfig(layout));

  say(
    `registered abaqus (python ${path.basename(layout.python)}, work ${cfg.workDir}, ` +
      `mode ${cfg.mode}, skill ${skill ? "installed" : "missing"}, ` +
      `gui ${guiLoader ? "installed" : "missing"})`,
  );
}

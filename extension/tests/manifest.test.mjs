import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const manifest = JSON.parse(await readFile(new URL("../manifest.json", import.meta.url), "utf8"));

test("manifest is MV3 and keeps permissions minimal", () => {
  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(manifest.permissions, ["sidePanel", "tabs", "nativeMessaging"]);
  assert.deepEqual(manifest.host_permissions, ["https://*.greenhouse.io/*"]);
  assert.equal(manifest.side_panel.default_path, "side-panel.html");
});

test("read-only extension has no arbitrary execution command", async () => {
  const source = await readFile(new URL("../src/content/common.ts", import.meta.url), "utf8");
  assert.doesNotMatch(source, /eval\s*\(|new Function|execute_command|shell/);
  assert.doesNotMatch(source, /\.click\s*\(|\.fill\s*\(|\.remove\s*\(/);
});

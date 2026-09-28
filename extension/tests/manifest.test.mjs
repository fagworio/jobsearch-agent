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

test("extension has no arbitrary execution command", async () => {
  const source = await readFile(new URL("../src/content/common.ts", import.meta.url), "utf8");
  assert.doesNotMatch(source, /eval\s*\(|new Function|execute_command|shell/);
  assert.doesNotMatch(source, /\.fill\s*\(|\.remove\s*\(/);
  assert.match(source, /\(buttons\[0\] as HTMLElement\)\.click\(\)/);
});

test("extension exposes typed fill/read/challenge commands and guarded submit", async () => {
  const source = await readFile(new URL("../src/content/common.ts", import.meta.url), "utf8");
  assert.match(source, /type === "FILL_FORM"/);
  assert.match(source, /type === "READ_FORM"/);
  assert.match(source, /type === "GET_CHALLENGE_STATE"/);
  assert.match(source, /type === "REQUEST_SUBMIT"/);
  assert.match(source, /form fingerprint does not match/);
  assert.match(source, /human challenge state/);
  assert.match(source, /type === "GET_SUBMIT_RESULT"/);
  assert.match(source, /FIELD_MISMATCH/);
  const worker = await readFile(new URL("../src/service-worker.ts", import.meta.url), "utf8");
  assert.match(worker, /nativeRequest/);
  assert.match(worker, /native host response timeout/);
  const greenhouse = await readFile(new URL("../src/content/greenhouse.ts", import.meta.url), "utf8");
  assert.match(greenhouse, /aria-labelledby/);
  assert.match(greenhouse, /aria-required/);
  assert.match(source, /getAttribute\("role"\) === "combobox"/);
  assert.match(source, /\[role="option"\]/);
  assert.match(source, /single-value/);
});

test("upload path is PDF-only and does not execute arbitrary code", async () => {
  const source = await readFile(new URL("../src/content/common.ts", import.meta.url), "utf8");
  assert.match(source, /type === "UPLOAD_ARTIFACT"/);
  assert.match(source, /application\/pdf/);
  assert.doesNotMatch(source, /eval\s*\(|new Function/);
});

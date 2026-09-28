import { build } from "esbuild";

const common = {
  bundle: true,
  format: "iife",
  platform: "browser",
  target: "es2022",
  sourcemap: false,
};

await build({ ...common, entryPoints: ["src/service-worker.ts"], outfile: "dist/service-worker.js", format: "esm" });
await build({ ...common, entryPoints: ["src/content/common.ts"], outfile: "dist/content/common.js" });
await build({ ...common, entryPoints: ["src/ui/side-panel.ts"], outfile: "dist/ui/side-panel.js" });
